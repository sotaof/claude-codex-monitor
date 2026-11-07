import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from claude_codex_monitor import collect as C, schedules as S, server
from claude_codex_monitor import schedule_evidence as E
from claude_codex_monitor.config import parse_args
from test_schedules import timestamp, write_json


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.now = timestamp('2026-11-07T12:00:00')

    def logs(self, lines):
        path = os.path.join(self.root, 'main.log')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        logs = E.ClaudeLogs(self.root)
        logs.update(self.now)
        return logs

    def test_pmset_only_exact_state_events(self):
        text = '\n'.join([
            '2026-11-06 09:00:00 +0800 Sleep                 Entering Sleep state',
            '2026-11-06 09:10:00 +0800 Wake Requests         Wake requested',
            '2026-11-06 09:20:00 +0800 Assertions            Wake due to something',
            '2026-11-06 10:00:00 +0800 DarkWake              DarkWake from Normal Sleep',
            '2026-11-06 10:30:00 +0800 Sleep                 Entering Sleep state',
            '2026-11-06 11:00:00 +0800 Wake                  Wake from Normal Sleep'])
        events = E.parse_pmset(text)
        self.assertEqual([v for _, v in events], ['Sleep', 'DarkWake', 'Sleep', 'Wake'])
        ts = lambda text: timestamp('2026-11-06T' + text + '+08:00')
        self.assertIsNone(E.asleep_at(events, ts('08:59:00')))
        self.assertTrue(E.asleep_at(events, ts('09:30:00')))
        self.assertFalse(E.asleep_at(events, ts('10:00:00')))
        self.assertTrue(E.asleep_at(events, ts('10:45:00')))
        self.assertFalse(E.asleep_at(events, ts('11:00:00')))

    def test_pmset_missing_and_malformed_unknown(self):
        self.assertEqual(E.parse_pmset('unavailable\nbad date Sleep   fake'), [])
        self.assertIsNone(E.asleep_at([], self.now))

    def test_log_timestamps_accept_compact_timezone_offsets(self):
        for text, expected in (
                ('2026-11-06 09:00:00 +0800', '2026-11-06T09:00:00+08:00'),
                ('2026-11-06T09:00:00+0800', '2026-11-06T09:00:00+08:00'),
                ('2026-11-06 09:00:00 -0530', '2026-11-06T09:00:00-05:30'),
                ('2026-11-06 09:00:00.123 +0800', '2026-11-06T09:00:00.123+08:00')):
            with self.subTest(text=text):
                self.assertEqual(E.log_timestamp(text), timestamp(expected))

    def test_dispatch_delay_and_update_parser(self):
        for text, kind in (
                ('Spawning new session for scheduled task daily { cronExpression: \'0 11 * * *\' }', 'spawn'),
                ('Delaying dispatch for daily by 713s (jitter)', 'delay'),
                ('Updated scheduled task: daily', 'updated')):
            event = E.claude_event('2026-11-06 11:00:00 [info] [CCDScheduledTasks] ' + text)
            self.assertEqual((event['task'], event['kind']), ('daily', kind))
        self.assertIsNone(E.claude_event('bad [CCDScheduledTasks] Updated scheduled task: daily'))
        self.assertIsNone(E.claude_event('2026-11-06 11:00:00 [info] Unrelated task daily'))

    def test_automatic_manual_and_missing_coverage_inference(self):
        logs = self.logs([
            '2026-11-06 10:00:00 [info] started',
            '2026-11-06 11:12:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task daily {',
            '2026-11-06 18:00:00 [info] active'])
        rule = S.Cron('0 11 * * *')
        start = timestamp('2026-11-06T11:13:00')
        self.assertEqual(logs.run_mode('daily', start, rule.previous, S.GRACE_SECONDS), ('automatic', False))
        self.assertEqual(logs.run_mode('daily', timestamp('2026-11-06T16:23:00'), rule.previous, S.GRACE_SECONDS),
                         ('manual', False))
        self.assertEqual(logs.run_mode('daily', start - 86400, rule.previous, S.GRACE_SECONDS), ('automatic', True))
        self.assertEqual(logs.run_mode('daily', start - 86400 + 3600, rule.previous, S.GRACE_SECONDS), ('manual', True))

    def test_other_task_proves_active_not_dispatch_or_enabled(self):
        logs = self.logs([
            '2026-11-06 10:00:00 [info] started',
            '2026-11-06 11:12:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task other {',
            '2026-11-06 12:00:00 [info] active'])
        result = logs.diagnose('daily', timestamp('2026-11-06T11:00:00'), S.GRACE_SECONDS)
        self.assertEqual((result['desktop_running'], result['dispatched'], result['modified']), (True, False, False))
        self.assertIsNone(result['disabled'])
        result = logs.diagnose('daily', timestamp('2026-11-05T11:00:00'), S.GRACE_SECONDS)
        self.assertIsNone(result['desktop_running'])
        self.assertIsNone(result['dispatched'])
        self.assertIsNone(result['modified'])

    def test_update_and_delay_evidence(self):
        logs = self.logs([
            '2026-11-06 11:00:00 [info] [CCDScheduledTasks] Delaying dispatch for daily by 713s (jitter)',
            '2026-11-06 11:01:00 [info] [CCDScheduledTasks] Updated scheduled task: daily'])
        result = logs.diagnose('daily', timestamp('2026-11-06T11:00:00'), S.GRACE_SECONDS)
        self.assertTrue(result['modified'])
        self.assertTrue(result['delayed'])
        self.assertIsNone(result['dispatched'])

    def test_activity_without_dispatch_and_historical_disabled_state(self):
        logs = self.logs([
            '2026-11-04 10:26:21 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-04 11:00:30 [info] [process-memory] active',
            '2026-11-06 11:00:30 [info] [process-memory] active',
            '2026-11-06 13:58:16 [info] [CCDScheduledTasks] Updated scheduled task: daily'])
        for day in ('04', '06'):
            result = logs.diagnose('daily', timestamp('2026-11-%sT11:00:00' % day), S.GRACE_SECONDS)
            self.assertTrue(result['desktop_running'])
            self.assertTrue(result['disabled'])
            self.assertFalse(result['dispatched'])
            self.assertEqual(result['last_change_at'], timestamp('2026-11-04T10:26:21'))
        after = logs.diagnose('daily', timestamp('2026-11-06T14:00:00'), S.GRACE_SECONDS)
        self.assertIsNone(after['disabled'])

    def test_cron_change_does_not_backdate_new_hours(self):
        logs = self.logs([
            "2026-11-04 19:13:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task daily { cronExpression: '0 10,19 * * *' }",
            "2026-11-06 10:13:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task daily { cronExpression: '0 10,19 * * *' }",
            '2026-11-06 11:11:17 [info] [CCDScheduledTasks] Updated scheduled task: daily',
            "2026-11-06 17:13:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task daily { cronExpression: '0 11,17 * * *' }"])
        since = timestamp('2026-11-04T14:25:00')
        segments = logs.cron_segments('daily', '0 11,17 * * *', since)
        edge = timestamp('2026-11-06T11:11:17')
        self.assertEqual(segments, [[since, edge, '0 10,19 * * *'], [edge, None, '0 11,17 * * *']])
        task = {'enabled': True, 'runs': []}
        for day, hour in (('04', '19'), ('05', '10'), ('05', '19'), ('06', '10'), ('06', '17')):
            start = timestamp('2026-11-%sT%s:13:00' % (day, hour))
            task['runs'].append({'id': str(start), 'start': start, 'status': 'ok', 'mode': 'automatic'})
        for lower, upper, rule in segments:
            S.missed_run(task, S.Cron(rule).previous, timestamp('2026-11-06T18:00:00'), lower, until=upper)
        self.assertFalse(any(r['status'] == 'miss' for r in task['runs']))

    def test_incremental_cache_and_rotation(self):
        logs = self.logs(['2026-11-06 11:00:00 [info] [CCDScheduledTasks] Updated scheduled task: daily'])
        with mock.patch('builtins.open', side_effect=AssertionError('unchanged log reopened')):
            logs.update(self.now)
        path = os.path.join(self.root, 'main.log')
        with open(path, 'a') as f:
            f.write('2026-11-06 11:01:00 [info] [CCDScheduledTasks] Delaying dispatch for daily')
        logs.update(self.now)
        self.assertEqual(len(logs.events()), 1)
        with open(path, 'a') as f:
            f.write(' by 1s\n')
        logs.update(self.now)
        self.assertEqual(len(logs.events()), 2)
        os.rename(path, path + '.old')
        with open(path, 'w') as f:
            f.write('2026-11-06 12:00:00 [info] [CCDScheduledTasks] Updated scheduled task: replacement\n')
        logs.update(self.now)
        self.assertEqual([e['task'] for e in logs.events()], ['replacement'])

    def test_diagnosis_cache_and_no_pmset_during_collection(self):
        runner = mock.Mock(return_value='2026-11-06 09:00:00 +0800 Wake                  Wake from Sleep\n')
        collector = S.ScheduleCollector(self.root, codex_dir=os.path.join(self.root, 'codex'),
                                        desktop_root=os.path.join(self.root, 'desktop'),
                                        launchd_root=os.path.join(self.root, 'agents'), runner=mock.Mock(),
                                        claude_log_root=os.path.join(self.root, 'logs'), pmset_runner=runner)
        collector.collect(now=self.now)
        runner.assert_not_called()
        task, run = {'source': 'claude', 'id': 'daily'}, {'id': 'miss', 'start': self.now - 3600}
        first = collector.diagnostics.get(task, run, self.now)
        self.assertIs(first, collector.diagnostics.get(task, run, self.now + 1))
        collector.diagnostics.get(task, dict(run, id='another'), self.now + 2)
        self.assertEqual(runner.call_count, 1)
        collector.diagnostics.get(task, run, self.now + 301)
        self.assertEqual(runner.call_count, 2)

    def test_pmset_failure_returns_unknown(self):
        for exc in (OSError('unavailable'), subprocess.TimeoutExpired('pmset', 20)):
            diagnosis = E.Diagnostics(E.ClaudeLogs(self.root), S.LaunchHistory(None),
                                      mock.Mock(side_effect=exc))
            data = diagnosis.get({'source': 'claude', 'id': 'daily'}, {'id': 'miss', 'start': self.now}, self.now)
            self.assertTrue(all(v is None for v in data.values()))

    def test_pmset_command_is_read_only_and_mocked(self):
        with mock.patch.object(E.subprocess, 'run', return_value=mock.Mock(stdout='fake')) as command:
            self.assertEqual(E.pmset_log(), 'fake')
        self.assertEqual(command.call_args[0][0], ['pmset', '-g', 'log'])

    def test_launchd_history_needs_bracketing_observations(self):
        history = S.LaunchHistory(None)
        due = self.now - 60
        history.labels['daily'] = {'observations': [{'at': due - 10, 'loaded': True},
                                                    {'at': due + 10, 'loaded': True}]}
        diagnostic = E.Diagnostics(E.ClaudeLogs(None), history, mock.Mock(return_value=''))
        task, run = {'source': 'launchd', 'id': 'daily'}, {'id': 'miss', 'start': due}
        self.assertTrue(diagnostic.get(task, run, self.now)['launchd_loaded'])
        history.labels['daily']['observations'][1]['loaded'] = False
        self.assertIsNone(diagnostic.get(task, dict(run, id='changed'), self.now)['launchd_loaded'])
        history.labels['daily']['observations'] = [{'at': due - 600, 'loaded': True}]
        self.assertIsNone(diagnostic.get(task, dict(run, id='gap'), self.now)['launchd_loaded'])

    def test_launchd_observations_compact_only_continuous_intervals(self):
        history = S.LaunchHistory(None)
        for at in range(0, 4500, 45):
            history.observe('daily', {'loaded': True}, {}, at)
        observations = history.labels['daily']['observations']
        self.assertEqual(observations, [{'at': 0, 'last': 4455, 'loaded': True}])
        history.observe('daily', {'loaded': False}, {}, 4500)
        history.observe('daily', {'loaded': False}, {}, 5000)
        self.assertEqual(len(history.labels['daily']['observations']), 3)
        diagnosis = E.Diagnostics(E.ClaudeLogs(None), history, mock.Mock(return_value=''))
        task = {'source': 'launchd', 'id': 'daily'}
        self.assertTrue(diagnosis.get(task, {'id': 'within', 'start': 2000}, 5100)['launchd_loaded'])
        self.assertIsNone(diagnosis.get(task, {'id': 'gap', 'start': 4800}, 5100)['launchd_loaded'])


