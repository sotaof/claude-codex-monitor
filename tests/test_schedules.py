import datetime as dt
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from claude_codex_monitor import collect as C
from claude_codex_monitor import demo
from claude_codex_monitor import schedules as S
from claude_codex_monitor import server
from claude_codex_monitor.config import parse_args


def timestamp(text):
    return dt.datetime.fromisoformat(text).timestamp()


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(value, f)


def row(client, sid, status='ok'):
    return {'key': client + ':' + sid, 'status': status, 'last_ts': timestamp('2026-11-06T11:10:00'),
            'line': 'Checked my-app.', 'note': None, 'items': [{'text': 'Continue with the release?'}] if status == 'wait' else []}


CALENDAR_CASES = [
    ([{'Hour': h, 'Minute': m} for h, m in ((13, 10), (16, 50), (19, 20))],
     'Daily 13:10, 16:50, 19:20', '每天 13:10、16:50、19:20'),
    ([{'Weekday': 1, 'Hour': h, 'Minute': 0} for h in (9, 17)],
     'Mon 09:00, 17:00', '每周一 09:00、17:00'),
    ([{'Weekday': d, 'Hour': 12, 'Minute': 30} for d in (1, 4)],
     'Mon, Thu 12:30', '每周一、四 12:30'),
    ([{'Weekday': d, 'Hour': 17, 'Minute': 0} for d in range(1, 6)],
     'Weekdays 17:00', '工作日 17:00'),
    ([{'Weekday': d, 'Hour': 17, 'Minute': 0} for d in range(1, 7)],
     'Mon–Sat 17:00', '周一至周六 17:00'),
    ([{'Weekday': 1, 'Hour': 12, 'Minute': 30}, {'Weekday': 4, 'Hour': 17, 'Minute': 0}],
     'Mon 12:30; Thu 17:00', '每周一 12:30；每周四 17:00'),
    ([{'Weekday': d, 'Hour': 9, 'Minute': 0} for d in (0, 1, 7)],
     'Mon, Sun 09:00', '每周一、日 09:00'),
    ([{'Weekday': d, 'Hour': 9, 'Minute': 0} for d in range(7)],
     'Daily 09:00', '每天 09:00'),
]


class CronTests(unittest.TestCase):
    def test_quarter_hour(self):
        rule = S.Cron('*/15 * * * *')
        self.assertEqual(rule.next(timestamp('2026-11-07T10:07:30')), timestamp('2026-11-07T10:15:00'))
        self.assertEqual(rule.previous(timestamp('2026-11-07T10:07:30')), timestamp('2026-11-07T10:00:00'))

    def test_lists_and_weekdays(self):
        rule = S.Cron('0 11 * * 1,3,5')
        self.assertEqual(rule.next(timestamp('2026-11-06T11:00:00')), timestamp('2026-11-09T11:00:00'))

    def test_ranges(self):
        rule = S.Cron('0 10-12 * * *')
        self.assertEqual(rule.next(timestamp('2026-11-07T10:30:00')), timestamp('2026-11-07T11:00:00'))
        self.assertEqual(S.Cron('10-12 * * * *').minute, {10, 11, 12})

    def test_sunday_zero_and_seven(self):
        for weekday in ('0', '7'):
            rule = S.Cron('0 10 * * ' + weekday)
            self.assertEqual(rule.next(timestamp('2026-11-07T12:00:00')), timestamp('2026-11-08T10:00:00'))

    def test_day_and_weekday_or(self):
        rule = S.Cron('0 10 1 * 1')
        self.assertEqual(rule.next(timestamp('2026-11-07T12:00:00')), timestamp('2026-11-09T10:00:00'))

    def test_range_step_and_month(self):
        rule = S.Cron('0 10-14/2 1 12 *')
        self.assertEqual(rule.next(timestamp('2026-11-07T12:00:00')), timestamp('2026-12-01T10:00:00'))

    def test_leap_day_and_impossible_date(self):
        self.assertEqual(S.Cron('0 0 29 2 *').next(timestamp('2025-03-01T00:00:00')),
                         timestamp('2028-02-29T00:00:00'))
        self.assertIsNone(S.Cron('0 0 31 2 *').next(timestamp('2026-11-07T00:00:00')))

    def test_invalid_rules(self):
        for rule in ('* * *', '*/0 * * * *', '60 * * * *', '* 24 * * *', '* * * * 8', '12-10 * * * *', 'a * * * *'):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                S.Cron(rule)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires tzset')
    def test_dst_skips_nonexistent_local_time(self):
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            self.assertEqual(S.Cron('30 2 * * *').next(timestamp('2026-03-08T00:00:00')),
                             timestamp('2026-03-09T02:30:00'))
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()


class RruleTests(unittest.TestCase):
    def test_daily(self):
        self.assertEqual(S.rrule_description('FREQ=DAILY;BYHOUR=13;BYMINUTE=0'), C.M('schedule.daily', '13:00'))

    def test_weekly(self):
        out = S.rrule_description('FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=16;BYMINUTE=30')
        self.assertEqual(out['m'], 'schedule.weekly')
        self.assertEqual(out['a'][1], '16:30')
        self.assertIn('schedule.day.WE', str(out))

    def test_weekdays(self):
        for freq in ('WEEKLY', 'DAILY'):
            for days in ('MO,TU,WE,TH,FR', 'FR,TH,WE,TU,MO'):
                with self.subTest(freq=freq, days=days):
                    self.assertEqual(S.rrule_description('FREQ=%s;BYDAY=%s;BYHOUR=16;BYMINUTE=0' % (freq, days)),
                                     C.M('schedule.weekdays_at', '16:00'))

    def test_all_seven_days_are_daily(self):
        for freq in ('WEEKLY', 'DAILY'):
            with self.subTest(freq=freq):
                self.assertEqual(S.rrule_description('FREQ=%s;BYDAY=SU,MO,TU,WE,TH,FR,SA;BYHOUR=9' % freq),
                                 C.M('schedule.daily', '09:00'))

    def test_weekday_interval_is_preserved(self):
        self.assertEqual(S.rrule_description('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TU,WE,TH,FR')['m'],
                         'schedule.weekly_interval')

    def test_hourly_interval(self):
        self.assertEqual(S.rrule_description('FREQ=HOURLY;INTERVAL=2;BYMINUTE=15'),
                         C.M('schedule.hourly', 2, '15'))

    def test_daily_interval(self):
        self.assertEqual(S.rrule_description('RRULE:FREQ=DAILY;INTERVAL=3;BYHOUR=9,17;BYMINUTE=5'),
                         C.M('schedule.daily_interval', 3, '09:05, 17:05'))

    def test_hour_and_weekday_constraints(self):
        description = S.rrule_description('FREQ=HOURLY;INTERVAL=2;BYHOUR=9,17;BYMINUTE=5;BYDAY=MO')
        self.assertEqual(description, C.M('schedule.on_days', C.M('schedule.hourly_at', 2, '05', '09, 17'),
                                         C.M('schedule.day.MO')))

    def test_daily_with_days_does_not_become_weekly_interval(self):
        self.assertEqual(S.rrule_description('FREQ=DAILY;INTERVAL=2;BYDAY=MO;BYHOUR=9'),
                         C.M('schedule.daily_days_interval', 2, C.M('schedule.day.MO'), '09:00'))

    def test_invalid_is_not_guessed(self):
        self.assertEqual(S.rrule_description('FREQ=MONTHLY')['m'], 'schedule.rrule')
        self.assertEqual(S.rrule_description('FREQ=DAILY;BYHOUR=99')['m'], 'schedule.rrule')


class TemporaryFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.now = timestamp('2026-11-07T10:40:00')


class ClaudeTests(TemporaryFiles):
    def config(self, tasks, account='account'):
        path = os.path.join(self.root, account, 'workspace', 'scheduled-tasks.json')
        write_json(path, {'scheduledTasks': tasks, 'recordedSkips': {}})
        return path

    def task(self, **changes):
        task = {'id': 'daily-check', 'cronExpression': '0 11 * * *', 'enabled': True,
                'createdAt': timestamp('2026-11-01T10:00:00') * 1000}
        task.update(changes)
        return task

    def local(self, name='local_fake', **changes):
        data = {'scheduledTaskId': 'daily-check', 'sessionId': name, 'cliSessionId': 'fake-cli',
                'createdAt': timestamp('2026-11-06T11:00:00') * 1000,
                'lastActivityAt': timestamp('2026-11-06T11:10:00') * 1000}
        data.update(changes)
        write_json(os.path.join(self.root, 'account', 'workspace', name + '.json'), data)

    def test_frontmatter_and_deduplicate(self):
        skill = os.path.join(self.root, 'SKILL.md')
        with open(skill, 'w') as f:
            f.write('---\nname: Daily my-app check\ndescription: >-\n  Check example.com.\n  Write a summary.\n---\n')
        self.config([self.task(filePath=skill)])
        self.config([self.task(filePath=skill)], 'another-account')
        self.local()
        tasks = S.read_claude(self.root, {}, self.now)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['title'], 'Daily my-app check')
        self.assertEqual(tasks[0]['description'], 'Check example.com. Write a summary.')
        self.assertEqual(tasks[0]['next_run'], timestamp('2026-11-07T11:00:00'))

    def test_title_overrides_name(self):
        self.config([self.task(title='Original task title')])
        self.assertEqual(S.read_claude(self.root, {}, self.now)[0]['title'], 'Original task title')

    def test_display_name_overrides_name(self):
        self.config([self.task(displayName='Shown task name')])
        self.assertEqual(S.read_claude(self.root, {}, self.now)[0]['title'], 'Shown task name')

    def test_run_association_and_statuses(self):
        self.config([self.task()])
        self.local()
        for status, expected in (('ok', 'ok'), ('fail', 'ok'), ('wait', 'stuck'), ('run', 'run')):
            data = S.read_claude(self.root, {'claude:fake-cli': row('claude', 'fake-cli', status)}, self.now)[0]
            self.assertEqual(data['runs'][0]['status'], expected)
            self.assertEqual(data['runs'][0]['session_key'], 'claude:fake-cli')

    def test_unlinked_without_error_is_success(self):
        self.config([self.task()])
        self.local()
        run = S.read_claude(self.root, {}, self.now)[0]['runs'][0]
        self.assertEqual((run['status'], run['session_key']), ('ok', None))
        self.assertEqual(run['summary'], '')

    def test_error_is_failure_and_overrides_monitor_status_and_summary(self):
        self.config([self.task()])
        errors = ("API Error: Can't reach the API server (ENOTFOUND)",
                  "Failed to authenticate. API Error: 403")
        for error in errors:
            self.local(error=error, postTurnSummary={'status_detail': 'Previously completed.'})
            for status in (None, 'ok', 'wait', 'run', 'fail'):
                with self.subTest(error=error, status=status):
                    rows = {'claude:fake-cli': row('claude', 'fake-cli', status)} if status else {}
                    run = S.read_claude(self.root, rows, self.now)[0]['runs'][0]
                    self.assertEqual((run['status'], run['summary']), ('fail', error))

    def test_cut_short_and_continued_without_error_are_success(self):
        self.config([self.task()])
        self.local(lastTurnCutShort=True, scheduledRunContinued=True)
        run = S.read_claude(self.root, {'claude:fake-cli': row('claude', 'fake-cli', 'fail')}, self.now)[0]['runs'][0]
        self.assertEqual(run['status'], 'ok')
        self.assertTrue(run['duration_unknown'])

    def test_summary_uses_status_detail_then_note_never_user_line(self):
        self.config([self.task()])
        monitor = row('claude', 'fake-cli')
        monitor['note'] = C.M('note.aborted')
        self.local(postTurnSummary={'status_category': 'completed', 'status_detail': 'Checked my-app.',
                                    'needs_action': '', 'summarizes_uuid': 'fake-uuid'})
        self.assertEqual(S.read_claude(self.root, {'claude:fake-cli': monitor}, self.now)[0]['runs'][0]['summary'],
                         'Checked my-app.')
        for summary in (None, {}, {'status_detail': ''}, 'invalid'):
            with self.subTest(summary=summary):
                self.local(postTurnSummary=summary)
                self.assertEqual(S.read_claude(self.root, {'claude:fake-cli': monitor}, self.now)[0]['runs'][0]['summary'],
                                 C.M('note.aborted'))

    def test_duration_uses_turn_edge_not_monitor_or_later_activity(self):
        self.config([self.task()])
        edge = timestamp('2026-11-06T11:02:00')
        self.local(lastTurnEdgeAt=edge * 1000)
        run = S.read_claude(self.root, {'claude:fake-cli': row('claude', 'fake-cli')}, self.now)[0]['runs'][0]
        self.assertEqual(run['end'], edge)
        self.assertEqual(run['end'] - run['start'], 120)
        self.assertFalse(run['duration_unknown'])

    def test_duration_falls_back_to_last_activity(self):
        self.config([self.task()])
        self.local()
        run = S.read_claude(self.root, {}, self.now)[0]['runs'][0]
        self.assertEqual(run['end'] - run['start'], 600)

    def test_running_has_no_end(self):
        self.config([self.task()])
        self.local()
        run = S.read_claude(self.root, {'claude:fake-cli': row('claude', 'fake-cli', 'run')}, self.now)[0]['runs'][0]
        self.assertIsNone(run['end'])

    def test_missed_after_grace_and_jitter(self):
        self.config([self.task(jitterSeconds=900)])
        before = timestamp('2026-11-06T11:44:00')
        # Start the task today, so there is no earlier missed occurrence.
        self.config([self.task(createdAt=timestamp('2026-11-06T10:00:00'), jitterSeconds=900)])
        self.assertEqual(S.read_claude(self.root, {}, before)[0]['runs'], [])
        task = S.read_claude(self.root, {}, before + 120)[0]
        self.assertEqual((task['runs'][0]['status'], task['group']), ('miss', 'fail'))

    def test_created_at_bounds_missed(self):
        self.config([self.task(createdAt=self.now)])
        self.assertEqual(S.read_claude(self.root, {}, self.now)[0]['runs'], [])

    def test_disabled_does_not_miss(self):
        self.config([self.task(enabled=False)])
        task = S.read_claude(self.root, {}, self.now)[0]
        self.assertEqual((task['group'], task['runs'], task['next_run']), ('off', [], None))

    def test_once_completed_and_future(self):
        self.config([self.task(id='once', cronExpression=None, fireAt=demo._iso(self.now + 86400)),
                     self.task(id='done', cronExpression=None, fireAt=demo._iso(self.now - 86400),
                               lastRunAt=demo._iso(self.now - 86300))])
        tasks = {t['id']: t for t in S.read_claude(self.root, {}, self.now)}
        self.assertEqual(tasks['once']['group'], 'ok')
        self.assertEqual(tasks['once']['next_run'], self.now + 86400)
        self.assertEqual(tasks['done']['group'], 'off')

    def assert_once_time(self, value, fire):
        self.config([self.task(cronExpression=None, fireAt=value)])
        task = S.read_claude(self.root, {}, self.now)[0]
        self.assertEqual(task['schedule'], C.M('schedule.once',
                         C.M('schedule.once_date', C.M('schedule.month.11'), 9, '15:00')))
        self.assertEqual(task['raw'], 'fireAt ' +
                         dt.datetime.fromtimestamp(fire).astimezone().isoformat(timespec='seconds'))
        self.assertEqual((task['kind'], task['next_run'], task['group']), ('once', fire, 'ok'))
        return task

    def test_once_fire_at_milliseconds(self):
        fire = timestamp('2026-11-09T15:00:00')
        self.assert_once_time(int(fire * 1000), fire)

    def test_once_fire_at_iso(self):
        fire = timestamp('2026-11-09T15:00:00')
        self.assert_once_time(dt.datetime.fromtimestamp(fire, dt.timezone.utc).isoformat().replace('+00:00', 'Z'), fire)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires tzset')
    def test_once_uses_local_timezone_not_iso_offset(self):
        try:
            with mock.patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}):
                time.tzset()
                fire = timestamp('2026-11-09T15:00:00')
                task = self.assert_once_time('2026-11-09T07:00:00Z', fire)
                self.assertEqual(task['raw'], 'fireAt 2026-11-09T15:00:00+08:00')
        finally:
            time.tzset()

    def test_once_other_year_includes_year(self):
        fire = timestamp('2027-01-09T15:00:00')
        self.config([self.task(cronExpression=None, fireAt=int(fire * 1000))])
        self.assertEqual(S.read_claude(self.root, {}, self.now)[0]['schedule'],
                         C.M('schedule.once', C.M('schedule.once_year', 2027,
                             C.M('schedule.once_date', C.M('schedule.month.01'), 9, '15:00'))))

    def test_once_missed_after_grace_for_both_formats(self):
        fire = self.now + 3600
        for value in (int(fire * 1000), demo._iso(fire)):
            with self.subTest(value=value):
                self.config([self.task(cronExpression=None, fireAt=value, jitterSeconds=60)])
                before = S.read_claude(self.root, {}, fire + S.GRACE_SECONDS + 59)[0]
                self.assertEqual(before['runs'], [])
                task = S.read_claude(self.root, {}, fire + S.GRACE_SECONDS + 60)[0]
                self.assertEqual((task['group'], task['runs'][0]['status']), ('fail', 'miss'))
                self.assertEqual(task['runs'][0]['start'], fire)
                self.assertIsNone(task['next_run'])

    def test_once_completed_or_paused_does_not_miss(self):
        fire = self.now - 3600
        for changes in ({'enabled': False}, {'lastRunAt': (fire + 60) * 1000}):
            with self.subTest(changes=changes):
                self.config([self.task(cronExpression=None, fireAt=fire * 1000, **changes)])
                task = S.read_claude(self.root, {}, self.now)[0]
                self.assertEqual((task['group'], task['runs'], task['next_run']), ('off', [], None))

    def test_once_recorded_run_does_not_miss(self):
        fire = self.now - 3600
        self.config([self.task(cronExpression=None, fireAt=fire * 1000)])
        self.local(createdAt=fire * 1000, lastActivityAt=(fire + 60) * 1000)
        task = S.read_claude(self.root, {}, self.now)[0]
        self.assertEqual([r['status'] for r in task['runs']], ['ok'])

    def test_mtime_cache_sees_edits(self):
        self.config([self.task(title='Before')])
        cache = S.FileCache()
        S.read_claude(self.root, {}, self.now, cache)
        self.config([self.task(title='After edit')])
        self.assertEqual(S.read_claude(self.root, {}, self.now, cache)[0]['title'], 'After edit')

    def test_invalid_json_does_not_lose_other_accounts(self):
        self.config([self.task()])
        path = self.config([], 'broken')
        with open(path, 'w') as f:
            f.write('{')
        self.assertEqual(len(S.read_claude(self.root, {}, self.now)), 1)


