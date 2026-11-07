import json
import os
import shutil
import tempfile
import time
import unittest

from claude_codex_monitor import collect as C
from claude_codex_monitor import cursor as K
from claude_codex_monitor import server as S
from claude_codex_monitor.config import parse_args


def user(text, when='Saturday, Nov 7, 2026, 10:14 PM (UTC+8)'):
    return {'role': 'user', 'message': {'content': [{'type': 'text', 'text': '<timestamp>%s</timestamp>\n<user_query>\n%s\n</user_query>' % (when, text)}]}}


def say(text):
    return {'role': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def tool(name):
    return {'role': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': {}}]}}


def ended(status='success'):
    return {'type': 'turn_ended', 'status': status}


class Cursor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.work = os.path.join(self.root, 'code', 'my-app')            # a folder with a dash, to rebuild from the name
        os.makedirs(self.work)
        self.project = self.work.strip('/').replace('/', '-').replace('.', '-').replace('_', '-')
        self.cursor = os.path.join(self.root, '.cursor')
        K._cwd_cache.clear()

    def chat(self, cid, lines, project=None, age=0, sub=False):
        folder = os.path.join(self.cursor, 'projects', project or self.project, 'agent-transcripts', cid)
        if sub:
            folder = os.path.join(folder, 'subagents')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, ('sub' if sub else cid) + '.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(x, ensure_ascii=False) for x in lines) + '\n')
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def monitor(self):
        cfg = parse_args(['--claude-dir', os.path.join(self.root, '.claude'), '--codex-dir', os.path.join(self.root, '.codex'),
                          '--state-dir', os.path.join(self.root, 'state'), '--no-desktop', '--summary', 'off'])
        self.assertEqual(cfg.cursor_dir, self.cursor)        # next to --claude-dir, never the real ~/.cursor in tests
        C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
        mon = S.Monitor(cfg)
        mon.scan()
        return {r['key']: r for r in mon.snapshot['rows'] if r['client'] == 'cursor'}, mon

    def test_states(self):
        self.chat('wait', [user('改一下按钮'), say('改好了。要不要顺便把颜色也改了？'), ended()], age=60)
        self.chat('run', [user('跑一下测试'), tool('Shell')], age=5)
        self.chat('dead', [user('跑一下测试'), tool('Shell')], age=3600)
        self.chat('fail', [user('跑一下测试'), say('出错了'), ended('error')], age=60)
        self.chat('done', [user('hi'), say('Hi. Done.'), ended()], age=60)
        rows, _ = self.monitor()
        got = {k[7:]: r['status'] for k, r in rows.items()}
        self.assertEqual(got, {'wait': 'wait', 'run': 'run', 'dead': 'fail', 'fail': 'fail', 'done': 'ok'})
        w = rows['cursor:wait']
        self.assertEqual((w['title'], w['cwd'], w['project']), ('改一下按钮', self.work, 'my-app'))
        self.assertTrue(w['items'] and '颜色' in w['items'][0]['text'])
        self.assertEqual(rows['cursor:run']['note'], {'m': 'note.doing', 'a': ['Shell']})

    def test_skips_temporary_projects_and_subagents(self):
        self.chat('tmp', [user('x'), say('y?'), ended()], project='var-folders-ab-T-gone-123')      # its folder is gone
        self.chat('own', [user('x'), say('y?'), ended()], project='private-var-folders-T-claude-codex-monitor-ab12-x9z')
        self.chat('main', [user('x'), say('y'), ended()])
        self.chat('main', [user('sub'), say('sub?'), ended()], sub=True)
        rows, _ = self.monitor()
        self.assertEqual(list(rows), ['cursor:main'])

    def test_detail_and_times(self):
        self.chat('wait', [user('改一下按钮'), say('要不要也改颜色？'), ended()], age=60)
        rows, mon = self.monitor()
        d = mon.detail('cursor:wait')
        self.assertEqual((d['info']['client'], d['info']['cwd']), ('Cursor', self.work))
        self.assertEqual(d['info']['start'], C.parse_ts('2026-11-07T22:14:00+08:00'))
        self.assertEqual(K.parse_stamp('<timestamp>Monday, Sep 1, 2026, 9:05 AM (UTC-5)</timestamp>'), '2026-09-01T09:05:00-05:00')
        self.assertIsNone(K.parse_stamp('no time here'))


if __name__ == '__main__':
    unittest.main()
