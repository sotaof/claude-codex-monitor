import contextlib
import io
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from claude_codex_monitor import collect as C
from claude_codex_monitor import demo
from claude_codex_monitor import server as S
from claude_codex_monitor.config import parse_args


class StackDump(unittest.TestCase):
    def test_registers_usr1(self):
        signals = types.SimpleNamespace(SIGUSR1=object(), signal=mock.Mock())
        with mock.patch.object(S, 'signal', signals):
            S.register_stack_dump()
        signals.signal.assert_called_once_with(signals.SIGUSR1, S.dump_stacks)

    def test_skips_platform_without_usr1(self):
        signals = types.SimpleNamespace(signal=mock.Mock())
        with mock.patch.object(S, 'signal', signals):
            S.register_stack_dump()
        signals.signal.assert_not_called()

    def test_header_precedes_dump_and_flushes(self):
        err = mock.Mock(wraps=io.StringIO())
        with mock.patch.object(S.sys, 'stderr', err), \
                mock.patch.object(S.dt, 'datetime') as clock, \
                mock.patch.object(S.os, 'getpid', return_value=42484), \
                mock.patch.object(S.faulthandler, 'dump_traceback') as dump:
            clock.now.return_value.strftime.return_value = '2026-11-07 12:40:01'
            dump.side_effect = lambda **kwargs: self.assertEqual(
                err.getvalue(), '--- stack dump 2026-11-07 12:40:01 (pid 42484) ---\n')
            S.dump_stacks(None, None)
        clock.now.return_value.strftime.assert_called_once_with('%Y-%m-%d %H:%M:%S')
        dump.assert_called_once_with(file=err, all_threads=True)
        self.assertEqual(err.flush.call_count, 2)

    def test_dump_contains_real_thread_stacks(self):
        stop = threading.Event()
        entered = threading.Event()

        def wait_for_stack_dump():
            entered.set()
            stop.wait()

        worker = threading.Thread(target=wait_for_stack_dump)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as err, contextlib.redirect_stderr(err):
                S.dump_stacks(None, None)
                err.seek(0)
                text = err.read()
        finally:
            stop.set()
            worker.join(timeout=5)
        self.assertRegex(text.splitlines()[0], r'^--- stack dump \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(pid %d\) ---$'
                         % os.getpid())
        self.assertIn('Thread', text)
        self.assertIn('Current thread', text)
        self.assertIn('wait_for_stack_dump', text)
        self.assertIn('test_dump_contains_real_thread_stacks', text)

    def test_main_registers_in_demo_mode_before_setup(self):
        cfg = parse_args(['--demo', '--no-open', '--summary', 'off'])
        with mock.patch.object(S, 'register_stack_dump') as register, \
                mock.patch.object(S, 'parse_args', return_value=cfg), \
                mock.patch.object(S, 'use_demo') as setup, \
                mock.patch.object(S, 'Monitor') as monitor, \
                mock.patch.object(S.http.server, 'ThreadingHTTPServer'), \
                mock.patch.object(S.threading, 'Thread'), \
                mock.patch.object(S.signal, 'signal'), \
                mock.patch.object(S.os, 'umask'), \
                mock.patch.object(S.C, 'configure'), \
                mock.patch.object(S.time, 'monotonic', return_value=100.0), \
                contextlib.redirect_stdout(io.StringIO()):
            setup.side_effect = lambda cfg: register.assert_called_once_with()
            S.main(['--demo'])
        register.assert_called_once_with()
        setup.assert_called_once_with(cfg)
        monitor.assert_called_once_with(cfg, started_at=100.0)


