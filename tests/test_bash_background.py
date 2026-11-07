import json
import os
import tempfile
import unittest

from claude_codex_monitor import collect as C, server as S
from claude_codex_monitor.config import parse_args


class BashBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.path = os.path.join(self.root, 'fake-session.jsonl')
        self.start = '2026-11-07T04:35:00Z'
        self.result_ts = '2026-11-07T04:37:00Z'
        self.end = '2026-11-07T04:53:00Z'
        self.output = os.path.join(self.root, 'fake-task.output')
        self.addCleanup(C.set_tail_func, C._tail)
        self.mon = S.Monitor(parse_args([
            '--state-dir', os.path.join(self.root, 'state'),
            '--claude-dir', self.root, '--codex-dir', self.root,
            '--summary', 'off', '--no-desktop']))

    def append(self, role, content, ts, **extra):
        record = {'type': role, 'timestamp': ts, 'sessionId': 'fake-session',
                  'cwd': self.root, 'message': {'role': role, 'content': content}}
        record.update(extra)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, separators=(',', ':')) + '\n')
        return self.mon.tail(self.path, C.ClaudeSession)

    def bash(self, tid='tool_bash', command='sleep 300', description='Wait for fake build', **extra):
        inp = {'command': command}
        if description is not None:
            inp['description'] = description
        inp.update(extra)
        return self.append('assistant', [
            {'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': inp}], self.start)

    def result(self, tid='tool_bash', background=True, is_error=False, tur=None):
        if background:
            tur = {'backgroundTaskId': 'fake-bg', 'timedOutAfterMs': 120000}
            text = ('Command did not complete within its 120s timeout and was moved to the '
                    'background (ID: fake-bg). Output is being written to: ' + self.output)
        else:
            text = 'Fake foreground result'
        return self.append('user', [
            {'type': 'tool_result', 'tool_use_id': tid, 'content': text, 'is_error': is_error}],
            self.result_ts, toolUseResult=tur)

    def end_turn(self, ts=None):
        return self.append('assistant', [{'type': 'text', 'text': 'Fake turn finished.'}],
                           ts or self.result_ts,
                           message={'role': 'assistant', 'stop_reason': 'end_turn',
                                    'content': [{'type': 'text', 'text': 'Fake turn finished.'}]})

    def notify(self, status='completed', summary='Fake build finished with exit code 0'):
        return self.append('user', '<task-notification><task-id>fake-bg</task-id>'
                           '<status>%s</status><summary>%s</summary></task-notification>' %
                           (status, summary), self.end)

    def row(self, ts=None):
        return self.mon.claude_row(self.path, {}, C.parse_ts(ts or self.result_ts) + 1)

    def tasks(self, ts=None):
        state = self.mon.state_of(self.path)
        return [C.claude_task(state.tasks[tid], self.path[:-6], {}, self.mon.rollout_state,
                              C.parse_ts(ts or self.result_ts) + 1)
                for tid in self.mon.claude_ids(state, {})]

    def test_timeout_background_keeps_ended_turn_running_until_notification(self):
        self.bash()
        self.result()
        state = self.end_turn()
        self.assertTrue(state.turn_ended())
        self.assertEqual(self.row()['status'], 'run')
        task, = self.tasks()
        self.assertEqual((task['status'], task['title']), ('run', 'Wait for fake build'))
        self.assertEqual(task['start'], C.parse_ts(self.start))
        self.assertIsNone(task['end'])
        self.assertEqual(task['note'], C.M('note.timeout_background'))
        self.assertEqual(task['executor'], C.M('exec.background'))
        self.assertEqual(task['command'], 'sleep 300')
        raw_task = state.tasks['tool_bash']
        self.assertEqual((raw_task['kind'], raw_task['background'], raw_task['bg_id']),
                         ('bash', True, 'fake-bg'))
        self.assertEqual(raw_task['output_file'], self.output)
        self.assertEqual(state.pending_bash, {})
        self.assertNotIn('tool_bash', state.watch)

        self.notify()
        task, = self.tasks(self.end)
        self.assertEqual((task['status'], task['end']), ('ok', C.parse_ts(self.end)))
        self.end_turn('2026-11-07T04:53:01Z')
        self.assertEqual(self.row(self.end)['status'], 'ok')

    def test_notifications_finish_promoted_task_by_background_id(self):
        for status, summary, expected in (
                ('completed', 'Fake exit code 0', 'completed'),
                ('completed', 'Fake exit code 2', 'failed'),
                ('failed', 'Fake failure', 'failed'),
                ('killed', 'Fake killed task', 'stopped'),
                ('stopped', 'Fake stopped task', 'stopped'),
                ('cancelled', 'Fake cancelled task', 'stopped'),
                ('canceled', 'Fake canceled task', 'stopped')):
            with self.subTest(status=status, summary=summary):
                state = C.ClaudeSession()
                for record in (
                        {'type': 'assistant', 'timestamp': self.start, 'message': {'content': [
                            {'type': 'tool_use', 'id': 'tool_bash', 'name': 'Bash',
                             'input': {'command': 'sleep 300'}}]}},
                        {'type': 'user', 'timestamp': self.result_ts,
                         'toolUseResult': {'backgroundTaskId': 'fake-bg', 'timedOutAfterMs': 120000},
                         'message': {'content': [
                             {'type': 'tool_result', 'tool_use_id': 'tool_bash', 'content': 'Fake timeout'}]}},
                        {'type': 'user', 'timestamp': self.end, 'message': {'content':
                            '<task-notification><task-id>fake-bg</task-id><status>%s</status>'
                            '<summary>%s</summary></task-notification>' % (status, summary)}}):
                    raw = json.dumps(record, separators=(',', ':')).encode()
                    self.assertTrue(state.want(raw))
                    state.feed(record, raw)
                task = state.tasks['tool_bash']
                self.assertEqual((task['status'], task['end'], task['note']),
                                 (expected, self.end, summary))

    def test_foreground_results_do_not_create_subtasks_or_retain_inputs(self):
        for is_error, tur in ((False, None), (False, {'stdout': 'fake'}), (True, 'fake error')):
            with self.subTest(is_error=is_error, tur=tur):
                self.bash()
                state = self.result(background=False, is_error=is_error, tur=tur)
                self.assertEqual(state.tasks, {})
                self.assertEqual(state.order, [])
                self.assertEqual(state.pending_bash, {})
                self.assertNotIn('tool_bash', state.watch)
        self.end_turn()
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.row()['status'], 'ok')

    def test_pending_inputs_keep_only_unanswered_bash_calls(self):
        state = self.bash(tid='tool_first')
        self.bash(tid='tool_second')
        self.assertEqual(set(state.pending_bash), {'tool_first', 'tool_second'})
        self.assertTrue({'tool_first', 'tool_second'} <= state.watch)
        self.result(tid='tool_second', background=False)
        self.assertEqual(set(state.pending_bash), {'tool_first'})
        self.assertNotIn('tool_second', state.watch)
        self.result(tid='tool_first')
        self.assertEqual(state.pending_bash, {})
        self.assertEqual(state.order, ['tool_first'])

    def test_original_cwd_and_start_are_preserved(self):
        state = self.bash()
        state.cwd = os.path.join(self.root, 'later-cwd')
        state._tool_result({'tool_use_id': 'tool_bash', 'content': 'Fake timeout'},
                           {'backgroundTaskId': 'fake-bg', 'timedOutAfterMs': 120000},
                           self.result_ts)
        task = state.tasks['tool_bash']
        self.assertEqual((task['cwd'], task['start']), (self.root, self.start))

    def test_explicit_background_task_is_not_duplicated_or_marked_timeout(self):
        state = self.bash(run_in_background=True)
        self.result(background=False, tur={'backgroundTaskId': 'fake-bg'})
        task = state.tasks['tool_bash']
        self.assertEqual(state.order, ['tool_bash'])
        self.assertEqual((task['status'], task['background'], task['bg_id']),
                         ('running', True, 'fake-bg'))
        self.assertIsNone(task.get('note'))
        self.assertEqual(state.pending_bash, {})

    def test_existing_cli_task_becomes_background_without_duplication(self):
        for command, flag in (('codex exec "Fake job"', 'codex'), ('claude -p "Fake job"', 'claude')):
            with self.subTest(command=command):
                state = self.bash(command=command)
                self.assertTrue(state.tasks['tool_bash'][flag])
                self.result()
                task = state.tasks['tool_bash']
                self.assertEqual(state.order, ['tool_bash'])
                self.assertTrue(task[flag])
                self.assertTrue(task['background'])
                self.assertEqual(task['note'], C.M('note.timeout_background'))
                self.assertEqual(state.pending_bash, {})
                # Reset the made-up log for the next CLI variant.
                os.remove(self.path)
                self.mon.tails.clear()

    def test_command_is_title_fallback_and_unknown_result_is_ignored(self):
        state = self.bash(description=None)
        self.result(background=False, tur={'backgroundTaskId': 'fake-bg'})
        self.assertEqual(state.tasks['tool_bash']['title'], 'sleep 300')
        state._tool_result({'tool_use_id': 'unknown', 'content': 'Fake timeout'},
                           {'backgroundTaskId': 'unknown-bg'}, self.result_ts)
        self.assertEqual(state.order, ['tool_bash'])


if __name__ == '__main__':
    unittest.main()
