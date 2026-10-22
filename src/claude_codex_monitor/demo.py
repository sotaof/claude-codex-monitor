"""Made-up sessions for --demo, screenshots and tests.

Writes Claude Code and Codex logs in the same shape the real tools write them, with timestamps relative to
now, so every feature has something to show: sessions waiting for an answer, running subtasks in a batch,
a pending approval, a failed turn.
"""
import datetime as dt
import json
import os
import shutil
import uuid

NS = uuid.UUID(int=0x5eed)


def _id(name):
    return str(uuid.uuid5(NS, name))


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n')


class _Claude:
    def __init__(self, root, name, cwd, title=None, model='claude-sonnet-5-5'):
        self.sid, self.cwd, self.model = _id('claude:' + name), cwd, model
        self.dir = os.path.join(root, 'projects', cwd.replace('/', '-'))
        self.rows, self.n = [], 0
        if title:
            self.rows.append({'type': 'custom-title', 'customTitle': title, 'sessionId': self.sid})

    def _base(self, kind, t):
        return {'type': kind, 'timestamp': _iso(t), 'sessionId': self.sid, 'cwd': self.cwd,
                'entrypoint': 'claude-desktop', 'gitBranch': 'main'}

    def user(self, t, text):
        self.rows.append(dict(self._base('user', t), message={'role': 'user', 'content': text}))

    def _asst(self, t, content, stop):
        self.n += 1
        usage = {'input_tokens': 900, 'cache_read_input_tokens': 18000, 'cache_creation_input_tokens': 1200,
                 'output_tokens': 350}
        self.rows.append(dict(self._base('assistant', t), message={
            'id': 'msg_%s_%d' % (self.sid[:8], self.n), 'role': 'assistant', 'model': self.model,
            'content': content, 'stop_reason': stop, 'usage': usage}))

    def say(self, t, text, stop='end_turn'):
        self._asst(t, [{'type': 'text', 'text': text}], stop)

    def tool(self, t, tid, name, inp, text=None):
        content = ([{'type': 'text', 'text': text}] if text else []) + [
            {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]
        self._asst(t, content, 'tool_use')

    def result(self, t, tid, text, extra=None, error=False):
        self.rows.append(dict(self._base('user', t), message={'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tid, 'content': text, 'is_error': error}]},
            toolUseResult=extra or {}))

    def notify(self, t, tid, task_id, status, summary):
        blk = ('<task-notification><tool-use-id>%s</tool-use-id><task-id>%s</task-id><status>%s</status>'
               '<summary>%s</summary></task-notification>') % (tid, task_id, status, summary)
        self.rows.append(dict(self._base('user', t), message={'role': 'user', 'content': blk}))

    def api_error(self, t, text):
        self.rows.append(dict(self._base('assistant', t), isApiErrorMessage=True, message={
            'id': 'err_%s' % self.sid[:8], 'role': 'assistant', 'model': '<synthetic>',
            'content': [{'type': 'text', 'text': text}], 'stop_reason': 'stop_sequence'}))

    def save(self):
        _write(os.path.join(self.dir, self.sid + '.jsonl'), self.rows)
        return self

    def subagent(self, agent_id, rows):
        _write(os.path.join(self.dir, self.sid, 'subagents', 'agent-%s.jsonl' % agent_id), rows)


class _Codex:
    def __init__(self, root, name, cwd, start, source='vscode', model='gpt-5-codex'):
        self.tid, self.root, self.start = _id('codex:' + name), root, start
        self.rows, self.calls = [], 0
        self.rows.append({'timestamp': _iso(start), 'type': 'session_meta', 'payload': {
            'id': self.tid, 'timestamp': _iso(start), 'cwd': cwd, 'originator': 'Codex Desktop',
            'source': source, 'git': {'branch': 'main'}}})
        self.rows.append({'timestamp': _iso(start), 'type': 'turn_context', 'payload': {'model': model, 'cwd': cwd}})

    def ev(self, t, kind, **kw):
        self.rows.append({'timestamp': _iso(t), 'type': 'event_msg', 'payload': dict({'type': kind}, **kw)})

    def call(self, t, name, args):
        self.calls += 1
        self.rows.append({'timestamp': _iso(t), 'type': 'response_item', 'payload': {
            'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': 'call_%d' % self.calls}})

    def turn(self, t, prompt):
        self.ev(t, 'task_started')
        self.ev(t + 1, 'user_message', message=prompt)

    def plan(self, t, steps, explanation=None):
        args = {'plan': [{'step': s, 'status': st} for s, st in steps]}
        if explanation:
            args['explanation'] = explanation
        self.call(t, 'update_plan', args)

    def cmd(self, t, command):
        self.call(t, 'exec_command', {'cmd': command})

    def tokens(self, t, total, output):
        self.ev(t, 'token_count', info={'total_token_usage': {'total_tokens': total, 'output_tokens': output}})

    def save(self):
        day = dt.datetime.fromtimestamp(self.start)
        name = 'rollout-%s-%s.jsonl' % (day.strftime('%Y-%m-%dT%H-%M-%S'), self.tid)
        _write(os.path.join(self.root, 'sessions', day.strftime('%Y/%m/%d'), name), self.rows)
        return self


def _key(client, name):
    return '%s:%s' % (client, _id(client + ':' + name))


# Sessions that start out dismissed, to show the Dismissed card
DISMISSED = [_key('claude', 'eslint'), _key('codex', 'badges')]


def build(root, now):
    """Write the demo data under root; returns (claude_dir, codex_dir)."""
    shutil.rmtree(root, ignore_errors=True)
    cdir, xdir = os.path.join(root, 'claude'), os.path.join(root, 'codex')
    m = 60
    web, api, docs = '/Users/demo/code/acme-web', '/Users/demo/code/acme-api', '/Users/demo/code/acme-docs'

    # Waiting: two questions in a "needs your decision" section, after a workflow reviewed every handler
    s = _Claude(cdir, 'webhooks', api, 'Refactor billing webhooks')
    s.user(now - 52 * m, 'Refactor the billing webhook handlers so each event type has its own handler and tests.')
    wf = os.path.join(s.dir, s.sid, 'subagents', 'workflows', 'wf_demo')
    s.tool(now - 50 * m, 'wf1', 'Workflow', {'script': "export const meta = {name: 'handler-review', "
                                                       "description: 'Review each handler for missing idempotency checks'}"},
           'Splitting the handlers first; a workflow checks each one for idempotency in parallel.')
    s.result(now - 50 * m, 'wf1', 'Workflow launched in background.', {
        'status': 'async_launched', 'taskId': 'wfdemo', 'workflowName': 'handler-review', 'transcriptDir': wf,
        'summary': 'Review each handler for missing idempotency checks'})
    for i in range(4):
        _write(os.path.join(wf, 'agent-a%d.jsonl' % i), [{'type': 'assistant', 'timestamp': _iso(now - (49 - i) * m),
                                                          'message': {'content': [{'type': 'text', 'text': 'Checked.'}]}}])
    s.notify(now - 41 * m, 'wf1', 'wfdemo', 'completed', 'Workflow "handler-review" completed')
    s.say(now - 36 * m, 'Split the webhook handlers into one module per event type and added 38 tests; all pass.\n\n'
                        '**Needs your decision:**\n'
                        '1. Keep the legacy `/hooks/v1` endpoint for 30 more days, or remove it now?\n'
                        '2. Should failed deliveries retry with exponential backoff (max 5 tries) or a fixed 1-minute delay?')
    s.save()

    # Running: three subtasks started together (an agent, a background command, a Codex run)
    s = _Claude(cdir, 'ci', web, 'Migrate CI to GitHub Actions')
    s.user(now - 26 * m, 'Move our CI from Jenkins to GitHub Actions. The release job has to keep working.')
    plan = ('Plan: survey the current Jenkins jobs, run the e2e suite once as a baseline, and let Codex port '
            'the release workflow in parallel.')
    s.tool(now - 25.6 * m, 'todo1', 'TodoWrite', {'todos': [
        {'content': 'Survey Jenkins jobs', 'activeForm': 'Surveying Jenkins jobs', 'status': 'completed'},
        {'content': 'Baseline e2e run', 'activeForm': 'Running the e2e baseline', 'status': 'in_progress'},
        {'content': 'Port the release workflow', 'activeForm': 'Porting the release workflow', 'status': 'in_progress'},
        {'content': 'Add dependency caching', 'activeForm': 'Adding dependency caching', 'status': 'pending'}]}, plan)
    s.result(now - 25.6 * m, 'todo1', 'Todos have been modified successfully')
    s.tool(now - 25.4 * m, 'agent1', 'Agent', {'description': 'Survey Jenkins jobs', 'subagent_type': 'Explore',
                                               'prompt': 'List every Jenkins job, its trigger and what it runs.',
                                               'run_in_background': True})
    s.result(now - 25.4 * m, 'agent1', 'Async agent launched', {'isAsync': True, 'agentId': 'a1'})
    s.subagent('a1', [
        {'type': 'assistant', 'timestamp': _iso(now - 25 * m), 'message': {'content': [
            {'type': 'tool_use', 'id': 'x1', 'name': 'Glob', 'input': {'pattern': '.jenkins/**/*.groovy'}}]}},
        {'type': 'assistant', 'timestamp': _iso(now - 22 * m), 'message': {'content': [
            {'type': 'tool_use', 'id': 'x2', 'name': 'Read', 'input': {'file_path': web + '/.jenkins/release.groovy'}}]}},
        {'type': 'assistant', 'timestamp': _iso(now - 19 * m), 'message': {'content': [
            {'type': 'text', 'text': 'Found 6 jobs: lint, unit, e2e, build, release and nightly. Release is the only '
                                     'one with secrets (NPM_TOKEN, SENTRY_AUTH).'}]}}])
    out = os.path.join(root, 'bg1.output')
    s.tool(now - 25.2 * m, 'bash1', 'Bash', {'command': 'npm run test:e2e -- --reporter=dot',
                                             'description': 'Run the e2e suite as a baseline', 'run_in_background': True})
    s.result(now - 25.2 * m, 'bash1', 'Command running in background with ID: bg1. Output is being written to: ' + out,
             {'backgroundTaskId': 'bg1'})
    prompt = ('Port .jenkins/release.groovy to .github/workflows/release.yml. Keep the same steps and secrets, '
              'and tag releases the same way.')
    s.tool(now - 25 * m, 'bash2', 'Bash', {'command': 'codex exec --full-auto "%s"' % prompt,
                                           'description': 'Port the release workflow (Codex)', 'run_in_background': True})
    s.result(now - 25 * m, 'bash2', 'Command running in background with ID: bg2.', {'backgroundTaskId': 'bg2'})
    s.notify(now - 19 * m, 'agent1', 'a1', 'completed', 'Agent "Survey Jenkins jobs" completed')
    s.tool(now - 1 * m, 'bash3', 'Bash', {'command': 'gh workflow list', 'description': 'List workflows'})
    s.save()
    with open(out, 'w', encoding='utf-8') as f:
        f.write('Running 214 tests using 4 workers\n' + '.' * 120 + '\n' + '.' * 61 + 'F\n')
    x = _Codex(xdir, 'ci-release', web, now - 24.9 * m, source='exec')
    x.turn(now - 24.9 * m, prompt)
    steps = ['Read the Jenkins release job', 'Write release.yml', 'Map secrets', 'Dry-run with act']
    x.plan(now - 24 * m, [(steps[0], 'in_progress'), (steps[1], 'pending'), (steps[2], 'pending'), (steps[3], 'pending')])
    x.cmd(now - 23.5 * m, 'cat .jenkins/release.groovy')
    x.plan(now - 18 * m, [(steps[0], 'completed'), (steps[1], 'completed'), (steps[2], 'in_progress'), (steps[3], 'pending')])
    x.cmd(now - 3 * m, 'grep -rn "credentials(" .jenkins/')
    x.tokens(now - 3 * m, 184000, 9200)
    x.save()

    # Waiting: a question box
    s = _Claude(cdir, 'dates', web, 'Pick a date library')
    s.user(now - 3.2 * 60 * m, 'We use three different date libraries. Pick one and plan the migration.')
    s.tool(now - 3 * 60 * m, 'ask1', 'AskUserQuestion', {'questions': [
        {'question': 'Which library should we standardize on: date-fns, Day.js or Luxon?', 'options': []}]},
        'date-fns is used in 41 files, Day.js in 12 and Luxon in 3.')
    s.save()

    # Done, and already dismissed
    s = _Claude(cdir, 'eslint', web, 'Tidy ESLint config')
    s.user(now - 20 * 60 * m, 'Merge our two ESLint configs into one flat config.')
    s.say(now - 19.6 * 60 * m, 'Merged them into eslint.config.js and removed .eslintrc.cjs. Lint passes with no new warnings.')
    s.save()
    x = _Codex(xdir, 'badges', docs, now - 30 * 60 * m)
    x.turn(now - 30 * 60 * m, 'Point the README badges at GitHub Actions instead of Jenkins.')
    x.ev(now - 29.8 * 60 * m, 'agent_message', message='Updated the build and coverage badges to GitHub Actions.')
    x.ev(now - 29.8 * 60 * m, 'task_complete', last_agent_message='Updated the build and coverage badges to GitHub Actions.')
    x.save()

    # Done
    s = _Claude(cdir, 'tz', api, 'Fix flaky timezone test')
    s.user(now - 5 * 60 * m, 'test_invoice_due_date fails about once a day on CI. Find out why and fix it.')
    s.say(now - 4.6 * 60 * m, 'Fixed. The test built "today" from the local clock while the code used UTC, so it '
                              'failed between 00:00 and 08:00 UTC+8. The test now freezes time at a fixed UTC '
                              'instant. All 412 tests pass.')
    s.save()

    # Failed: the API returned an error and the turn stopped
    s = _Claude(cdir, 'node', web, 'Upgrade to Node 22')
    s.user(now - 7 * 60 * m, 'Upgrade the project to Node 22 and fix whatever breaks.')
    s.tool(now - 6.9 * 60 * m, 'b9', 'Bash', {'command': 'nvm install 22 && npm ci', 'description': 'Install Node 22'})
    s.result(now - 6.8 * 60 * m, 'b9', 'added 1294 packages in 41s')
    s.api_error(now - 6.8 * 60 * m, 'API Error: 529 Overloaded. Try again in a moment.')
    s.save()

    # Codex, running: working through its own plan
    x = _Codex(xdir, 'dark', web, now - 14 * m)
    x.turn(now - 14 * m, 'Add a dark mode toggle to the settings page and remember the choice.')
    steps = ['Find theme tokens', 'Add the toggle', 'Persist the choice', 'Update screenshots']
    x.plan(now - 13 * m, [(steps[0], 'in_progress'), (steps[1], 'pending'), (steps[2], 'pending'), (steps[3], 'pending')])
    x.cmd(now - 12.5 * m, 'rg "--color-" src/styles')
    x.plan(now - 9 * m, [(steps[0], 'completed'), (steps[1], 'in_progress'), (steps[2], 'pending'), (steps[3], 'pending')])
    x.plan(now - 4 * m, [(steps[0], 'completed'), (steps[1], 'completed'), (steps[2], 'in_progress'), (steps[3], 'pending')])
    x.cmd(now - 1 * m, 'npm test -- settings')
    x.tokens(now - 1 * m, 96000, 5100)
    x.save()

    # Codex, waiting: asked for approval to run a command
    x = _Codex(xdir, 'flags', api, now - 41 * m)
    x.turn(now - 41 * m, 'Remove feature flags that have been fully rolled out for more than 90 days.')
    x.ev(now - 39 * m, 'agent_message', message='Removed 7 stale flags and their dead branches. Tests pass.')
    x.ev(now - 38 * m, 'exec_approval_request', command=['git', 'push', 'origin', 'cleanup/stale-flags'])
    x.save()

    # Codex, waiting: finished with a question
    x = _Codex(xdir, 'notes', docs, now - 2 * 60 * m)
    x.turn(now - 2 * 60 * m, 'Write release notes for v2.4 from the merged pull requests.')
    x.cmd(now - 118 * m, 'gh pr list --state merged --search "milestone:v2.4"')
    msg = ('Draft is in docs/release-notes/v2.4.md: 14 changes grouped into Features, Fixes and Internal.\n\n'
           'Should I also post it to the #releases channel?')
    x.ev(now - 112 * m, 'agent_message', message=msg)
    x.ev(now - 112 * m, 'task_complete', last_agent_message=msg)
    x.save()

    # Codex, done: two helper threads it spawned
    x = _Codex(xdir, 'search', api, now - 9 * 60 * m)
    x.turn(now - 9 * 60 * m, 'Search got slow this week. Find out why.')
    for i, (nick, task) in enumerate((('Scout', 'Profile the /search endpoint'), ('Ledger', 'Diff schema changes since last Monday'))):
        k = _Codex(xdir, 'search-sub-%d' % i, api, now - (9 * 60 - 2 - i) * m,
                   source={'subagent': {'thread_spawn': {'parent_thread_id': x.tid, 'agent_nickname': nick}}})
        k.turn(k.start, task)
        k.cmd(k.start + 60, 'psql -c "EXPLAIN ANALYZE SELECT ..."' if i == 0 else 'git log --since=monday -- db/migrations')
        k.ev(k.start + (14 + 6 * i) * m, 'task_complete', last_agent_message='Done: ' + task.lower() + '.')
        k.save()
    final = 'Root cause: migration 0041 dropped the trigram index on products.name. Added 0042 to restore it; p95 is back to 80 ms.'
    x.ev(now - 8.4 * 60 * m, 'agent_message', message=final)
    x.ev(now - 8.4 * 60 * m, 'task_complete', last_agent_message=final)
    x.save()

    names = {'dark': 'Dark mode toggle', 'flags': 'Clean up stale feature flags', 'notes': 'Release notes v2.4',
             'search': 'Why is search slow?', 'badges': 'Update README badges'}
    _write(os.path.join(xdir, 'session_index.jsonl'), [{'id': _id('codex:' + k), 'thread_name': v} for k, v in names.items()])

    # The CI session is "open and working" in Claude Code: point its process file at this process
    os.makedirs(os.path.join(cdir, 'sessions'), exist_ok=True)
    with open(os.path.join(cdir, 'sessions', 'demo.json'), 'w', encoding='utf-8') as f:
        json.dump({'pid': os.getpid(), 'sessionId': _id('claude:ci'), 'status': 'busy', 'name': 'Migrate CI'}, f)
    return cdir, xdir
