"""Read-only data layer: reads the session logs Claude Code and Codex write on this computer.

Scan local Claude Code and Codex logs for session status, unanswered requests, subtasks and batches.
- Claude: ~/.claude/projects/<proj>/<session>.jsonl; subagents in <session>/subagents/; processes in ~/.claude/sessions/<pid>.json.
- Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl; titles in ~/.codex/session_index.jsonl.
  - Desktop / CLI sessions appear at the top level.
  - codex exec sessions (mostly launched by Claude) are subtasks of their calling Claude session.
  - Desktop thread_spawn sessions belong to their parent; approval guardian threads are hidden.
"""
import collections
import datetime as dt
import glob
import json
import os
import re
import sys
import time

HOME = os.path.expanduser('~')
CLAUDE_PROJECTS = os.path.join(HOME, '.claude', 'projects')
CLAUDE_SESSIONS = os.path.join(HOME, '.claude', 'sessions')
CODEX_SESSIONS = os.path.join(HOME, '.codex', 'sessions')
CODEX_INDEX = os.path.join(HOME, '.codex', 'session_index.jsonl')
SCAN_DAYS = 7


def configure(claude_dir, codex_dir, days):
    """Point the readers at the given Claude Code / Codex data folders."""
    global CLAUDE_PROJECTS, CLAUDE_SESSIONS, CODEX_SESSIONS, CODEX_INDEX, SCAN_DAYS
    CLAUDE_PROJECTS = os.path.join(claude_dir, 'projects')
    CLAUDE_SESSIONS = os.path.join(claude_dir, 'sessions')
    CODEX_SESSIONS = os.path.join(codex_dir, 'sessions')
    CODEX_INDEX = os.path.join(codex_dir, 'session_index.jsonl')
    SCAN_DAYS = days
STALE_SECONDS = 10 * 60          # Flag subtasks with no activity for this long as possibly stuck.
TURN_DEAD_SECONDS = 20 * 60      # An unfinished turn with no records for this long ended abnormally.
BATCH_GAP_SECONDS = 120          # Subtasks started within this gap belong to the same batch.
SCRIPT_EXT = ('.sh', '.zsh', '.bash', '.py', '.js', '.mjs', '.ts')


# ---------- Common helpers ----------

def join_clauses(parts):
    """Join short sentences with the semicolon of their own language."""
    return ('；' if any(re.search(r'[\u4e00-\u9fff]', p) for p in parts) else '; ').join(parts)


def M(code, *args):
    """A message the page translates: {'m': code, 'a': [args]}. Args may be strings, numbers or other messages."""
    return {'m': code, 'a': list(args)}


def parse_ts(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e11 else 1)
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def load_json(s):
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s or '{}')
    except ValueError:
        return {}


def norm(s):
    return re.sub(r'\s+', '', s or '')


def clip(s, n=200):
    s = (s or '').strip()
    return s if len(s) <= n else s[:n] + '…'


def iter_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from iter_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from iter_strings(v)


def tag(block, name):
    m = re.search(r'<%s>(.*?)</%s>' % (name, name), block, re.S)
    return m.group(1).strip() if m else None


def plain_line(text, n=140):
    """First nonempty reply line without Markdown markers, for the session list."""
    for ln in (text or '').splitlines():
        ln = re.sub(r'[*`#>]+', '', ln).strip(' -\t')
        if ln and not ln.startswith('```') and not ln.startswith('|'):
            return clip(ln, n)
    return ''


def tool_brief(name, inp):
    inp = inp or {}
    for k in ('description', 'command', 'file_path', 'pattern', 'url', 'query', 'prompt', 'path'):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return M('tool.brief', name, clip(v.replace('\n', ' '), 140))
    return name


def tail_text(path, nbytes=6000, nlines=14):
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - nbytes))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    data = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', data)
    return [ln for ln in data.splitlines() if ln.strip()][-nlines:]


_project_cache = {}
WORKTREE_GITDIR_RE = re.compile(r'gitdir:\s*(.+?)/\.git/worktrees/')
WORKTREE_PATH_RE = re.compile(r'^(.*?)/\.(?:claude|codex)/worktrees/')


def project_of(cwd):
    """Project name for a working directory: the folder name of its git repository (a worktree maps back
    to its main repository), otherwise the folder name itself."""
    if not cwd:
        return ''
    cwd = cwd.rstrip('/') or '/'
    if cwd in _project_cache:
        return _project_cache[cwd]
    name, d = None, cwd
    while name is None:
        g = os.path.join(d, '.git')
        if os.path.isdir(g):
            name = os.path.basename(d)
        elif os.path.isfile(g):
            try:
                with open(g, encoding='utf-8', errors='replace') as f:
                    m = WORKTREE_GITDIR_RE.match(f.read().strip())
            except OSError:
                m = None
            name = os.path.basename(m.group(1)) if m else os.path.basename(d)
        elif os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    if name is None:        # folder gone (e.g. a removed worktree) or not in a repository
        m = WORKTREE_PATH_RE.match(cwd + '/')
        name = os.path.basename(m.group(1)) if m else os.path.basename(cwd)
    _project_cache[cwd] = name or cwd
    return _project_cache[cwd]


_script_cache = {}

# A command segment (split at ; && || | newlines $( `) must start with `claude ... -p/--print` or `codex ... exec`.
# Quoted mentions (grep, echo, ps) do not count. A segment may start with ( time nohup timeout N, variables, env, a path.
CLI_PREFIX = r'^(?:\(\s*)*(?:(?:time|nohup)\s+|timeout\s+\S+\s+)*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:env\s+(?:\S+=\S*\s+)*)?(?:[\w.~/-]*/)?'
CLAUDE_SEG_RE = re.compile(CLI_PREFIX + r'claude\s+(?:.*\s)?(?:-p|--print)(?:\s|$|=)', re.S)
CODEX_SEG_RE = re.compile(CLI_PREFIX + r'codex\s+(?:\S+\s+)*?exec(?:\s|$)', re.S)
HELP_RE = re.compile(r'\s(?:--help|-h|--version|-V)(?:\s|$)')


def _segments(text):
    return [seg.strip() for seg in re.split(r'\|\||&&|[;|\n]|\$\(|`', _unquote(text))]


def _codex_in(text):
    return any(CODEX_SEG_RE.match(seg) and not HELP_RE.search(seg + ' ') for seg in _segments(text))


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2(?=\s|$)", re.S)


