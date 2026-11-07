import datetime as dt
import http.client
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from claude_codex_monitor import collect as C
from claude_codex_monitor import demo
from claude_codex_monitor import server as S
from claude_codex_monitor import sources
from claude_codex_monitor.config import parse_args


def local(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi).timestamp()


class Inbox(sources.Source):
    """A made-up source: two items, a tab and one API route."""
    label = {'en': 'Inbox', 'zh': '收件箱'}
    tab_script = 'tab.js'

    def __init__(self, context):
        super().__init__(context)
        self.static_dir = os.path.join(context.state_dir, 'static')
        os.makedirs(self.static_dir, exist_ok=True)
        with open(os.path.join(self.static_dir, 'tab.js'), 'w') as f:
            f.write('CCMTabs.register("inbox", {render: () => "inbox"});')
        self.updated = 100.0

    def items(self, now):
        return [{'key': 'a', 'kind': 'approve', 'title': 'Approve access', 'who': 'Alex', 'since': 50, 'updated': self.updated},
                {'key': 'b', 'kind': 'nonsense', 'title': 'Weekly note', 'url': 'javascript:alert(1)'},
                {'title': 'no key, dropped'}]

    def handle(self, method, path, query, body):
        if path == 'echo':
            return 200, {'method': method, 'query': query, 'body': body}
        return None


class EP:
    def __init__(self, name, factory):
        self.name, self.factory = name, factory

    def load(self):
        return self.factory


class DayWindow(unittest.TestCase):
    def test_after_midnight_is_still_the_day_before(self):
        self.assertEqual(sources.day_window(local(2026, 11, 8, 1)), (local(2026, 11, 7), local(2026, 11, 8, 4)))

    def test_morning_starts_a_new_day_that_includes_the_night(self):
        self.assertEqual(sources.day_window(local(2026, 11, 8, 10)), (local(2026, 11, 8), local(2026, 11, 9, 4)))

    def test_the_boundary_itself(self):
        self.assertEqual(sources.day_window(local(2026, 11, 8, 4))[0], local(2026, 11, 8))
        self.assertEqual(sources.day_window(local(2026, 11, 8, 3, 59))[0], local(2026, 11, 7))


class Loading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_loads_a_source_with_a_folder_of_its_own(self):
        found, errors = sources.load(self.dir, entry_points=[EP('inbox', Inbox)])
        self.assertEqual(([s.name for s in found], errors), (['inbox'], []))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'ext', 'inbox')))

    def test_a_broken_source_only_reports_itself(self):
        def boom(context):
            raise RuntimeError('missing tool')
        found, errors = sources.load(self.dir, entry_points=[EP('Bad Name', Inbox), EP('boom', boom),
                                                             EP('plain', lambda c: object()), EP('inbox', Inbox),
                                                             EP('inbox', Inbox)])
        self.assertEqual([s.name for s in found], ['inbox'])
        self.assertEqual([e['name'] for e in errors], ['Bad Name', 'boom', 'plain', 'inbox'])

    def test_a_source_named_in_the_environment(self):
        with mock.patch.dict(os.environ, {'CCM_SOURCES': 'inbox=tests.test_sources:Inbox, broken'}):
            eps = sources._env_entry_points()
            self.assertEqual([e.name for e in eps], ['inbox'])
            found, errors = sources.load(self.dir, entry_points=eps)
        self.assertEqual(([s.name for s in found], errors), (['inbox'], []))

    def test_no_plugins_installed(self):
        with mock.patch.object(sources, '_entry_points', return_value=[]):
            self.assertEqual(sources.load(self.dir), ([], []))


class Items(unittest.TestCase):
    def test_clean_keeps_well_formed_fields_only(self):
        a, b, c = (sources.clean('inbox', r) for r in Inbox(sources.Context('inbox', tempfile.mkdtemp(), '')).items(0))
        self.assertEqual((a['key'], a['kind'], a['open']), ('inbox:a', 'approve', True))
        self.assertEqual((b['kind'], b['url']), ('info', None))      # unknown kind, unsafe link
        self.assertIsNone(c)

    def test_poller_keeps_the_last_good_result(self):
        src = Inbox(sources.Context('inbox', tempfile.mkdtemp(), ''))
        src.name = 'inbox'
        p = sources.Poller(src)
        p.poll(now=1)
        self.assertEqual(len(p.items), 2)
        with mock.patch.object(src, 'items', side_effect=OSError('db locked')):
            with mock.patch('traceback.print_exc'):
                p.poll(now=2)
        self.assertEqual((len(p.items), p.error), (2, "OSError('db locked')"))


