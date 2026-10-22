import atexit
import contextlib
import http.server
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest

from claude_codex_monitor import summarize as Z
from claude_codex_monitor.config import parse_args


# Everything these tests create goes into one folder, removed when they finish
tempfile.tempdir = tempfile.mkdtemp(prefix='claude-codex-monitor-tests-')
atexit.register(shutil.rmtree, tempfile.tempdir, True)

# Fake secrets are put together at run time so secret scanners do not flag this file
FAKE_KEY = 'sk' + '-proj-' + 'AbCdEf0123456789xyzXYZ'
FAKE_GH = 'gh' + 'p_' + 'aBcDeFgHiJkLmNoPqRsTuVwXyZ012345'
REPLY = '{"waiting": true, "items": [{"text": "Pick Postgres or SQLite", "from": 2}], "optional": []}'
TURNS = [{'role': 'user', 'ts': '2026-10-22T01:00:00Z', 'text': 'Set up the database. My email is jane@example.com'},
         {'role': 'assistant', 'ts': '2026-10-22T01:05:00Z',
          'text': 'Done. Needs your decision:\n1. Postgres or SQLite?\nThe key ' + FAKE_KEY + ' is in .env'}]


class Redaction(unittest.TestCase):
    MASKED = {
        'key ' + FAKE_KEY + ' here': '[KEY]',
        'export OPENAI_API_KEY=abc123def456ghi': '[REDACTED]',
        'token: ' + FAKE_GH: '[KEY]',
        'Authorization: Bearer abcdefghijklmnop0123456789': '[TOKEN]',
        'password = "correct9horse"': '[REDACTED]',
        'mail jane.doe+ci@example.co.uk now': '[EMAIL]',
        '手机13812345678联系': '[PHONE]',
        'call +1 415 555 0132': '[PHONE]',
        'https://x.io/cb?token=abc123&x=1': '[REDACTED]',
        '密钥' + FAKE_KEY + '已换': '[KEY]',
        '-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----': '[PRIVATE KEY]',
        '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk': '[PRIVATE KEY]',         # pasted without its END line
        '-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----': '[PRIVATE KEY]',
        'postgres://admin:S3cretPass@localhost:5432/db': 'postgres://[REDACTED]@localhost',
        'redis://:p4ss@cache-1': 'redis://[REDACTED]@cache-1',
        'DB_PASS=s3cretvalue99': '[REDACTED]',
        'PASSWORD=correcthorsebatterystaple': '[REDACTED]',
        'PASSWORD=8675309123': '[REDACTED]',
        'SERVICE_CREDENTIALS=abc.def.ghi': '[REDACTED]',
        'AUTH=Zm9vYmFyYmF6': '[REDACTED]',
        'Authorization: Basic dXNlcjpwYXNzd29yZA==': 'Basic [TOKEN]',
        'Cookie: sessionid=abc123; csrftoken=xyz': 'Cookie: [REDACTED]',
        'https://hooks.slack.com/services/T000/B000/XXXX': '[WEBHOOK]',
        'ASIAABCDEFGHIJKLMNOP': '[KEY]',
        '数据库密码：Abc12345xyz': '密码：[REDACTED]',
        '密码是 Abc12345xyz': '[REDACTED]',
        '登录口令 Qw3rty!2024': '[REDACTED]',
        'token：abcdefgh12345': '[REDACTED]',
    }
    KEPT = ['date 2026-10-22 12:30', 'total_tokens 184000', 'max_tokens: 16000', 'port 8767, pid 12345',
            'max_tokens: 16000000', 'tokens_used: 123456789', 'author: Jane Smith-Brown', 'tests passed: 12345678',
            '密码规则：长度至少 8 位', 'basic configuration for the server', 'password reset link sent',
            'sha 0123456789abcdef0123456789abcdef01234567', 'session 00000000-1111-2222-3333-444444444444',
            'v12 deployed, 30 urls', 'token 用量 12,345']

    def test_masked(self):
        for text, mark in self.MASKED.items():
            with self.subTest(text=text):
                self.assertIn(mark, Z.redact(text))

    def test_kept(self):
        for text in self.KEPT:
            with self.subTest(text=text):
                self.assertEqual(Z.redact(text), text)

    def test_masked_before_clipping(self):
        # A long message keeps its first 800 characters; a key cut short there would no longer look like a key
        body = 'x' * 786 + ' sk-' + 'A' * 40 + ' ' + 'y' * 4000
        self.assertNotIn('[KEY]', Z.redact(Z._clip_middle(body)))           # clipped first: the stub slips through
        _, prompt = Z.build_prompt([{'role': 'user', 'ts': '2026-10-22T01:00:00Z', 'text': body}], lambda ts: ts, 'en')
        self.assertNotIn('AAAA', prompt)
        self.assertIn('[KEY]', prompt)

    def test_prompt_is_masked_and_split(self):
        system, prompt = Z.build_prompt(TURNS, lambda ts: ts[11:16], 'en')
        self.assertTrue(system.startswith('You are helping'))
        self.assertIn('[#2 Assistant 01:05]', prompt)
        self.assertNotIn('sk-proj', prompt)
        self.assertNotIn('jane@example.com', prompt)
        system, _ = Z.build_prompt(TURNS, lambda ts: ts, 'zh')
        self.assertIn('用户', system)
        for lang in ('en', 'zh'):        # the user's own instructions decide the reply language
            self.assertIn('CLAUDE.md', Z.PROMPTS[lang])


