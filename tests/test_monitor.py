import atexit
import contextlib
import glob
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from claude_codex_monitor import collect as C
from claude_codex_monitor import demo
from claude_codex_monitor import server as S
from claude_codex_monitor.config import parse_args


# Everything these tests create goes into one folder, removed when they finish
tempfile.tempdir = tempfile.mkdtemp(prefix='claude-codex-monitor-tests-')
atexit.register(shutil.rmtree, tempfile.tempdir, True)


def flat(v):
    """Message codes as text, so tests can look inside them."""
    if isinstance(v, dict) and 'm' in v:
        return '%s(%s)' % (v['m'], ', '.join(flat(a) for a in v['a']))
    return str(v)


class DemoScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.now = time.time()
        cd, xd = demo.build(os.path.join(cls.root, 'data'), cls.now)
        cls.args = ['--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(cls.root, 'state'), '--no-desktop',
                    '--summary', 'off']
        cfg = parse_args(cls.args)
        C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
        cls.mon = S.Monitor(cfg)
        cls.mon.scan()
        cls.rows = {r['title']: r for r in cls.mon.snapshot['rows']}

    def test_statuses(self):
        want = {'Migrate CI to GitHub Actions': 'run', 'Dark mode toggle': 'run', 'Refactor billing webhooks': 'wait',
                'Clean up stale feature flags': 'wait', 'Release notes v2.4': 'wait', 'Pick a date library': 'wait',
                'Fix flaky timezone test': 'ok', 'Why is search slow?': 'ok', 'Upgrade to Node 22': 'fail',
                'Tidy ESLint config': 'ok', 'Update README badges': 'ok'}
        self.assertEqual({t: r['status'] for t, r in self.rows.items()}, want)

    def test_waiting_items(self):
        self.assertEqual(len(self.rows['Refactor billing webhooks']['items']), 2)
        self.assertIn('git push', flat(self.rows['Clean up stale feature flags']['items'][0]['text']))
        self.assertIn('Which library', self.rows['Pick a date library']['items'][0]['text'])
        self.assertEqual(self.mon.snapshot['summary'], {'state': 'none'})

    def test_subtasks_in_one_batch(self):
        d = self.mon.detail(self.rows['Migrate CI to GitHub Actions']['key'])
        got = sorted((t['title'], t['status'], t['batch']) for t in d['tasks'])
        self.assertEqual(got, [('Port the release workflow', 'run', 0), ('Run the e2e suite as a baseline', 'run', 0),
                               ('Survey Jenkins jobs', 'ok', 0)])
        codex = next(t for t in d['tasks'] if t['title'] == 'Port the release workflow')
        self.assertEqual(flat(codex['executor']), 'exec.codex_steps(4)')
        self.assertEqual((codex['progress']['done'], codex['progress']['total']), (2, 4))
        self.assertEqual((d['todos']['done'], d['todos']['total']), (1, 4))

    def test_workflow_subtask(self):
        d = self.mon.detail(self.rows['Refactor billing webhooks']['key'])
        wf = d['tasks'][0]
        self.assertEqual((wf['title'], wf['status'], flat(wf['executor'])),
                         ('Review each handler for missing idempotency checks', 'ok', 'exec.workflow_agents(4)'))

    def test_demo_mode_reads_only_made_up_sessions(self):
        cfg = parse_args(['--demo', '--summary', 'claude-cli'])
        S.use_demo(cfg)
        self.assertTrue(cfg.claude_dir.startswith(tempfile.gettempdir()))
        self.assertEqual((cfg.summary, cfg.desktop), ('off', False))
        with open(os.path.join(cfg.state_dir, 'dismissed.json')) as f:
            self.assertEqual(sorted(json.load(f)), sorted(demo.DISMISSED))

    def test_codex_plan_and_children(self):
        d = self.mon.detail(self.rows['Dark mode toggle']['key'])
        self.assertEqual([t['status'] for t in d['tasks']], ['ok', 'ok', 'run', 'pending'])
        d = self.mon.detail(self.rows['Why is search slow?']['key'])
        self.assertEqual(sorted(t['status'] for t in d['tasks']), ['ok', 'ok'])

    def test_dismissed_session_comes_back_on_new_message(self):
        key = self.rows['Fix flaky timezone test']['key']
        self.mon.set_dismissed([key], True)
        self.mon.scan()
        row = next(r for r in self.mon.snapshot['rows'] if r['key'] == key)
        self.assertEqual((row['dismissed'], row['reappeared']), (True, False))
        path = self.mon.paths[key]
        line = {'type': 'user', 'timestamp': demo._iso(time.time()), 'sessionId': key[7:],
                'message': {'role': 'user', 'content': 'One more thing: also pin the TZ in CI.'}}
        with open(path, 'a') as f:
            f.write(json.dumps(line, separators=(',', ':')) + '\n')
        self.mon.scan()
        row = next(r for r in self.mon.snapshot['rows'] if r['key'] == key)
        self.assertEqual((row['dismissed'], row['reappeared']), (False, True))