class ReadyLog(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        cd, xd = demo.build(os.path.join(root.name, 'data'), time.time())
        self.cfg = parse_args(['--demo', '--summary', 'off', '--no-desktop',
                               '--claude-dir', cd, '--codex-dir', xd,
                               '--state-dir', os.path.join(root.name, 'state')])
        C.configure(cd, xd, self.cfg.days)
        self.mon = S.Monitor(self.cfg, started_at=100.0)

    def test_callbacks_log_once_per_snapshot_type(self):
        self.mon.snapshot = {'ready': True, 'rows': [{}] * 148}
        self.mon.schedule_snapshot = {'ready': True, 'tasks': [{}] * 17}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                S.time, 'monotonic', side_effect=[126.3, 131.0]) as clock:
            self.mon.log_ready()
            self.mon.log_ready()
            self.mon.log_ready(scheduled=True)
            self.mon.log_ready(scheduled=True)
        self.assertEqual(out.getvalue().splitlines(), [
            'ready after 26.3s (sessions: 148)', 'schedules ready after 31.0s (tasks: 17)'])
        self.assertEqual(clock.call_count, 2)

    def test_unready_callbacks_do_not_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mon.log_ready()
            self.mon.log_ready(scheduled=True)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(self.mon.ready_logged, set())

    def test_empty_snapshots_log_and_flush(self):
        self.mon.snapshot['ready'] = True
        self.mon.schedule_snapshot['ready'] = True
        out = mock.Mock(wraps=io.StringIO())
        with mock.patch.object(S.sys, 'stdout', out), mock.patch.object(S.time, 'monotonic', return_value=100.0):
            self.mon.log_ready()
            self.mon.log_ready(scheduled=True)
        self.assertEqual(out.getvalue().splitlines(), [
            'ready after 0.0s (sessions: 0)', 'schedules ready after 0.0s (tasks: 0)'])
        self.assertEqual(out.flush.call_count, 2)

    def test_session_scans_log_once_before_housekeeping(self):
        out = io.StringIO()

        def housekeep(*args):
            self.assertTrue(self.mon.snapshot['ready'])
            self.assertEqual(out.getvalue().splitlines(), ['ready after 26.3s (sessions: 11)'])

        with contextlib.redirect_stdout(out), mock.patch.object(S.time, 'monotonic', return_value=126.3), \
                mock.patch.object(self.mon, 'housekeep', side_effect=housekeep):
            self.mon.scan()
            self.mon.scan()
        self.assertEqual(out.getvalue().splitlines(), ['ready after 26.3s (sessions: 11)'])

    def test_demo_schedule_scans_log_once(self):
        tasks = len(demo.schedules(time.time())['tasks'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(S.time, 'monotonic', return_value=131.0):
            self.mon.scan_schedules()
            self.mon.scan_schedules()
        self.assertEqual(out.getvalue().splitlines(), ['schedules ready after 31.0s (tasks: %d)' % tasks])

    def test_collected_schedule_scans_log_once(self):
        self.mon.cfg.demo = False
        snapshot = demo.schedules(time.time())
        self.mon.schedule_collector = mock.Mock()
        self.mon.schedule_collector.collect.return_value = snapshot
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(S.time, 'monotonic', return_value=131.0):
            self.mon.scan_schedules()
            self.mon.scan_schedules()
        self.assertEqual(out.getvalue().splitlines(), [
            'schedules ready after 31.0s (tasks: %d)' % len(snapshot['tasks'])])

    def test_failed_scan_can_retry_without_false_ready_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                S.C, 'live_claude_sessions', side_effect=RuntimeError('fake scan failure')):
            with self.assertRaisesRegex(RuntimeError, 'fake scan failure'):
                self.mon.scan()
        self.assertEqual(out.getvalue(), '')
        self.assertNotIn('sessions', self.mon.ready_logged)
        with contextlib.redirect_stdout(out), mock.patch.object(S.time, 'monotonic', return_value=126.3):
            self.mon.scan()
        self.assertEqual(out.getvalue().splitlines(), ['ready after 26.3s (sessions: 11)'])

    def test_default_timer_starts_before_monitor_setup(self):
        with mock.patch.object(S.time, 'monotonic', return_value=200.0), \
                mock.patch.object(S, 'secure_dir', side_effect=lambda path: self.assertEqual(
                    S.time.monotonic.call_count, 1)):
            mon = S.Monitor(self.cfg)
        self.assertEqual(mon.started_at, 200.0)


if __name__ == '__main__':
    unittest.main()
