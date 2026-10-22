"""HTTP server and scan loop.

- A background thread re-reads the session logs every 3 seconds (read-only, incremental) and builds a snapshot
  of the list; the API returns that snapshot.
- Dismissed sessions are kept in <state dir>/dismissed.json; a dismissed session comes back when it gets a new
  text message (from you or from the agent).

Run: python -m claude_codex_monitor [--port 8767], then open http://127.0.0.1:8767/
"""
import atexit
import dataclasses
import datetime as dt
import errno
import glob
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import autostart
from . import collect as C
from . import demo
from .collect import M
from .config import parse_args
from .summarize import (AUTO_ORDER, CLAUDE_MODELS, MODEL_RE, Summarizer, conversation_lang, installed, make_backend,
                        quick_asks, resolve, VERSION as SUMM_VERSION)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
STATIC_FILES = {  # URL path -> (file in STATIC_DIR, content type)
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/i18n.js': ('i18n.js', 'application/javascript; charset=utf-8'),
    '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
    '/favicon.ico': ('favicon.ico', 'image/x-icon'),
    '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
}
VIEWABLE_EXT = ('.md', '.txt', '.csv', '.json', '.jsonl', '.html', '.log', '.sql', '.yaml', '.yml', '.output', '.py', '.mjs', '.js', '.sh', '.toml')
SCAN_INTERVAL = 3
EVICT_SECONDS = 600              # forget the read state of a log nothing has looked at for this long


def fmt_local(ts):
    t = C.parse_ts(ts)
    return dt.datetime.fromtimestamp(t).strftime('%m-%d %H:%M') if t else ''


STATE_FILES = ('token', 'settings.json', 'summaries.json', 'dismissed.json', 'consent.json', 'monitor.log')


def secure_dir(path):
    """The state folder holds text taken from your conversations: only you may read it. Only this program's own files
    are changed, and the folder itself only when it is the default one or new, since --state-dir may name a folder
    that holds other things too."""
    new = not os.path.exists(path)
    os.makedirs(path, exist_ok=True)
    if new or os.path.realpath(path) == os.path.realpath(os.path.expanduser('~/.claude-codex-monitor')):
        os.chmod(path, 0o700)
    for name in STATE_FILES + tuple(n + '.tmp' for n in STATE_FILES):
        p = os.path.join(path, name)
        if os.path.isfile(p) and not os.path.islink(p):
            os.chmod(p, 0o600)


def read_token(state_dir):
    try:
        with open(os.path.join(state_dir, 'token'), encoding='utf-8') as f:
            return f.read().strip() or None
    except OSError:
        return None


def load_token(state_dir):
    """The access token the page needs, kept in <state dir>/token (only you can read it) so it survives restarts."""
    tok = read_token(state_dir)
    if tok:
        return tok
    path = os.path.join(state_dir, 'token')
    secure_dir(state_dir)
    tok = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(tok + '\n')
    return tok


