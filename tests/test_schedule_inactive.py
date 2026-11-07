import os
import plistlib
import tempfile
import unittest
from unittest import mock

from claude_codex_monitor import schedule_evidence as E, schedules as S
from test_schedules import timestamp, write_json


class InactiveClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.now = timestamp('2026-11-07T12:00:00')
        self.since = timestamp('2026-11-04T00:00:00')

    def logs(self, lines):
        with open(os.path.join(self.root, 'main.log'), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        logs = E.ClaudeLogs(self.root)
        logs.update(self.now)
        return logs

    def task(self, logs, **changes):
        config = {'id': 'daily', 'enabled': True, 'cronExpression': '0 11 * * 1,3,5',
                  'createdAt': self.since * 1000}
        config.update(changes)
        write_json(os.path.join(self.root, 'account', 'workspace', 'scheduled-tasks.json'),
                   {'scheduledTasks': [config]})
        return S.read_claude(self.root, {}, self.now, logs=logs)[0]

    def pause_logs(self, resume='Updated'):
        return self.logs([
            '2026-11-04 10:26:21 [info] [CCDScheduledTasksApi] updateScheduledTaskStatus: scheduledTaskId=daily, status=disabled, removeFiles=undefined',
            '2026-11-04 10:26:21 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-06 13:58:16 [info] [CCDScheduledTasks] %s scheduled task: daily' % resume])

    def test_both_explicit_status_formats(self):
        for prefix, text in (
                ('[CCDScheduledTasksApi]', 'updateScheduledTaskStatus: scheduledTaskId=daily, status=%s, removeFiles=undefined'),
                ('[CCDScheduledTasks]', '%s scheduled task: daily')):
            for state in ('disabled', 'enabled'):
                spelling = state if 'Api' in prefix else state.title()
                event = E.claude_event('2026-11-06 11:00:00 [info] ' + prefix + ' ' + text % spelling)
                self.assertEqual((event['task'], event['kind']), ('daily', state))

    def test_explicit_enable_excludes_both_disabled_triggers(self):
        for resume in ('Enabled', 'API'):
            with self.subTest(resume=resume):
                if resume == 'API':
                    line = '[CCDScheduledTasksApi] updateScheduledTaskStatus: scheduledTaskId=daily, status=enabled'
                else:
                    line = '[CCDScheduledTasks] Enabled scheduled task: daily'
                logs = self.logs([
                    '2026-11-04 10:26:21 [info] [CCDScheduledTasksApi] updateScheduledTaskStatus: scheduledTaskId=daily, status=disabled',
                    '2026-11-06 13:58:16 [info] ' + line])
                task = self.task(logs)
                self.assertEqual((task['group'], task['runs'], task['consecutive_misses']), ('ok', [], 0))
                self.assertEqual(len(task['disabled_periods']), 1)
                period = task['disabled_periods'][0]
                self.assertEqual((period['start'], period['end']),
                                 (timestamp('2026-11-04T10:26:21'), timestamp('2026-11-06T13:58:16')))
                self.assertFalse(period['inferred'])
                self.assertFalse(period['end_inferred'])

    def test_updated_and_saved_infer_resume_once(self):
        for change in ('Updated', 'Saved'):
            with self.subTest(change=change):
                task = self.task(self.pause_logs(change))
                self.assertEqual((task['group'], task['runs']), ('ok', []))
                self.assertEqual(len(task['disabled_periods']), 1)
                self.assertTrue(task['disabled_periods'][0]['end_inferred'])
                self.assertTrue(task['state_periods'][-1]['enabled'])
                self.assertTrue(task['state_periods'][-1]['inferred'])

    def test_updated_does_not_enable_currently_disabled_task(self):
        task = self.task(self.pause_logs(), enabled=False)
        self.assertEqual((task['group'], task['runs']), ('off', []))
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertIsNone(task['disabled_periods'][0]['end'])
        self.assertFalse(E.period_at(task['state_periods'], timestamp('2026-11-04T11:00:00'))['inferred'])

    def test_inferred_initial_pause_and_explicit_disable_share_one_note(self):
        task = self.task(self.pause_logs(), enabled=False,
                         createdAt=timestamp('2026-11-02T00:00:00') * 1000)
        self.assertEqual(task['runs'], [])
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertTrue(task['disabled_periods'][0]['inferred'])
        self.assertEqual(task['disabled_periods'][0]['start'], timestamp('2026-11-02T00:00:00'))

    def test_unknown_initial_state_uses_current_config_and_is_inferred(self):
        logs = self.logs(['2026-11-06 13:58:16 [info] active'])
        for enabled in (True, False):
            task = self.task(logs, enabled=enabled)
            period = task['state_periods'][0]
            self.assertEqual((period['enabled'], period['inferred'], period['reason']),
                             (enabled, True, 'current_config'))
            if enabled:
                self.assertEqual(len(task['runs']), 2)
                self.assertTrue(all(r['state_inferred'] and r['state_unconfirmed'] for r in task['runs']))
            else:
                self.assertEqual(task['runs'], [])
                self.assertEqual(len(task['disabled_periods']), 1)

    def test_real_misses_before_and_after_pause_are_preserved(self):
        self.now = timestamp('2026-11-09T12:00:00')
        task = self.task(self.pause_logs(), createdAt=timestamp('2026-11-02T00:00:00') * 1000)
        self.assertEqual([r['start'] for r in task['runs']],
                         [timestamp('2026-11-09T11:00:00'), timestamp('2026-11-02T11:00:00')])
        self.assertTrue(all(r['status'] == 'miss' for r in task['runs']))
        self.assertEqual((task['group'], task['consecutive_misses']), ('fail', 2))
        self.assertEqual(len(task['disabled_periods']), 1)

    def test_miss_before_pause_is_preserved_when_currently_disabled(self):
        task = self.task(self.logs([
            '2026-11-02 10:00:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily',
            '2026-11-04 10:26:21 [info] [CCDScheduledTasks] Disabled scheduled task: daily']),
            enabled=False, createdAt=timestamp('2026-11-02T00:00:00') * 1000)
        self.assertEqual([r['start'] for r in task['runs']], [timestamp('2026-11-02T11:00:00')])
        self.assertEqual(task['group'], 'off')

    def test_disabled_boundary_included_enabled_boundary_excluded(self):
        task = self.task(self.logs([
            '2026-11-04 11:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-06 11:00:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily']))
        self.assertEqual([r['start'] for r in task['runs']], [timestamp('2026-11-06T11:00:00')])
        self.assertEqual(len(task['disabled_periods']), 1)

    def test_separate_pauses_stay_separate(self):
        self.now = timestamp('2026-11-09T12:00:00')
        task = self.task(self.logs([
            '2026-11-04 10:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-04 12:00:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily',
            '2026-11-06 10:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-06 12:00:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily']))
        self.assertEqual(len(task['disabled_periods']), 2)
        self.assertEqual([r['start'] for r in task['runs']], [timestamp('2026-11-09T11:00:00')])

    def test_manual_execution_during_pause_is_not_removed_or_replaced_as_last_run(self):
        logs = self.pause_logs()
        start = timestamp('2026-11-05T16:23:00')
        write_json(os.path.join(self.root, 'account', 'workspace', 'local_fake.json'),
                   {'scheduledTaskId': 'daily', 'sessionId': 'manual', 'createdAt': start * 1000,
                    'lastActivityAt': (start + 60) * 1000})
        task = self.task(logs)
        self.assertEqual((task['group'], task['last_run'], task['consecutive_misses']), ('ok', start, 0))
        self.assertEqual([(r['id'], r['mode']) for r in task['runs']], [('manual', 'manual')])
        self.assertEqual(len(task['disabled_periods']), 1)

    def test_pause_does_not_consume_high_frequency_history_budget(self):
        logs = self.logs([
            '2026-11-07 10:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-07 11:30:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily'])
        since = timestamp('2026-11-07T09:00:00')
        task = {'enabled': True, 'runs': [],
                'state_periods': logs.state_periods('daily', True, since, self.now)}
        previous = mock.Mock(side_effect=lambda ts: ts // 60 * 60)
        S.missed_run(task, previous, self.now, since)
        S.finish_task(task)
        self.assertEqual(len(task['runs']), S.HISTORY_LIMIT)
        self.assertEqual(task['runs'][0]['start'], timestamp('2026-11-07T11:30:00'))
        self.assertEqual(task['runs'][1]['start'], timestamp('2026-11-07T09:59:00'))
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertLessEqual(previous.call_count, S.HISTORY_LIMIT + 2)

    def test_pause_deduplicates_across_schedule_segments(self):
        logs = self.logs([
            "2026-11-02 11:01:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task daily { cronExpression: '0 11 * * *' }",
            '2026-11-04 10:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily',
            '2026-11-05 10:00:00 [info] [CCDScheduledTasks] Updated scheduled task: daily',
            '2026-11-06 10:00:00 [info] [CCDScheduledTasks] Enabled scheduled task: daily'])
        task = self.task(logs, cronExpression='0 12 * * *')
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertTrue(all(r['start'] != timestamp('2026-11-04T11:00:00') for r in task['runs']))

    def test_unreadable_logs_preserve_misses_and_mark_history_unavailable(self):
        logs = self.logs(['2026-11-06 13:58:16 [info] active'])
        with mock.patch('builtins.open', side_effect=PermissionError('fake denied')):
            fresh = E.ClaudeLogs(self.root)
            fresh.update(self.now)
        task = self.task(fresh)
        self.assertEqual(len(task['runs']), 2)
        self.assertTrue(task['state_history_unavailable'])
        self.assertTrue(all(r['state_unconfirmed'] for r in task['runs']))
        self.assertEqual(task['disabled_periods'], [])

    def test_state_event_before_retention_window_is_preserved_on_append(self):
        self.now = timestamp('2026-12-07T12:00:00')
        logs = self.logs(['2026-10-01 10:00:00 [info] [CCDScheduledTasks] Disabled scheduled task: daily'])
        with open(os.path.join(self.root, 'main.log'), 'a') as f:
            f.write('2026-12-07 10:00:00 [info] active\n')
        logs.update(self.now)
        task = self.task(logs)
        self.assertEqual(task['runs'], [])
        self.assertEqual(task['disabled_periods'][0]['start'], timestamp('2026-10-01T10:00:00'))

    def test_diagnosis_marks_inferred_enabled_after_update(self):
        logs = self.pause_logs()
        task = self.task(logs, cronExpression='0 14 * * *', createdAt=timestamp('2026-11-06T13:00:00') * 1000)
        run = next(r for r in task['runs'] if r['start'] == timestamp('2026-11-06T14:00:00'))
        data = E.Diagnostics(logs, S.LaunchHistory(None), mock.Mock(return_value='')).get(task, run, self.now)
        self.assertFalse(data['disabled'])
        self.assertTrue(data['state_inferred'])


class InactiveLaunchdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.path = os.path.join(self.root, 'daily.plist')
        self.history = S.LaunchHistory(None)

    def collect(self, at, loaded=True, disabled=False):
        with open(self.path, 'wb') as f:
            plistlib.dump({'Label': 'daily', 'Disabled': disabled,
                          'StartCalendarInterval': {'Hour': 11, 'Minute': 0}}, f)
        return S.read_launchd(self.root, timestamp(at), self.history,
                              runner=lambda label: {'loaded': loaded})[0][0]

    def test_unloaded_period_does_not_miss_after_loading(self):
        self.collect('2026-11-04T10:59:30', loaded=False)
        self.collect('2026-11-04T11:00:15', loaded=False)
        self.collect('2026-11-04T11:01:00')
        task = self.collect('2026-11-04T12:00:00')
        self.assertEqual((task['group'], task['runs'], task['consecutive_misses']), ('ok', [], 0))
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertEqual(task['disabled_periods'][0]['reason'], 'unloaded')

    def test_disabled_plist_is_recorded_even_when_loaded(self):
        self.collect('2026-11-04T10:59:30', disabled=True)
        self.collect('2026-11-04T11:00:15', disabled=True)
        self.collect('2026-11-04T11:01:00')
        task = self.collect('2026-11-04T12:00:00')
        self.assertEqual((task['group'], task['runs']), ('ok', []))
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertEqual(task['disabled_periods'][0]['reason'], 'disabled')
        self.assertTrue(self.history.labels['daily']['observations'][0]['disabled'])

    def test_misses_before_and_after_unloaded_period_remain(self):
        self.collect('2026-11-03T10:00:00')
        self.collect('2026-11-04T10:59:30', loaded=False)
        self.collect('2026-11-04T11:00:15', loaded=False)
        self.collect('2026-11-04T11:01:00')
        task = self.collect('2026-11-05T12:00:00')
        self.assertEqual([r['start'] for r in task['runs']],
                         [timestamp('2026-11-05T11:00:00'), timestamp('2026-11-03T11:00:00')])
        self.assertEqual(len(task['disabled_periods']), 1)

    def test_unobserved_gap_is_not_assumed_unloaded(self):
        self.collect('2026-11-04T10:00:00', loaded=False)
        self.collect('2026-11-04T12:00:00')
        task = self.collect('2026-11-04T12:01:00')
        self.assertEqual([r['start'] for r in task['runs']], [timestamp('2026-11-04T11:00:00')])
        self.assertTrue(task['runs'][0]['state_unconfirmed'])
        self.assertEqual(task['disabled_periods'], [])

    def test_last_observation_timestamp_is_included(self):
        self.collect('2026-11-04T10:59:15', loaded=False)
        self.collect('2026-11-04T11:00:00', loaded=False)
        task = self.collect('2026-11-04T12:00:00')
        self.assertEqual(task['runs'], [])
        self.assertEqual(len(task['disabled_periods']), 1)

    def test_disabled_observation_survives_save_and_old_loaded_format(self):
        self.history = S.LaunchHistory(os.path.join(self.root, 'state'))
        self.collect('2026-11-04T10:59:30', disabled=True)
        self.collect('2026-11-04T11:00:15', disabled=True)
        self.history = S.LaunchHistory(os.path.join(self.root, 'state'))
        self.collect('2026-11-04T11:01:00')
        task = self.collect('2026-11-04T12:00:00')
        self.assertEqual(task['runs'], [])
        self.assertEqual(len(task['disabled_periods']), 1)
        self.assertEqual(task['disabled_periods'][0]['reason'], 'disabled')


if __name__ == '__main__':
    unittest.main()