def _unquote(cmd):
    """Remove heredoc bodies and quoted content, keeping command structure (not claude mentions in python3 -c or cat <<EOF)."""
    cmd = HEREDOC_RE.sub('<<HEREDOC', cmd)
    out, q, i = [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            if ch == '\\' and q == '"' and i + 1 < len(cmd):
                i += 2
                continue
            if ch == q:
                q = None
                out.append(ch)
            i += 1
            continue
        if ch in '\'"':
            q = ch
        out.append(ch)
        i += 1
    return ''.join(out)


def _claude_in(text):
    return any(CLAUDE_SEG_RE.match(seg) and not HELP_RE.search(seg + ' ') for seg in _segments(text))


INTERPRETERS = {'bash', 'sh', 'zsh', 'python', 'python3', 'node', 'source', '.', 'tsx', 'bun', 'deno'}
ASSIGN_RE = re.compile(r'(?:^|[\s;&(])([A-Za-z_]\w*)=([^\s;&|`$()\'"]+)')


def _expand(tok, cmd):
    """Substitute variables the command defines itself (SP=/tmp/x; ... $SP/a.md), then expand ~."""
    if '$' in tok:
        env = dict(ASSIGN_RE.findall(cmd))
        tok = re.sub(r'\$\{?(\w+)\}?', lambda m: env.get(m.group(1), m.group(0)), tok)
    return os.path.expanduser(tok)


def _executed_scripts(cmd):
    """Scripts a command actually runs: a segment that starts with the script, or a script after an interpreter.
    Scripts that sed, grep, cat or ls merely read do not count."""
    for seg in _segments(cmd):
        toks = [t.lstrip('(') for t in seg.split()]
        i = 0
        while i < len(toks) and (not toks[i] or toks[i] in ('time', 'nohup', 'env', 'exec') or re.match(r'^[A-Za-z_]\w*=', toks[i])):
            i += 1
        if i < len(toks) and toks[i] == 'timeout':
            i += 2
        if i >= len(toks):
            continue
        if toks[i].endswith(SCRIPT_EXT):
            yield toks[i]
        elif os.path.basename(toks[i]) in INTERPRETERS or re.match(r'^python3?(\.\d+)?$', os.path.basename(toks[i])):
            for t in toks[i + 1:]:
                if t in ('-c', '-m', '-', '-e'):
                    break
                if t.endswith(SCRIPT_EXT):
                    yield t
                    break


def _calls_cli(cmd, cwd, found):
    """Whether the command, or a script it runs, invokes a CLI."""
    if found(cmd):
        return True
    for tok in _executed_scripts(cmd):
        tok = _expand(tok, cmd)
        p = tok if tok.startswith('/') else os.path.join(cwd or '', tok)
        key = (p, found.__name__)
        if key not in _script_cache:
            try:
                with open(p, encoding='utf-8', errors='replace') as f:
                    _script_cache[key] = found(f.read(200000))
            except OSError:
                _script_cache[key] = False
        if _script_cache[key]:
            return True
    return False


def mentions_codex(cmd, cwd):
    return _calls_cli(cmd, cwd, _codex_in)


def mentions_claude(cmd, cwd):
    return _calls_cli(cmd, cwd, _claude_in)


_snip_cache = {}


def prompt_snippets(cmd, cwd):
    """Opening snippets of referenced prompt files, matched against Codex's first user message."""
    key = (cmd, cwd)
    if key in _snip_cache:
        return _snip_cache[key]
    out = []
    for tok in re.findall(r'[^\s\'"`;|&()<>]+', cmd):
        tok = _expand(tok, cmd)
        p = tok if tok.startswith('/') else os.path.join(cwd or '', tok)
        if p.endswith(SCRIPT_EXT) or not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) > 500000:
                continue
            with open(p, encoding='utf-8', errors='replace') as f:
                snip = norm(f.read(3000))[:200]
        except OSError:
            continue
        if len(snip) >= 20:
            out.append(snip)
    _snip_cache[key] = out
    return out


# ---------- Incremental reading ----------

READ_BLOCK = 8 * 1024 * 1024