class NoSessions(unittest.TestCase):
    def test_says_where_it_looked(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, '.claude', 'projects'))
        with mock.patch.dict(os.environ, {'HOME': home}):
            cfg = parse_args(['--claude-dir', os.path.join(home, '.claude'), '--codex-dir', os.path.join(home, '.codex'),
                              '--state-dir', os.path.join(home, 'state'), '--no-desktop', '--summary', 'off'])
            C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
            mon = S.Monitor(cfg)
            mon.scan()
        snap = mon.snapshot
        self.assertEqual(snap['rows'], [])
        claude, codex = snap['sources']
        self.assertEqual((claude['path'], claude['found'], claude['logs']), ('~/.claude/projects/', True, 0))
        self.assertEqual((codex['path'], codex['found'], codex['logs']), ('~/.codex/sessions/', False, 0))


class Startup(unittest.TestCase):
    def test_summary_defaults_to_auto(self):
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != 'CCM_SUMMARY'}, clear=True):
            self.assertEqual(parse_args([]).summary, 'auto')

    def test_bad_environment_values(self):
        for name, value in (('CCM_DAYS', 'abc'), ('CCM_PORT', '87x'), ('CCM_LANG', 'zh-CN')):
            with mock.patch.dict(os.environ, {name: value}), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    parse_args([])
            self.assertIn(name, err.getvalue())                     # one line naming the variable, not a traceback

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parse_args(['--version'])
        self.assertRegex(out.getvalue(), r'^claude-codex-monitor \d+\.\d+\.\d+')

    def test_no_open(self):
        self.assertTrue(parse_args([]).open)
        self.assertFalse(parse_args(['--no-open']).open)
        with mock.patch.dict(os.environ, {'CCM_NO_OPEN': '1'}):
            self.assertFalse(parse_args([]).open)

    def test_no_browser_over_ssh(self):
        with mock.patch.dict(os.environ, {'SSH_CONNECTION': '203.0.113.5 50000 192.0.2.10 22'}), \
                mock.patch('webbrowser.open') as opened:
            self.assertFalse(S.open_page('http://127.0.0.1:8767/'))
        opened.assert_not_called()

    def test_opens_the_page(self):
        env = {k: v for k, v in os.environ.items() if k != 'SSH_CONNECTION'}
        env['DISPLAY'] = ':0'
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('webbrowser.open', return_value=True) as opened:
            self.assertTrue(S.open_page('http://127.0.0.1:8767/'))
        opened.assert_called_once_with('http://127.0.0.1:8767/')


class Consent(unittest.TestCase):
    def make(self, model):
        root = tempfile.mkdtemp() if not hasattr(self, 'root') else self.root
        self.root = root
        os.environ['CCM_API_KEY'] = 'k'
        cfg = parse_args(['--summary', 'openai', '--base-url', 'http://127.0.0.1:9/v1', '--model', model,
                          '--state-dir', root, '--claude-dir', root, '--codex-dir', root])
        return S.Monitor(cfg)

    def tearDown(self):
        os.environ.pop('CCM_API_KEY', None)

    def test_on_by_default_and_remembers_off(self):
        m = self.make('m1')
        self.assertTrue(m.summ.active)
        info = m.summary_info()
        self.assertEqual((info['state'], info['label'], info['locked']), ('on', 'openai · m1', True))
        m.set_consent('off')
        self.assertFalse(m.summ.active)
        self.assertEqual(m.summary_info()['state'], 'off')
        self.assertEqual(self.make('m1').summary_state(), 'off')     # remembered across restarts
        self.assertEqual(self.make('m2').summary_state(), 'off')     # and with another model
        m.set_consent('on')
        self.assertTrue(m.summ.active)
        self.assertEqual(self.make('m1').summary_state(), 'on')