def same(a, b):
    """Compare secrets in constant time, as bytes (str comparison refuses non-ASCII text)."""
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def show_path(p):
    home = os.path.expanduser('~')
    return '~' + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def claude_tmp(base='/tmp'):
    """Claude Code's own folder for background task output, /tmp/claude-<uid>. Anyone can create /tmp/claude-* names,
    so it is only trusted when it is a real folder that belongs to you and is closed to everyone else."""
    p = os.path.join(base, 'claude-%d' % os.getuid())
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None
    return os.path.realpath(p)


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lang = cfg.lang
        secure_dir(cfg.state_dir)
        self.lock = threading.RLock()
        self.tails = {}
        self.paths = {}               # Session key -> log file.
        self.snapshot = {'ready': False, 'rows': [], 'progress': {'done': 0, 'total': 0}, 'lang': self.lang,
                         'lang_locked': cfg.lock_lang}
        self.settings_path = os.path.join(cfg.state_dir, 'settings.json')
        try:
            with open(self.settings_path, encoding='utf-8') as f:
                self.settings = json.load(f)
        except (OSError, ValueError):
            self.settings = {}
        try:
            self.backend = make_backend(self.summary_cfg())
        except ValueError:          # the tool chosen in the page has since been uninstalled
            self.backend = make_backend(cfg)
        self.summ = Summarizer(os.path.join(cfg.state_dir, 'summaries.json'), self.backend)
        self.dismiss_path = os.path.join(cfg.state_dir, 'dismissed.json')
        self.consent_path = os.path.join(cfg.state_dir, 'consent.json')
        try:
            with open(self.consent_path, encoding='utf-8') as f:
                self.consent = json.load(f)
        except (OSError, ValueError):
            self.consent = {}
        self.summ.active = self.summary_state() == 'on'
        try:
            with open(self.dismiss_path, encoding='utf-8') as f:
                self.dismissed = json.load(f)
        except (OSError, ValueError):
            self.dismissed = {}
        self.link_cache = {}
        self.exec_metas = []
        self.spawn_metas = {}
        self.cl_paths = {}            # claude:<id> -> log file.
        self.cli_links = {}           # claude -p command ID -> resulting Claude session key.
        self.cli_children = set()     # Child Claude sessions, excluded from the top-level list.
        self.cli_calls = {}           # Parent session key -> Codex commands invoking claude -p.
        self.cards = {}               # Claude session ID -> pending desktop new-session cards.
        self.local_ids = {}           # Claude session ID -> desktop local_xxx ID for open links.
        self.spawned = {}             # Proposed new session task_id -> local_xxx of the session started from it.
        self.local_to_cli = {}        # local_xxx -> Claude session ID, to find new sessions started from a session.
        self.prev_rows = {}           # Previous scan's rows by key; new-session subtasks take their status from it.
        self.last_error = None
        self.pruned_at = 0
        self.unreadable = set()       # (log, errno) already reported, so the log does not repeat it every scan
        C.set_tail_func(self.tail)

    # ---- Reading ----
    def tail(self, path, factory):
        with self.lock:
            t = self.tails.get(path)
            if t is None:
                t = self.tails[path] = C.JsonlTail(path, factory)
            t.used = time.time()
            return t.poll()

    def state_of(self, path):
        t = self.tails.get(path)
        return t.state if t else None

    def rollout_state(self, path):
        return self.tail(path, C.CodexRollout) if path else None

    def loop(self):
        while True:
            t0 = time.time()
            try:
                self.scan()
            except Exception as e:      # Keep the thread alive after scan errors and retry next time.
                if repr(e) != self.last_error:
                    self.last_error = repr(e)
                    traceback.print_exc()
                self.snapshot = dict(self.snapshot, error=M('error.scan', repr(e)))
            time.sleep(max(1.0, SCAN_INTERVAL - (time.time() - t0)))

    def scan(self):
        now = time.time()
        today = dt.datetime.combine(dt.date.today(), dt.time())
        cutoff = (today - dt.timedelta(days=C.SCAN_DAYS - 1)).timestamp()
        live = C.live_claude_sessions()
        mt = {}

        def fresh(files, since):
            for f in files:
                try:
                    mt[f] = os.path.getmtime(f)      # a log can disappear between listing and reading
                except OSError:
                    continue
                if mt[f] >= since:
                    yield f
        cfiles = list(fresh(glob.glob(os.path.join(C.CLAUDE_PROJECTS, '*', '*.jsonl')), cutoff))
        # Codex keeps writing a resumed session into the folder of the day it started, so go by modification time,
        # not folder date (a day further back for codex exec runs that started just before the window)
        xfiles = list(fresh(glob.glob(os.path.join(C.CODEX_SESSIONS, '*', '*', '*', 'rollout-*.jsonl')), cutoff - 86400))
        metas = {f: C.codex_meta(f) for f in xfiles}
        by_id = {}                     # Resumed Codex sessions can have multiple logs; keep the latest.
        for f, m in metas.items():
            if m['kind'] == 'thread' and mt[f] >= cutoff:
                if m['id'] not in by_id or mt[f] > mt[by_id[m['id']]]:
                    by_id[m['id']] = f
        threads = list(by_id.values())
        self.exec_metas = sorted([m for m in metas.values() if m['kind'] == 'exec' and m['start']], key=lambda m: m['start'])
        spawn = {}
        for m in metas.values():
            if m['kind'] == 'spawn' and m.get('parent'):
                spawn.setdefault(m['parent'], []).append(m)
        self.spawn_metas = spawn

        todo = sorted(cfiles + threads, key=lambda f: mt[f], reverse=True)
        prog = {'done': 0, 'total': len(todo)}
        if not self.snapshot.get('ready'):
            self.snapshot = dict(self.snapshot, progress=prog)
        for f in todo:
            try:
                self.tail(f, C.ClaudeSession if f.startswith(C.CLAUDE_PROJECTS) else C.CodexThread)
            except OSError as e:        # skip a log that cannot be read right now; try again next scan
                if (f, e.errno) not in self.unreadable:
                    self.unreadable.add((f, e.errno))
                    print('claude-codex-monitor: could not read %s: %r' % (f, e), file=sys.stderr, flush=True)
            prog['done'] += 1
        names = C.codex_thread_names()
        self.cards, self.local_ids, self.spawned = C.ccd_cards(cutoff) if self.cfg.desktop else ({}, {}, {})
        self.local_to_cli = {v: k for k, v in self.local_ids.items()}
        self.prev_rows = {r['key']: r for r in self.snapshot.get('rows', [])}
        rows = []
        with self.lock:
            self.link_cli(cfiles, threads, now)
            for f in cfiles:
                r = self.claude_row(f, live, now)
                if r:
                    rows.append(r)
            for f in threads:
                r = self.codex_row(f, names, now)
                if r:
                    rows.append(r)
        rows.sort(key=lambda r: r['last_ts'] or 0, reverse=True)
        self.snapshot = {'ready': True, 'now': now, 'rows': rows, 'progress': prog,
                         'scan_ms': int((time.time() - now) * 1000), 'days': self.cfg.days, 'lang': self.lang,
                         'lang_locked': self.cfg.lock_lang, 'sources': self.sources(len(cfiles), len(threads)),
                         'summary': self.summary_info()}
        self.housekeep(now, xfiles, rows)

    def housekeep(self, now, codex_files, rows):
        """Keep memory and the state folder from growing while it runs for weeks."""
        keys = {r['key'] for r in rows}
        with self.lock:
            for p in [p for p, t in self.tails.items() if now - getattr(t, 'used', 0) > EVICT_SECONDS]:
                del self.tails[p]        # read again from the start if it is needed later
            self.paths = {k: p for k, p in self.paths.items() if k in keys}
            self.link_cache = {p: v for p, v in self.link_cache.items() if p in self.tails}
        C.prune_caches(set(codex_files))
        if now - self.pruned_at > 600:
            self.pruned_at = now
            before = now - (self.cfg.days + 1) * 86400
            self.summ.prune(before)
            with self.lock:
                old = [k for k, v in self.dismissed.items() if k not in keys and v.get('at', 0) < before]
                if old:
                    for k in old:
                        del self.dismissed[k]
                    self.save_dismissed()

    def sources(self, claude_logs, codex_logs):
        """Where the logs were looked for, so an empty list can say why it is empty."""
        home = os.path.expanduser('~')
        def show(p):
            return ('~' + p[len(home):] if p == home or p.startswith(home + os.sep) else p) + os.sep
        return [{'client': 'Claude Code', 'path': show(C.CLAUDE_PROJECTS), 'found': os.path.isdir(C.CLAUDE_PROJECTS),
                 'logs': claude_logs, 'flag': '--claude-dir'},
                {'client': 'Codex', 'path': show(C.CODEX_SESSIONS), 'found': os.path.isdir(C.CODEX_SESSIONS),
                 'logs': codex_logs, 'flag': '--codex-dir'}]

    # ---- model summaries: the on / off switch ----
    def summary_state(self):
        """none: no backend (--summary off, or neither claude nor codex is installed); otherwise on, unless you
        turned it off in the page."""
        if not self.backend:
            return 'none'
        return self.consent.get('decision') or 'on'

    def set_consent(self, decision):
        self.consent = {'decision': decision, 'at': time.time()}
        tmp = self.consent_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.consent, f)
        os.replace(tmp, self.consent_path)
        self.summ.active = decision == 'on'
        self.snapshot = dict(self.snapshot, summary=self.summary_info())

    def summary_method(self):
        s = self.settings.get('summary')
        return s if not self.cfg.summary_fixed and s in ('auto',) + AUTO_ORDER else self.cfg.summary

    def summary_cfg(self, method=None, model=None):
        """The startup options, with the tool and model chosen in the page (unless startup options fixed them)."""
        method = method or self.summary_method()
        if self.cfg.summary_fixed or method not in ('auto',) + AUTO_ORDER:
            return self.cfg
        if model is None:
            model = (self.settings.get('models') or {}).get(resolve(method), '')
        return dataclasses.replace(self.cfg, summary=method, model=model)

    def set_summary(self, method, model):
        """Switch tool and model from the page. model None: the one last used with that tool."""
        if self.cfg.summary_fixed or method not in ('auto',) + AUTO_ORDER:
            raise ValueError(method)
        name = resolve(method)
        if model is not None and not (name == 'claude-cli' and model in CLAUDE_MODELS
                                      or name == 'codex-cli' and (model == '' or MODEL_RE.match(model))):
            raise ValueError(model)
        backend = make_backend(self.summary_cfg(method, model))      # ValueError when the tool is not installed
        if not backend:
            raise ValueError(method)
        models = dict(self.settings.get('models') or {})
        models[backend.name] = backend.model if backend.model != backend.default_model else ''
        self.settings = {'summary': method, 'models': models}
        tmp = self.settings_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f)
        os.replace(tmp, self.settings_path)
        self.backend = backend
        self.summ.set_backend(backend)
        self.snapshot = dict(self.snapshot, summary=self.summary_info())

    def summary_info(self):
        st = self.summary_state()
        if st == 'none':
            return {'state': 'none'}
        b = self.backend
        return {'state': st, 'label': b.label, 'method': self.summary_method(), 'backend': b.name, 'model': b.model,
                'locked': self.cfg.summary_fixed, 'installed': installed(), 'auto': resolve('auto'),
                'claude_models': CLAUDE_MODELS}

    # ---- Child sessions launched with claude -p ----
    def link_cli(self, cfiles, threads, now):
        """Find claude -p calls in Claude Bash, Codex sessions and Claude's codex exec runs; match them to CLI sessions."""
        self.cl_paths = {'claude:' + os.path.basename(f)[:-6]: f for f in cfiles}
        cands, calls, via = [], [], {}
        for key, f in self.cl_paths.items():
            st = self.state_of(f)
            if st is None:
                continue
            if st.first_ts and st.first_user:
                # Desktop-launched claude -p also records claude-desktop, so match prompt content rather than entrypoint.
                # Only definite CLI sessions (print mode or a non-desktop entrypoint) use the nearest-time fallback.
                cands.append({'key': key, 'start': C.parse_ts(st.first_ts), 'first_user': st.first_user, 'cwd': st.root,
                              'cli': st.print_mode or bool(st.entrypoint and st.entrypoint != 'claude-desktop')})
            links = self.links_for(f, st, now)
            for tid in st.order:
                t = st.tasks[tid]
                if t['kind'] == 'bash' and t.get('claude') and tid not in links:
                    calls.append({'id': tid, 'ts': t['start'], 'cmd': t['command'], 'cwd': t.get('cwd')})
            for tid, rpath in links.items():
                for c in self.rollout_state(rpath).claude_calls.values():
                    c2 = dict(c, title=None)
                    calls.append(c2)
                    via.setdefault(key, []).append((c2, M('exec.codex_claude_cli')))
        for f in threads:
            th = self.state_of(f)
            if th is not None and th.id:
                for c in th.claude_calls.values():
                    calls.append(c)
                    via.setdefault('codex:' + th.id, []).append((c, M('exec.claude_cli')))
        self.cli_links = C.link_claude_cli(calls, cands) if calls and cands else {}
        self.cli_children = set(self.cli_links.values())
        self.cli_calls = via

    def cli_child(self, call_id):
        key = self.cli_links.get(call_id)
        path = self.cl_paths.get(key) if key else None
        return self.state_of(path) if path else None

    def cli_tasks(self, key, now, full):
        return [C.cli_task(c, self.cli_child(c['id']), now, full, executor=label) for c, label in self.cli_calls.get(key, [])]

    # ---- Waiting for user confirmation ----
    def waiting_info(self, key, turns, msg_key, last_user_ts, last_asst_ts):
        """Return (waiting, items, optional suggestions, summarized, summarizing, summary error)."""
        lu, la = C.parse_ts(last_user_ts) or 0, C.parse_ts(last_asst_ts) or 0
        if not la or lu > la:          # An unanswered latest user message is not waiting on the user.
            return False, [], [], True, False, None
        lang = conversation_lang(turns)          # summaries use the conversation's language, not the page's
        self.summ.request(key, msg_key, turns, -la, fmt_local, lang)
        s = self.summ.get(key, lang)
        if s and s.get('msg_key') == msg_key and not s.get('error'):
            items = [{'text': i['text'], 'from_ts': C.parse_ts(i.get('from_ts'))} for i in s.get('items') or []]
            return bool(s.get('waiting')), items, s.get('optional') or [], True, False, None
        err = None
        if s and s.get('msg_key') == msg_key and s.get('error'):
            err = M('summary.error', M('summary.timeout') if 'timed out' in s['error'] else M('summary.call_failed'),
                    M('summary.retry_soon') if s.get('fails', 1) < 3 else M('summary.retry_later'))
        q = quick_asks(turns)
        items = [{'text': x, 'from_ts': la} for x in q]
        return bool(q), items, [], False, self.summ.is_pending(key, lang), err

    def waiting_busy(self, key, turns, last_user_ts):
        """While a session works, check whether earlier requests remain unanswered.
        - Reuse the last summary if there is no newer user message (for example, a child-task notification woke it).
        - Reassess once per new user message, not for every assistant progress update.
        Until that assessment finishes, do not infer waiting status from progress replies."""
        lu = C.parse_ts(last_user_ts) or 0
        lang = conversation_lang(turns)
        s = self.summ.get(key, lang)
        if s and not s.get('error') and s.get('v') == SUMM_VERSION:
            k = s.get('msg_key') or ''
            if (C.parse_ts(k[2:] if k.startswith('u:') else k) or 0) >= lu:
                items = [{'text': i['text'], 'from_ts': C.parse_ts(i.get('from_ts'))} for i in s.get('items') or []]
                return bool(s.get('waiting')) and bool(items), items, s.get('optional') or [], True, False, None
        if not lu:
            return False, [], [], True, False, None
        mk = 'u:' + last_user_ts
        self.summ.request(key, mk, turns, -lu, fmt_local, lang)
        return False, [], [], False, self.summ.is_pending(key, lang), None

    def dismiss_state(self, key, msg_ts):
        d = self.dismissed.get(key)
        if not d:
            return False, False
        if (msg_ts or 0) > (d.get('msg_ts') or 0) + 0.5:
            return False, True
        return True, False

    # ---- Claude rows ----
    def claude_ids(self, sess, links):
        return [i for i in sess.order if sess.tasks[i]['kind'] in ('agent', 'session', 'workflow') or sess.tasks[i]['background']
                or i in links or sess.tasks[i].get('claude') or sess.tasks[i].get('codex')]

    def session_child(self, local_id):
        """The row, from the previous scan, of a new session started from this one (desktop id local_xxx)."""
        cli = self.local_to_cli.get(local_id)
        return self.prev_rows.get('claude:' + cli) if cli else None

    def task_kw(self, sid):
        return {'cli_child': self.cli_child, 'session_child': self.session_child, 'spawned': self.spawned,
                'cards': {c.get('id') for c in self.cards.get(sid, [])}}

    def links_for(self, path, sess, now):
        ck = (path, sess.mtime, len(self.exec_metas))
        c = self.link_cache.get(path)
        if c and c[0] == ck:
            return c[1]
        links = C.link_codex(sess, self.exec_metas, now)
        self.link_cache[path] = (ck, links)
        return links

    def claude_row(self, path, live, now):
        sess = self.state_of(path)
        if sess is None or not sess.last_ts:
            return None
        sid = os.path.basename(path)[:-6]
        key = 'claude:' + sid
        if key in self.cli_children:        # Sessions launched by claude -p belong under their parent.
            return None
        self.paths[key] = path
        sdir = path[:-6]
        links = self.links_for(path, sess, now)
        tasks = [C.claude_task(sess.tasks[i], sdir, links, self.rollout_state, now, full=False, **self.task_kw(sid))
                 for i in self.claude_ids(sess, links)] + self.cli_tasks(key, now, False)
        prog = None
        if tasks:
            prog = {k: sum(1 for t in tasks if t['status'] == k) for k in ('ok', 'run', 'fail', 'stop', 'wait')}
            prog['total'] = len(tasks)
        running = [t for t in tasks if t['status'] == 'run']
        lv = live.get(sid) or {}
        last = C.parse_ts(sess.last_ts)
        msg_ts = C.parse_ts(sess.last_msg_ts) or last
        row = {'key': key, 'client': 'claude', 'title': sess.title or (lv.get('name')) or self.first_user(sess) or sid[:8],
               'project': C.project_of(sess.root), 'cwd': sess.root, 'last_ts': last, 'start_ts': C.parse_ts(sess.first_ts),
               'msg_ts': msg_ts, 'progress': prog, 'items': [], 'optional': [], 'summarized': True, 'summarizing': False,
               'line': C.plain_line(sess.last_text), 'note': None, 'wait_since': None,
               'running_title': running[0]['title'] if running else None}
        busy = lv.get('status') in ('busy', 'shell')
        ended = sess.turn_ended()
        cards = [{'text': M('ask.new_session', c['title']), 'src': 'ask',
                  'from_ts': float(c.get('createdAt') or 0) / 1000 or None} for c in self.cards.get(sid, [])]
        if lv.get('status') == 'waiting':
            # Claude Code shows a permission prompt or another dialog and waits for you; its log does not say so
            asks = [{'text': a['text'], 'from_ts': C.parse_ts(a['ts']), 'src': 'ask'} for a in sess.asks.values()]
            if not asks:
                tool = sess.last_tool.get('text') if sess.last_tool else None
                what = lv.get('waitingFor') or ''
                permission = what in ('', 'permission prompt') and tool
                asks = [{'text': M('ask.approve', tool) if permission else M('ask.dialog', what or 'input needed'),
                         'from_ts': last, 'src': 'ask'}]
            row.update(status='wait', items=asks + cards, wait_since=max(i['from_ts'] or 0 for i in asks + cards) or last)
        elif busy or (not lv and not ended and now - last < 120):
            # Waiting takes priority over working when questions, cards or earlier unanswered requests remain.
            note = M('note.doing', sess.last_tool.get('text', M('note.reply'))) if sess.last_tool else M('note.claude_reply')
            asks = [{'text': a['text'], 'from_ts': C.parse_ts(a['ts']), 'src': 'ask'} for a in sess.asks.values()]
            w, items, opt, done, pend, err = self.waiting_busy(key, sess.turns, sess.last_user_ts)
            row.update(items=asks + cards + (items if w else []), optional=opt, summarized=done, summarizing=pend, summ_error=err, note=note)
            if row['items']:
                row['status'], row['running'] = 'wait', True
                row['wait_since'] = max(i['from_ts'] or 0 for i in row['items']) or None
            else:
                row['status'] = 'run'
                row['wait_since'] = C.parse_ts(sess.last_user_ts) or last
        elif sess.asks:
            row['status'] = 'wait'
            row['items'] = [{'text': a['text'], 'from_ts': C.parse_ts(a['ts']), 'src': 'ask'} for a in sess.asks.values()] + cards
            row['wait_since'] = max(i['from_ts'] or 0 for i in row['items'])
        elif sess.api_error and (C.parse_ts(sess.api_error['ts']) or 0) >= (C.parse_ts(sess.last_asst_text_ts) or 0):
            row['status'], row['note'] = 'fail', M('note.error', sess.api_error['text'] or M('error.api'))
        elif sess.interrupted_ts and (C.parse_ts(sess.interrupted_ts) or 0) >= (C.parse_ts(sess.last_asst_text_ts) or 0):
            row['status'], row['note'] = 'fail', M('note.interrupted')
        elif not ended and now - last > C.TURN_DEAD_SECONDS and not running:
            row['status'], row['note'] = 'fail', M('note.claude_incomplete')
        else:
            w, items, opt, done, pend, err = self.waiting_info(key, sess.turns, sess.last_msg_ts, sess.last_user_ts, sess.last_asst_text_ts)
            row.update(items=cards + (items if w else []), optional=opt, summarized=done, summarizing=pend, summ_error=err)
            if w or cards:
                # New-session cards require a click even if the model treats the suggestion as optional.
                row['status'] = 'wait'
                row['wait_since'] = C.parse_ts(sess.last_asst_text_ts) if w else max(i['from_ts'] or 0 for i in cards)
            elif running:
                row['status'] = 'run'
                row['note'] = M('note.waiting_tasks', len(running), running[0]['title'])
            else:
                row['status'] = 'ok'
        row['dismissed'], row['reappeared'] = self.dismiss_state(key, msg_ts)
        return row

    @staticmethod
    def first_user(st):
        u = next((t for t in st.turns if t['role'] == 'user'), None)
        return C.clip(u['text'].replace('\n', ' '), 40) if u else None

    # ---- Codex rows ----
    def children(self, tid):
        return [m for m in self.spawn_metas.get(tid, [])]

    def codex_row(self, path, names, now):
        th = self.state_of(path)
        if th is None or not th.id or not th.last_ts:
            return None
        key = 'codex:' + th.id
        self.paths[key] = path
        last = C.parse_ts(th.last_ts)
        msg_ts = C.parse_ts(th.last_msg_ts) or last
        kids = [self.rollout_state(m['path']) for m in self.children(th.id)]
        sts = ['ok' if k.complete else 'fail' if k.aborted else 'run' for k in kids]
        sts += [t['status'] for t in self.cli_tasks(key, now, False)]
        prog = None
        if sts:
            prog = {k: sts.count(k) for k in ('ok', 'run', 'fail', 'stop')}
            prog['total'] = len(sts)
        cur = next((s.get('step') for s in th.plan if s.get('status') == 'in_progress'), None)
        last_text = next((t['text'] for t in reversed(th.turns) if t['role'] == 'assistant'), '')
        row = {'key': key, 'client': 'codex', 'title': names.get(th.id) or C.clip((th.first_user or '').replace('\n', ' '), 40) or th.id[:8],
               'project': C.project_of(th.cwd), 'cwd': th.cwd, 'last_ts': last, 'start_ts': C.parse_ts(th.first_ts),
               'msg_ts': msg_ts, 'progress': prog, 'items': [], 'optional': [], 'summarized': True, 'summarizing': False,
               'line': C.plain_line(last_text), 'note': None, 'wait_since': None, 'running_title': None}
        working = th.in_turn and now - last < C.TURN_DEAD_SECONDS
        if working:
            done = sum(1 for s in th.plan if s.get('status') == 'completed')
            row['note'] = M('note.step', done + 1, len(th.plan), cur) if cur else (M('note.doing', th.last_cmd) if th.last_cmd else M('note.codex_reply'))
        if th.pending:
            row['status'], row['running'] = 'wait', working
            row['items'] = [{'text': th.pending['text'], 'from_ts': C.parse_ts(th.pending['ts']), 'src': 'ask'}]
            row['wait_since'] = C.parse_ts(th.pending['ts'])
        elif working:
            # Earlier unanswered requests take priority over a running turn; also mark it as still running.
            w, items, opt, done_, pend, err = self.waiting_busy(key, th.turns, th.last_user_ts)
            row.update(optional=opt, summarized=done_, summarizing=pend, summ_error=err)
            if w:
                row.update(status='wait', running=True, items=items,
                           wait_since=max(i['from_ts'] or 0 for i in items) or None)
            else:
                row['status'] = 'run'
                row['wait_since'] = C.parse_ts(th.turn_start)
        elif th.in_turn:
            row['status'], row['note'] = 'fail', M('note.codex_incomplete')
        elif th.last_event == 'aborted':
            row['status'], row['note'] = 'fail', M('note.aborted')
        else:
            w, items, opt, done, pend, err = self.waiting_info(key, th.turns, th.last_msg_ts, th.last_user_ts, th.last_asst_text_ts)
            row.update(items=items, optional=opt, summarized=done, summarizing=pend, summ_error=err)
            row['status'] = 'wait' if w else 'ok'
            if w:
                row['wait_since'] = C.parse_ts(th.last_asst_text_ts)
        row['dismissed'], row['reappeared'] = self.dismiss_state(key, msg_ts)
        return row

    # ---- Details ----
    def open_url(self, key):
        """Build a desktop session URL only from server-known IDs, never from page-supplied URLs."""
        if not self.cfg.desktop or sys.platform != 'darwin':     # both desktop apps' links are macOS only here
            return None
        if key.startswith('claude:'):
            lid = self.local_ids.get(key[len('claude:'):])
            return 'claude://claude.ai/epitaxy/' + lid if lid and re.fullmatch(r'local_[0-9a-f-]{36}', lid) else None
        if key.startswith('codex:'):
            tid = key[len('codex:'):]
            return 'codex://threads/' + tid if re.fullmatch(r'[0-9a-f-]{36}', tid) else None
        return None

    def detail(self, key):
        row = next((r for r in self.snapshot.get('rows', []) if r['key'] == key), None)
        path = self.paths.get(key)
        if not row or not path:
            return None
        now = time.time()
        with self.lock:
            d = self.claude_detail(row, path, now) if key.startswith('claude:') else self.codex_detail(row, path, now)
        # Browser links open on a user click; macOS does not foreground apps opened by this background server.
        d['open_url'] = self.open_url(key)
        return d

    def claude_detail(self, row, path, now):
        sess = self.state_of(path)
        sid = os.path.basename(path)[:-6]
        lv = C.live_claude_sessions().get(sid) or {}
        links = self.links_for(path, sess, now)
        tasks = [C.claude_task(sess.tasks[i], path[:-6], links, self.rollout_state, now, full=True, **self.task_kw(sid))
                 for i in self.claude_ids(sess, links)] + self.cli_tasks(row['key'], now, True)
        bs = C.batches(tasks)
        u = sess.usage_sum()
        ep = sess.entrypoint or ''
        info = {'status': row['status'], 'start': C.parse_ts(sess.first_ts), 'last': C.parse_ts(sess.last_ts),
                'cwd': sess.root, 'branch': sess.branch, 'model': sess.model, 'pid': lv.get('pid'),
                'proc': M('proc.open', M({'busy': 'proc.busy', 'shell': 'proc.busy', 'waiting': 'proc.waiting'}.get(
                    lv.get('status'), 'proc.idle'))) if lv else M('proc.closed'),
                'id': sid, 'usage': {'output': u['output'], 'cache_read': u['cache_read'], 'input': u['input'] + u['cache_write']},
                'files': list(sess.files.keys()), 'client': M('client.claude_desktop') if ep == 'claude-desktop' else (M('client.claude_cli') if ep == 'cli' else M('client.claude_other', ep) if ep else 'Claude')}
        return {'row': row, 'info': info, 'tasks': tasks, 'batches': bs, 'todos': C.todo_progress(sess.todos)}

    def codex_detail(self, row, path, now):
        th = self.state_of(path)
        tasks = []
        for m in self.children(th.id):
            r = self.rollout_state(m['path'])
            st = 'ok' if r.complete else 'fail' if r.aborted else 'run'
            prog = None
            if r.plan:
                done = sum(1 for s in r.plan if s.get('status') == 'completed')
                prog = {'done': len(r.plan) if st == 'ok' else done, 'total': len(r.plan), 'steps': r.plan,
                        'current': next((s.get('step') for s in r.plan if s.get('status') == 'in_progress'), None)}
            title = C.clip(m['prompt'].replace('\n', ' '), 60 - len(m.get('nickname') or '') - (1 if m.get('nickname') else 0)) if m.get('prompt') else M('task.child_thread')
            if m.get('nickname'):
                title = M('tool.brief', m['nickname'], title)
            t = {'id': m['path'], 'title': title,
                 'executor': M('exec.codex_thread'), 'status': st, 'start': C.parse_ts(r.start), 'end': C.parse_ts(r.end),
                 'last_activity': C.parse_ts(r.last_ts), 'progress': prog, 'current': r.last_cmd, 'last_cmd': r.last_cmd,
                 'stats': {'commands': r.n_cmd, 'patches': r.n_patch, 'tokens': r.tokens}, 'events': list(r.events),
                 'result': C.clip(r.final or r.last_msg or '', 3000) or None, 'files': [], 'say': M('task.spawn_thread')}
            if st == 'run':
                t['idle_seconds'] = now - (t['last_activity'] or now)
                t['stale'] = t['idle_seconds'] > C.STALE_SECONDS
            tasks.append(t)
        tasks += self.cli_tasks(row['key'], now, True)
        bs = C.batches(tasks)
        steps = th.plan_steps()
        if steps:
            idx = len(bs)
            ids = []
            for i, s in enumerate(steps):
                st = {'completed': 'ok', 'in_progress': 'run' if th.in_turn else 'stop'}.get(s['status'], 'pending')
                tid = 'step-%d' % i
                tasks.append({'id': tid, 'title': s['step'], 'executor': M('exec.plan_step', i + 1), 'status': st,
                              'start': s['start'], 'end': s['end'], 'last_activity': s['end'] or s['start'], 'progress': None,
                              'stats': {}, 'events': [], 'files': [], 'batch': idx})
                ids.append(tid)
            bs.append({'idx': idx, 'at': C.parse_ts(th.turn_start), 'say': M('batch.plan', th.turn_n), 'tasks': ids, 'plan': True})
        info = {'status': row['status'], 'start': C.parse_ts(th.first_ts), 'last': C.parse_ts(th.last_ts),
                'cwd': th.cwd, 'branch': th.branch, 'model': th.model, 'pid': None, 'proc': '—',
                'id': th.id, 'usage': {'output': th.out_tokens, 'total': th.tokens}, 'files': list(th.files.keys()),
                'client': M('client.codex_desktop') if (th.originator or '').startswith('Codex Desktop') else (M('client.codex_cli') if th.originator else 'Codex')}
        return {'row': row, 'info': info, 'tasks': tasks, 'batches': bs, 'todos': None}

    # ---- Language and dismissal ----
    def set_dismissed(self, keys, on):
        rows = {r['key']: r for r in self.snapshot.get('rows', [])}
        with self.lock:
            for k in keys:
                if on and k in rows:
                    self.dismissed[k] = {'at': time.time(), 'msg_ts': rows[k]['msg_ts']}
                    rows[k]['dismissed'], rows[k]['reappeared'] = True, False
                elif not on:
                    self.dismissed.pop(k, None)
                    if k in rows:
                        rows[k]['dismissed'] = rows[k]['reappeared'] = False
            self.save_dismissed()

    def save_dismissed(self):
        tmp = self.dismiss_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.dismissed, f, ensure_ascii=False)
        os.replace(tmp, self.dismiss_path)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def mon(self):
        return self.server.mon

    def _send(self, code, body, ctype='application/json; charset=utf-8', csp="frame-ancestors 'none'", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', csp)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return (self.headers.get('Host') or '').split(':')[0] in ('127.0.0.1', 'localhost')

    @property
    def cookie_name(self):
        return 'ccm_%d' % self.server.server_address[1]       # cookies are per host, not per port

    def _token_ok(self):
        """Data needs the access token, sent as a cookie set when the page was opened with ?token=. Cookies are read
        one by one: other local apps on 127.0.0.1 may leave cookies a strict parser would reject, hiding ours."""
        tok = getattr(self.server, 'token', None)
        if not tok:
            return True
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == self.cookie_name and same(value.strip().strip('"'), tok):
                return True
        return False

    def _need_token(self):
        return self._send(401, {'error': M('error.token'), 'token_file': show_path(self.server.token_file)})

    def _origin_ok(self):
        """Browsers send Origin with a POST; only this very page may post. Programs that send none still need the token."""
        origin = self.headers.get('Origin')
        port = self.server.server_address[1]
        return origin is None or origin in ('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': M('error.host')})
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in STATIC_FILES:
                tok = getattr(self.server, 'token', None)
                if tok and q.get('token') and same(q['token'], tok):
                    # Remember the token in this browser, then drop it from the address
                    cookie = '%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000' % (self.cookie_name, tok)
                    rest = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != 'token'])
                    return self._send(302, b'', 'text/plain', headers=(('Set-Cookie', cookie),
                                                                        ('Location', u.path + ('?' + rest if rest else ''))))
                name, ctype = STATIC_FILES[u.path]
                with open(os.path.join(STATIC_DIR, name), 'rb') as f:
                    return self._send(200, f.read(), ctype)
            if not self._token_ok():
                return self._need_token()
            if u.path == '/api/sessions':
                return self._send(200, self.mon.snapshot)
            if u.path == '/api/session':
                d = self.mon.detail(q.get('key') or '')
                return self._send(200, d) if d else self._send(404, {'error': M('error.session_missing')})
            if u.path == '/file':
                return self._file(q)
            return self._send(404, {'error': M('error.not_found')})
        except Exception as e:
            return self._send(500, {'error': M('error.internal', repr(e))})

    def do_POST(self):
        # JSON only: a page on another site cannot send that without asking first, and this server never says yes
        json_body = (self.headers.get('Content-Type') or '').split(';')[0].strip() == 'application/json'
        if not self._host_ok() or not self._origin_ok() or not json_body:
            return self._send(403, {'error': M('error.forbidden')})
        if not self._token_ok():
            return self._need_token()
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
            if self.path == '/api/consent':
                decision = body.get('decision') if isinstance(body, dict) else None
                if decision not in ('on', 'off') or not self.mon.backend:
                    return self._send(400, {'error': M('error.consent')})
                self.mon.set_consent(decision)
                return self._send(200, {'ok': True, 'summary': self.mon.summary_info()})
            if self.path == '/api/summary':
                body = body if isinstance(body, dict) else {}
                model = body.get('model')
                try:
                    self.mon.set_summary(body.get('method'), model.strip() if isinstance(model, str) else None)
                except ValueError:
                    return self._send(400, {'error': M('error.summary_settings')})
                return self._send(200, {'ok': True, 'summary': self.mon.summary_info()})
            keys = [k for k in body.get('keys') or [] if isinstance(k, str)]
            if self.path == '/api/dismiss':
                self.mon.set_dismissed(keys, True)
            elif self.path == '/api/open':
                # Fall back to the server opener if an embedded browser blocks the page's desktop link.
                # The app switches sessions, but macOS may not foreground a window opened by a background process.
                url = self.mon.open_url(keys[0]) if keys else None
                if not url:
                    return self._send(404, {'error': M('error.desktop_session')})
                opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                r = subprocess.run([opener, url], capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    return self._send(500, {'error': M('error.open', r.stderr.strip() or str(r.returncode))})
                return self._send(200, {'ok': True})
            elif self.path == '/api/restore':
                self.mon.set_dismissed(keys, False)
            else:
                return self._send(404, {'error': M('error.not_found')})
            return self._send(200, {'ok': True, 'keys': keys})
        except Exception as e:
            return self._send(500, {'error': M('error.internal', repr(e))})

    def _file(self, q):
        """Read-only output viewer, restricted to text files in session and Claude background-output folders."""
        p = os.path.realpath(q.get('path') or '')
        row = next((r for r in self.mon.snapshot.get('rows', []) if r['key'] == q.get('key')), None)
        roots = [r for r in [claude_tmp()] if r]       # Claude Code background task output
        if row and row.get('cwd'):
            roots.append(os.path.realpath(row['cwd']))
        if not any(p.startswith(r + os.sep) for r in roots) or not p.endswith(VIEWABLE_EXT) or not os.path.isfile(p):
            return self._send(403, 'Not viewable (outside the session folder): ' + p, 'text/plain; charset=utf-8')
        size = os.path.getsize(p)
        with open(p, 'rb') as f:
            if size > 3_000_000:
                f.seek(size - 3_000_000)
            data = f.read()
        if p.endswith('.html'):
            # Shown as a page, but as a separate, sandboxed origin: its scripts cannot read this monitor's data
            return self._send(200, data, 'text/html; charset=utf-8',
                              csp="sandbox allow-scripts allow-popups allow-downloads allow-modals; frame-ancestors 'none'")
        return self._send(200, data, 'text/plain; charset=utf-8')


def port_free(port):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)    # as the server does; a port just closed is free
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def autostart_cmd(cfg, argv):
    if cfg.remove_autostart:
        path, existed = autostart.remove()
        print('claude-codex-monitor: ' + ('no longer starts at login (removed %s)' % path if existed else 'was not set to start at login'))
        return
    # Check now what would make the login item stop at once, so it is not installed in a broken state
    if not port_free(cfg.port):
        if monitor_at(cfg.port):
            sys.exit('claude-codex-monitor: a monitor is already running on port %d. Stop it first; if it is the login '
                     'item, run --remove-autostart, then this command again.' % cfg.port)
        sys.exit('claude-codex-monitor: port %d is in use by another program. Add --port with another number.' % cfg.port)
    try:
        make_backend(cfg)
    except ValueError as e:
        sys.exit('claude-codex-monitor: %s' % e)
    page = page_url(cfg)        # the login item uses the same state folder, so the same token
    path, log = autostart.install(argv, cfg.state_dir)
    print('claude-codex-monitor: started, and starts again whenever you log in.\n'
          '  page:     %s\n  settings: %s\n  log:      %s\n'
          '  stop it:  run the same command with --remove-autostart\n'
          'If another copy is already running on port %d, stop that one first.' % (page, path, log, cfg.port))