class JsonlTail:
    """Track byte offsets and read only new complete lines; state.want(raw) decides which lines to decode."""

    def __init__(self, path, factory):
        self.path, self.factory = path, factory
        self.state = factory()
        self.pos, self.buf, self.ino = 0, b'', None
        self.size = self.mtime = 0
        self.bad = 0                    # lines that could not be read; skipped one by one

    def poll(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return self.state
        if st.st_ino != self.ino or st.st_size < self.pos:
            self.state, self.pos, self.buf, self.ino = self.factory(), 0, b'', st.st_ino
        if st.st_size > self.pos:
            with open(self.path, 'rb') as f:
                f.seek(self.pos)
                while True:     # in blocks, so a log of a few hundred MB never sits in memory whole
                    data = f.read(READ_BLOCK)
                    if not data:
                        break
                    self.pos += len(data)
                    lines = (self.buf + data).split(b'\n')
                    self.buf = lines.pop()
                    self._feed(lines)
        self.size, self.mtime = st.st_size, st.st_mtime
        self.state.mtime = st.st_mtime
        return self.state

    def _feed(self, lines):
        want = self.state.want
        for raw in lines:
            if not raw.strip() or not want(raw):
                continue
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    self.state.feed(d, raw)
            except Exception as e:      # one odd line must not lose the lines after it
                self.bad += 1
                if self.bad == 1:
                    print('claude-codex-monitor: skipped a line it could not read in %s: %r' % (self.path, e),
                          file=sys.stderr, flush=True)


# ---------- Main Claude sessions ----------

NOTIF_RE = re.compile(r'<task-notification>(.*?)</task-notification>', re.S)
TOOL_ID_RE = re.compile(rb'"tool_use_id":"([A-Za-z0-9_]+)"')
FILE_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')


class ClaudeSession:
    def __init__(self):
        self.sid = self.title = self.root = self.cwd = None
        self.first_ts = self.last_ts = None
        self.entrypoint = self.branch = self.model = None
        self.tasks, self.order, self.watch = {}, [], set()
        self.todos = []
        self.task_list = collections.OrderedDict()
        self.pending_create = {}
        self.asks = {}                                # Unanswered AskUserQuestion / ExitPlanMode calls.
        self.turns = collections.deque(maxlen=80)     # Text turns: {role, ts, text}.
        self.last_text = ''                           # Latest assistant text, used as a batch description.
        self.last_tool = None
        self.last_msg_ts = self.last_asst_ts = self.last_asst_text_ts = self.last_user_ts = None
        self.last_stop_reason = None
        self.last_hook_stop_ts = None
        self.interrupted_ts = None
        self.api_error = None
        self.usage = {}                               # Message ID -> usage; deduplicate replies split across lines.
        self.files = collections.OrderedDict()
        self.first_user = None
        self.n_tools = 0
        self.print_mode = False                      # Sessions launched with claude -p record querySource=sdk.
        self.proposals = {}                           # Proposed new session task_id -> subtask id.
        self.starts = {}                              # start_session call that starts a proposal -> that subtask id.
        self.mtime = 0

    def want(self, raw):
        if b'task-notification' in raw:
            return True
        if b'"type":"attachment"' in raw or b'"type":"progress"' in raw:
            return False
        if b'"type":"api-request-blob"' in raw or b'"type":"api-request-shape"' in raw:
            return False
        if b'"tool_result"' in raw and b'"type":"user"' in raw:
            ids = TOOL_ID_RE.findall(raw)
            return any(i.decode() in self.watch for i in ids) or b'[Request interrupted' in raw
        return True

    def feed(self, d, raw):
        ts = d.get('timestamp')
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        t = d.get('type')
        if t == 'custom-title':
            self.title = d.get('customTitle') or self.title
            return
        if d.get('sessionId'):
            self.sid = d['sessionId']
        if d.get('cwd'):
            self.root = self.root or d['cwd']
            self.cwd = d['cwd']
        if d.get('entrypoint'):
            self.entrypoint = d['entrypoint']
        if d.get('querySource') == 'sdk':
            self.print_mode = True
        if d.get('gitBranch'):
            self.branch = d['gitBranch']
        if b'task-notification' in raw:
            self._notifications(d, ts)
        if t == 'system' and d.get('subtype') == 'stop_hook_summary':
            self.last_hook_stop_ts = ts
            return
        if t == 'queue-operation':
            c = d.get('content')
            if d.get('operation') == 'enqueue' and isinstance(c, str):
                self._user_text(c, ts)
            return
        if d.get('isSidechain') or t not in ('assistant', 'user'):
            return
        msg = d.get('message') or {}
        content = msg.get('content')
        if t == 'assistant':
            self.last_asst_ts = ts
            if msg.get('model') and msg['model'] != '<synthetic>':
                self.model = msg['model']
            if msg.get('id') and msg.get('usage'):
                self.usage[msg['id']] = msg['usage']
            if msg.get('stop_reason'):
                self.last_stop_reason = msg['stop_reason']
            if d.get('isApiErrorMessage'):
                txt = ' '.join(b.get('text', '') for b in content or [] if isinstance(b, dict))
                self.api_error = {'ts': ts, 'text': clip(txt, 300)}
            elif isinstance(content, list):
                self.api_error = None
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'text' and b.get('text', '').strip():
                        text = b['text'].strip()
                        self.last_text = text
                        self.last_asst_text_ts = self.last_msg_ts = ts
                        self.turns.append({'role': 'assistant', 'ts': ts, 'text': text})
                    elif b.get('type') == 'tool_use':
                        self._tool_use(b, ts)
        elif t == 'user' and not d.get('isMeta'):
            if isinstance(content, str):
                self._user_text(content, ts)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'tool_result':
                        self._tool_result(b, d.get('toolUseResult'), ts)
                    elif b.get('type') == 'text':
                        self._user_text(b.get('text', ''), ts)

    def _user_text(self, text, ts):
        s = (text or '').strip()
        if s.startswith('[Request interrupted'):
            self.interrupted_ts = ts
            return
        if not s or s.startswith('<') or s.startswith('Caveat:'):
            return
        if any(x['role'] == 'user' and x['text'] == s for x in list(self.turns)[-6:]):
            return
        self.first_user = self.first_user or s
        self.turns.append({'role': 'user', 'ts': ts, 'text': s})
        self.last_user_ts = self.last_msg_ts = ts
        self.interrupted_ts = None

    def _add(self, tid, **kw):
        kw.update(id=tid, status='running', end=None, say=clip(self.last_text, 300))
        self.tasks[tid] = kw
        self.order.append(tid)
        self.watch.add(tid)

    def _tool_use(self, b, ts):
        name, inp, tid = b.get('name'), b.get('input') or {}, b.get('id')
        self.last_tool = {'ts': ts, 'text': tool_brief(name, inp)}
        self.n_tools += 1
        if name in ('Agent', 'Task'):
            self._add(tid, kind='agent', title=inp.get('description') or M('task.agent'),
                      subtype=inp.get('subagent_type') or 'general-purpose',
                      background=bool(inp.get('run_in_background')), start=ts,
                      prompt=clip(inp.get('prompt'), 3000))
        elif name == 'Bash':
            cmd = inp.get('command') or ''
            bg = bool(inp.get('run_in_background'))
            codex = mentions_codex(cmd, self.cwd)
            ccli = mentions_claude(cmd, self.cwd)
            if bg or codex or ccli:
                self._add(tid, kind='bash', title=inp.get('description') or clip(cmd, 60), command=cmd,
                          background=bg, codex=codex, claude=ccli, start=ts, cwd=self.cwd)
        elif name in FILE_TOOLS:
            p = inp.get('file_path') or inp.get('notebook_path')
            if p:
                self.files[p] = ts
        elif name == 'TodoWrite':
            self.todos = inp.get('todos') or []
        elif name == 'TaskCreate':
            self.pending_create[tid] = inp
            self.watch.add(tid)
        elif name == 'TaskUpdate':
            item = self.task_list.get(str(inp.get('taskId') or inp.get('id') or ''))
            if item is not None:
                for k in ('status', 'subject', 'activeForm'):
                    if inp.get(k):
                        item[k] = inp[k]
        elif name.endswith('__start_session') or name.endswith('__hand_off_to_session'):
            pid = inp.get('proposal_id')
            if pid and pid in self.proposals:       # starting an earlier proposal: same subtask
                self.starts[tid] = self.proposals[pid]
                self.watch.add(tid)
            else:
                self._add(tid, kind='session', title=inp.get('title') or inp.get('name') or M('task.new_session'),
                          background=True, start=ts, prompt=clip(inp.get('prompt') or inp.get('remaining_plan'), 3000),
                          handoff=name.endswith('hand_off_to_session'))
        elif name == 'Workflow':
            script = inp.get('script') or ''
            m = re.search(r"description:\s*(['\"`])(.+?)\1", script)
            self._add(tid, kind='workflow', title=(m.group(2) if m else None) or inp.get('name') or
                      os.path.basename(inp.get('scriptPath') or '') or 'Workflow', background=True, start=ts)
        elif name in ('AskUserQuestion', 'ExitPlanMode'):
            qs = inp.get('questions') or []
            text = join_clauses([q.get('question', '') for q in qs if isinstance(q, dict)]) if qs else M('ask.plan' if name == 'ExitPlanMode' else 'ask.question')
            self.asks[tid] = {'ts': ts, 'text': clip(text, 200) if qs else text}
            self.watch.add(tid)
        elif name in ('KillShell', 'KillBash', 'TaskStop'):
            target = inp.get('shell_id') or inp.get('bash_id') or inp.get('task_id')
            for task in self.tasks.values():
                if target and target in (task.get('bg_id'), task.get('agent_id')) and task['status'] == 'running':
                    task.update(status='stopped', end=ts, note=M('note.stopped_by_parent'))

    def _tool_result(self, b, tur, ts):
        tuid = b.get('tool_use_id')
        if tuid in self.asks:
            self.asks.pop(tuid)
            return
        if tuid in self.pending_create:
            inp = self.pending_create.pop(tuid)
            m = re.search(r'#(\d+)', ' '.join(iter_strings(b.get('content'))))
            key = m.group(1) if m else str(len(self.task_list) + 1)
            self.task_list[key] = {'id': key, 'subject': inp.get('subject') or inp.get('description'),
                                   'status': 'pending', 'activeForm': inp.get('activeForm')}
            return
        if tuid in self.starts:
            tuid = self.starts.pop(tuid)
        task = self.tasks.get(tuid)
        if not task:
            return
        tur = tur if isinstance(tur, dict) else {}
        err = bool(b.get('is_error'))
        text = ' '.join(iter_strings(b.get('content')))
        if task['kind'] == 'session':
            m = re.search(r'session_id: (local_[0-9a-f-]{36})', text)
            p = re.search(r'task_id: (task_[0-9a-z]+)', text)
            if m:
                task.update(local_id=m.group(1), started=ts, proposal_pending=False)
            elif p and not task.get('local_id'):
                task.update(proposal=p.group(1), proposal_pending=True)
                self.proposals[p.group(1)] = tuid
            elif not task.get('local_id'):
                task.update(status='failed' if err else 'stopped', end=ts, note=clip(text, 300))
            return
        if task['kind'] == 'workflow':
            if tur.get('status') == 'async_launched' or tur.get('taskId'):
                task.update(bg_id=tur.get('taskId'), wf_dir=tur.get('transcriptDir'), wf_name=tur.get('workflowName'))
                task['title'] = tur.get('summary') or task['title']
            else:
                task.update(status='failed' if err else 'completed', end=ts, note=clip(text, 300) if err else None)
            return
        if task['kind'] == 'bash':
            if tur.get('backgroundTaskId'):
                task['bg_id'] = tur['backgroundTaskId']
                m = re.search(r'written to: (\S+?\.output)', text)
                if m:
                    task['output_file'] = m.group(1)
            else:
                task.update(status='failed' if err else 'completed', end=ts)
                if err:
                    task['note'] = clip(text, 300)
                if task.get('codex') or task.get('claude'):
                    task['output'] = clip(text, 3000)
        else:
            task['agent_id'] = tur.get('agentId') or task.get('agent_id')
            if not (tur.get('isAsync') or tur.get('status') == 'async_launched'):
                task.update(status='failed' if err else 'completed', end=ts)

    def _notifications(self, d, ts):
        for s in iter_strings(d):
            for blk in NOTIF_RE.findall(s):
                tuid, tid = tag(blk, 'tool-use-id'), tag(blk, 'task-id')
                task = self.tasks.get(tuid)
                if task is None:
                    task = next((x for x in self.tasks.values() if tid and tid in (x.get('bg_id'), x.get('agent_id'))), None)
                if task is None:
                    continue
                status, summary = (tag(blk, 'status') or '').lower(), tag(blk, 'summary') or ''
                m = re.search(r'exit code (-?\d+)', summary)
                if status == 'completed' and m and m.group(1) != '0':
                    status = 'failed'
                elif status in ('killed', 'stopped', 'cancelled', 'canceled'):
                    status = 'stopped'
                elif status not in ('completed', 'failed'):
                    continue
                if task['status'] == 'running' or task.get('notif_ts') == ts:
                    task.update(status=status, end=ts, notif_ts=ts, note=summary)

    # Totals
    def usage_sum(self):
        out = {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0}
        for u in self.usage.values():
            out['input'] += u.get('input_tokens') or 0
            out['cache_read'] += u.get('cache_read_input_tokens') or 0
            out['cache_write'] += u.get('cache_creation_input_tokens') or 0
            out['output'] += u.get('output_tokens') or 0
        return out

    def turn_ended(self):
        a = parse_ts(self.last_asst_ts) or 0
        h = parse_ts(self.last_hook_stop_ts) or 0
        return h >= a or self.last_stop_reason in ('end_turn', 'stop_sequence')


# ---------- Claude subagents ----------

class ClaudeSubagent:
    def __init__(self):
        self.first_ts = self.last_ts = None
        self.tool_calls, self.last_tool, self.todos, self.last_text = 0, None, [], None
        self.events = collections.deque(maxlen=15)
        self.mtime = 0

    def want(self, raw):
        return b'"type":"assistant"' in raw

    def feed(self, d, raw):
        ts = d.get('timestamp')
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if d.get('type') != 'assistant':
            return
        for b in (d.get('message') or {}).get('content') or []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'tool_use':
                self.tool_calls += 1
                if b.get('name') == 'SubagentHandback':
                    continue
                self.last_tool = tool_brief(b.get('name'), b.get('input'))
                self.events.append({'ts': ts, 'kind': 'tool', 'text': self.last_tool})
                if b.get('name') == 'TodoWrite':
                    self.todos = (b.get('input') or {}).get('todos') or []
            elif b.get('type') == 'text' and b.get('text', '').strip():
                self.last_text = b['text'].strip()
                self.events.append({'ts': ts, 'kind': 'msg', 'text': clip(self.last_text, 400)})


# ---------- Codex ----------

PAYLOAD_TYPE_RE = re.compile(rb'"payload":\{"type":"([a-z_]+)"')
CODEX_SKIP = {b'function_call_output', b'reasoning', b'patch_apply_end', b'custom_tool_call_output', b'world_state',
              b'token_usage_record', b'tool_search_output', b'tool_search_call', b'web_search_call', b'web_search_end',
              b'exec_command_end', b'exec_command_begin', b'mcp_tool_call_end', b'thread_name_updated'}


def codex_want(raw):
    head = raw[:400]
    m = PAYLOAD_TYPE_RE.search(head)
    if not m:
        return b'"type":"session_meta"' in head or b'"type":"turn_context"' in head
    pt = m.group(1)
    if pt in CODEX_SKIP:
        return False
    if pt == b'item_completed':
        return b'"type":"UserMessage"' in raw[:700]
    if pt == b'message':
        return b'"role":"assistant"' in head
    return True


CALL_ID_RE = re.compile(rb'"call_id":"([^"]+)"')
EXIT_RE = re.compile(r'Process exited with code (-?\d+)')
RUNNING_RE = re.compile(r'Process running with session ID (\d+)')


class ClaudeCallsMixin:
    """Track Codex commands that invoke claude -p and their exit codes."""

    def _init_calls(self):
        self.claude_calls = collections.OrderedDict()
        self.watch = set()
        self.exec_sessions = {}     # Background process session ID -> original Claude call.
        self.poll_calls = {}        # Later write_stdin polling call -> original Claude call.

    def want(self, raw):
        m = PAYLOAD_TYPE_RE.search(raw[:400])
        if m and m.group(1) == b'function_call_output':
            c = CALL_ID_RE.search(raw[:500])
            return bool(c and c.group(1).decode() in self.watch)
        return codex_want(raw)

    def _maybe_claude_call(self, p, args, cmd, ts, say):
        cwd = args.get('workdir') or self.cwd
        if cmd and mentions_claude(cmd, cwd):
            cid = p.get('call_id') or p.get('id')
            self.claude_calls[cid] = {'id': cid, 'ts': ts, 'cmd': cmd, 'cwd': cwd, 'say': clip(say, 300),
                                      'status': 'running', 'end': None, 'exit': None}
            self.watch.add(cid)

    def _poll_call(self, p, args):
        """Codex polls long-running processes with write_stdin; the exit code appears in that output."""
        cid = self.exec_sessions.get(str(args.get('session_id')))
        if cid:
            self.poll_calls[p.get('call_id')] = cid
            self.watch.add(p.get('call_id'))

    def _call_output(self, p, ts):
        call_id = p.get('call_id')
        c = self.claude_calls.get(call_id) or self.claude_calls.get(self.poll_calls.get(call_id))
        if not c:
            return
        out = ' '.join(iter_strings(p.get('output')))
        m = EXIT_RE.search(out)
        if m:
            c.update(exit=int(m.group(1)), status='completed' if m.group(1) == '0' else 'failed', end=ts)
            return
        r = RUNNING_RE.search(out)
        if r:
            self.exec_sessions[r.group(1)] = c['id']


class CodexRollout(ClaudeCallsMixin):
    """A codex exec run or desktop child thread, represented as a subtask."""

    def __init__(self):
        self._init_calls()
        self.id = self.cwd = self.start = self.prompt = None
        self.source = None
        self.plan, self.plan_note = [], None
        self.n_cmd = self.n_patch = self.n_tool = 0
        self.last_cmd = self.last_msg = self.final = self.error = None
        self.tokens = self.out_tokens = 0
        self.complete = self.aborted = False
        self.end = self.last_ts = None
        self.events = collections.deque(maxlen=20)
        self.files = collections.OrderedDict()
        self.mtime = 0

    def _ev(self, ts, kind, text):
        self.events.append({'ts': ts, 'kind': kind, 'text': text})

    def feed(self, d, raw):
        ts, t, p = d.get('timestamp'), d.get('type'), d.get('payload') or {}
        pt = p.get('type')
        if ts:
            self.last_ts = ts
        if t == 'session_meta':
            self.id, self.cwd, self.start = p.get('id'), p.get('cwd'), p.get('timestamp') or ts
            self.source = p.get('source')
            return
        if pt == 'user_message':
            if self.prompt is None:
                self.prompt = p.get('message') or ''
        elif pt == 'item_completed':
            item = p.get('item') or {}
            if item.get('type') == 'UserMessage' and self.prompt is None:
                self.prompt = ' '.join(c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict))
        elif pt == 'function_call':
            name, args = p.get('name'), load_json(p.get('arguments'))
            if name == 'update_plan':
                self.plan, self.plan_note = args.get('plan') or [], args.get('explanation')
                done = sum(1 for s in self.plan if s.get('status') == 'completed')
                self._ev(ts, 'plan', M('event.plan_note', done, len(self.plan), self.plan_note) if self.plan_note else M('event.plan', done, len(self.plan)))
            elif name in ('exec_command', 'shell', 'local_shell'):
                cmd = args.get('cmd') or args.get('command') or ''
                if isinstance(cmd, list):
                    cmd = ' '.join(cmd)
                self.n_cmd += 1
                self.last_cmd = clip(cmd, 300)
                self._ev(ts, 'cmd', self.last_cmd)
                self._maybe_claude_call(p, args, cmd, ts, self.last_msg)
            elif name == 'write_stdin':
                self._poll_call(p, args)
            else:
                self.n_tool += 1
                self._ev(ts, 'tool', name)
        elif pt == 'function_call_output':
            self._call_output(p, ts)
        elif pt == 'custom_tool_call':
            if p.get('name') == 'apply_patch':
                self.n_patch += 1
                files = re.findall(r'\*\*\* (?:Add|Update|Delete) File: (\S+)', p.get('input') or '')
                for f in files:
                    self.files[f] = ts
                self._ev(ts, 'patch', M('event.patch', ', '.join(os.path.basename(f) for f in files[:4])))
            else:
                self.n_tool += 1
        elif pt == 'agent_message':
            self.last_msg = p.get('message') or ''
            self._ev(ts, 'msg', clip(self.last_msg, 500))
        elif pt == 'message' and p.get('role') == 'assistant':
            txt = ' '.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict))
            if txt.strip():
                self.last_msg = txt
        elif pt == 'token_count':
            tu = ((p.get('info') or {}).get('total_token_usage')) or {}
            self.tokens = tu.get('total_tokens', self.tokens)
            self.out_tokens = tu.get('output_tokens', self.out_tokens)
        elif pt == 'task_started':
            self.complete = self.aborted = False
            self.end = None
        elif pt == 'task_complete':
            self.complete, self.end = True, ts
            self.final = p.get('last_agent_message') or self.last_msg
        elif pt == 'turn_aborted':
            self.aborted, self.end = True, ts
            self._ev(ts, 'error', M('event.aborted'))
        elif pt in ('error', 'stream_error'):
            self.error = clip(p.get('message'), 300)
            self._ev(ts, 'error', self.error)