class MissedTriggerTests(unittest.TestCase):
    def task(self, runs=()):
        return {'id': 'daily', 'source': 'claude', 'enabled': True, 'runs': list(runs)}

    def test_each_trigger_is_independent_and_manual_does_not_cancel(self):
        cron = S.Cron('0 11 * * 1,3,5')
        task = self.task([{'id': 'manual', 'start': timestamp('2026-11-05T16:23:00'),
                           'status': 'ok', 'mode': 'manual'}])
        S.missed_run(task, cron.previous, timestamp('2026-11-07T12:00:00'), timestamp('2026-11-04T00:00:00'))
        self.assertEqual([r['start'] for r in task['runs'] if r['status'] == 'miss'],
                         [timestamp('2026-11-06T11:00:00'), timestamp('2026-11-04T11:00:00')])
        self.assertEqual(S.finish_task(task)['consecutive_misses'], 2)

    def test_manual_inside_grace_does_not_fill_trigger(self):
        task = self.task([{'id': 'manual', 'start': timestamp('2026-11-06T11:01:00'),
                           'status': 'ok', 'mode': 'manual'}])
        S.missed_run(task, S.Cron('0 11 * * *').previous, timestamp('2026-11-06T12:00:00'),
                     timestamp('2026-11-06T10:00:00'))
        self.assertEqual(task['runs'][-1]['status'], 'miss')

    def test_automatic_grace_boundary_and_late_automatic(self):
        due = timestamp('2026-11-06T11:00:00')
        for offset, missed in ((0, False), (1800, False), (1801, True)):
            task = self.task([{'id': 'auto', 'start': due + offset, 'status': 'ok', 'mode': 'automatic'}])
            S.missed_run(task, S.Cron('0 11 * * *').previous, due + 3600, due - 60)
            self.assertEqual(any(r['status'] == 'miss' for r in task['runs']), missed)

    def test_next_trigger_cannot_fill_previous_overlapping_window(self):
        task = self.task([{'id': 'auto', 'start': timestamp('2026-11-06T11:15:00'),
                           'status': 'ok', 'mode': 'automatic'}])
        S.missed_run(task, S.Cron('*/15 * * * *').previous, timestamp('2026-11-06T11:46:00'),
                     timestamp('2026-11-06T11:00:00'))
        self.assertEqual([r['start'] for r in task['runs'] if r['status'] == 'miss'],
                         [timestamp('2026-11-06T11:00:00')])

    def test_window_is_only_thirty_days_and_after_creation(self):
        now = timestamp('2026-11-07T12:00:00')
        for since, count in ((now - 100 * 86400, 30), (timestamp('2026-11-06T12:00:00'), 1)):
            task = self.task()
            S.missed_run(task, S.Cron('0 11 * * *').previous, now, since)
            self.assertEqual(len(task['runs']), count)
            self.assertTrue(all(r['start'] >= max(since, now - 30 * 86400) for r in task['runs']))

    def test_later_success_does_not_handle_earlier_miss(self):
        task = self.task([{'id': 'success', 'start': timestamp('2026-11-06T11:12:00'),
                           'status': 'ok', 'mode': 'automatic'}])
        S.missed_run(task, S.Cron('0 11 * * 1,3,5').previous, timestamp('2026-11-07T12:00:00'),
                     timestamp('2026-11-04T10:00:00'))
        self.assertEqual([r['start'] for r in task['runs'] if r['status'] == 'miss'],
                         [timestamp('2026-11-04T11:00:00')])
        self.assertFalse(task['runs'][-1].get('handled', False))

    def test_high_frequency_checks_only_visible_history(self):
        now = timestamp('2026-11-07T12:00:00')
        task = self.task()
        previous = mock.Mock(side_effect=lambda ts: ts // 60 * 60)
        S.missed_run(task, previous, now, now - 100 * 86400)
        self.assertEqual(len(task['runs']), S.HISTORY_LIMIT)
        self.assertLessEqual(previous.call_count, S.HISTORY_LIMIT + 1)


class HandlingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = parse_args(['--demo', '--summary', 'off', '--state-dir', self.tmp.name])
        self.mon = server.Monitor(cfg)
        self.row = {'key': 'claude:session', 'msg_ts': 500, 'last_ts': 500, 'dismissed': False,
                    'reappeared': False, 'items': [{'text': 'Approve deployment?'}]}
        self.mon.snapshot = {'rows': [self.row]}
        self.task = {'id': 'daily', 'source': 'claude', 'enabled': True, 'runs': [
            {'id': 'latest', 'start': 400, 'end': 500, 'msg_ts': 500, 'status': 'stuck', 'session_key': 'claude:session'},
            {'id': 'old', 'start': 200, 'end': 300, 'msg_ts': 300, 'status': 'fail', 'session_key': None},
            {'id': 'miss', 'start': 100, 'end': None, 'status': 'miss', 'session_key': None},
            {'id': 'success', 'start': 50, 'end': 80, 'status': 'ok', 'session_key': None}]}
        self.mon.schedule_snapshot = {'ready': True, 'tasks': [self.task], 'services': []}
        self.mon.update_schedule_state()

    def key(self, index=0):
        return self.mon.schedule_key(self.task, self.task['runs'][index])

    def test_run_dismisses_session_and_restore_both(self):
        key = self.key()
        self.mon.set_dismissed([key], True)
        self.assertTrue(self.row['dismissed'])
        self.assertTrue(self.task['runs'][0]['handled'])
        self.assertEqual(self.task['group'], 'ok')
        self.assertEqual(sum(t['group'] == 'fail' for t in self.mon.schedule_snapshot['tasks']), 0)
        self.mon.set_dismissed([key], False)
        self.assertFalse(self.row['dismissed'])
        self.assertFalse(self.task['runs'][0]['handled'])
        self.assertEqual(self.task['group'], 'fail')

    def test_session_dismisses_run_and_restore_both(self):
        self.mon.set_dismissed([self.row['key']], True)
        self.assertTrue(self.task['runs'][0]['handled'])
        self.mon.set_dismissed([self.row['key']], False)
        self.assertFalse(self.task['runs'][0]['handled'])

    def test_codex_bidirectional_link(self):
        self.task['source'] = 'codex'
        self.row['key'] = self.task['runs'][0]['session_key'] = 'codex:session'
        self.test_run_dismisses_session_and_restore_both()
        self.test_session_dismisses_run_and_restore_both()

    def test_new_message_invalidates_both_directions(self):
        for key in (self.key(), self.row['key']):
            self.mon.set_dismissed([key], True)
            self.row['msg_ts'] += 1
            self.mon.update_schedule_state()
            self.assertFalse(self.task['runs'][0]['handled'])
            self.assertEqual(self.mon.dismiss_state(self.row['key'], self.row['msg_ts']), (False, True))
            self.assertEqual(self.task['group'], 'fail')

    def test_ignore_miss_persists_and_can_undo(self):
        self.mon.set_dismissed([self.key(2)], True)
        self.assertTrue(self.task['runs'][2]['handled'])
        with open(self.mon.dismiss_path) as f:
            saved = json.load(f)
        self.assertEqual(set(saved[self.key(2)]), {'at', 'msg_ts', 'session_key'})
        restored = server.Monitor(self.mon.cfg)
        restored.schedule_snapshot = self.mon.schedule_snapshot
        restored.update_schedule_state()
        self.assertTrue(restored.schedule_snapshot['tasks'][0]['runs'][2]['handled'])
        self.mon.set_dismissed([self.key(2)], False)
        self.assertFalse(self.task['runs'][2]['handled'])

    def test_bulk_excludes_latest_success_and_already_handled(self):
        self.mon.set_dismissed([self.key(1)], True)
        keys = self.mon.schedule_previous('claude', 'daily')
        self.assertEqual(keys, [self.key(2)])
        self.assertFalse(self.task['runs'][0]['handled'])
        self.assertFalse(self.task['runs'][3]['handled'])
        self.mon.set_dismissed(keys, False)
        self.assertTrue(self.task['runs'][1]['handled'])
        self.assertFalse(self.task['runs'][2]['handled'])

    def test_bulk_only_handles_previous_attention_results(self):
        for status in ('ok', 'run', 'unknown', 'stuck'):
            self.task['runs'].append({'id': status, 'start': 10, 'end': 20, 'status': status})
        keys = self.mon.schedule_previous('claude', 'daily')
        self.assertEqual(keys, [self.key(1), self.key(2), self.key(7)])
        for index, run in enumerate(self.task['runs']):
            self.assertEqual(run['handled'], index in (1, 2, 7))
            if index not in (1, 2, 7):
                self.assertNotIn(self.key(index), self.mon.dismissed)

    def test_bulk_linked_old_run_and_persistent_per_row_undo(self):
        old = dict(self.row, key='claude:old-session', msg_ts=300)
        self.mon.snapshot['rows'].append(old)
        self.task['runs'][1]['session_key'] = old['key']
        keys = self.mon.schedule_previous('claude', 'daily')
        self.assertTrue(old['dismissed'])
        self.assertFalse(self.row['dismissed'])
        self.mon.set_dismissed([self.key(1)], False)
        self.assertFalse(old['dismissed'])
        self.assertTrue(self.task['runs'][2]['handled'])
        self.assertIn(self.key(1), keys)

    def test_unknown_run_does_not_write_arbitrary_key(self):
        self.mon.set_dismissed(['schedule:made-up'], True)
        self.assertEqual(self.mon.dismissed, {})
        self.assertIsNone(self.mon.schedule_previous('claude', 'missing'))

    def test_original_result_is_unchanged(self):
        self.mon.set_dismissed([self.key()], True)
        self.assertEqual(self.task['runs'][0]['status'], 'stuck')
        self.assertEqual(self.task['runs'][0]['items'], self.row['items'])

    def test_reply_needs_execution_and_is_reversible(self):
        self.task['runs'][0].update(replied_at=450, reply_continued=False)
        self.mon.update_schedule_state()
        self.assertFalse(self.task['runs'][0]['handled'])
        self.task['runs'][0]['reply_continued'] = True
        self.mon.update_schedule_state()
        self.assertTrue(self.task['runs'][0]['handled'])
        self.assertEqual(self.task['runs'][0]['handled_reason'], 'replied')
        self.mon.set_dismissed([self.key()], False)
        self.assertFalse(self.task['runs'][0]['handled'])

    def test_non_attention_results_are_not_handled_by_session_reply(self):
        run = self.task['runs'][0]
        self.row.update(scheduled_reply_ts=450, scheduled_reply_continued=True)
        for source in ('claude', 'codex'):
            self.task['source'] = source
            self.row['key'] = run['session_key'] = source + ':session'
            for status, group in (('ok', 'ok'), ('run', 'run'), ('unknown', 'ok')):
                with self.subTest(source=source, status=status):
                    run.update(status=status, handled=True, handled_reason='replied')
                    self.mon.update_schedule_state()
                    self.assertFalse(run['handled'])
                    self.assertIsNone(run['handled_reason'])
                    self.assertEqual(self.task['group'], group)

    def test_non_attention_results_are_not_handled_by_archived_reply(self):
        run = self.task['runs'][0]
        self.mon.snapshot['rows'] = []
        run.update(replied_at=450, reply_continued=True)
        for status in ('ok', 'run', 'unknown'):
            with self.subTest(status=status):
                run['status'] = status
                self.mon.update_schedule_state()
                self.assertFalse(run['handled'])
                self.assertIsNone(run['handled_reason'])

    def test_non_attention_results_are_not_handled_by_session_dismissal(self):
        run = self.task['runs'][0]
        for status in ('ok', 'run', 'unknown'):
            with self.subTest(status=status):
                run['status'] = status
                self.mon.set_dismissed([self.row['key']], True)
                self.assertTrue(self.row['dismissed'])
                self.assertFalse(run['handled'])
                self.assertIsNone(run['handled_reason'])
                self.assertEqual(run['status'], status)

    def test_non_attention_results_ignore_saved_run_handling_without_rewriting_it(self):
        run = self.task['runs'][0]
        self.mon.set_dismissed([self.key()], True)
        saved = dict(self.mon.dismissed)
        with open(self.mon.dismiss_path) as f:
            saved_text = f.read()
        for status in ('ok', 'run', 'unknown'):
            with self.subTest(status=status):
                run['status'] = status
                self.mon.update_schedule_state()
                self.assertFalse(run['handled'])
                self.assertIsNone(run['handled_reason'])
                self.assertEqual(self.mon.dismissed, saved)
                with open(self.mon.dismiss_path) as f:
                    self.assertEqual(f.read(), saved_text)
        run['status'] = 'stuck'
        self.mon.update_schedule_state()
        self.assertTrue(run['handled'])

    def test_attention_results_are_still_handled_by_session_reply(self):
        run = self.task['runs'][0]
        self.row.update(scheduled_reply_ts=450, scheduled_reply_continued=True)
        for status in ('fail', 'stuck', 'miss'):
            with self.subTest(status=status):
                run['status'] = status
                self.mon.update_schedule_state()
                self.assertTrue(run['handled'])
                self.assertEqual(run['handled_reason'], 'replied')
                self.assertEqual(self.task['group'], 'ok')

    def test_old_reply_does_not_override_reappearing_session(self):
        self.task['runs'][0].update(replied_at=450, reply_continued=True)
        self.mon.set_dismissed([self.key()], True)
        self.row['msg_ts'] = 600
        self.mon.update_schedule_state()
        self.assertFalse(self.task['runs'][0]['handled'])
        self.row.update(scheduled_reply_ts=650, scheduled_reply_continued=True, msg_ts=700)
        self.mon.update_schedule_state()
        self.assertTrue(self.task['runs'][0]['handled'])

    def test_undo_automatic_handling_without_session(self):
        self.task['runs'][1].update(replied_at=250, reply_continued=True)
        self.mon.update_schedule_state()
        self.assertTrue(self.task['runs'][1]['handled'])
        self.mon.set_dismissed([self.key(1)], False)
        self.assertFalse(self.task['runs'][1]['handled'])

    def test_existing_session_dictionary_format_is_preserved(self):
        write_json(self.mon.dismiss_path, {'claude:session': {'at': 100, 'msg_ts': 500}})
        mon = server.Monitor(self.mon.cfg)
        mon.snapshot = self.mon.snapshot
        mon.schedule_snapshot = self.mon.schedule_snapshot
        mon.update_schedule_state()
        self.assertTrue(self.task['runs'][0]['handled'])
        self.assertEqual(mon.dismissed['claude:session'], {'at': 100, 'msg_ts': 500})

    def test_dismissal_file_is_private(self):
        self.mon.set_dismissed([self.key()], True)
        self.assertEqual(os.stat(self.mon.dismiss_path).st_mode & 0o777, 0o600)

    def test_post_routes_bulk_dismiss_and_restore_without_ports(self):
        def post(path, body, origin=None):
            handler = object.__new__(server.Handler)
            raw = json.dumps(body).encode()
            handler.server = mock.Mock(mon=self.mon, token=None, server_address=('127.0.0.1', 8769))
            handler.path = path
            handler.headers = {'Host': '127.0.0.1:8769', 'Content-Type': 'application/json',
                               'Content-Length': str(len(raw))}
            if origin:
                handler.headers['Origin'] = origin
            handler.rfile, handler._send = io.BytesIO(raw), mock.Mock()
            handler.do_POST()
            return handler._send.call_args[0]
        self.assertEqual(post('/api/dismiss', {'keys': [self.key()]})[0], 200)
        self.assertTrue(self.row['dismissed'])
        self.assertEqual(post('/api/restore', {'keys': [self.key()]})[0], 200)
        code, result = post('/api/schedules/previous', {'source': 'claude', 'id': 'daily'})
        self.assertEqual((code, len(result['keys'])), (200, 2))
        self.assertEqual(post('/api/schedules/previous', {'source': 'claude', 'id': 'missing'})[0], 404)
        self.assertEqual(post('/api/schedules/previous', {'source': 'claude', 'id': 'daily'},
                              'https://evil.example')[0], 403)

    def test_diagnosis_route_is_authenticated_lazy_and_cached(self):
        runner = mock.Mock(return_value='')
        self.mon.cfg.demo = False
        self.mon.schedule_collector = mock.Mock(diagnostics=E.Diagnostics(
            E.ClaudeLogs(self.tmp.name), S.LaunchHistory(None), runner))
        self.task['runs'][0]['status'] = 'miss'
        def get(path, token='secret'):
            handler = object.__new__(server.Handler)
            handler.server = mock.Mock(mon=self.mon, token='secret', token_file='fake/token',
                                       server_address=('127.0.0.1', 8769))
            handler.path = path
            handler.headers = {'Host': '127.0.0.1:8769', 'Cookie': 'ccm_8769=' + token}
            handler._send = mock.Mock()
            handler.do_GET()
            return handler._send.call_args[0]
        url = '/api/schedules/claude/daily/runs/latest/diagnosis'
        self.assertEqual(get('/api/schedules')[0], 200)
        self.assertEqual(get(url, 'wrong')[0], 401)
        runner.assert_not_called()
        self.assertEqual(get(url)[0], 200)
        self.assertEqual(get(url)[0], 200)
        runner.assert_called_once()
        self.assertEqual(get('/api/schedules/claude/daily/runs/missing/diagnosis')[0], 404)


class ReplyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.session = C.ClaudeSession()

    def feed(self, timestamp, kind, content, **extra):
        data = {'timestamp': timestamp, 'type': kind, 'message': {'content': content}}
        data.update(extra)
        self.session.feed(data, json.dumps(data).encode())

    def test_claude_human_after_original_end_not_latest_desktop_frame(self):
        self.feed('2026-11-06T11:00:00Z', 'user', '<scheduled-task name="daily">fake</scheduled-task>')
        self.feed('2026-11-06T11:10:00Z', 'assistant', [{'type': 'text', 'text': 'Approve deployment?'}],
                  message={'content': [{'type': 'text', 'text': 'Approve deployment?'}], 'stop_reason': 'end_turn'})
        self.feed('2026-11-06T11:20:00Z', 'user', '<task-notification>Done</task-notification>')
        self.feed('2026-11-06T11:21:00Z', 'user', 'Resume after an internal stop.', isMeta=True)
        self.feed('2026-11-06T11:22:00Z', 'user', 'This session is being continued from a previous conversation.')
        self.assertIsNone(self.session.scheduled_reply_ts)
        self.feed('2026-11-06T11:30:00Z', 'user', 'Deploy the four sites.')
        self.assertEqual(self.session.scheduled_reply_ts, '2026-11-06T11:30:00Z')
        self.assertEqual(self.session.scheduled_run_end, '2026-11-06T11:10:00Z')

    def test_codex_real_reply_not_later_heartbeat(self):
        history = S.AutomationTurns()
        def feed(second, kind, **kw):
            d = {'timestamp': '2026-11-06T11:%02d:00Z' % second, 'type': 'event_msg',
                 'payload': dict(type=kind, **kw)}
            history.feed(d, json.dumps(d).encode())
        feed(0, 'user_message', message='<heartbeat><automation_id>daily</automation_id></heartbeat>')
        feed(1, 'task_complete', last_agent_message='Approve deployment?')
        feed(2, 'task_started')
        feed(3, 'user_message', message='<heartbeat><automation_id>daily</automation_id></heartbeat>')
        self.assertNotIn('replied_at', history.records['daily'][0])
        feed(4, 'task_complete', last_agent_message='Second result.')
        feed(5, 'task_started')
        feed(6, 'user_message', message='Proceed.')
        feed(7, 'agent_message', message='Deploying.')
        self.assertTrue(history.records['daily'][0]['reply_continued'])
        self.assertTrue(history.records['daily'][1]['reply_continued'])

    def test_archived_claude_success_reply_and_dismissal_do_not_handle_run(self):
        with tempfile.TemporaryDirectory() as root:
            desktop, projects = os.path.join(root, 'desktop'), os.path.join(root, 'projects')
            folder = os.path.join(desktop, 'account', 'workspace')
            start = timestamp('2026-11-06T11:00:00+00:00')
            write_json(os.path.join(folder, 'scheduled-tasks.json'), {'scheduledTasks': [
                {'id': 'daily', 'enabled': True, 'cronExpression': '0 11 * * *', 'createdAt': start}]})
            write_json(os.path.join(folder, 'local_fake.json'), {
                'scheduledTaskId': 'daily', 'sessionId': 'local_fake', 'cliSessionId': 'archived-cli',
                'createdAt': start, 'lastTurnEdgeAt': start + 1800})
            path = os.path.join(projects, 'fake-project', 'archived-cli.jsonl')
            os.makedirs(os.path.dirname(path))
            with open(path, 'w') as f:
                for at, kind, content, stop in (
                        ('11:00:00', 'user', '<scheduled-task name="daily">Fake</scheduled-task>', None),
                        ('11:10:00', 'assistant', [{'type': 'text', 'text': 'Continue?'}], 'end_turn'),
                        ('11:20:00', 'user', 'Proceed.', None),
                        ('11:30:00', 'assistant', [{'type': 'text', 'text': 'Completed.'}], 'end_turn')):
                    f.write(json.dumps({'timestamp': '2026-11-06T' + at + 'Z', 'type': kind,
                                        'message': {'content': content, 'stop_reason': stop}}) + '\n')
            collector = S.ScheduleCollector(os.path.join(root, 'state'),
                                            codex_dir=os.path.join(root, 'codex'), desktop_root=desktop,
                                            launchd_root=os.path.join(root, 'agents'),
                                            claude_log_root=os.path.join(root, 'logs'), runner=mock.Mock(),
                                            pmset_runner=mock.Mock(side_effect=AssertionError('not on demand')))
            with mock.patch.object(C, 'CLAUDE_PROJECTS', projects):
                snapshot = collector.collect(rows=[], now=start + 3600)
            run = next(r for r in snapshot['tasks'][0]['runs'] if r['id'] == 'local_fake')
            self.assertEqual(run['session_key'], 'claude:archived-cli')
            self.assertTrue(run['reply_continued'])
            cfg = parse_args(['--demo', '--summary', 'off', '--state-dir', os.path.join(root, 'state')])
            mon = server.Monitor(cfg)
            mon.schedule_snapshot = snapshot
            mon.update_schedule_state()
            self.assertEqual(mon.snapshot['rows'], [])
            self.assertEqual(run['status'], 'ok')
            self.assertFalse(run['handled'])
            self.assertIsNone(run['handled_reason'])
            mon.set_dismissed([run['dismiss_key']], True)
            self.assertIn('claude:archived-cli', mon.dismissed)
            self.assertFalse(run['handled'])
            self.assertIsNone(run['handled_reason'])
            self.assertEqual(mon.snapshot['rows'], [])


if __name__ == '__main__':
    unittest.main()