class Hardening(unittest.TestCase):
    def test_desktop_links_only_on_macos(self):
        root = tempfile.mkdtemp()
        mon = S.Monitor(parse_args(['--state-dir', root, '--claude-dir', root, '--codex-dir', root, '--summary', 'off']))
        key = 'codex:' + demo._id('codex:dark')
        with mock.patch.object(S.sys, 'platform', 'darwin'):
            self.assertTrue(mon.open_url(key).startswith('codex://threads/'))
        with mock.patch.object(S.sys, 'platform', 'linux'):
            self.assertIsNone(mon.open_url(key))

    def test_demo_runs_do_not_share_folders(self):
        a, b = parse_args(['--demo']), parse_args(['--demo'])
        S.use_demo(a)
        S.use_demo(b)
        self.assertNotEqual(a.claude_dir, b.claude_dir)
        self.assertFalse(a.token)

    def test_login_item_is_not_installed_when_the_port_is_taken(self):
        self.addCleanup(os.umask, os.umask(0o022))     # main() narrows the process umask; give it back
        with socket.socket() as other:
            other.bind(('127.0.0.1', 0))
            other.listen(1)
            port = other.getsockname()[1]
            with mock.patch.object(S.autostart, 'install') as install, self.assertRaises(SystemExit) as e:
                S.main(['--install-autostart', '--port', str(port), '--summary', 'off', '--state-dir', tempfile.mkdtemp()])
        install.assert_not_called()
        self.assertIn('in use', str(e.exception))

    def test_trusts_only_your_own_claude_tmp(self):
        base = tempfile.mkdtemp()
        own = os.path.join(base, 'claude-%d' % os.getuid())
        os.mkdir(own, 0o700)
        self.assertEqual(S.claude_tmp(base), os.path.realpath(own))
        os.chmod(own, 0o755)
        self.assertIsNone(S.claude_tmp(base))                    # open to others
        os.rmdir(own)
        os.symlink(tempfile.mkdtemp(), own)
        self.assertIsNone(S.claude_tmp(base))                    # a link anyone could have planted in /tmp

    def test_only_its_own_files_change(self):
        state = tempfile.mkdtemp()                       # a --state-dir that holds other things too
        os.chmod(state, 0o755)
        script = os.path.join(state, 'statusline.sh')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(script, 0o755)
        S.secure_dir(state)
        S.load_token(state)
        self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state, 'token')).st_mode), 0o600)

    def test_unreadable_log_is_reported_once(self):
        if os.getuid() == 0:
            self.skipTest('root can read any file')
        root = tempfile.mkdtemp()
        cd, xd = demo.build(os.path.join(root, 'data'), time.time())
        bad = glob.glob(os.path.join(cd, 'projects', '*', '*.jsonl'))[0]
        os.chmod(bad, 0)
        C.configure(cd, xd, 7)
        mon = S.Monitor(parse_args(['--state-dir', os.path.join(root, 'state'), '--claude-dir', cd, '--codex-dir', xd,
                                    '--summary', 'off', '--no-desktop']))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                mon.scan()
        self.assertEqual(err.getvalue().count('could not read'), 1)

    def test_reads_in_blocks(self):
        path = os.path.join(tempfile.mkdtemp(), 'x.jsonl')

        class Seen:
            def __init__(self):
                self.text = []

            def want(self, raw):
                return True

            def feed(self, d, raw):
                self.text.append(d['t'])
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"t": "中文一"}\n{"t": "second"}\n{"t": "半')
        with mock.patch.object(C, 'READ_BLOCK', 3):          # blocks that cut lines and characters in half
            tail = C.JsonlTail(path, Seen)
            self.assertEqual(tail.poll().text, ['中文一', 'second'])
            with open(path, 'a', encoding='utf-8') as f:
                f.write('行"}\n')
            self.assertEqual(tail.poll().text, ['中文一', 'second', '半行'])

    def test_housekeeping(self):
        root = tempfile.mkdtemp()
        cd, xd = demo.build(os.path.join(root, 'data'), time.time())
        C.configure(cd, xd, 7)
        mon = S.Monitor(parse_args(['--state-dir', os.path.join(root, 'state'), '--claude-dir', cd, '--codex-dir', xd,
                                    '--summary', 'off', '--no-desktop']))
        mon.scan()
        stale = os.path.join(root, 'gone.jsonl')
        mon.tails[stale] = C.JsonlTail(stale, C.ClaudeSession)
        mon.tails[stale].used = time.time() - S.EVICT_SECONDS - 1
        mon.summ.cache = {'claude:old|en': {'at': time.time() - 30 * 86400}, 'claude:new|en': {'at': time.time()}}
        mon.dismissed['claude:gone'] = {'at': time.time() - 30 * 86400, 'msg_ts': 0}
        mon.pruned_at = 0                                    # old summaries are dropped every 10 minutes; due now
        mon.scan()
        self.assertNotIn(stale, mon.tails)                   # not looked at for 10 minutes: dropped
        self.assertTrue(mon.tails)                           # the logs still in range stay
        self.assertEqual(list(mon.summ.cache), ['claude:new|en'])
        self.assertNotIn('claude:gone', mon.dismissed)

    def test_port_test_matches_the_server(self):
        with socket.socket() as s:                           # a port just closed is free again for the server
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
            s.listen(1)
            self.assertFalse(S.port_free(port))
        self.assertTrue(S.port_free(port))

    def test_no_abbreviated_options(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(['--base', 'https://u:pw@gw.example.com/v1'])

    def test_state_folder_is_private(self):
        root = tempfile.mkdtemp()
        state = os.path.join(root, 'state')
        args = ['--state-dir', state, '--claude-dir', root, '--codex-dir', root, '--summary', 'off']
        S.Monitor(parse_args(args))
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)            # a new state folder is closed
        with open(os.path.join(state, 'summaries.json'), 'w') as f:
            f.write('{}')
        os.chmod(os.path.join(state, 'summaries.json'), 0o644)                   # say, left by an older version
        S.Monitor(parse_args(args))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state, 'summaries.json')).st_mode), 0o600)

    def test_token_file(self):
        root = tempfile.mkdtemp()
        tok = S.load_token(root)
        self.assertEqual(S.load_token(root), tok)                 # the same token after a restart
        self.assertGreaterEqual(len(tok), 24)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, 'token')).st_mode), 0o600)
        self.assertTrue(parse_args([]).token)
        self.assertFalse(parse_args(['--no-token']).token)

    def test_odd_line_does_not_lose_later_lines(self):
        path = os.path.join(tempfile.mkdtemp(), 'x.jsonl')

        class Seen:
            def __init__(self):
                self.n = []

            def want(self, raw):
                return True

            def feed(self, d, raw):
                if d.get('odd'):
                    raise AttributeError('unexpected shape')
                self.n.append(d['n'])
        with open(path, 'w') as f:
            f.write('{"n": 1}\n{"odd": true}\nnot json\n{"n": 2}\n')
        tail = C.JsonlTail(path, Seen)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tail.poll().n, [1, 2])
        self.assertEqual(tail.bad, 2)                            # the odd shape and the line that is not JSON

    @unittest.skipIf(os.getuid() == 0, 'root can read any file')
    def test_unreadable_process_file(self):
        root = tempfile.mkdtemp()
        sessions = os.path.join(root, 'sessions')
        os.makedirs(sessions)
        p = os.path.join(sessions, 'x.json')
        with open(p, 'w') as f:
            f.write('{"pid": 1, "sessionId": "a"}')
        os.chmod(p, 0)
        C.configure(root, root, 7)
        self.assertEqual(C.live_claude_sessions(), {})

    def scan(self, edit):
        root = tempfile.mkdtemp()
        cd, xd = demo.build(os.path.join(root, 'data'), time.time())
        edit(cd, xd)
        C.configure(cd, xd, 7)
        mon = S.Monitor(parse_args(['--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(root, 'state'),
                                    '--summary', 'off', '--no-desktop']))
        mon.scan()
        return {r['title']: r for r in mon.snapshot['rows']}

    def test_permission_prompt_needs_your_reply(self):
        for what, text in (('permission prompt', 'ask.approve'), ('dialog open', 'ask.dialog')):
            def waiting(cd, xd):
                with open(os.path.join(cd, 'sessions', 'demo.json'), 'w') as f:
                    json.dump({'pid': os.getpid(), 'sessionId': demo._id('claude:ci'), 'status': 'waiting',
                               'waitingFor': what}, f)
            row = self.scan(waiting)['Migrate CI to GitHub Actions']
            self.assertEqual((row['status'], row['items'][0]['text']['m']), ('wait', text), what)

    def test_resumed_codex_session_in_an_old_folder(self):
        def resume(cd, xd):          # Codex keeps writing a resumed session into the folder of the day it started
            f = glob.glob(os.path.join(xd, 'sessions', '*', '*', '*', '*%s.jsonl' % demo._id('codex:dark')))[0]
            old = os.path.join(xd, 'sessions', '2020', '01', '01')
            os.makedirs(old)
            shutil.move(f, os.path.join(old, os.path.basename(f)))
            os.utime(os.path.join(old, os.path.basename(f)))
        self.assertIn('Dark mode toggle', self.scan(resume))