class CodexThread(ClaudeCallsMixin):
    """A top-level Codex desktop / CLI session."""

    def __init__(self):
        self._init_calls()
        self.id = self.cwd = self.start = self.branch = self.model = self.originator = None
        self.first_ts = self.last_ts = None
        self.in_turn = False
        self.turn_start = None
        self.turn_n = 0
        self.last_event = None
        self.pending = None            # Command approval or question awaiting the user.
        self.turns = collections.deque(maxlen=80)
        self.first_user = None
        self.last_msg_ts = self.last_asst_text_ts = self.last_user_ts = None
        self.plan, self.plan_hist = [], []
        self.n_cmd = 0
        self.last_cmd = None
        self.files = collections.OrderedDict()
        self.tokens = self.out_tokens = 0
        self.mtime = 0

    def _asst(self, text, ts):
        text = (text or '').strip()
        if not text or any(x['role'] == 'assistant' and x['text'] == text for x in list(self.turns)[-4:]):
            return
        self.turns.append({'role': 'assistant', 'ts': ts, 'text': text})
        self.last_asst_text_ts = self.last_msg_ts = ts

    def feed(self, d, raw):
        ts, t, p = d.get('timestamp'), d.get('type'), d.get('payload') or {}
        pt = p.get('type')
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if t == 'session_meta':
            self.id, self.cwd, self.start = p.get('id'), p.get('cwd'), p.get('timestamp') or ts
            self.originator = p.get('originator')
            self.branch = (p.get('git') or {}).get('branch')
            return
        if t == 'turn_context':
            self.model = p.get('model') or self.model
            return
        if pt == 'thread_settings_applied':
            self.model = (p.get('thread_settings') or {}).get('model') or self.model
        elif pt == 'task_started':
            self.in_turn, self.turn_start, self.last_event = True, ts, 'started'
            self.turn_n += 1
            self.plan, self.plan_hist = [], []
            self.pending = None
        elif pt == 'task_complete':
            self.in_turn, self.last_event = False, 'complete'
            self._asst(p.get('last_agent_message'), ts)
            self.pending = None
        elif pt == 'turn_aborted':
            self.in_turn, self.last_event = False, 'aborted'
            self.pending = None
        elif pt == 'item_completed':
            item = p.get('item') or {}
            if item.get('type') == 'UserMessage':
                txt = ' '.join(c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict)).strip()
                if txt and not txt.startswith('<'):
                    self.first_user = self.first_user or txt
                    self.turns.append({'role': 'user', 'ts': ts, 'text': txt})
                    self.last_user_ts = self.last_msg_ts = ts
        elif pt == 'user_message':
            txt = (p.get('message') or '').strip()
            if txt and not txt.startswith('<') and not any(x['role'] == 'user' and x['text'] == txt for x in list(self.turns)[-3:]):
                self.first_user = self.first_user or txt
                self.turns.append({'role': 'user', 'ts': ts, 'text': txt})
                self.last_user_ts = self.last_msg_ts = ts
        elif pt == 'message' and p.get('role') == 'assistant':
            self._asst(' '.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict)), ts)
            self.pending = None
        elif pt == 'agent_message':
            self._asst(p.get('message'), ts)
        elif pt == 'function_call':
            name, args = p.get('name'), load_json(p.get('arguments'))
            self.pending = None
            if name == 'update_plan':
                self.plan = args.get('plan') or []
                self.plan_hist.append((ts, self.plan))
            elif name in ('exec_command', 'shell', 'local_shell'):
                cmd = args.get('cmd') or args.get('command') or ''
                cmd = ' '.join(cmd) if isinstance(cmd, list) else cmd
                self.n_cmd += 1
                self.last_cmd = clip(cmd, 300)
                say = next((t['text'] for t in reversed(self.turns) if t['role'] == 'assistant'), '')
                self._maybe_claude_call(p, args, cmd, ts, say)
            elif name == 'write_stdin':
                self._poll_call(p, args)
        elif pt == 'function_call_output':
            self._call_output(p, ts)
        elif pt == 'custom_tool_call' and p.get('name') == 'apply_patch':
            for f in re.findall(r'\*\*\* (?:Add|Update|Delete) File: (\S+)', p.get('input') or ''):
                self.files[f if f.startswith('/') else os.path.join(self.cwd or '', f)] = ts
        elif pt == 'token_count':
            tu = ((p.get('info') or {}).get('total_token_usage')) or {}
            self.tokens = tu.get('total_tokens', self.tokens)
            self.out_tokens = tu.get('output_tokens', self.out_tokens)
        elif pt and (pt.endswith('_approval_request') or pt in ('request_user_input', 'elicitation_request')):
            what = p.get('command') or p.get('reason') or p.get('message') or pt
            if isinstance(what, list):
                what = ' '.join(what)
            self.pending = {'ts': ts, 'text': M('ask.approve', clip(str(what), 120))}

    def plan_steps(self):
        """Start and end of each latest-turn plan step, inferred from successive update_plan calls."""
        if not self.plan_hist:
            return []
        first_seen, started, done = {}, {}, {}
        for ts, plan in self.plan_hist:
            for s in plan:
                name = s.get('step') or ''
                first_seen.setdefault(name, ts)
                if s.get('status') == 'in_progress':
                    started.setdefault(name, ts)
                elif s.get('status') == 'completed':
                    done.setdefault(name, ts)
        out, prev_end = [], parse_ts(self.turn_start)
        for s in self.plan_hist[-1][1]:
            name = s.get('step') or ''
            st = parse_ts(started.get(name)) or (prev_end if name in done else None)
            en = parse_ts(done.get(name))
            out.append({'step': name, 'status': s.get('status'), 'start': st, 'end': en})
            if en:
                prev_end = en
        return out