class CodexTests(TemporaryFiles):
    def db(self):
        path = os.path.join(self.root, 'sqlite', 'codex.db')
        os.makedirs(os.path.dirname(path))
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE automations (id TEXT, name TEXT, status TEXT, rrule TEXT, kind TEXT, '
                   'next_run_at INTEGER, last_run_at INTEGER, target_thread_id TEXT, cwds TEXT)')
        db.execute('CREATE TABLE automation_runs (thread_id TEXT, automation_id TEXT, status TEXT, '
                   'created_at INTEGER, updated_at INTEGER, inbox_title TEXT, inbox_summary TEXT)')
        self.addCleanup(db.close)
        return db

    def toml(self):
        path = os.path.join(self.root, 'automations', 'daily', 'automation.toml')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            f.write('id = "daily"\nname = "Daily my-app check"\nstatus = "PAUSED"\n'
                    'rrule = "FREQ=DAILY;BYHOUR=11;BYMINUTE=0"\nkind = "heartbeat"\n'
                    'cwds = ["/Users/alice/my-app"]\nprompt = """\nCheck my-app.\n"""\n')
        return path

    def test_sqlite_preferred_readonly_and_paused(self):
        self.toml()
        with self.db() as db:
            db.execute('INSERT INTO automations VALUES (?,?,?,?,?,?,?,?,?)',
                       ('db-task', 'Database task', 'PAUSED', 'FREQ=HOURLY', 'cron', None, None, None, '[]'))
        tasks = S.read_codex(self.root, {}, self.now)
        self.assertEqual([(t['id'], t['group']) for t in tasks], [('db-task', 'off')])
        uri = 'file:' + os.path.join(self.root, 'sqlite', 'codex.db') + '?mode=ro'
        with S.context_db(uri) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM automations')

    def test_toml_fallback(self):
        self.toml()
        task = S.read_codex(self.root, {}, self.now)[0]
        self.assertEqual((task['id'], task['group']), ('daily', 'off'))
        self.assertEqual(task['cwd'], ['/Users/alice/my-app'])

    def test_python39_toml_reader(self):
        path = self.toml()
        with mock.patch.object(S, 'tomllib', None):
            data = S.simple_toml(path)
        self.assertEqual(data['prompt'], 'Check my-app.\n')
        self.assertEqual(data['cwds'], ['/Users/alice/my-app'])

    def test_toml_prompt_cannot_override_configuration_keys(self):
        path = self.toml()
        with open(path, 'a') as f:
            f.write('notes = """\nid = "not-a-config-id"\nstatus = "ACTIVE"\n"""\n')
        with mock.patch.object(S, 'tomllib', None):
            data = S.simple_toml(path)
        self.assertEqual((data['id'], data['status']), ('daily', 'PAUSED'))

    def test_unopenable_db_falls_back(self):
        self.toml()
        write_json(os.path.join(self.root, 'sqlite', 'codex.db'), {'not': 'sqlite'})
        self.assertEqual(S.read_codex(self.root, {}, self.now)[0]['id'], 'daily')

    def test_database_runs_and_millisecond_dates(self):
        with self.db() as db:
            db.execute('INSERT INTO automations VALUES (?,?,?,?,?,?,?,?,?)',
                       ('daily', 'Daily check', 'ACTIVE', 'FREQ=DAILY', 'cron', int((self.now + 1000) * 1000),
                        int((self.now - 60) * 1000), None, '[]'))
            db.execute('INSERT INTO automation_runs VALUES (?,?,?,?,?,?,?)',
                       ('thread-one', 'daily', 'FAILED', int((self.now - 60) * 1000),
                        int(self.now * 1000), 'Daily check', 'Request timed out.'))
        task = S.read_codex(self.root, {}, self.now)[0]
        self.assertEqual(task['runs'][0]['status'], 'fail')
        self.assertEqual(task['runs'][0]['start'], self.now - 60)
        self.assertEqual(task['next_run'], self.now + 1000)

    def test_database_run_summary_uses_assistant_not_inbox_prompt(self):
        with self.db() as db:
            db.execute('INSERT INTO automations VALUES (?,?,?,?,?,?,?,?,?)',
                       ('daily', 'Daily check', 'ACTIVE', 'FREQ=DAILY', 'cron', None, None, None, '[]'))
            db.execute('INSERT INTO automation_runs VALUES (?,?,?,?,?,?,?)',
                       ('thread-one', 'daily', 'COMPLETED', int((self.now - 60) * 1000),
                        int(self.now * 1000), 'User trigger', '<heartbeat>User prompt</heartbeat>'))
        history = S.AutomationTurns()
        history.thread._asst('<result>**Checked my-app.** More details.</result>', demo._iso(self.now))
        task = S.read_codex(self.root, {}, self.now, heartbeat={'thread-one': history})[0]
        self.assertEqual(task['runs'][0]['summary'], 'Checked my-app.')

    def test_heartbeat_last_run_fallback(self):
        with self.db() as db:
            db.execute('INSERT INTO automations VALUES (?,?,?,?,?,?,?,?,?)',
                       ('daily', 'Daily check', 'ACTIVE', 'FREQ=DAILY', 'heartbeat', None,
                        int((self.now - 60) * 1000), 'thread-one', '[]'))
        task = S.read_codex(self.root, {'codex:thread-one': row('codex', 'thread-one', 'wait')}, self.now)[0]
        self.assertTrue(task['history_fallback'])
        self.assertEqual(task['runs'][0]['status'], 'stuck')


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.reader = S.AutomationTurns()
        self.base = timestamp('2026-11-07T10:00:00')

    def feed(self, seconds, pt, **fields):
        data = {'timestamp': demo._iso(self.base + seconds), 'type': 'event_msg', 'payload': dict(type=pt, **fields)}
        self.reader.feed(data, json.dumps(data).encode())

    def test_only_explicit_marker_and_human_turns_stay_separate(self):
        self.feed(0, 'task_started')
        self.feed(1, 'message', role='user', content=[{'text': '<heartbeat>\n<automation_id>daily</automation_id>\n</heartbeat>'}])
        self.feed(60, 'task_complete', last_agent_message='Checked my-app.')
        self.feed(90, 'task_started')
        self.feed(91, 'user_message', message='Please change the schedule.')
        self.feed(100, 'turn_aborted')
        self.assertEqual(len(self.reader.records['daily']), 1)
        self.assertEqual(self.reader.records['daily'][0]['status'], 'ok')

    def test_item_completed_format_and_question(self):
        self.feed(0, 'task_started')
        self.feed(1, 'item_completed', item={'type': 'UserMessage', 'content': [
            {'text': '<heartbeat><automation_id>daily</automation_id></heartbeat>'}]})
        self.feed(60, 'task_complete', last_agent_message='Should I keep the legacy endpoint?')
        self.assertEqual(self.reader.records['daily'][0]['status'], 'stuck')
        self.assertEqual(self.reader.records['daily'][0]['summary'], 'Should I keep the legacy endpoint?')

    def test_summary_is_last_assistant_first_sentence_without_markup(self):
        self.feed(0, 'task_started')
        self.feed(1, 'user_message', message='<heartbeat><automation_id>daily</automation_id></heartbeat>')
        self.feed(2, 'agent_message', message='Starting the check.')
        self.feed(59, 'agent_message', message='**Checked [my-app](https://example.com).** More details.')
        self.feed(60, 'task_complete')
        self.assertEqual(self.reader.records['daily'][0]['summary'], 'Checked my-app.')

    def test_summary_uses_heartbeat_result_not_xml_metadata(self):
        self.feed(0, 'task_started')
        self.feed(1, 'user_message', message='<heartbeat><automation_id>daily</automation_id>Check user prompt.</heartbeat>')
        self.feed(60, 'task_complete', last_agent_message=(
            '<heartbeat><automation_id>daily</automation_id><decision>DONT_NOTIFY</decision>'
            '<message>**Checked my-app.** No action needed.</message></heartbeat>'))
        self.assertEqual(self.reader.records['daily'][0]['summary'], 'Checked my-app.')

    def test_summary_limit_and_chinese_sentence(self):
        self.assertEqual(S.assistant_summary('<result>检查完成。后续内容。</result>'), '检查完成。')
        self.assertLessEqual(len(S.assistant_summary('a' * 200)), 120)
        self.assertEqual(S.assistant_summary('# **Result**\nMore details.'), 'Result')
        self.assertEqual(S.assistant_summary('<result>*Done* &amp; checked.</result>'), 'Done & checked.')

    def test_no_assistant_summary_does_not_use_user_prompt(self):
        self.feed(0, 'task_started')
        self.feed(1, 'user_message', message='<heartbeat><automation_id>daily</automation_id>User prompt.</heartbeat>')
        self.feed(60, 'task_complete')
        self.assertEqual(self.reader.records['daily'][0]['summary'], '')

    def test_latest_monitor_status_does_not_overwrite_assistant_summary(self):
        self.test_summary_uses_heartbeat_result_not_xml_metadata()
        config = [({'id': 'daily', 'status': 'ACTIVE', 'kind': 'heartbeat',
                    'target_thread_id': 'fake-thread'}, '/fake/automation.toml')]
        monitor = row('codex', 'fake-thread')
        monitor.update(last_ts=self.base + 60, line='<heartbeat>', note='User trigger prompt.')
        with mock.patch.object(S, 'read_codex_config', return_value=(config, [])):
            task = S.read_codex('/nonexistent', {'codex:fake-thread': monitor},
                                self.base + 61, heartbeat={'fake-thread': self.reader})[0]
        self.assertEqual(task['runs'][0]['summary'], 'Checked my-app.')
        self.assertEqual(task['runs'][0]['end'], self.base + 60)

    def test_finished_run_does_not_absorb_later_context(self):
        self.test_summary_uses_heartbeat_result_not_xml_metadata()
        self.feed(90, 'token_count')
        self.assertEqual(self.reader.records['daily'][0]['last_activity'], self.base + 60)

    def test_session_and_schedule_share_heartbeat_waiting_boundary(self):
        thread = C.CodexThread()
        for seconds, payload in (
                (0, {'type': 'user_message', 'message': 'Check the source.'}),
                (1, {'type': 'agent_message', 'message': 'https://example.com/transcript?tab=notes'}),
                (2, {'type': 'task_complete'}),
                (10, {'type': 'task_started'}),
                (11, {'type': 'user_message', 'message': '<heartbeat><automation_id>daily</automation_id></heartbeat>'}),
                (12, {'type': 'task_complete', 'last_agent_message': 'Checked the transcript. No action needed.'})):
            data = {'timestamp': demo._iso(self.base + seconds), 'type': 'event_msg', 'payload': payload}
            raw = json.dumps(data).encode()
            thread.feed(data, raw)
            self.reader.feed(data, raw)
        self.assertEqual(S.quick_asks(thread.turns), [])
        self.assertEqual(self.reader.records['daily'][0]['status'], 'ok')
        self.assertEqual(thread.first_user, 'Check the source.')
        self.assertEqual(thread.last_user_ts, demo._iso(self.base + 11))

    def test_heartbeat_question_is_still_waiting_in_session(self):
        thread = C.CodexThread()
        for seconds, pt, fields in (
                (0, 'task_started', {}),
                (1, 'item_completed', {'item': {'type': 'UserMessage', 'content': [
                    {'text': '<heartbeat><automation_id>daily</automation_id></heartbeat>'}]}}),
                (60, 'task_complete', {'last_agent_message': 'Should I keep the legacy endpoint?'})):
            data = {'timestamp': demo._iso(self.base + seconds), 'payload': dict(type=pt, **fields)}
            thread.feed(data, json.dumps(data).encode())
        self.assertEqual(S.quick_asks(thread.turns), ['Should I keep the legacy endpoint?'])

    def test_repeated_heartbeat_text_in_new_turn_sets_new_user_boundary(self):
        thread = C.CodexThread()
        for offset in (0, 100):
            for seconds, pt, fields in (
                    (offset, 'task_started', {}),
                    (offset + 1, 'user_message', {
                        'message': '<heartbeat><automation_id>daily</automation_id></heartbeat>'}),
                    (offset + 60, 'task_complete', {
                        'last_agent_message': 'Should I continue?' if offset == 0 else 'Done.'})):
                data = {'timestamp': demo._iso(self.base + seconds), 'payload': dict(type=pt, **fields)}
                thread.feed(data, json.dumps(data).encode())
        self.assertEqual(S.quick_asks(thread.turns), [])
        self.assertEqual(thread.last_user_ts, demo._iso(self.base + 101))

    def test_user_message_format_and_failure(self):
        self.feed(0, 'task_started')
        self.feed(1, 'user_message', message='<heartbeat><automation_id>daily</automation_id></heartbeat>')
        self.feed(60, 'turn_aborted')
        self.assertEqual(self.reader.records['daily'][0]['status'], 'fail')

    def test_marker_in_quoted_text_does_not_count(self):
        self.feed(0, 'task_started')
        self.feed(1, 'user_message', message='Here is an example: <heartbeat><automation_id>daily</automation_id>')
        self.assertFalse(self.reader.records)

    def test_duplicate_message_formats_are_one_run(self):
        text = '<heartbeat><automation_id>daily</automation_id></heartbeat>'
        self.feed(0, 'task_started')
        self.feed(1, 'message', role='user', content=[{'text': text}])
        self.feed(2, 'item_completed', item={'type': 'UserMessage', 'content': [{'text': text}]})
        self.feed(60, 'task_complete', last_agent_message='Checked my-app.')
        self.assertEqual(len(self.reader.records['daily']), 1)

    def test_limits_fifty_per_automation(self):
        for n in range(60):
            self.feed(n * 100, 'task_started')
            self.feed(n * 100 + 1, 'user_message', message='<heartbeat><automation_id>daily</automation_id>')
            self.feed(n * 100 + 60, 'task_complete', last_agent_message='Done.')
        self.assertEqual(len(self.reader.records['daily']), 50)

    def test_association_uses_split_results_not_last_human_turn(self):
        self.test_only_explicit_marker_and_human_turns_stay_separate()
        config = [({'id': 'daily', 'name': 'Daily check', 'status': 'ACTIVE', 'kind': 'heartbeat',
                    'target_thread_id': 'fake-thread', 'rrule': 'FREQ=DAILY'}, '/Users/alice/.codex/sqlite/codex.db')]
        with mock.patch.object(S, 'read_codex_config', return_value=(config, [])):
            task = S.read_codex('/nonexistent', {'codex:fake-thread': row('codex', 'fake-thread', 'fail')},
                                self.base + 100, heartbeat={'fake-thread': self.reader})[0]
        self.assertEqual(task['runs'][0]['status'], 'ok')
        self.assertEqual(task['runs'][0]['session_key'], 'codex:fake-thread')
        self.assertFalse(task['history_fallback'])