class PageSettings(unittest.TestCase):
    """Choosing the tool and model in the page. Fake claude / codex commands; nothing is called."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.bin = os.path.join(self.root, 'bin')
        os.makedirs(self.bin)
        for name in ('claude', 'codex'):
            self.tool(name)
        env = {k: v for k, v in os.environ.items() if k not in ('CCM_SUMMARY', 'CCM_MODEL')}
        env['PATH'] = self.bin
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def tool(self, name):
        p = os.path.join(self.bin, name)
        with open(p, 'w') as f:
            f.write('#!/bin/sh\nexit 0\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def make(self, *args):
        return S.Monitor(parse_args(['--state-dir', self.root, '--claude-dir', self.root, '--codex-dir', self.root] + list(args)))

    def test_switch_tool_and_model(self):
        m = self.make()
        info = m.summary_info()
        self.assertEqual((info['method'], info['backend'], info['model'], info['locked']), ('auto', 'claude-cli', 'sonnet', False))
        m.set_summary('codex-cli', None)
        self.assertEqual((m.backend.name, m.backend.model, m.summ.backend), ('codex-cli', '', m.backend))
        self.assertEqual(m.summary_info()['auto'], 'claude-cli')                 # what Automatic would pick
        m.set_summary('claude-cli', 'haiku')
        self.assertEqual(m.backend.label, 'claude-cli · haiku')
        self.assertEqual(self.make().backend.label, 'claude-cli · haiku')        # kept across restarts
        m.set_summary('codex-cli', 'gpt-x')
        m.set_summary('claude-cli', None)
        self.assertEqual(m.backend.label, 'claude-cli · haiku')                  # each tool keeps its own model
        m.set_summary('codex-cli', None)
        self.assertEqual(m.backend.label, 'codex-cli · gpt-x')
        for method, model in (('claude-cli', 'gpt-x'), ('codex-cli', 'bad name; rm'), ('openai', None), ('off', None)):
            with self.assertRaises(ValueError):
                m.set_summary(method, model)

    def test_startup_options_win(self):
        self.make().set_summary('claude-cli', 'opus')
        m = self.make('--summary', 'codex-cli')
        self.assertEqual((m.backend.name, m.summary_info()['locked']), ('codex-cli', True))
        with self.assertRaises(ValueError):
            m.set_summary('claude-cli', None)
        self.assertIsNone(self.make('--summary', 'off').backend)

    def test_uninstalled_choice_falls_back(self):
        self.make().set_summary('codex-cli', None)
        os.remove(os.path.join(self.bin, 'codex'))
        m = self.make()
        self.assertEqual(m.backend.name, 'claude-cli')
        self.assertFalse(m.summary_info()['installed']['codex-cli'])
        with self.assertRaises(ValueError):
            m.set_summary('codex-cli', None)


class Translations(unittest.TestCase):
    def test_no_duplicate_or_missing_keys(self):
        path = os.path.join(os.path.dirname(S.__file__), 'static', 'i18n.js')
        with open(path, encoding='utf-8') as f:
            en, zh = f.read().split('\n  zh: {')
        keys = {}
        for name, block in (('en', en), ('zh', zh)):
            found = re.findall(r"^    '([^']+)':", block, re.M)
            self.assertEqual(sorted(k for k in set(found) if found.count(k) > 1), [], name)   # a repeat silently wins
            keys[name] = set(found)
        self.assertEqual(keys['en'], keys['zh'])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp()
        cd, xd = demo.build(os.path.join(root, 'data'), time.time())
        cfg = parse_args(['--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(root, 'state'), '--no-desktop',
                          '--summary', 'off'])
        C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
        mon = S.Monitor(cfg)
        mon.scan()
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), S.Handler)
        cls.srv.mon = mon
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def test_sessions(self):
        st, body = self.req('GET', '/api/sessions')
        self.assertEqual(st, 200)
        self.assertEqual(len(json.loads(body)['rows']), 11)

    def test_page_and_translations(self):
        self.assertEqual(self.req('GET', '/')[0], 200)
        self.assertEqual(self.req('GET', '/i18n.js')[0], 200)

    def test_icons(self):
        for path, magic in (('/favicon.svg', b'<svg'), ('/favicon.ico', b'\x00\x00\x01\x00'), ('/apple-touch-icon.png', b'\x89PNG')):
            st, body = self.req('GET', path)
            self.assertEqual(st, 200, path)
            self.assertTrue(body.startswith(magic), path)

    def test_rejects_other_hosts(self):
        self.assertEqual(self.req('GET', '/api/sessions', headers={'Host': 'evil.example'})[0], 403)

    def test_rejects_cross_site_posts(self):
        st, _ = self.req('POST', '/api/dismiss', {'keys': ['x']}, {'Origin': 'https://evil.example', 'Content-Type': 'application/json'})
        self.assertEqual(st, 403)

    def test_file_viewer_stays_in_session_folder(self):
        self.assertEqual(self.req('GET', '/file?key=x&path=/etc/hosts')[0], 403)

    def test_security_headers(self):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request('GET', '/')
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual((r.getheader('X-Frame-Options'), r.getheader('X-Content-Type-Options')), ('DENY', 'nosniff'))

    def test_html_files_are_sandboxed(self):
        row = self.srv.mon.snapshot['rows'][0]
        folder, saved = tempfile.mkdtemp(), row['cwd']
        page = os.path.join(folder, 'report.html')
        with open(page, 'w') as f:
            f.write('<script>fetch("/api/sessions")</script>')
        row['cwd'] = folder
        try:
            headers = {'Cookie': 'ccm_%d=%s' % (self.port, getattr(self.srv, 'token', ''))}
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            c.request('GET', '/file?key=%s&path=%s' % (urllib.parse.quote(row['key']), urllib.parse.quote(page)), headers=headers)
            r = c.getresponse()
            r.read()
            c.close()
        finally:
            row['cwd'] = saved
        self.assertEqual(r.status, 200)
        self.assertIn('sandbox', r.getheader('Content-Security-Policy'))

    def test_posts_need_json_from_this_page(self):
        ok = {'Content-Type': 'application/json'}
        self.assertEqual(self.req('POST', '/api/restore', {'keys': []}, ok)[0], 200)
        self.assertEqual(self.req('POST', '/api/restore', {'keys': []}, dict(ok, Origin='http://127.0.0.1:%d' % self.port))[0], 200)
        self.assertEqual(self.req('POST', '/api/restore', {'keys': []}, dict(ok, Origin='http://127.0.0.1:5173'))[0], 403)
        self.assertEqual(self.req('POST', '/api/restore', {'keys': []}, {'Content-Type': 'text/plain'})[0], 403)

    def test_consent_without_backend(self):
        st, _ = self.req('POST', '/api/consent', {'decision': 'on'}, {'Content-Type': 'application/json'})
        self.assertEqual(st, 400)

    def test_port_already_taken(self):
        self.addCleanup(os.umask, os.umask(0o022))
        tmp = tempfile.mkdtemp()
        args = ['--no-open', '--summary', 'off', '--state-dir', tmp, '--claude-dir', tmp, '--codex-dir', tmp, '--port']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(S.main(args + [str(self.port)]))         # a monitor is already there: say so, open it
        self.assertIn('already running at http://127.0.0.1:%d/' % self.port, out.getvalue())
        self.assertEqual(os.listdir(tmp), [])                          # and change nothing
        with socket.socket() as other:                                  # another program holds the port
            other.bind(('127.0.0.1', 0))
            other.listen(1)
            port = other.getsockname()[1]
            with self.assertRaises(SystemExit) as e:
                S.main(args + [str(port)])
        self.assertIn('in use by another program', str(e.exception))

    def test_summary_settings_fixed_at_startup(self):
        st, _ = self.req('POST', '/api/summary', {'method': 'claude-cli'}, {'Content-Type': 'application/json'})
        self.assertEqual(st, 400)


class TokenHttp(Http):
    """Every request above again with the access token on, plus what happens without it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv.token = 'test-token-' + 'x' * 24
        cls.srv.token_file = os.path.join(tempfile.mkdtemp(), 'token')

    def req(self, method, path, body=None, headers=None, cookie=True):
        headers = dict(headers or {})
        if cookie:
            headers.setdefault('Cookie', 'ccm_%d=%s' % (self.port, self.srv.token))
        return super().req(method, path, body, headers)

    def test_without_the_token(self):
        st, body = self.req('GET', '/api/sessions', cookie=False)
        self.assertEqual(st, 401)
        self.assertIn('token_file', json.loads(body))
        self.assertEqual(self.req('GET', '/file?key=x&path=/etc/hosts', cookie=False)[0], 401)
        self.assertEqual(self.req('POST', '/api/restore', {'keys': []}, {'Content-Type': 'application/json'}, cookie=False)[0], 401)
        self.assertEqual(self.req('GET', '/', cookie=False)[0], 200)        # the page, which then asks for the token
        self.assertEqual(self.req('GET', '/api/sessions', headers={'Cookie': 'ccm_%d=wrong' % self.port}, cookie=False)[0], 401)

    def test_other_cookies_do_not_hide_the_token(self):
        for other in ('g_state={"i_l":0}', 'pref=dark mode', 'foo[bar]=1'):
            cookie = '%s; ccm_%d=%s' % (other, self.port, self.srv.token)
            self.assertEqual(self.req('GET', '/api/sessions', headers={'Cookie': cookie}, cookie=False)[0], 200, other)

    def test_odd_tokens_do_not_break_the_page(self):
        self.assertEqual(self.req('GET', '/?token=%C3%A9', cookie=False)[0], 200)
        self.assertEqual(self.req('GET', '/api/sessions', headers={'Cookie': 'ccm_%d=é' % self.port}, cookie=False)[0], 401)

    def test_token_in_the_address_is_remembered(self):
        def get(path):
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            c.request('GET', path)
            r = c.getresponse()
            r.read()
            c.close()
            return r
        r = get('/?token=' + self.srv.token)
        self.assertEqual((r.status, r.getheader('Location')), (302, '/'))
        cookie = r.getheader('Set-Cookie')
        self.assertTrue(cookie.startswith('ccm_%d=%s;' % (self.port, self.srv.token)))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(get('/?lang=en&token=%s&theme=dark' % self.srv.token).getheader('Location'), '/?lang=en&theme=dark')
        r = get('/?token=wrong')
        self.assertEqual((r.status, r.getheader('Set-Cookie')), (200, None))

    def test_a_running_monitor_is_recognized(self):
        self.assertTrue(S.monitor_at(self.port))


if __name__ == '__main__':
    unittest.main()