# ---------- Desktop new-session cards ----------

CCD_DIR = os.path.join(HOME, 'Library', 'Application Support', 'Claude', 'claude-code-sessions')
CLI_ID_RE = re.compile(rb'"cliSessionId":"([0-9a-f-]+)"')
CARDS_KEY = b'"backgroundTaskSuggestions":'
SPAWN_KEY = b'"spawnedFrom":'
_ccd_cache = {}


def ccd_cards(since):
    """Read local desktop session records and return:
    - Pending new-session cards: Claude session ID -> cards (removed after clicking Start, verified).
    - Desktop IDs: Claude session ID -> local_xxx filename, for claude://claude.ai/epitaxy/local_xxx links.
    - Sessions started from a card: proposal task_id -> local_xxx of the new session (its spawnedFrom). When you
      click Start on the card yourself, the proposing session's log has no start result; this is the only link.
    Each file is about 600 KB; decode only the sections needed and reread only modified files."""
    out, local_ids, spawned, now_ms, dec = {}, {}, {}, time.time() * 1000, json.JSONDecoder()
    seen = set()
    for p in glob.glob(os.path.join(CCD_DIR, '*', '*', 'local_*.json')):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt < since:
            continue
        seen.add(p)
        c = _ccd_cache.get(p)
        if not c or c[0] != mt:
            try:
                with open(p, 'rb') as f:
                    raw = f.read()
            except OSError:
                continue
            m = CLI_ID_RE.search(raw)

            def section(key):
                i = raw.find(key)
                if i < 0:
                    return None
                try:
                    return dec.raw_decode(raw[i + len(key):].decode('utf-8', 'replace').lstrip())[0]
                except ValueError:
                    return None
            cards, sp = section(CARDS_KEY), section(SPAWN_KEY)
            c = _ccd_cache[p] = (mt, m.group(1).decode() if m else None, cards if isinstance(cards, list) else [],
                                 sp.get('taskId') if isinstance(sp, dict) else None)
        if c[1]:
            local_ids[c[1]] = os.path.basename(p)[:-5]
        if c[3]:
            spawned[c[3]] = os.path.basename(p)[:-5]
        live = [x for x in c[2] if isinstance(x, dict) and x.get('title') and float(x.get('expiresAt') or 0) > now_ms]
        if c[1] and live:
            out[c[1]] = live
    for p in [p for p in _ccd_cache if p not in seen]:      # out of range or deleted
        del _ccd_cache[p]
    return out, local_ids, spawned