class FakeAPI(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeAPI.calls.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
        if 'moved' in self.path:              # a redirect elsewhere must not take the key along
            self.send_response(302)
            self.send_header('Location', '/elsewhere/v1/messages')
            self.end_headers()
            return
        if 'fail' in self.path:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "boom"}')
            return
        if self.path.endswith('/chat/completions'):
            out = {'choices': [{'message': {'content': REPLY}}]}
        else:
            out = {'content': [{'type': 'text', 'text': REPLY}], 'stop_reason': 'end_turn'}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ApiBackends(unittest.TestCase):
    def test_redirects_are_not_followed(self):
        os.environ['CCM_API_KEY'] = 'k-redirect'
        try:
            b = Z.make_backend(parse_args(['--summary', 'anthropic', '--base-url', self.url + '/moved']))
            FakeAPI.calls.clear()
            with self.assertRaisesRegex(RuntimeError, 'HTTP 302'):
                b.complete('SYS', 'CONV', 5)
        finally:
            os.environ.pop('CCM_API_KEY', None)
        self.assertEqual([c['path'] for c in FakeAPI.calls], ['/moved/v1/messages'])

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeAPI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d' % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeAPI.calls.clear()
        os.environ['CCM_API_KEY'] = 'test-key'

    def tearDown(self):
        os.environ.pop('CCM_API_KEY', None)

    def test_openai_compatible(self):
        b = Z.make_backend(parse_args(['--summary', 'openai', '--base-url', self.url + '/v1', '--model', 'm1']))
        res = Z.parse_result(b.complete('SYS', 'CONV', 5))
        self.assertTrue(res['waiting'])
        c = FakeAPI.calls[0]
        self.assertEqual(c['path'], '/v1/chat/completions')
        self.assertEqual(c['headers'].get('authorization'), 'Bearer test-key')
        self.assertEqual(c['body']['model'], 'm1')
        self.assertEqual([m['role'] for m in c['body']['messages']], ['system', 'user'])

    def test_openai_needs_model(self):
        with self.assertRaises(ValueError):
            Z.make_backend(parse_args(['--summary', 'openai', '--base-url', self.url]))

    def test_anthropic(self):
        b = Z.make_backend(parse_args(['--summary', 'anthropic', '--base-url', self.url]))
        self.assertEqual(b.model, 'claude-sonnet-5-5')
        self.assertTrue(Z.parse_result(b.complete('SYS', 'CONV', 5))['waiting'])
        c = FakeAPI.calls[0]
        self.assertEqual(c['path'], '/v1/messages')
        self.assertEqual(c['headers'].get('x-api-key'), 'test-key')
        self.assertEqual(c['headers'].get('anthropic-version'), '2023-06-01')
        self.assertEqual(c['body']['system'], 'SYS')

    def test_anthropic_needs_key(self):
        os.environ.pop('CCM_API_KEY', None)
        saved = os.environ.pop('ANTHROPIC_API_KEY', None)
        try:
            with self.assertRaises(ValueError):
                Z.make_backend(parse_args(['--summary', 'anthropic', '--base-url', self.url]))
        finally:
            if saved is not None:
                os.environ['ANTHROPIC_API_KEY'] = saved

    def test_http_error(self):
        b = Z.make_backend(parse_args(['--summary', 'openai', '--base-url', self.url + '/fail', '--model', 'm']))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            b.complete('SYS', 'CONV', 5)


FAKE_CLAUDE = r'''
import json, sys
args, stdin = sys.argv[1:], sys.stdin.read()
json.dump({'args': args, 'stdin': stdin}, open(__file__ + '.log', 'w'))
if 'FAIL' in stdin:
    sys.exit(3)
print(json.dumps({'type': 'result', 'is_error': False, 'result': %r}))
''' % REPLY
FAKE_CODEX = r'''
import json, sys
args, stdin = sys.argv[1:], sys.stdin.read()
json.dump({'args': args, 'stdin': stdin}, open(__file__ + '.log', 'w'))
open(args[args.index('-o') + 1], 'w').write(%r)
''' % REPLY