class LaunchdTests(TemporaryFiles):
    def plist(self, name='com.example.check', **extra):
        path = os.path.join(self.root, 'LaunchAgents', name + '.plist')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {'Label': name, 'ProgramArguments': ['/bin/sh', '/Users/alice/my-app/check.sh']}
        data.update(extra)
        with open(path, 'wb') as f:
            plistlib.dump(data, f)
        return path

    def test_dict_and_list_calendars(self):
        self.plist(StartCalendarInterval={'Hour': 11, 'Minute': 0})
        self.plist('com.example.two', StartCalendarInterval=[{'Hour': 11, 'Minute': 0}, {'Hour': 17, 'Minute': 0}])
        tasks, _ = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None),
                                  runner=lambda label: {'loaded': True, 'runs': 0})
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t['next_run'] == timestamp('2026-11-07T11:00:00') for t in tasks))

    def test_calendar_merges_daily_times(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[0][0]),
                         C.M('schedule.calendar_daily',
                             C.M('schedule.join', C.M('schedule.join', '13:10', '16:50'), '19:20')))

    def test_calendar_merges_same_weekday_times(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[1][0]),
                         C.M('schedule.calendar_weekly', C.M('schedule.day.MO'),
                             C.M('schedule.join', '09:00', '17:00')))

    def test_calendar_merges_same_time_weekdays(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[2][0]),
                         C.M('schedule.calendar_weekly',
                             C.M('schedule.join', C.M('schedule.day.MO'), C.M('schedule.day.TH')), '12:30'))

    def test_calendar_merges_workdays(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[3][0]),
                         C.M('schedule.calendar_weekdays', '17:00'))

    def test_calendar_merges_monday_to_saturday(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[4][0]),
                         C.M('schedule.calendar_mon_sat', '17:00'))

    def test_calendar_mixed_groups_have_semicolon(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[5][0]),
                         C.M('schedule.group_join',
                             C.M('schedule.calendar_weekly', C.M('schedule.day.MO'), '12:30'),
                             C.M('schedule.calendar_weekly', C.M('schedule.day.TH'), '17:00')))

    def test_calendar_normalizes_sunday_sorts_and_deduplicates(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[6][0]),
                         C.M('schedule.calendar_weekly',
                             C.M('schedule.join', C.M('schedule.day.MO'), C.M('schedule.day.SU')), '09:00'))

    def test_calendar_all_weekdays_become_daily(self):
        self.assertEqual(S.calendar_description(CALENDAR_CASES[7][0]), C.M('schedule.calendar_daily', '09:00'))

    def test_calendar_constraints_are_not_merged_into_daily(self):
        calendars = [{'Day': 1, 'Hour': 9, 'Minute': 0}, {'Month': 12, 'Hour': 9, 'Minute': 0},
                     {'Hour': 9}]
        descriptions = [S.cron_description('0 9 1 * *'), S.cron_description('0 9 * 12 *'),
                        S.cron_description('* 9 * * *')]
        self.assertEqual(S.calendar_description(calendars), S.join_messages(descriptions, 'schedule.group_join'))

    def test_calendar_summary_keeps_full_raw_and_next_run(self):
        calendars = CALENDAR_CASES[0][0]
        self.plist(StartCalendarInterval=calendars)
        task = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None),
                              runner=lambda label: {'loaded': True, 'runs': 0})[0][0]
        self.assertEqual(task['schedule'], S.calendar_description(calendars))
        self.assertEqual(task['raw_summary'], C.M('schedule.launchd_times', 3))
        self.assertEqual(json.loads(task['raw'].removeprefix('StartCalendarInterval ')), calendars)
        self.assertEqual(task['next_run'], timestamp('2026-11-07T13:10:00'))

    def test_interval_uses_hours_minutes_or_seconds_without_rounding(self):
        for interval, key, amount in ((7200, 'every_hours', 2), (3600, 'every_hours', 1),
                                      (5400, 'every_minutes', 90), (60, 'every_minutes', 1),
                                      (90, 'every_seconds', 90)):
            with self.subTest(interval=interval):
                self.plist(StartInterval=interval)
                task = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None),
                                      runner=lambda label: {'loaded': True, 'runs': 0})[0][0]
                self.assertEqual(task['schedule'], C.M('schedule.' + key, amount))
                self.assertEqual(task['raw'], 'StartInterval %d' % interval)
                self.assertEqual(task['next_run'], self.now + interval)

    def test_service_detection_and_disabled(self):
        self.plist(KeepAlive={'SuccessfulExit': False}, RunAtLoad=True)
        self.plist('com.example.timer', KeepAlive=True, StartInterval=3600, Disabled=True)
        tasks, services = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None),
                                         runner=lambda label: {'loaded': True, 'runs': 1, 'pid': 12345})
        self.assertEqual((len(services), tasks[0]['group']), (1, 'off'))
        self.assertEqual(services[0]['pid'], 12345)
        self.assertEqual(tasks[0]['off_reason'], C.M('schedule.disabled'))
        self.assertIsNone(tasks[0]['next_run'])
        self.assertIsNone(services[0]['off_reason'])

    def test_unloaded_is_off_without_next_or_missed_runs(self):
        self.plist(StartCalendarInterval={'Hour': 11, 'Minute': 0})
        history = S.LaunchHistory(None)
        root = os.path.join(self.root, 'LaunchAgents')
        for now in (self.now, self.now + 86400):
            task = S.read_launchd(root, now, history, runner=lambda label: {'loaded': False})[0][0]
            self.assertFalse(task['enabled'])
            self.assertEqual(task['group'], 'off')
            self.assertEqual(task['off_reason'], C.M('schedule.unloaded'))
            self.assertIsNone(task['next_run'])
            self.assertEqual(task['runs'], [])

    def test_enabled_has_no_off_reason(self):
        self.plist(StartInterval=60)
        task = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None),
                              runner=lambda label: {'loaded': True})[0][0]
        self.assertTrue(task['enabled'])
        self.assertIsNone(task['off_reason'])

    def test_disabled_calendar_does_not_miss(self):
        self.plist(StartCalendarInterval={'Hour': 11, 'Minute': 0}, Disabled=True)
        history = S.LaunchHistory(None)
        for now in (self.now, self.now + 86400):
            task = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), now, history,
                                  runner=lambda label: {'loaded': True, 'runs': 0})[0][0]
            self.assertEqual((task['group'], task['off_reason']), ('off', C.M('schedule.disabled')))
            self.assertIsNone(task['next_run'])
            self.assertEqual(task['runs'], [])

    def test_launchctl_output(self):
        text = ('gui/502/com.example.check = {\n\tstate = not running\n\truns = 7\n\tlast exit code = 1\n'
                '\tendpoints = {\n\t\tstate = running\n\t\tpid = 999\n\t}\n}\n')
        state = S.parse_launchctl(text)
        self.assertEqual((state['state'], state['runs'], state['exit_code'], state['pid']), ('not running', 7, 1, None))

    def test_launchctl_pid(self):
        state = S.parse_launchctl('service = {\n    state = running\n    pid = 12345\n    runs = 8\n}')
        self.assertEqual(state['pid'], 12345)

    def test_only_print_command(self):
        with mock.patch.object(S.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='\tstate = running\n')) as run:
            S.launchctl_print('com.example.check')
        self.assertEqual(run.call_args[0][0][:2], ['launchctl', 'print'])

    def test_launchctl_113_is_unloaded(self):
        with mock.patch.object(S.subprocess, 'run', return_value=mock.Mock(returncode=113, stdout='')):
            self.assertEqual(S.launchctl_print('com.example.check'), {'loaded': False})

    def test_missing_launchctl_empty(self):
        self.plist(StartInterval=60)
        with mock.patch.object(S.shutil, 'which', return_value=None):
            self.assertEqual(S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None)), ([], []))

    def test_runner_failure_unloaded(self):
        self.plist(StartInterval=60)
        def fail(label):
            raise OSError('not loaded')
        tasks, _ = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), self.now, S.LaunchHistory(None), runner=fail)
        self.assertFalse(tasks[0]['loaded'])

    def test_baseline_records_exit_result_with_unknown_start(self):
        history = S.LaunchHistory(None)
        entry = history.observe('com.example.check', {'loaded': True, 'runs': 25, 'exit_code': 0}, {}, self.now)
        self.assertEqual(len(entry['history']), 1)
        run = entry['history'][0]
        self.assertTrue(run['before_monitoring'])
        self.assertEqual((run['start'], run['end'], run['status'], run['exit_code']), (None, None, 'ok', 0))
        self.assertEqual(run['summary'], C.M('schedule.before_monitoring_result', C.M('schedule.exit', 0)))
        history.observe('com.example.check', {'loaded': True, 'runs': 25, 'exit_code': 0}, {}, self.now + 60)
        self.assertEqual(len(entry['history']), 1)

    def test_baseline_requires_runs_and_exit_code(self):
        for state in ({'runs': 0, 'exit_code': 0}, {'runs': 25, 'exit_code': None}, {'runs': 0}):
            with self.subTest(state=state):
                entry = S.LaunchHistory(None).observe('com.example.check', dict(state, loaded=True), {}, self.now)
                self.assertEqual(entry['history'], [])

    def test_existing_empty_baseline_is_backfilled_without_duplicate_observed_run(self):
        history = S.LaunchHistory(None)
        label = 'com.example.check'
        history.labels[label] = {'since': self.now - 60, 'seq': 0, 'history': [],
                                 'previous': {'loaded': True, 'runs': 25, 'exit_code': 0, 'at': self.now - 60}}
        entry = history.observe(label, {'loaded': True, 'runs': 25, 'exit_code': 0}, {}, self.now)
        self.assertEqual(len(entry['history']), 1)
        self.assertTrue(entry['history'][0]['before_monitoring'])
        entry = history.observe(label, {'loaded': True, 'runs': 26, 'exit_code': 0}, {}, self.now + 60)
        self.assertEqual(len(entry['history']), 2)
        self.assertEqual(entry['history'][1]['start'], self.now)
        self.assertNotIn('before_monitoring', entry['history'][1])

    def test_baseline_end_uses_latest_log_mtime_and_records_failure(self):
        paths = {}
        for stream, seconds in (('stdout', 200), ('stderr', 100)):
            path = os.path.join(self.root, stream + '.log')
            with open(path, 'w') as f:
                f.write('Earlier unscoped output.\n')
            os.utime(path, (self.now - seconds, self.now - seconds))
            paths[stream] = path
        history = S.LaunchHistory(None)
        entry = history.observe('com.example.check', {'loaded': True, 'runs': 3, 'exit_code': 1}, paths, self.now)
        run = entry['history'][0]
        self.assertEqual((run['start'], run['end'], run['status']), (None, self.now - 100, 'fail'))
        self.assertEqual(history.log('com.example.check', run['id'])['lines'], [])

    def test_baseline_and_current_process_are_separate(self):
        entry = S.LaunchHistory(None).observe('com.example.check',
            {'loaded': True, 'runs': 3, 'exit_code': 1, 'pid': 123}, {}, self.now)
        self.assertEqual([r['status'] for r in entry['history']], ['fail', 'run'])
        self.assertIsNone(entry['history'][0]['start'])
        self.assertEqual(entry['history'][1]['start'], self.now)

    def test_baseline_without_start_does_not_anchor_interval_to_epoch(self):
        self.plist(StartInterval=900)
        now = self.now + 17
        task = S.read_launchd(os.path.join(self.root, 'LaunchAgents'), now, S.LaunchHistory(None),
                              runner=lambda label: {'loaded': True, 'runs': 3, 'exit_code': 0})[0][0]
        self.assertEqual(task['next_run'], now + 900)

    def test_runs_increase_persists_and_truncates(self):
        history = S.LaunchHistory(os.path.join(self.root, 'state'))
        for n in range(62):
            history.observe('com.example.check', {'loaded': True, 'runs': n, 'exit_code': 0}, {}, self.now + n * 60)
        history.save()
        loaded = S.LaunchHistory(os.path.join(self.root, 'state'))
        self.assertEqual(len(loaded.labels['com.example.check']['history']), 50)
        self.assertEqual(stat.S_IMODE(os.stat(history.path).st_mode), 0o600)
        self.assertEqual(loaded.labels['com.example.check']['history'][-1]['status'], 'ok')

    def test_pid_end_and_replacement(self):
        history = S.LaunchHistory(None)
        history.observe('com.example.check', {'loaded': True, 'runs': 1, 'pid': 123}, {}, self.now)
        entry = history.observe('com.example.check', {'loaded': True, 'runs': 2, 'pid': 456, 'exit_code': 1}, {}, self.now + 60)
        self.assertEqual([r['status'] for r in entry['history']], ['fail', 'run'])
        entry = history.observe('com.example.check', {'loaded': True, 'runs': 2, 'pid': None, 'exit_code': 0}, {}, self.now + 120)
        self.assertEqual([r['status'] for r in entry['history']], ['fail', 'ok'])

    def test_logs_use_only_recorded_ranges_and_redact(self):
        path = os.path.join(self.root, 'stdout.log')
        with open(path, 'w') as f:
            f.write('earlier run\n')
        history = S.LaunchHistory(None)
        paths = {'stdout': path, 'stderr': None}
        history.observe('com.example.check', {'loaded': True, 'runs': 1}, paths, self.now)
        with open(path, 'a') as f:
            f.write('my-app ready\nmail alice@example.com\n<script>alert(1)</script>\n')
        entry = history.observe('com.example.check', {'loaded': True, 'runs': 2, 'exit_code': 0}, paths, self.now + 60)
        with open(path, 'a') as f:
            f.write('later run\n')
        log = history.log('com.example.check', entry['history'][0]['id'])
        text = '\n'.join(line['text'] for line in log['lines'])
        self.assertNotIn('earlier', text)
        self.assertNotIn('later', text)
        self.assertNotIn('alice@example.com', text)
        self.assertIn('[EMAIL]', text)
        self.assertIn('<script>', text)  # The browser escapes every log line, never innerHTML from data.

    def test_log_rotation_is_reported(self):
        path = os.path.join(self.root, 'stdout.log')
        with open(path, 'w') as f:
            f.write('start\n')
        history = S.LaunchHistory(None)
        history.observe('com.example.check', {'loaded': True, 'runs': 0}, {'stdout': path}, self.now)
        history.observe('com.example.check', {'loaded': True, 'runs': 1, 'exit_code': 0}, {'stdout': path}, self.now + 60)
        os.rename(path, path + '.old')
        with open(path, 'w') as f:
            f.write('unrelated log\n')
        self.assertTrue(history.log('com.example.check', '1')['unavailable'])

    def test_log_tail_fifty_lines(self):
        path = os.path.join(self.root, 'stdout.log')
        with open(path, 'w') as f:
            f.write('')
        history = S.LaunchHistory(None)
        history.observe('com.example.check', {'loaded': True, 'runs': 0}, {'stdout': path}, self.now)
        with open(path, 'a') as f:
            f.write('line\n' * 90)
        history.observe('com.example.check', {'loaded': True, 'runs': 1, 'exit_code': 0}, {'stdout': path}, self.now + 60)
        self.assertEqual(len(history.log('com.example.check', '1')['lines']), 50)

    def test_missed_only_after_monitoring_begins(self):
        self.plist(StartCalendarInterval={'Hour': 11, 'Minute': 0})
        history = S.LaunchHistory(None)
        root = os.path.join(self.root, 'LaunchAgents')
        runner = lambda label: {'loaded': True, 'runs': 4, 'exit_code': 0}
        first, _ = S.read_launchd(root, self.now, history, runner)
        self.assertEqual(len(first[0]['runs']), 1)
        self.assertTrue(first[0]['runs'][0]['before_monitoring'])
        after, _ = S.read_launchd(root, timestamp('2026-11-07T11:31:00'), history, runner)
        self.assertEqual(after[0]['runs'][0]['status'], 'miss')

    def test_calendar_uses_and_for_day_fields(self):
        calendar = S.Calendar({'Hour': 10, 'Minute': 0, 'Day': 1, 'Weekday': 1})
        self.assertEqual(calendar.next(timestamp('2026-11-07T12:00:00')), timestamp('2027-02-01T10:00:00'))