# ---------- Session processes and Codex titles ----------

def live_claude_sessions():
    out = {}
    for f in glob.glob(os.path.join(CLAUDE_SESSIONS, '*.json')):
        try:
            with open(f, encoding='utf-8') as fh:
                d = json.load(fh)
            pid, sid = int(d['pid']), d['sessionId']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:     # alive, but run by another user
            pass
        except OSError:
            continue
        out[sid] = d
    return out


_index_cache = {'mtime': 0, 'names': {}}


def codex_thread_names():
    try:
        mt = os.path.getmtime(CODEX_INDEX)
    except OSError:
        return {}
    if mt != _index_cache['mtime']:
        names = {}
        with open(CODEX_INDEX, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get('id') and d.get('thread_name'):
                    names[d['id']] = d['thread_name']
        _index_cache.update(mtime=mt, names=names)
    return _index_cache['names']


_codex_meta = {}


def prune_caches(codex_paths):
    """Forget facts about Codex logs that are no longer scanned; cap the small per-command caches."""
    for p in [p for p in _codex_meta if p not in codex_paths]:
        del _codex_meta[p]
    for cache in (_snip_cache, _script_cache):
        if len(cache) > 5000:
            cache.clear()


def codex_meta(path):
    """Read initial lines for session ID, source (desktop / exec / child), start time, cwd and first prompt."""
    m = _codex_meta.get(path)
    if m and m.get('done'):
        return m
    m = {'path': path, 'id': None, 'start': None, 'cwd': None, 'prompt': None, 'originator': None,
         'kind': None, 'parent': None, 'nickname': None, 'done': False}
    try:
        with open(path, 'rb') as f:
            for i, raw in enumerate(f):
                if i == 0:
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        break
                    p = d.get('payload') or {}
                    if d.get('type') != 'session_meta':
                        break
                    src = p.get('source')
                    m.update(id=p.get('id'), start=parse_ts(p.get('timestamp') or d.get('timestamp')),
                             cwd=p.get('cwd'), originator=p.get('originator'))
                    if src == 'exec':
                        m['kind'] = 'exec'
                    elif isinstance(src, dict):
                        sub = src.get('subagent')
                        spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
                        if spawn:
                            m.update(kind='spawn', parent=spawn.get('parent_thread_id'), nickname=spawn.get('agent_nickname'))
                        else:
                            m['kind'] = 'skip'          # Approval guardian threads and similar helpers.
                    else:
                        m['kind'] = 'thread'
                    if m['kind'] not in ('exec', 'spawn'):
                        m['done'] = True
                        break
                    continue
                if i > 120:
                    m['done'] = True
                    break
                if b'"user_message"' not in raw[:300] and b'"UserMessage"' not in raw[:700]:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                p = d.get('payload') or {}
                if p.get('type') == 'user_message':
                    m['prompt'] = p.get('message') or ''
                    m['done'] = True
                    break
                elif p.get('type') == 'item_completed':
                    item = p.get('item') or {}
                    if item.get('type') == 'UserMessage':
                        m['prompt'] = ' '.join(c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict))
                        m['done'] = True
                        break
    except OSError:
        pass
    _codex_meta[path] = m
    return m