class CliBackends(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, code in (('claude', FAKE_CLAUDE), ('codex', FAKE_CODEX)):
            p = os.path.join(self.dir, name)
            with open(p, 'w') as f:
                f.write('#!%s\n%s' % (sys.executable, code))
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.path = os.environ['PATH']
        os.environ['PATH'] = self.dir + os.pathsep + self.path

    def tearDown(self):
        os.environ['PATH'] = self.path

    def log(self, name):
        with open(os.path.join(self.dir, name + '.log')) as f:
            return json.load(f)

    def test_claude(self):
        b = Z.make_backend(parse_args(['--summary', 'claude-cli']))
        self.assertEqual((b.workers, b.model), (1, 'sonnet'))
        self.assertTrue(Z.parse_result(b.complete('SYS', 'CONV', 10))['waiting'])
        a = self.log('claude')['args']
        for flag in ('-p', '--no-session-persistence', '--strict-mcp-config', '--disable-slash-commands'):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index('--system-prompt') + 1], 'SYS')
        self.assertEqual(a[a.index('--settings') + 1], '{"disableAllHooks": true}')
        self.assertEqual(a[-2:], ['--tools', ''])           # last, so the list option swallows nothing
        self.assertEqual(self.log('claude')['stdin'], 'CONV')

    def test_claude_failure(self):
        b = Z.make_backend(parse_args(['--summary', 'claude-cli']))
        with self.assertRaisesRegex(RuntimeError, 'exited with 3'):
            b.complete('SYS', 'FAIL', 10)

    def test_codex(self):
        b = Z.make_backend(parse_args(['--summary', 'codex-cli', '--model', 'gpt-x']))
        self.assertTrue(Z.parse_result(b.complete('SYS', 'CONV', 10))['waiting'])
        a = self.log('codex')['args']
        self.assertEqual(a[:3], ['exec', '-m', 'gpt-x'])
        for flag in ('--ephemeral', '--skip-git-repo-check', 'notify=[]'):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index('-s') + 1], 'read-only')
        self.assertEqual(a[-1], '-')
        self.assertEqual(self.log('codex')['stdin'], 'SYS\nCONV')
        self.assertEqual(os.listdir(b.cwd), [])             # output file cleaned up

    def test_one_work_folder_per_process(self):
        a = Z.make_backend(parse_args(['--summary', 'claude-cli']))
        b = Z.make_backend(parse_args(['--summary', 'codex-cli']))
        self.assertEqual(a.cwd, b.cwd)
        self.assertTrue(os.path.isdir(a.cwd))

    def test_auto_prefers_claude_then_codex(self):
        self.assertEqual(Z.make_backend(parse_args(['--summary', 'auto'])).name, 'claude-cli')
        only_codex = tempfile.mkdtemp()
        os.symlink(os.path.join(self.dir, 'codex'), os.path.join(only_codex, 'codex'))
        os.environ['PATH'] = only_codex
        self.assertEqual(Z.make_backend(parse_args(['--summary', 'auto'])).name, 'codex-cli')
        os.environ['PATH'] = tempfile.mkdtemp()
        self.assertIsNone(Z.make_backend(parse_args(['--summary', 'auto'])))

    def test_missing_command(self):
        os.environ['PATH'] = tempfile.mkdtemp()
        with self.assertRaisesRegex(ValueError, 'needs the `codex` command'):
            Z.make_backend(parse_args(['--summary', 'codex-cli']))


class Separators(unittest.TestCase):
    def test_semicolon_follows_the_language(self):
        from claude_codex_monitor import collect as C
        self.assertEqual(C.join_clauses(['Which library?', 'Migrate now?']), 'Which library?; Migrate now?')
        self.assertEqual(C.join_clauses(['用哪个库？', '现在迁移吗？']), '用哪个库？；现在迁移吗？')
        turns = [{'role': 'user', 'ts': '2026-10-22T10:00:00Z', 'text': 'go'},
                 {'role': 'assistant', 'ts': '2026-10-22T10:01:00Z',
                  'text': 'Done.\n\n**Needs your decision:**\n1. Which database should we use?\n   A) Postgres\n   B) SQLite'}]
        self.assertEqual(Z.quick_asks(turns)[0], 'Which database should we use?; A) Postgres; B) SQLite')