class State(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), 'items.json')
        self.state = sources.ItemState(self.path)
        self.item = {'key': 'inbox:a', 'updated': 100}

    def test_done_and_skip_stay_and_are_saved(self):
        self.state.set('inbox:a', 'done', now=200)
        self.assertEqual(sources.ItemState(self.path).state(self.item, 10 ** 10), 'done')
        self.state.set('inbox:a', 'skip', now=300)
        self.assertEqual(self.state.state(dict(self.item, updated=999), 1000), 'skip')

    def test_later_comes_back_on_time_or_with_news(self):
        self.state.set('inbox:a', 'later', until=500, now=200)
        self.assertEqual(self.state.state(self.item, 300), 'later')
        self.assertIsNone(self.state.state(self.item, 500))                       # its time came
        self.assertIsNone(self.state.state(dict(self.item, updated=250), 300))    # something new happened

    def test_restore_and_bad_input(self):
        self.state.set('inbox:a', 'done', now=200)
        self.state.set('inbox:a', 'restore', now=201)
        self.assertIsNone(self.state.state(self.item, 300))
        with self.assertRaises(ValueError):
            self.state.set('inbox:a', 'delete')
        with self.assertRaises(ValueError):
            self.state.set('inbox:a', 'later', until=100, now=200)               # in the past

    def test_prune_forgets_old_items_no_source_reports(self):
        self.state.set('inbox:a', 'done', now=100)
        self.state.set('inbox:b', 'done', now=100)
        self.state.set('inbox:c', 'done', now=900)
        self.state.prune({'inbox:a'}, before=500)
        self.assertEqual(sorted(self.state.data), ['inbox:a', 'inbox:c'])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, root, True)
        cd, xd = demo.build(os.path.join(root, 'data'), time.time())
        cfg = parse_args(['--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(root, 'state'),
                          '--no-desktop', '--summary', 'off'])
        C.configure(cfg.claude_dir, cfg.codex_dir, cfg.days)
        with mock.patch.object(sources, '_entry_points', return_value=[EP('inbox', Inbox)]):
            cls.mon = S.Monitor(cfg)
        cls.mon.scan()
        cls.mon.pollers[0].poll()
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), S.Handler)
        cls.srv.mon = cls.mon
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def today(self):
        st, body = self.req('GET', '/api/today')
        self.assertEqual(st, 200)
        return json.loads(body)

    def test_today_has_every_source(self):
        d = self.today()
        self.assertEqual({a['title'] for a in d['agents']},
                         {r['title'] for r in self.mon.snapshot['rows'] if r['status'] == 'wait' and not r['dismissed']})
        inbox = d['sources'][0]
        self.assertEqual((inbox['name'], inbox['tab'], len(inbox['items'])), ('inbox', True, 2))
        self.assertLess(d['window']['start'], d['now'])
        self.assertGreater(d['window']['end'], d['now'])

    def test_marking_an_item_done_and_back(self):
        self.assertEqual(self.req('POST', '/api/items', {'key': 'inbox:a', 'action': 'done'})[0], 200)
        self.assertEqual(self.today()['sources'][0]['items'][0]['handled'], 'done')
        self.assertEqual(self.req('POST', '/api/items', {'key': 'inbox:a', 'action': 'restore'})[0], 200)
        self.assertIsNone(self.today()['sources'][0]['items'][0]['handled'])

    def test_item_errors(self):
        self.assertEqual(self.req('POST', '/api/items', {'key': 'other:a', 'action': 'done'})[0], 404)
        self.assertEqual(self.req('POST', '/api/items', {'key': 'inbox:a', 'action': 'explode'})[0], 400)

    def test_source_api_and_files(self):
        st, body = self.req('POST', '/api/ext/inbox/echo', {'x': 1})
        self.assertEqual((st, json.loads(body)['body']), (200, {'x': 1}))
        self.assertEqual(self.req('GET', '/api/ext/inbox/nothing')[0], 404)
        self.assertEqual(self.req('GET', '/api/ext/missing/echo')[0], 404)
        st, body = self.req('GET', '/ext/inbox/tab.js')
        self.assertEqual((st, body[:7]), (200, b'CCMTabs'))
        for bad in ('/ext/inbox/..%2Fitems.json', '/ext/inbox/tab.exe', '/ext/missing/tab.js'):
            self.assertEqual(self.req('GET', bad)[0], 404, bad)

    def test_pages_need_the_token_when_set(self):
        self.srv.token, self.srv.token_file = 'secret', os.path.join(self.mon.cfg.state_dir, 'token')
        try:
            self.assertEqual(self.req('GET', '/api/today')[0], 401)
            self.assertEqual(self.req('GET', '/api/ext/inbox/echo')[0], 401)
            self.assertEqual(self.req('GET', '/today.js')[0], 200)
        finally:
            self.srv.token = None


if __name__ == '__main__':
    unittest.main()