def link_codex(sess, exec_metas, now):
    """Match Claude's Codex commands to exec logs: prompt content first, then nearest start time."""
    cands = [sess.tasks[i] for i in sess.order if sess.tasks[i]['kind'] == 'bash']
    if not cands:
        return {}
    first = min(parse_ts(t['start']) or now for t in cands)
    rollouts = [m for m in exec_metas if m.get('prompt') is not None and m['start'] and m['start'] >= first - 60]
    links, used = {}, set()

    def window(t, m):
        s = parse_ts(t['start']) or 0
        e = parse_ts(t['end']) if t.get('end') else now
        return s - 10 <= m['start'] <= e + 5

    for t in cands:
        snips = prompt_snippets(t['command'], t.get('cwd'))
        cmd_n = norm(t['command'])
        for m in rollouts:
            if m['path'] in used or not window(t, m):
                continue
            pn = norm(m['prompt'][:20000])
            if any(s in pn for s in snips) or (len(pn) >= 30 and pn[:80] in cmd_n):
                links[t['id']] = m['path']
                used.add(m['path'])
                break
    for t in cands:
        if t['id'] in links or not t.get('codex'):
            continue
        s = parse_ts(t['start']) or 0
        dirs = [os.path.normpath(os.path.join(t.get('cwd') or '', _expand(d, t['command'])))
                for d in re.findall(r'(?:\bcd|\s-C|\s--cd)\s+([^\s;&|)]+)', _unquote(t['command']))]
        pool = [m for m in rollouts if m['path'] not in used and window(t, m)
                and (not sess.root or not m.get('cwd') or m['cwd'].startswith(sess.root) or sess.root.startswith(m['cwd'])
                     or any(m['cwd'].startswith(d) for d in dirs) or abs(m['start'] - s) <= 15)]
        if pool:
            m = min(pool, key=lambda m: abs(m['start'] - s))
            links[t['id']] = m['path']
            used.add(m['path'])
    return links


def link_claude_cli(calls, cands):
    """Match claude -p calls to sessions: prompt content first, then nearest start (same cwd, within a minute)."""
    links, used = {}, set()
    cands = sorted(cands, key=lambda m: m['start'] or 0)
    for c in sorted(calls, key=lambda c: parse_ts(c['ts']) or 0):
        t = parse_ts(c['ts']) or 0
        snips = prompt_snippets(c['cmd'], c.get('cwd'))
        cmd_n = norm(c['cmd'])
        for m in cands:
            if m['key'] in used or not m['start'] or not (t - 10 <= m['start'] <= t + 600):
                continue
            fu = norm(m.get('first_user'))
            if (len(fu) >= 4 and fu[:80] in cmd_n) or any(s in fu for s in snips):
                links[c['id']] = m['key']
                used.add(m['key'])
                break
    for c in calls:
        if c['id'] in links:
            continue
        t = parse_ts(c['ts']) or 0
        pool = [m for m in cands if m['key'] not in used and m.get('cli') and m['start'] and abs(m['start'] - t) <= 60
                and (not c.get('cwd') or not m.get('cwd') or m['cwd'] == c['cwd'])]
        if pool:
            m = min(pool, key=lambda m: abs(m['start'] - t))
            links[c['id']] = m['key']
            used.add(m['key'])
    return links


def cli_child_status(child, now):
    last = parse_ts(child.last_ts) or now
    if child.api_error:
        return 'fail', M('note.error', child.api_error.get('text') or M('error.api'))
    if child.last_asst_ts and child.turn_ended():
        return 'ok', None
    if now - last < TURN_DEAD_SECONDS:
        return 'run', None
    return 'fail', M('note.incomplete')


def cli_task(call, child, now, full=True, executor=None):
    """Represent a claude -p call as a subtask: use its session state if available, otherwise its command state."""
    start = parse_ts(call['ts'])
    title = call.get('title') or (child is not None and plain_line(child.first_user, 50)) or clip(call['cmd'].replace('\n', ' '), 50)
    out = {'id': call['id'], 'title': short_title(title), 'status': status_key(call.get('status') or 'running'),
           'start': start, 'end': parse_ts(call.get('end')), 'note': call.get('note'), 'progress': None,
           'last_activity': start, 'say': call.get('say')}
    if child is not None:
        st, note = cli_child_status(child, now)
        out.update(status=st, note=note or out['note'], start=parse_ts(child.first_ts) or start,
                   last_activity=parse_ts(child.last_ts) or start,
                   end=parse_ts(child.last_asst_ts) if st != 'run' else None)
        out['progress'] = todo_progress(child.todos)
    if full:
        out['executor'] = executor if executor is not None else M('exec.claude_cli')
        if child is None:
            out['executor'] = M('exec.no_session', out['executor'])
        out['command'] = call['cmd']
        out['stats'], out['events'], out['files'] = {}, [], []
        if child is not None:
            u = child.usage_sum()
            out['stats'] = {'tool_calls': child.n_tools, 'tokens': u['input'] + u['cache_read'] + u['cache_write'] + u['output']}
            if child.last_text:
                out['result'] = clip(child.last_text, 3000)
            out['files'] = list(child.files.keys())[:12]
            out['current'] = (child.last_tool or {}).get('text')
            out['events'] = [{'ts': x['ts'], 'kind': 'msg' if x['role'] == 'assistant' else 'ask', 'text': clip(x['text'], 300)}
                             for x in list(child.turns)[-8:]]
            out['child_id'] = child.sid
            if child.model:
                out['executor'] = M('exec.model', out['executor'], child.model)
    if out['status'] == 'run':
        idle = now - (out['last_activity'] or now)
        out['stale'] = idle > STALE_SECONDS
        out['idle_seconds'] = idle
    return out


# ---------- Subtasks ----------

AGENT_LABEL = {'Explore': M('agent.explore'), 'general-purpose': M('agent.general'), 'Plan': M('agent.plan')}


def short_title(t):
    if isinstance(t, dict):
        return t
    t = re.sub(r'^后台运行\s*', '', t or '')
    return re.sub(r'[（(]Codex[)）]\s*$', '', t).strip()


def todo_progress(todos):
    if not todos:
        return None
    done = sum(1 for t in todos if t.get('status') == 'completed')
    cur = next((t.get('activeForm') or t.get('content') for t in todos if t.get('status') == 'in_progress'), None)
    return {'done': done, 'total': len(todos), 'current': cur,
            'steps': [{'step': t.get('content') or t.get('subject'), 'status': t.get('status')} for t in todos]}


def extract_paths(text):
    out = []
    home = re.escape(HOME.rstrip('/'))
    for p in re.findall(r'\]\((/[^)\s]+)\)', text or '') + re.findall(r'(?<![\w(])(%s/[^\s`)\]，。、]+)' % home, text or ''):
        p = p.split('#')[0].rstrip('.,;:')
        if p not in out and os.path.isfile(p):
            out.append(p)
    return out[:8]


def status_key(s):
    return {'completed': 'ok', 'running': 'run', 'failed': 'fail', 'stopped': 'stop'}.get(s, s)


SESSION_ST = {'run': 'run', 'wait': 'wait', 'ok': 'ok', 'fail': 'fail'}