def use_demo(cfg):
    """Point the monitor at freshly made-up sessions, with a state folder of their own; never read this computer's
    sessions, the desktop app's files or send anything to a model."""
    root = tempfile.mkdtemp(prefix='claude-codex-monitor-demo-')      # one per run, so runs never overwrite each other
    atexit.register(shutil.rmtree, root, True)
    now = time.time()
    cfg.claude_dir, cfg.codex_dir = demo.build(os.path.join(root, 'data'), now)
    cfg.state_dir, cfg.desktop, cfg.summary, cfg.token = os.path.join(root, 'state'), False, 'off', False
    os.makedirs(cfg.state_dir)
    with open(os.path.join(cfg.state_dir, 'dismissed.json'), 'w', encoding='utf-8') as f:
        json.dump({k: {'at': now, 'msg_ts': now} for k in demo.DISMISSED}, f)


def open_page(url):
    """Open the page in the default browser, unless there is no screen here to show it on: over SSH, or on Linux
    without a desktop, Python would fall back to a text browser that takes over the terminal."""
    if os.environ.get('SSH_CONNECTION') or (sys.platform.startswith('linux') and
                                             not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))):
        return False
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def monitor_at(port):
    """Whether the program on this port is a claude-codex-monitor (it may answer that it needs the token)."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/sessions' % port, timeout=2) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            try:
                d = json.load(e)
            except ValueError:
                return False
    except (OSError, ValueError):
        return False
    return isinstance(d, dict) and ('rows' in d or 'token_file' in d)


def page_url(cfg, create=True):
    """The address to open, with the token. create=False only reads an existing token (for a monitor already running)."""
    url = 'http://127.0.0.1:%d/' % cfg.port
    tok = cfg.token and (load_token if create else read_token)(cfg.state_dir)
    return url + '?token=' + tok if tok else url


def main(argv=None):
    if os.name == 'nt':
        sys.exit('claude-codex-monitor: Windows is not supported; it runs on macOS and Linux.')
    cfg = parse_args(argv)
    if cfg.demo:
        cfg.token = False       # made-up sessions only, nothing to protect
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))     # stopping the service: exit normally, clean up
    os.umask(0o077)     # everything it writes (state, summaries, log, login item) is readable by you only
    if cfg.install_autostart or cfg.remove_autostart:
        return autostart_cmd(cfg, sys.argv[1:] if argv is None else argv)
    url = 'http://127.0.0.1:%d/' % cfg.port
    try:        # take the port before anything else, so a second start changes nothing
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', cfg.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if not monitor_at(cfg.port):
            sys.exit('claude-codex-monitor: port %d is in use by another program. Start with --port and another number, '
                     'for example --port %d.' % (cfg.port, cfg.port + 1))
        page = page_url(cfg, create=False)
        print('claude-codex-monitor: already running at %s' % page, flush=True)
        if cfg.open:
            open_page(page)
        return
    if cfg.demo:
        use_demo(cfg)
    C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
    try:
        mon = Monitor(cfg)
    except ValueError as e:
        sys.exit('claude-codex-monitor: %s' % e)
    threading.Thread(target=mon.loop, daemon=True).start()
    srv.mon = mon
    srv.token = load_token(cfg.state_dir) if cfg.token else None
    srv.token_file = os.path.join(cfg.state_dir, 'token')
    page = page_url(cfg)
    st = mon.summary_state()
    summ = 'off' if st == 'none' else 'off (turned off in the page)' if st == 'off' else mon.backend.label
    print('claude-codex-monitor: %s  (%smodel summaries: %s)  Press Ctrl+C to stop.' % (page, 'demo data, ' if cfg.demo else '', summ),
          flush=True)
    if cfg.open:
        threading.Thread(target=open_page, args=(page,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