class Gate(unittest.TestCase):
    def test_nothing_sent_until_active(self):
        class Spy(Z.Backend):
            name, workers, sent = 'spy', 1, []

            def complete(self, system, prompt, timeout):
                Spy.sent.append(prompt)
                return REPLY
        b = Spy(parse_args(['--model', 'x']))
        s = Z.Summarizer(os.path.join(tempfile.mkdtemp(), 'summaries.json'), b)
        s.request('claude:a', 'k1', TURNS, 0, lambda ts: ts, 'en')
        self.assertFalse(s.is_pending('claude:a', 'en'))
        s.active = True
        s.request('claude:a', 'k1', TURNS, 0, lambda ts: ts, 'en')
        for _ in range(100):
            if s.get('claude:a', 'en'):
                break
            threading.Event().wait(0.02)
        self.assertEqual(s.get('claude:a', 'en')['items'][0]['text'], 'Pick Postgres or SQLite')
        self.assertIsNone(s.get('claude:a', 'zh'))           # cached per language
        self.assertEqual(len(Spy.sent), 1)
        s.request('claude:a', 'k1', TURNS, 0, lambda ts: ts, 'en')
        self.assertFalse(s.is_pending('claude:a', 'en'))     # same message, same model: kept
        s.set_backend(Spy(parse_args(['--model', 'y'])))
        s.request('claude:a', 'k1', TURNS, 0, lambda ts: ts, 'en')
        for _ in range(100):
            if s.get('claude:a', 'en')['model'] == 'spy · y':
                break
            threading.Event().wait(0.02)
        self.assertEqual(len(Spy.sent), 2)                   # another model: summarized again


class WorkerKeepsGoing(unittest.TestCase):
    def test_a_failed_save_does_not_stop_summaries(self):
        class Spy(Z.Backend):
            name, workers = 'spy', 1

            def complete(self, system, prompt, timeout):
                return REPLY
        path = os.path.join(tempfile.mkdtemp(), 'missing-folder', 'summaries.json')     # every save fails
        s = Z.Summarizer(path, Spy(parse_args(['--model', 'x'])))
        s.active = True
        with contextlib.redirect_stderr(io.StringIO()):
            for key in ('claude:a', 'claude:b'):
                s.request(key, 'k1', TURNS, 0, lambda ts: ts, 'en')
                for _ in range(100):
                    if s.get(key, 'en'):
                        break
                    threading.Event().wait(0.02)
        self.assertTrue(s.get('claude:a', 'en') and s.get('claude:b', 'en'))


class ConversationLanguage(unittest.TestCase):
    """Summaries are written in the conversation's language, whatever language the page is shown in."""

    def lang(self, *users, replies=()):
        return Z.conversation_lang([{'role': 'user', 'text': u, 'ts': ''} for u in users] +
                                   [{'role': 'assistant', 'text': a, 'ts': ''} for a in replies])

    def test_chinese_with_english_terms(self):
        self.assertEqual(self.lang('用 Postgres 还是 SQLite？那个 API 的 rate limit 是多少'), 'zh')
        self.assertEqual(self.lang('把 README.md 里的 badge 换成 GitHub Actions 的，再跑一下 npm test'), 'zh')

    def test_english_and_other_languages(self):
        self.assertEqual(self.lang('Set up the database please'), 'en')
        self.assertEqual(self.lang('Please update the 用户手册 section about login and signup flows'), 'en')
        self.assertEqual(self.lang('データベースを設定してください'), 'en')     # English instructions ask for the user's language

    def test_pasted_text_does_not_count(self):
        pasted = '<pasted_content id="b7">' + 'I built the features worth borrowing. ' * 30 + '</pasted_content id="b7">'
        self.assertEqual(self.lang('翻译：\n' + pasted, '重启服务'), 'zh')

    def test_short_english_progress_notes_do_not_outvote_replies(self):
        notes = ['Rebuilding the frontend and restarting the server now.'] * 6
        self.assertEqual(self.lang('都开发完了吗', replies=notes + ['都开发完了，已经合并进 main，本地可以直接测。' * 8]), 'zh')


class QuickAsks(unittest.TestCase):
    def ask(self, text):
        return Z.quick_asks([{'role': 'assistant', 'ts': 'x', 'text': text}])

    def test_english(self):
        self.assertEqual(self.ask('Done. Two things need your decision:\n1. Use Postgres or SQLite?\n'
                                  '2. Should I also migrate the old data?'),
                         ['Use Postgres or SQLite?', 'Should I also migrate the old data?'])
        self.assertEqual(self.ask('All tests pass. If you want, I can also add a CI badge.'), [])

    def test_chinese(self):
        self.assertEqual(self.ask('改好了。\n\n**请你确认：**\n1. 标题用 A 还是 B？\n2. 要不要保留旧版？'),
                         ['标题用 A 还是 B？', '要不要保留旧版？'])


if __name__ == '__main__':
    unittest.main()