def session_task(t, child, cards, now, full, started_by_card=False):
    """A new session this one started: once started, it takes that session's own status. A proposal not started
    yet waits for you while its card is still there. started_by_card: you clicked Start on the card yourself."""
    out = {'id': t['id'], 'title': t['title'], 'start': parse_ts(t.get('started') or t['start']), 'end': None,
           'progress': None, 'last_activity': parse_ts(t['start']), 'note': t.get('note'), 'child_key': None}
    if t.get('local_id') or started_by_card:
        if child:
            if started_by_card and not t.get('local_id'):
                out['start'] = child.get('start_ts') or out['start']
            out.update(status=SESSION_ST.get(child['status'], 'run'), child_key=child['key'],
                       last_activity=child.get('last_ts') or out['start'], note=child.get('note'))
            if out['status'] != 'run':
                out['end'] = child.get('msg_ts') or child.get('last_ts')
            if child['status'] == 'wait' and child.get('items'):
                out['note'] = M('note.child_waiting', child['items'][0]['text'])
        else:
            out.update(status='ok', note=M('note.child_missing'))
    elif t.get('proposal_pending') and t.get('proposal') in cards:
        out.update(status='wait', note=M('note.proposal_pending'))
    elif t.get('proposal_pending'):
        out.update(status='stop', note=M('note.proposal_dropped'), end=out['start'])
    else:
        out.update(status=status_key(t['status']), end=parse_ts(t.get('end')))
    if full:
        out.update(executor=M('exec.handoff' if t.get('handoff') else 'exec.new_session'), prompt=t.get('prompt'),
                   say=t.get('say'), stats={}, events=[], files=[])
    if out['status'] == 'run':
        out['idle_seconds'] = now - (out['last_activity'] or now)
        out['stale'] = False
    return out


def workflow_agents(d):
    """Number of agents in a workflow's transcript folder, and when any of them last wrote."""
    n, last = 0, None
    try:
        for e in os.scandir(d):
            if e.name.startswith('agent-') and e.name.endswith('.jsonl'):
                n += 1
                last = max(last or 0, e.stat().st_mtime)
    except (OSError, TypeError):
        pass
    return n, last


def claude_task(t, sdir, links, rollout_state, now, full=True, cli_child=None, session_child=None, cards=(), spawned=None):
    """Subtask state; full=False calculates only status for the list page."""
    if t['kind'] == 'session':
        local = t.get('local_id') or (spawned or {}).get(t.get('proposal'))
        return session_task(t, session_child(local) if session_child and local else None, cards, now, full,
                            started_by_card=bool(local and not t.get('local_id')))
    start = parse_ts(t['start'])
    rpath = links.get(t['id']) if t['kind'] == 'bash' else None
    if t['kind'] == 'bash' and t.get('claude') and not rpath:
        call = {'id': t['id'], 'ts': t['start'], 'cmd': t['command'], 'cwd': t.get('cwd'), 'say': t.get('say'),
                'title': t['title'], 'status': t['status'], 'end': t['end'], 'note': t.get('note')}
        return cli_task(call, cli_child(t['id']) if cli_child else None, now, full)
    out = {'id': t['id'], 'title': short_title(t['title']), 'status': t['status'], 'start': start,
           'end': parse_ts(t['end']), 'note': t.get('note'), 'progress': None, 'last_activity': start}
    r = rollout_state(rpath) if rpath else None
    if r is not None:
        out['start'] = parse_ts(r.start) or start
        out['last_activity'] = parse_ts(r.last_ts) or start
        if r.complete:
            out['status'], out['end'] = 'completed', parse_ts(r.end)
        elif r.aborted:
            out['status'], out['end'] = 'failed', parse_ts(r.end)
            out['note'] = r.error or M('note.codex_aborted')
        if r.plan:
            done = sum(1 for s in r.plan if s.get('status') == 'completed')
            cur = next((s.get('step') for s in r.plan if s.get('status') == 'in_progress'), None)
            out['progress'] = {'done': len(r.plan) if out['status'] == 'completed' else done, 'total': len(r.plan),
                               'current': cur, 'note': r.plan_note, 'steps': r.plan}
    out['status'] = status_key(out['status'])
    if not full:
        return out
    out['say'] = t.get('say')
    out['stats'], out['events'], out['files'] = {}, [], []
    if t['kind'] == 'agent':
        out['executor'] = M('exec.subagent', AGENT_LABEL.get(t.get('subtype'), t.get('subtype') or ''))
        aid = t.get('agent_id')
        path = os.path.join(sdir, 'subagents', 'agent-%s.jsonl' % aid) if aid else None
        if path and os.path.exists(path):
            a = _tail(path, ClaudeSubagent)
            out['last_activity'] = parse_ts(a.last_ts) or start
            out['current'] = a.last_tool
            out['stats'] = {'tool_calls': a.tool_calls}
            out['events'] = list(a.events)
            out['progress'] = todo_progress(a.todos)
            if out['status'] != 'run' and a.last_text:
                out['result'] = clip(a.last_text, 3000)
        out['prompt'] = t.get('prompt')
    elif t['kind'] == 'workflow':
        n, last = workflow_agents(t.get('wf_dir'))
        out['executor'] = M('exec.workflow_agents', n) if n else M('exec.workflow')
        out['last_activity'] = max(start or 0, last or 0) or start
    elif r is not None:
        out['executor'] = M('exec.codex_steps', len(r.plan)) if r.plan else M('exec.codex')
        out['current'] = (out['progress'] or {}).get('current') or r.last_cmd
        out['last_cmd'] = r.last_cmd
        out['stats'] = {'commands': r.n_cmd, 'patches': r.n_patch, 'tokens': r.tokens}
        out['events'] = list(r.events)
        if r.final or r.last_msg:
            out['result'] = clip(r.final or r.last_msg, 3000)
        out['files'] = extract_paths((r.final or '') + '\n' + (r.last_msg or ''))
        out['command'] = t.get('command')
    elif t.get('codex'):
        out['executor'] = M('exec.codex_ephemeral' if '--ephemeral' in (t.get('command') or '') else 'exec.codex_no_log')
        out['command'] = t.get('command')
        if t.get('output'):
            out['result'] = t['output']
    else:
        out['executor'] = M('exec.background')
        out['command'] = t.get('command')
        ofile = t.get('output_file')
        if ofile and os.path.exists(ofile):
            out['events'] = [{'ts': None, 'kind': 'out', 'text': ln} for ln in tail_text(ofile)]
            out['last_activity'] = max(start or 0, os.path.getmtime(ofile))
            out['files'] = [ofile]
    if out['status'] == 'run':
        idle = now - (out['last_activity'] or now)
        out['stale'] = idle > STALE_SECONDS
        out['idle_seconds'] = idle
    return out


def batches(tasks):
    """Group by start time: consecutive subtasks within BATCH_GAP_SECONDS belong to the same batch."""
    out, cur, prev = [], None, None
    for t in sorted(tasks, key=lambda x: x['start'] or 0):
        if cur is None or prev is None or (t['start'] or 0) - prev > BATCH_GAP_SECONDS:
            cur = {'idx': len(out), 'at': t['start'], 'say': t.get('say') or '', 'tasks': []}
            out.append(cur)
        cur['tasks'].append(t['id'])
        t['batch'] = cur['idx']
        prev = t['start'] or prev
    return out


# Subagent log readers share the incremental cache injected by Monitor.
_tail = None


def set_tail_func(fn):
    global _tail
    _tail = fn