class ClassificationTests(unittest.TestCase):
    def test_mutually_exclusive_priority(self):
        cases = [
            ({'enabled': False, 'runs': [{'status': 'run'}]}, 'off'),
            ({'enabled': True, 'runs': [{'status': 'fail'}, {'status': 'run'}]}, 'run'),
            ({'enabled': True, 'runs': [{'status': 'stuck'}]}, 'fail'),
            ({'enabled': True, 'runs': [{'status': 'miss'}]}, 'fail'),
            ({'enabled': True, 'runs': [{'status': 'ok'}, {'status': 'fail'}]}, 'ok'),
            ({'enabled': True, 'runs': []}, 'ok'),
            ({'enabled': True, 'runs': [{'status': 'unknown'}]}, 'ok'),
            ({'enabled': False, 'service': True}, 'svc')
        ]
        for task, expected in cases:
            with self.subTest(task=task):
                self.assertEqual(S.classify(task), expected)

    def test_finish_keeps_latest_fifty(self):
        task = S.finish_task({'enabled': True, 'runs': [
            {'id': str(n), 'start': n, 'status': 'ok'} for n in range(70)]})
        self.assertEqual(len(task['runs']), 50)
        self.assertEqual(task['runs'][0]['start'], 69)


class IntegrationTests(TemporaryFiles):
    def test_database_run_reads_assistant_from_archived_thread(self):
        xd = os.path.join(self.root, 'codex')
        thread = demo._Codex(xd, 'archived-run', '/Users/alice/my-app', self.now - 40 * 86400)
        thread.turn(thread.start, 'User trigger prompt.')
        thread.ev(thread.start + 60, 'task_complete', last_agent_message='<result>**Checked my-app.** Done.</result>')
        thread.save()
        path = os.path.join(xd, 'sqlite', 'codex.db')
        os.makedirs(os.path.dirname(path))
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        with db:
            db.execute('CREATE TABLE automations (id TEXT, status TEXT, kind TEXT)')
            db.execute('CREATE TABLE automation_runs (automation_id TEXT, thread_id TEXT, created_at INTEGER, status TEXT)')
            db.execute('INSERT INTO automations VALUES (?,?,?)', ('daily', 'ACTIVE', 'cron'))
            db.execute('INSERT INTO automation_runs VALUES (?,?,?,?)',
                       ('daily', thread.tid, int(thread.start * 1000), 'COMPLETED'))
        collector = S.ScheduleCollector(None, codex_dir=xd,
                                        launchd_root=os.path.join(self.root, 'LaunchAgents'))
        task = collector.collect(now=self.now)['tasks'][0]
        self.assertEqual(task['runs'][0]['summary'], 'Checked my-app.')
        self.assertEqual(task['runs'][0]['status'], 'ok')

    def test_old_heartbeat_thread_is_readable_without_expanding_session_list(self):
        cd, xd = os.path.join(self.root, 'claude'), os.path.join(self.root, 'codex')
        thread = demo._Codex(xd, 'old-heartbeat', '/Users/alice/my-app', self.now - 40 * 86400)
        thread.turn(thread.start, '<heartbeat><automation_id>daily</automation_id></heartbeat>')
        thread.ev(thread.start + 60, 'task_complete', last_agent_message='Checked my-app.')
        thread.save()
        config = os.path.join(xd, 'automations', 'daily', 'automation.toml')
        os.makedirs(os.path.dirname(config))
        with open(config, 'w') as f:
            f.write('id = "daily"\nstatus = "PAUSED"\nkind = "heartbeat"\n'
                    'target_thread_id = "%s"\nrrule = "FREQ=DAILY"\n' % thread.tid)
        cfg = parse_args(['--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(self.root, 'state'),
                          '--summary', 'off', '--no-desktop'])
        C.configure(cd, xd, cfg.days)
        mon = server.Monitor(cfg)
        mon.schedule_collector.launchd_root = os.path.join(self.root, 'LaunchAgents')
        mon.scan_schedules()
        task = mon.schedule_snapshot['tasks'][0]
        self.assertEqual(len(task['runs']), 1)
        self.assertEqual(task['runs'][0]['status'], 'ok')
        self.assertEqual(mon.snapshot['rows'], [])
        detail = mon.detail(task['runs'][0]['session_key'])
        self.assertEqual(detail['row']['key'], 'codex:' + thread.tid)
        self.assertEqual(mon.snapshot['rows'], [])

    @unittest.skipUnless(shutil.which('node'), 'optional page-script check requires Node (no packages)')
    def test_actual_page_scripts(self):
        cd, xd = demo.build(os.path.join(self.root, 'ui'), time.time())
        cfg = parse_args(['--demo', '--summary', 'off', '--no-desktop',
                          '--claude-dir', cd, '--codex-dir', xd, '--state-dir', os.path.join(self.root, 'state')])
        C.configure(cd, xd, cfg.days)
        mon = server.Monitor(cfg)
        mon.scan()
        mon.scan_schedules()
        fixtures = {'sessions': mon.snapshot, 'schedules': mon.schedule_snapshot,
                    'details': {r['key']: mon.detail(r['key']) for r in mon.snapshot['rows']},
                    'calendar_descriptions': [{'message': S.calendar_description(calendars), 'en': en, 'zh': zh}
                                              for calendars, en, zh in CALENDAR_CASES],
                    'once_descriptions': [
                        {'message': S.once_description(timestamp('2026-11-09T15:00:00'), self.now),
                         'en': 'Once · Nov 9 15:00', 'zh': '一次性 · 11/9 15:00'},
                        {'message': S.once_description(timestamp('2027-01-09T15:00:00'), self.now),
                         'en': 'Once · 2027 Jan 9 15:00', 'zh': '一次性 · 2027/1/9 15:00'}]}
        result = subprocess.run([shutil.which('node'), os.path.join(os.path.dirname(__file__), 'schedules_ui.js'),
                                 os.path.join(os.path.dirname(S.__file__), 'static')],
                                input=json.dumps(fixtures), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS:', result.stdout)

    def test_demo_reads_no_scheduler_sources(self):
        cfg = parse_args(['--demo', '--state-dir', self.root])
        server.use_demo(cfg)
        mon = server.Monitor(cfg)
        with mock.patch.object(S, 'read_claude', side_effect=AssertionError('real read')), \
                mock.patch.object(S, 'read_codex', side_effect=AssertionError('real read')), \
                mock.patch.object(S, 'read_launchd', side_effect=AssertionError('real read')):
            mon.scan_schedules()
        data = mon.schedule_snapshot
        self.assertEqual({t['source'] for t in data['tasks']}, {'launchd', 'claude', 'codex'})
        self.assertEqual({t['group'] for t in data['tasks']}, {'fail', 'run', 'ok', 'off'})
        self.assertEqual({r['status'] for t in data['tasks'] for r in t['runs']}, {'ok', 'fail', 'stuck', 'miss', 'run'})
        self.assertTrue(data['services'])
        self.assertTrue(any(not t['runs'] for t in data['tasks']))
        self.assertIsNotNone(mon.schedule_log('com.example.backup', '1'))

    def test_session_scan_does_not_collect_schedules(self):
        cd, xd = demo.build(os.path.join(self.root, 'data'), self.now)
        cfg = parse_args(['--claude-dir', cd, '--codex-dir', xd, '--summary', 'off', '--no-desktop',
                          '--state-dir', os.path.join(self.root, 'state')])
        C.configure(cd, xd, cfg.days)
        mon = server.Monitor(cfg)
        with mock.patch.object(mon.schedule_collector, 'collect', side_effect=AssertionError('slow collection')):
            mon.scan()
        self.assertTrue(mon.snapshot['ready'])

    def test_get_routes_auth_and_lazy_logs_without_network(self):
        mon = mock.Mock()
        mon.schedule_snapshot = demo.schedules(self.now)
        mon.schedule_log.return_value = {'lines': [{'stream': 'stdout', 'text': 'my-app ready'}], 'unavailable': False}
        def request(path, cookie='test-token'):
            handler = object.__new__(server.Handler)
            handler.server = mock.Mock(mon=mon, token='test-token', token_file='/Users/alice/state/token',
                                       server_address=('127.0.0.1', 8769))
            handler.path = path
            handler.headers = {'Host': '127.0.0.1:8769', 'Cookie': 'ccm_8769=' + cookie}
            handler._send = mock.Mock()
            handler.do_GET()
            return handler._send.call_args[0]
        code, data = request('/api/schedules')
        self.assertEqual(code, 200)
        self.assertEqual(data, mon.schedule_snapshot)
        self.assertNotIn('lines', json.dumps(data))
        self.assertEqual(request('/api/schedules', cookie='wrong')[0], 401)
        self.assertEqual(request('/api/schedules/launchd/com.example.check/runs/1/log')[0], 200)
        mon.schedule_log.assert_called_once_with('com.example.check', '1')
        mon.schedule_log.return_value = None
        self.assertEqual(request('/api/schedules/launchd/com.example.check/runs/2/log')[0], 404)
        self.assertEqual(request('/api/schedules/launchd/com.example.check/runs/2/log', cookie='wrong')[0], 401)

    def test_python39_syntax(self):
        import ast
        for name in ('schedules.py', 'schedule_evidence.py', 'server.py', 'demo.py', 'collect.py'):
            with open(os.path.join(os.path.dirname(S.__file__), name)) as f:
                ast.parse(f.read(), feature_version=(3, 9))


if __name__ == '__main__':
    unittest.main()
