"""Read-only scheduled-task readers. Only launchd observations are written, in the monitor state folder."""
import collections
import contextlib
import datetime as dt
import glob
import html
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import time
import urllib.parse

from . import collect as C
from .collect import M
from .summarize import quick_asks, redact
from .schedule_evidence import ClaudeLogs, Diagnostics, period_at

try:
    import tomllib
except ImportError:                  # Python 3.9 / 3.10
    tomllib = None

REFRESH_SECONDS = 45
HISTORY_LIMIT = 50
GRACE_SECONDS = 30 * 60
WEEKDAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')


def cron_field(text, low, high, sunday=False):
    values = set()
    for part in text.split(','):
        pieces = part.split('/')
        if len(pieces) > 2:
            raise ValueError('invalid cron step')
        base = pieces[0]
        step = int(pieces[1]) if len(pieces) == 2 else 1
        if step < 1:
            raise ValueError('invalid cron step')
        if base == '*':
            start, end = low, high
        elif '-' in base:
            bounds = base.split('-')
            if len(bounds) != 2:
                raise ValueError('invalid cron range')
            start, end = map(int, bounds)
        else:
            start = int(base)
            end = high if len(pieces) == 2 else start
        if not low <= start <= end <= high:
            raise ValueError('cron value out of range')
        values.update(range(start, end + 1, step))
    return {0 if sunday and n == 7 else n for n in values}


class Cron:
    """Five-field cron in local time; restricted day-of-month and weekday fields use cron's OR rule."""

    def __init__(self, expression):
        self.parts = expression.split()
        if len(self.parts) != 5:
            raise ValueError('cron needs five fields')
        self.minute, self.hour, self.day, self.month, self.weekday = [
            cron_field(p, lo, hi, i == 4)
            for i, (p, lo, hi) in enumerate(zip(self.parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)))]
        self.times = [(h, m) for h in sorted(self.hour) for m in sorted(self.minute)]

    def matches_day(self, date):
        if date.month not in self.month:
            return False
        day, weekday = date.day in self.day, (date.weekday() + 1) % 7 in self.weekday
        return (day and weekday) if self.parts[2].startswith('*') or self.parts[4].startswith('*') else (day or weekday)

    def occurrence(self, timestamp, direction=1):
        date = dt.datetime.fromtimestamp(timestamp).date()
        # Eight years includes the leap-day gap across a non-leap century.
        for _ in range(366 * 8):
            if self.matches_day(date):
                candidates = set()
                for hour, minute in self.times:
                    local = dt.datetime.combine(date, dt.time(hour, minute))
                    for fold in (0, 1):
                        ts = local.replace(fold=fold).timestamp()
                        if dt.datetime.fromtimestamp(ts) == local:     # skip nonexistent DST wall times
                            candidates.add(ts)
                for ts in sorted(candidates, reverse=direction < 0):
                    if (direction > 0 and ts > timestamp) or (direction < 0 and ts <= timestamp):
                        return ts
            date += dt.timedelta(days=direction)
        return None

    def next(self, timestamp):
        return self.occurrence(timestamp)

    def previous(self, timestamp):
        return self.occurrence(timestamp, -1)


def join_messages(items, key='schedule.join'):
    if not items:
        return ''
    out = items[0]
    for item in items[1:]:
        out = M(key, out, item)
    return out


def once_description(fire, now):
    local = dt.datetime.fromtimestamp(fire)
    date = M('schedule.once_date', M('schedule.month.%02d' % local.month), local.day,
             local.strftime('%H:%M'))
    if local.year != dt.datetime.fromtimestamp(now).year:
        date = M('schedule.once_year', local.year, date)
    return M('schedule.once', date)


def cron_description(expression):
    cron = Cron(expression)
    minute, hour, day, month, weekday = cron.parts
    if hour == day == month == weekday == '*' and re.fullmatch(r'\*/[1-9]\d*', minute):
        return M('schedule.every_minutes', int(minute[2:]))
    if minute == hour == day == month == weekday == '*':
        return M('schedule.every_minutes', 1)
    if day == month == '*' and len(cron.times) <= 24:
        times = ', '.join('%02d:%02d' % pair for pair in cron.times)
        if weekday == '*':
            return M('schedule.daily', times)
        days = join_messages([M('schedule.day.' + WEEKDAYS[(n - 1) % 7]) for n in sorted(cron.weekday)])
        return M('schedule.weekly', days, times)
    return M('schedule.cron', expression)


def rrule_description(rrule):
    try:
        fields = dict(part.split('=', 1) for part in (rrule or '').removeprefix('RRULE:').split(';') if part)
        freq = fields.get('FREQ')
        interval = int(fields.get('INTERVAL', '1'))
        if interval < 1 or freq not in ('DAILY', 'WEEKLY', 'HOURLY'):
            raise ValueError()
        hours = [int(x) for x in fields.get('BYHOUR', '0').split(',')]
        minutes = [int(x) for x in fields.get('BYMINUTE', '0').split(',')]
        if any(not 0 <= x <= 23 for x in hours) or any(not 0 <= x <= 59 for x in minutes):
            raise ValueError()
        days = fields.get('BYDAY', '').split(',') if fields.get('BYDAY') else []
        if any(d not in WEEKDAYS for d in days):
            raise ValueError()
        times = ', '.join('%02d:%02d' % (h, m) for h in hours for m in minutes)
        day_text = join_messages([M('schedule.day.' + d) for d in days])
        if freq == 'HOURLY':
            minute_text = ', '.join('%02d' % m for m in minutes)
            description = (M('schedule.hourly_at', interval, minute_text, ', '.join('%02d' % h for h in hours))
                           if 'BYHOUR' in fields else M('schedule.hourly', interval, minute_text))
            return M('schedule.on_days', description, day_text) if days else description
        if days:
            if interval == 1 and set(days) == set(WEEKDAYS):
                return M('schedule.daily', times)
            if interval == 1 and set(days) == set(WEEKDAYS[:5]):
                return M('schedule.weekdays_at', times)
            return M('schedule.weekly' if interval == 1 else
                     'schedule.daily_days_interval' if freq == 'DAILY' else 'schedule.weekly_interval',
                     *([interval, day_text, times] if interval != 1 else [day_text, times]))
        return M('schedule.daily' if freq == 'DAILY' and interval == 1 else
                 'schedule.daily_interval' if freq == 'DAILY' else 'schedule.weekly_repeat',
                 *([times] if freq == 'DAILY' and interval == 1 else [interval, times]))
    except (ValueError, TypeError, AttributeError):
        return M('schedule.rrule', rrule or '')


def classify(task):
    if task.get('service'):
        return 'svc'
    if not task.get('enabled'):
        return 'off'
    runs = task.get('runs') or []
    if any(r.get('status') == 'run' for r in runs):
        return 'run'
    if runs and runs[0].get('status') in ('fail', 'stuck', 'miss') and not runs[0].get('handled'):
        return 'fail'
    return 'ok'


def session_run(row, run_id, start, last=None):
    status = {'ok': 'ok', 'fail': 'fail', 'wait': 'stuck', 'run': 'run'}.get((row or {}).get('status'), 'unknown')
    summary = ((row or {}).get('items') or [{}])[0].get('text') if status == 'stuck' else None
    summary = summary or (row or {}).get('note') or (row or {}).get('line') or M('schedule.result.' + status)
    end = None if status in ('run', 'unknown') else (row or {}).get('last_ts') or last
    return {'id': str(run_id), 'start': start, 'end': end, 'status': status, 'summary': summary,
            'session_key': row.get('key') if row else None,
            'items': (row or {}).get('items', [])[:3], 'msg_ts': (row or {}).get('msg_ts')}


def finish_task(task):
    task['runs'] = sorted(task.get('runs') or [], key=lambda r: r.get('start') or 0, reverse=True)[:HISTORY_LIMIT]
    task['last_run'] = next((r['start'] for r in task['runs'] if r['status'] != 'miss'), task.get('last_run'))
    task['group'] = classify(task)
    automatic = [r for r in task['runs'] if r.get('mode') != 'manual']
    consecutive = 0
    for run in automatic:
        if run['status'] != 'miss':
            break
        consecutive += 1
    task['consecutive_misses'] = consecutive
    pauses = []
    for period in task.get('state_periods', []):
        if period['enabled']:
            continue
        if pauses and pauses[-1]['end'] == period['start']:
            previous = pauses[-1]
            previous.update(end=period['end'], end_inclusive=period.get('end_inclusive', False),
                            inferred=previous['inferred'] or period['inferred'],
                            end_inferred=period.get('end_inferred', False),
                            skipped=previous.get('skipped') or period.get('skipped'))
        else:
            pauses.append(dict(period))
    task['disabled_periods'] = [p for p in pauses if p.get('skipped')]
    if not task['enabled']:
        task['next_run'] = None
    return task


def missed_run(task, previous, now, since, jitter=0, until=None):
    if since is None or (not task['enabled'] and not task.get('state_periods')):
        return
    grace = GRACE_SECONDS + max(0, jitter)
    task['grace_seconds'] = grace
    floor = max(since, now - 30 * 86400)
    due = previous(min(now - grace, until - 1) if until is not None else now - grace)
    actual = [r for r in task['runs'] if r['status'] != 'miss' and r.get('mode') != 'manual']
    matched = set()
    for run in actual:
        start = run.get('start')
        trigger = previous(start) if start is not None else None
        if trigger is not None and start <= trigger + grace:
            matched.add(trigger)
    # Keep the most recent 50 results, but check each occurrence separately.
    checked = 0
    while due is not None and due >= floor and checked < HISTORY_LIMIT:
        period = period_at(task.get('state_periods', []), due)
        if period and not period['enabled']:
            period['skipped'] = True
            earlier = previous(period['start'] - 1)
            if earlier is None or earlier >= due:
                break
            due = earlier
            continue
        checked += 1
        if due not in matched and (period or task['enabled']):
            task['runs'].append({'id': 'miss-%d' % due, 'start': due, 'end': None, 'status': 'miss',
                                 'summary': M('schedule.missed'), 'session_key': None,
                                 'state_inferred': bool(period and period['inferred']),
                                 'state_unconfirmed': period is None or period['reason'] == 'current_config'})
        earlier = previous(due - 1)
        if earlier is None or earlier >= due:
            break
        due = earlier


class FileCache:
    def __init__(self):
        self.files = {}

    def read(self, path, loader):
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
            old = self.files.get(path)
            if old and old[0] == key:
                return old[1]
            value = loader(path)
            self.files[path] = (key, value)
            return value
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            return {}


def json_file(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def local_session(path):
    data = json_file(path)
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in ('scheduledTaskId', 'sessionId', 'cliSessionId', 'createdAt',
                                    'lastActivityAt', 'lastTurnEdgeAt', 'scheduledRunContinued',
                                    'latestUserFrameAt', 'error', 'postTurnSummary', 'title', 'cwd')}


def claude_run(data, row, run_id):
    error = data.get('error')
    status = ('fail' if error else
              {'run': 'run', 'wait': 'stuck'}.get((row or {}).get('status'), 'ok'))
    summary = data.get('postTurnSummary')
    summary = summary.get('status_detail') if isinstance(summary, dict) else None
    end = C.parse_ts(data.get('lastTurnEdgeAt')) or C.parse_ts(data.get('lastActivityAt'))
    return {'id': str(run_id), 'start': C.parse_ts(data.get('createdAt')),
            'end': None if status == 'run' else end, 'status': status,
            'summary': error or summary or (row or {}).get('note') or '',
            'duration_unknown': bool(data.get('scheduledRunContinued')),
            'session_key': row.get('key') if row else None,
            'items': (row or {}).get('items', [])[:3], 'msg_ts': (row or {}).get('msg_ts') or end,
            'replied_at': (row or {}).get('scheduled_reply_ts'),
            'reply_continued': (row or {}).get('scheduled_reply_continued', False),
            'original_end': (row or {}).get('scheduled_run_end')}


def frontmatter(path):
    with open(path, encoding='utf-8') as f:
        lines = f.read(32768).splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    out, current, block = {}, None, False
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if line[:1].isspace() and current and block:
            out[current] = (out[current] + ' ' + line.strip()).strip()
            continue
        match = re.match(r'^(name|description):\s*(.*)$', line)
        current, block = None, False
        if match:
            current, text = match.groups()
            block = text in ('>', '|', '>-', '|-')
            out[current] = '' if block else text.strip('\'"')
    return out


def read_claude(root, rows, now, cache=None, logs=None):
    cache = cache or FileCache()
    configs = {}
    for path in sorted(glob.glob(os.path.join(root, '*', '*', 'scheduled-tasks.json'))):
        data = cache.read(path, json_file)
        for task in data.get('scheduledTasks', []) if isinstance(data, dict) else []:
            if not isinstance(task, dict) or not task.get('id'):
                continue
            old = configs.get(task['id'])
            version = C.parse_ts(task.get('lastRunAt')) or C.parse_ts(task.get('createdAt')) or 0
            if not old or version >= old[0]:
                configs[task['id']] = (version, task, path)
    runs = collections.defaultdict(dict)
    for path in glob.glob(os.path.join(root, '*', '*', 'local_*.json')):
        data = cache.read(path, local_session)
        aid = data.get('scheduledTaskId')
        if aid not in configs:
            continue
        sid = data.get('cliSessionId')
        row = rows.get('claude:' + sid) if sid else None
        rid = data.get('sessionId') or os.path.basename(path)[:-5]
        run = claude_run(data, row, rid)
        runs[aid][sid or rid] = run
    out = []
    for aid, (_, data, path) in configs.items():
        skill_path = os.path.expanduser(data.get('filePath') or '')
        meta = cache.read(skill_path, frontmatter) if skill_path else {}
        fire = C.parse_ts(data.get('fireAt'))
        fire_local = dt.datetime.fromtimestamp(fire).astimezone().isoformat(timespec='seconds') if fire is not None else ''
        last = C.parse_ts(data.get('lastRunAt'))
        enabled = bool(data.get('enabled', True)) and not (fire is not None and last is not None)
        expression = data.get('cronExpression') or ''
        task = {'id': aid, 'source': 'claude', 'title': data.get('title') or data.get('displayName') or meta.get('name') or aid,
                'description': meta.get('description') or '', 'enabled': enabled,
                'off_reason': M('schedule.once_done' if fire is not None and last is not None else 'schedule.paused'),
                'kind': 'once' if fire is not None else 'cron', 'raw': expression or ('fireAt ' + fire_local),
                'config_path': path, 'skill_path': skill_path, 'cwd': data.get('cwd'), 'last_run': last,
                'next_run': fire if fire is not None and fire > now else None,
                'runs': list(runs[aid].values()),
                'schedule': once_description(fire, now) if fire is not None else M('schedule.invalid')}
        since = C.parse_ts(data.get('createdAt'))
        evidence = logs or ClaudeLogs(None)
        task['state_periods'] = evidence.state_periods(
            aid, enabled, since if since is not None else now - 30 * 86400, now)
        task['state_history_unavailable'] = not evidence.available()
        if expression:
            try:
                cron = Cron(expression)
                grace = GRACE_SECONDS + max(0, data.get('jitterSeconds') or 0)
                for run in task['runs']:
                    run['mode'], run['mode_inferred'] = (logs or ClaudeLogs(None)).run_mode(
                        aid, run['start'], cron.previous, grace)
                task.update(schedule=cron_description(expression), next_run=cron.next(now) if enabled else None)
                segments = logs.cron_segments(aid, expression, since) if logs else [(since, None, expression)]
                for lower, upper, rule in segments:
                    try:
                        historical = Cron(rule)
                    except (ValueError, TypeError):
                        continue
                    missed_run(task, historical.previous, now, max(since, lower) if since is not None else None,
                               data.get('jitterSeconds') or 0, upper)
            except (ValueError, TypeError):
                task.update(schedule=M('schedule.invalid'), next_run=None)
        elif fire is not None:
            for run in task['runs']:
                run['mode'], run['mode_inferred'] = (logs or ClaudeLogs(None)).run_mode(
                    aid, run['start'], lambda ts: fire if ts >= fire else None,
                    GRACE_SECONDS + max(0, data.get('jitterSeconds') or 0))
            missed_run(task, lambda ts: fire if fire <= ts else None, now,
                       C.parse_ts(data.get('createdAt')), data.get('jitterSeconds') or 0)
        out.append(finish_task(task))
    return out


def simple_toml(path):
    """Only scalar key=value and string-array values used by automation.toml; no general TOML interpretation."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if tomllib is not None:
        return tomllib.loads(text)
    out = {}
    consumed = 0
    for match in re.finditer(r'(?m)^([A-Za-z_]\w*)\s*=\s*', text):
        if match.start() < consumed:
            continue
        key, pos = match.group(1), match.end()
        rest = text[pos:]
        if rest.startswith(('"""', "'''")):
            quote = rest[:3]
            end = rest.find(quote, 3)
            if end >= 0:
                out[key] = rest[3:end].lstrip('\n')
                consumed = pos + end + 3
            continue
        decoder = json.JSONDecoder()
        try:
            out[key], end = decoder.raw_decode(rest)
            consumed = pos + end
        except ValueError:
            value = rest.splitlines()[0].split('#', 1)[0].strip()
            if value.startswith("'") and value.endswith("'"):
                out[key] = value[1:-1]
    return out


def read_codex_config(root, cache):
    path = os.path.join(root, 'sqlite', 'codex.db')
    configs, runs = None, []
    try:
        uri = 'file:' + urllib.parse.quote(os.path.abspath(path), safe='/') + '?mode=ro'
        with context_db(uri) as db:
            configs = [dict(r) for r in db.execute('SELECT * FROM automations')]
            try:
                # Bound each history without requiring SQLite window functions.
                for item in configs:
                    runs.extend(dict(r) for r in db.execute(
                        'SELECT * FROM automation_runs WHERE automation_id=? ORDER BY created_at DESC LIMIT 50', (item['id'],)))
            except sqlite3.Error:
                pass
    except (sqlite3.Error, OSError):
        pass
    if configs is not None:
        return [(data, path) for data in configs], runs
    configs = []
    for path in sorted(glob.glob(os.path.join(root, 'automations', '*', 'automation.toml'))):
        data = cache.read(path, simple_toml)
        if data.get('id'):
            configs.append((data, path))
    return configs, []


@contextlib.contextmanager
def context_db(uri):
    """sqlite3.Connection's own context manager does not close the connection."""
    db = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def user_text(payload):
    if payload.get('type') == 'user_message':
        return payload.get('message') or ''
    item = payload.get('item') if payload.get('type') == 'item_completed' else payload
    if isinstance(item, dict) and (item.get('type') == 'UserMessage' or
                                  item.get('type') == 'message' and item.get('role') == 'user'):
        return '\n'.join(c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict))
    return ''


def assistant_summary(text):
    """First sentence of the assistant's result, without heartbeat metadata or markup."""
    text = C.tag(text or '', 'message') or text or ''
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:```[^\n]*|#{1,6}\s+|>\s*|[-+*]\s+|\d+[.)]\s+)', '', text)
    text = re.sub(r'(\*\*|__|~~|`+)(.*?)\1', r'\2', text, flags=re.S)
    text = re.sub(r'(?<!\w)([*_])([^\n]+?)\1(?!\w)', r'\2', text)
    for sentence in re.split(r'(?<=[。！？!?])\s*|(?<=\.)\s+|\n+', text):
        sentence = sentence.strip()
        if sentence:
            return C.clip(sentence, 119) if len(sentence) > 120 else sentence
    return ''


class AutomationTurns:
    """Incremental heartbeat turn reader. Ordinary user turns never become automation runs."""
    def __init__(self):
        self.records = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY_LIMIT))
        self.current = None
        self.thread = C.CodexThread()
        self.mtime = 0

    def want(self, raw):
        return b'function_call_output' not in raw[:400] and b'"reasoning"' not in raw[:400]

    def feed(self, data, raw):
        payload = data.get('payload') or {}
        pt, timestamp = payload.get('type'), C.parse_ts(data.get('timestamp'))
        if pt == 'task_started':
            self.thread = C.CodexThread()
            self.current = None
        self.thread.feed(data, raw)
        text = user_text(payload)
        marker = re.match(r'^\s*<heartbeat>\s*<automation_id>([^<]+)</automation_id>', text)
        if text.strip() and not text.lstrip().startswith('<'):
            for records in self.records.values():
                for record in records:
                    if record.get('end') is not None and timestamp is not None and timestamp > record['end']:
                        record['replied_at'] = timestamp
        if payload.get('type') in ('agent_message', 'task_complete') or self.thread.last_asst_text_ts == data.get('timestamp'):
            for records in self.records.values():
                for record in records:
                    if timestamp and timestamp > (record.get('replied_at') or float('inf')):
                        record['reply_continued'] = True
        if marker and self.current is None:
            aid = marker.group(1).strip()
            self.current = {'id': 'heartbeat-%s' % data.get('timestamp'), 'start': timestamp, 'end': None,
                            'status': 'run', 'summary': M('schedule.result.run'), 'session_key': None}
            self.records[aid].append(self.current)
        if not self.current or self.current.get('end') is not None:
            return
        if text and not self.thread.last_user_ts:
            # Some desktop versions use response_item/message rather than item_completed/UserMessage.
            self.thread.last_user_ts = data.get('timestamp')
        self.current['last_activity'] = timestamp
        if self.thread.pending:
            self.current.update(status='stuck', summary=self.thread.pending['text'])
        elif pt in ('error', 'stream_error', 'turn_aborted'):
            self.current.update(status='fail', end=timestamp,
                                summary=payload.get('message') or M('note.aborted'))
        elif pt == 'task_complete':
            text = payload.get('last_agent_message') or next(
                (t['text'] for t in reversed(self.thread.turns) if t['role'] == 'assistant'), '')
            asks = quick_asks(self.thread.turns)
            self.current.update(status='stuck' if asks else 'ok', end=timestamp,
                                summary=assistant_summary(text))
        elif self.current['status'] in ('run', 'stuck') and not self.thread.pending:
            self.current.update(status='run', summary=assistant_summary(next(
                (t['text'] for t in reversed(self.thread.turns) if t['role'] == 'assistant'), '')) or M('schedule.result.run'))


def read_codex(root, rows, now, cache=None, heartbeat=None):
    cache, heartbeat = cache or FileCache(), heartbeat or {}
    configs, db_runs = read_codex_config(root, cache)
    by_automation = collections.defaultdict(list)
    for data in db_runs:
        sid = data.get('thread_id')
        row = rows.get('codex:' + sid) if sid else None
        start, end = C.parse_ts(data.get('created_at')), C.parse_ts(data.get('updated_at'))
        run = session_run(row, sid or start, start, end)
        if sid:
            run['session_key'] = 'codex:' + sid
        if not row:
            run['status'] = {'SUCCESS': 'ok', 'SUCCEEDED': 'ok', 'COMPLETED': 'ok', 'ACCEPTED': 'ok',
                             'FAILED': 'fail', 'ERROR': 'fail', 'RUNNING': 'run',
                             'WAITING': 'stuck', 'IN_PROGRESS': 'run'}.get(str(data.get('status')).upper(), 'unknown')
            run['end'] = end if run['status'] not in ('run', 'unknown') else None
        history = heartbeat.get(sid)
        if history:
            reply = next((t for t in reversed(history.thread.turns)
                          if t['role'] == 'user' and not t['text'].lstrip().startswith('<')
                          and (C.parse_ts(t['ts']) or 0) > (end or float('inf'))), None)
            if reply:
                run.update(replied_at=C.parse_ts(reply['ts']),
                           reply_continued=(C.parse_ts(history.thread.last_asst_text_ts) or 0) > C.parse_ts(reply['ts']))
        if history and not run.get('msg_ts'):
            run['msg_ts'] = C.parse_ts(history.thread.last_msg_ts)
        text = next((t['text'] for t in reversed(history.thread.turns) if t['role'] == 'assistant'), '') if history else ''
        run['summary'] = assistant_summary(text or (row or {}).get('line') or '')
        by_automation[data.get('automation_id')].append(run)
    out = []
    for data, path in configs:
        aid, tid = data['id'], data.get('target_thread_id')
        runs = by_automation[aid]
        row = rows.get('codex:' + tid) if tid else None
        if data.get('kind') == 'heartbeat' and tid:
            history = heartbeat.get(tid)
            if history:
                seen = {r['id'] for r in runs}
                for run in history.records.get(aid, []):
                    if run['id'] in seen:
                        continue
                    copy = dict(run, session_key='codex:' + tid,
                                msg_ts=(row or {}).get('msg_ts') or C.parse_ts(history.thread.last_msg_ts))
                    if row and abs((row.get('last_ts') or 0) - (copy.get('last_activity') or 0)) < 1:
                        latest = session_run(row, copy['id'], copy['start'])
                        copy.update(status=latest['status'])
                        if copy['end'] is None:
                            copy['end'] = latest['end']
                    if copy['status'] == 'run' and now - (copy.get('last_activity') or now) > C.TURN_DEAD_SECONDS:
                        copy.update(status='fail', summary=M('note.codex_incomplete'))
                    runs.append(copy)
        last = C.parse_ts(data.get('last_run_at'))
        fallback = False
        if not runs and last is not None:
            fallback = True
            run = session_run(row, 'last-%d' % last, last)
            history = heartbeat.get(tid)
            text = next((t['text'] for t in reversed(history.thread.turns) if t['role'] == 'assistant'), '') if history else ''
            run['summary'] = assistant_summary(text or (row or {}).get('line') or '')
            runs.append(run)
        cwds = data.get('cwds') or []
        if isinstance(cwds, str):
            try:
                cwds = json.loads(cwds)
            except ValueError:
                cwds = [cwds]
        task = {'id': aid, 'source': 'codex', 'title': data.get('name') or aid,
                'description': data.get('prompt') or '', 'enabled': data.get('status') == 'ACTIVE',
                'off_reason': M('schedule.paused'), 'kind': data.get('kind') or 'cron',
                'raw': data.get('rrule') or '', 'schedule': rrule_description(data.get('rrule')),
                'next_run': C.parse_ts(data.get('next_run_at')), 'last_run': last, 'runs': runs,
                'config_path': path, 'cwd': cwds, 'target_thread_id': tid,
                'notification_policy': data.get('notification_policy'), 'history_fallback': fallback}
        out.append(finish_task(task))
    return out


def parse_launchctl(text):
    out = {'loaded': True, 'state': None, 'runs': 0, 'exit_code': None, 'pid': None}
    # Only top-level service fields: nested endpoint / spawn records can have their own state or PID.
    for line in text.splitlines():
        match = re.match(r'^(?:\t| {4})(state|runs|last exit code|pid) = (.*?)\s*$', line)
        if not match:
            continue
        key, value = match.groups()
        if key == 'state':
            out['state'] = value
        else:
            try:
                out[{'runs': 'runs', 'last exit code': 'exit_code', 'pid': 'pid'}[key]] = int(value)
            except ValueError:
                pass
    return out


def launchctl_print(label):
    result = subprocess.run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)],
                            capture_output=True, text=True, timeout=2)
    return parse_launchctl(result.stdout) if result.returncode == 0 else {'loaded': False}


class Calendar(Cron):
    """launchd calendar dictionaries have wildcard missing fields and conjunctive day fields."""
    def __init__(self, data):
        self.data = data
        super().__init__(' '.join(str(data.get(key, '*')) for key in ('Minute', 'Hour', 'Day', 'Month', 'Weekday')))

    def matches_day(self, date):
        return date.month in self.month and date.day in self.day and (date.weekday() + 1) % 7 in self.weekday


def calendar_description(calendars):
    by_time, other = collections.defaultdict(set), []
    for calendar in calendars:
        if ('Hour' in calendar and 'Minute' in calendar and
                'Day' not in calendar and 'Month' not in calendar):
            days = {calendar['Weekday'] % 7} if 'Weekday' in calendar else set(range(7))
            by_time[(calendar['Hour'], calendar['Minute'])].update(days)
        else:
            other.append(cron_description(' '.join(
                str(calendar.get(key, '*')) for key in ('Minute', 'Hour', 'Day', 'Month', 'Weekday'))))
    by_days = collections.defaultdict(list)
    for pair, days in sorted(by_time.items()):
        by_days[tuple(sorted(days))].append('%02d:%02d' % pair)
    groups = []
    for days, times in by_days.items():
        times = join_messages(times)
        if len(days) == 7:
            groups.append(M('schedule.calendar_daily', times))
        elif days == (1, 2, 3, 4, 5):
            groups.append(M('schedule.calendar_weekdays', times))
        elif days == (1, 2, 3, 4, 5, 6):
            groups.append(M('schedule.calendar_mon_sat', times))
        else:
            names = join_messages([M('schedule.day.' + WEEKDAYS[(day - 1) % 7])
                                   for day in sorted(days, key=lambda day: (day - 1) % 7)])
            groups.append(M('schedule.calendar_weekly', names, times))
    return join_messages(groups + other, 'schedule.group_join')


def plist_file(path):
    with open(path, 'rb') as f:
        data = plistlib.load(f)
    return data if isinstance(data, dict) else {}


def log_position(path):
    try:
        st = os.stat(path)
        if stat.S_ISREG(st.st_mode):
            return {'path': path, 'offset': st.st_size, 'ino': st.st_ino, 'dev': st.st_dev, 'mtime': st.st_mtime}
    except (OSError, TypeError):
        pass
    return {'path': path, 'offset': 0, 'ino': None, 'dev': None}


def log_spans(start, end):
    out = {}
    for stream in ('stdout', 'stderr'):
        a, b = start.get(stream) or {}, end.get(stream) or {}
        same_file = a.get('ino') == b.get('ino') and a.get('dev') == b.get('dev') and a.get('path') == b.get('path')
        truncated = same_file and b.get('offset', 0) < a.get('offset', 0)
        out[stream] = dict(b, start=a.get('offset', 0) if same_file and not truncated else 0,
                           end=b.get('offset', 0), rotated=bool(a.get('ino') and not same_file) or truncated)
    return out


class LaunchHistory:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, 'schedules-launchd.json') if state_dir else None
        self.labels = {}
        if self.path and not os.path.islink(self.path):
            try:
                self.labels = json_file(self.path)
                if not isinstance(self.labels, dict):
                    self.labels = {}
            except (OSError, ValueError):
                pass

    def observe(self, label, state, paths, now):
        offsets = {stream: log_position(path) for stream, path in paths.items()}
        entry = self.labels.setdefault(label, {'since': now, 'seq': 0, 'history': [], 'previous': None})
        observations = entry.setdefault('observations', [])
        loaded, disabled = bool(state.get('loaded')), bool(state.get('disabled'))
        last = observations[-1] if observations else None
        if (last and last['loaded'] == loaded and bool(last.get('disabled')) == disabled
                and 0 <= now - last.get('last', last['at']) <= 90):
            last['last'] = now
        else:
            observations.append({'at': now, 'last': now, 'loaded': loaded})
            if disabled:
                observations[-1]['disabled'] = True
        entry['observations'] = [o for o in observations if now - o.get('last', o['at']) <= 30 * 86400]
        if not state.get('loaded'):
            return entry
        before = entry['previous']
        history = entry['history']
        pid = state.get('pid')
        active = next((run for run in history if run['status'] == 'run'), None)
        new_process = pid and (not before or pid != before.get('pid'))
        increased = bool(before and state.get('runs', 0) > before.get('runs', 0))
        ended = active and (not pid or pid != active.get('pid'))

        def append(start, end, status, positions, approximate=True):
            entry['seq'] += 1
            run = {'id': str(entry['seq']), 'label': label, 'start': start, 'end': end, 'status': status,
                   'pid': pid if status == 'run' else None, 'exit_code': state.get('exit_code') if end else None,
                   'logs': log_spans(positions, offsets), 'approximate': approximate, 'session_key': None}
            history.append(run)
            return run

        if (before is None or not history and not increased) and state.get('runs', 0) > 0 and state.get('exit_code') is not None:
            end = max((p['mtime'] for p in offsets.values() if p.get('mtime') is not None), default=None)
            baseline = append(None, end, 'ok' if state['exit_code'] == 0 else 'fail', offsets)
            baseline.update(before_monitoring=True, exit_code=state['exit_code'])
        if ended:
            code = state.get('exit_code')
            active.update(end=now, status='ok' if code == 0 else 'fail' if code is not None else 'unknown',
                          exit_code=code, logs=log_spans(active.get('offsets') or {}, offsets))
            active = None
        if new_process or (pid and active is None):
            active = append(now, None, 'run', offsets)
            active['offsets'] = offsets
        elif increased and not pid and not ended:
            append(before['at'], now, 'ok' if state.get('exit_code') == 0 else
                   'fail' if state.get('exit_code') is not None else 'unknown', before.get('offsets') or {})
        if active:
            active['logs'] = log_spans(active.get('offsets') or offsets, offsets)
        for run in history:
            run['summary'] = M('schedule.exit', run['exit_code']) if run.get('exit_code') is not None else M('schedule.result.' + run['status'])
            if run.get('before_monitoring'):
                run['summary'] = M('schedule.before_monitoring_result', run['summary'])
        entry['history'] = history[-HISTORY_LIMIT:]
        # A failed print is an observation gap, not a PID disappearance or a counter reset.
        if state.get('loaded'):
            entry['previous'] = dict(state, at=now, offsets=offsets)
        return entry

    def state_periods(self, label):
        observations = (self.labels.get(label) or {}).get('observations', [])
        periods = []
        for index, observation in enumerate(observations):
            end = observation.get('last', observation['at'])
            following = observations[index + 1] if index + 1 < len(observations) else None
            connected = following is not None and 0 <= following['at'] - end <= 90
            periods.append({'start': observation['at'], 'end': following['at'] if connected else end,
                            'end_inclusive': not connected,
                            'enabled': observation['loaded'] and not observation.get('disabled', False),
                            'inferred': False, 'end_inferred': bool(connected),
                            'reason': 'disabled' if observation.get('disabled') else
                                      'enabled' if observation['loaded'] else 'unloaded'})
        return periods

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(self.labels, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def log(self, label, run_id):
        entry = self.labels.get(label) or {}
        run = next((r for r in entry.get('history', []) if r['id'] == run_id), None)
        if run is None:
            return None
        lines, unavailable = [], False
        for stream, span in (run.get('logs') or {}).items():
            path = span.get('path')
            if not path:
                continue
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_dev) != (span.get('ino'), span.get('dev')) or st.st_size < span['end']:
                    unavailable = True
                    continue
                start, end = span['start'], span['end']
                begin = max(start, end - 65536)
                os.lseek(fd, begin, os.SEEK_SET)
                text = os.read(fd, max(0, end - begin)).decode('utf-8', 'replace')
                if begin > start:
                    text = text.partition('\n')[2]
                text = redact(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text))
                lines.extend({'stream': stream, 'text': line} for line in text.splitlines()[-50:])
                unavailable = unavailable or span.get('rotated', False)
            except OSError:
                unavailable = True
            finally:
                if fd is not None:
                    os.close(fd)
        return {'lines': lines[-50:], 'unavailable': unavailable}


def read_launchd(root, now, history, runner=None, cache=None):
    if runner is None and not shutil.which('launchctl'):
        return [], []
    runner, cache = runner or launchctl_print, cache or FileCache()
    tasks, services = [], []
    for path in sorted(glob.glob(os.path.join(root, '*.plist'))):
        data = cache.read(path, plist_file)
        label = data.get('Label')
        if not isinstance(label, str) or not label:
            continue
        try:
            state = runner(label)
            if isinstance(state, str):
                state = parse_launchctl(state)
        except (OSError, subprocess.SubprocessError):
            state = {'loaded': False}
        calendars = data.get('StartCalendarInterval') or []
        calendars = calendars if isinstance(calendars, list) else [calendars]
        service = bool(data.get('KeepAlive')) and not calendars and not data.get('StartInterval')
        paths = {'stdout': data.get('StandardOutPath'), 'stderr': data.get('StandardErrorPath')}
        disabled = bool(data.get('Disabled'))
        state = dict(state, disabled=disabled)
        entry = history.observe(label, state, paths, now)
        runs = [dict(r) for r in entry['history']]
        if not state.get('loaded'):
            for run in runs:
                if run['status'] == 'run':
                    run.update(status='unknown', summary=M('schedule.unloaded'))
        args = data.get('ProgramArguments') or [data.get('Program') or '']
        enabled = bool(state.get('loaded')) and not disabled
        task = {'id': label, 'source': 'launchd', 'title': label, 'service': service,
                'enabled': enabled,
                'off_reason': M('schedule.disabled' if disabled else 'schedule.unloaded') if not enabled else None,
                'kind': 'service' if service else 'interval' if data.get('StartInterval') else 'calendar',
                'description': ' '.join(shlex.quote(str(arg)) for arg in args), 'config_path': path,
                'log_paths': [p for p in paths.values() if p], 'runs': runs, 'next_run': None,
                'loaded': bool(state.get('loaded')), 'pid': state.get('pid'), 'state': state.get('state'),
                'monitoring_since': entry['since'], 'run_at_load': bool(data.get('RunAtLoad')),
                'keep_alive': data.get('KeepAlive'),
                'schedule': M('schedule.at_load' if data.get('RunAtLoad') else 'schedule.on_demand'),
                'raw': 'RunAtLoad' if data.get('RunAtLoad') else ''}
        task['state_periods'] = history.state_periods(label)
        if service:
            active = next((r for r in runs if r['status'] == 'run'), None)
            starts = [r for r in runs if r.get('start') is not None and now - 86400 <= r['start'] <= now]
            task.update(uptime=now - active['start'] if active else None,
                        restarts_24h=max(0, len(starts) - (1 if runs and len(starts) == len(runs) else 0)),
                        history_limited=len(runs) >= HISTORY_LIMIT)
            services.append(finish_task(task))
            continue
        if calendars:
            try:
                rules = [Calendar(calendar) for calendar in calendars]
                nexts = [rule.next(now) for rule in rules]
                task['next_run'] = min((ts for ts in nexts if ts is not None), default=None)
                task['schedule'] = calendar_description(calendars)
                task['raw'] = 'StartCalendarInterval ' + json.dumps(calendars, separators=(',', ':'))
                task['raw_summary'] = M('schedule.launchd_times', len(calendars))
                def previous(ts):
                    return max((p for p in (rule.previous(ts) for rule in rules) if p is not None), default=None)
                missed_run(task, previous, now, entry['since'])
            except (ValueError, TypeError, AttributeError):
                task.update(schedule=M('schedule.invalid'), raw=str(data.get('StartCalendarInterval')))
        elif data.get('StartInterval'):
            try:
                interval = int(data['StartInterval'])
                if interval <= 0:
                    raise ValueError()
                last = max((r['start'] for r in runs if r.get('start') is not None), default=entry['since'])
                unit = 3600 if interval % 3600 == 0 else 60 if interval % 60 == 0 else 1
                key = {3600: 'schedule.every_hours', 60: 'schedule.every_minutes', 1: 'schedule.every_seconds'}[unit]
                task.update(schedule=M(key, interval // unit), raw='StartInterval %d' % interval,
                            next_run=last + (int((now - last) / interval) + 1) * interval, next_estimated=True)
                missed_run(task, lambda ts: last + int((ts - last) / interval) * interval if ts >= last + interval else None,
                           now, entry['since'])
            except (ValueError, TypeError):
                task.update(schedule=M('schedule.invalid'), raw=str(data['StartInterval']))
        tasks.append(finish_task(task))
    history.save()
    return tasks, services


class ScheduleCollector:
    def __init__(self, state_dir, codex_dir=None, desktop_root=None, launchd_root=None, runner=None,
                 claude_log_root=None, pmset_runner=None):
        home = os.path.expanduser('~')
        self.codex_dir = codex_dir or os.path.join(home, '.codex')
        self.desktop_root = desktop_root
        self.launchd_root = launchd_root or os.path.join(home, 'Library', 'LaunchAgents')
        self.runner = runner
        self.cache = FileCache()
        self.history = LaunchHistory(state_dir)
        self.heartbeat_tails = {}
        self.claude_tails = {}
        self.session_paths = {}
        self.claude_logs = ClaudeLogs(claude_log_root if claude_log_root is not None else
                                     os.path.join(home, 'Library', 'Logs', 'Claude') if desktop_root else None)
        self.diagnostics = Diagnostics(self.claude_logs, self.history, pmset_runner)

    def collect(self, rows=(), codex_paths=None, now=None):
        now = time.time() if now is None else now
        rows = {r['key']: r for r in rows}
        states = {}
        configs, db_runs = read_codex_config(self.codex_dir, self.cache)
        targets = {data.get('target_thread_id') for data, _ in configs if data.get('kind') == 'heartbeat'}
        targets.update(run.get('thread_id') for run in db_runs)
        paths = {tid: path for tid, path in (codex_paths or {}).items() if tid in targets}
        # A paused automation can refer to a thread older than the session list's --days window.
        for tid in targets:
            if not isinstance(tid, str) or tid in paths:
                continue
            matches = glob.glob(os.path.join(self.codex_dir, 'sessions', '*', '*', '*',
                                             'rollout-*' + glob.escape(tid) + '.jsonl'))
            for path in matches:
                try:
                    if tid not in paths or os.path.getmtime(path) > os.path.getmtime(paths[tid]):
                        paths[tid] = path
                except OSError:
                    continue
        self.session_paths = {'codex:' + tid: path for tid, path in paths.items()}
        for tid, path in paths.items():
            tail = self.heartbeat_tails.get(path)
            if tail is None:
                tail = self.heartbeat_tails[path] = C.JsonlTail(path, AutomationTurns)
            try:
                states[tid] = tail.poll()
            except OSError:
                pass
        if self.desktop_root:
            self.claude_logs.update(now)
            used_claude = set()
            for path in glob.glob(os.path.join(self.desktop_root, '*', '*', 'local_*.json')):
                data = self.cache.read(path, local_session)
                sid = data.get('cliSessionId')
                if not data.get('scheduledTaskId') or not sid or 'claude:' + sid in rows:
                    continue
                matches = glob.glob(os.path.join(C.CLAUDE_PROJECTS, '*', glob.escape(sid) + '.jsonl'))
                if not matches:
                    continue
                session_path = matches[0]
                self.session_paths['claude:' + sid] = session_path
                used_claude.add(session_path)
                tail = self.claude_tails.get(session_path)
                if tail is None:
                    tail = self.claude_tails[session_path] = C.JsonlTail(session_path, C.ClaudeSession)
                try:
                    session = tail.poll()
                except OSError:
                    continue
                asks = quick_asks(session.turns)
                rows['claude:' + sid] = {
                    'key': 'claude:' + sid, 'status': 'wait' if asks else 'ok',
                    'items': [{'text': text, 'from_ts': C.parse_ts(session.last_asst_text_ts)} for text in asks],
                    'last_ts': C.parse_ts(session.last_ts), 'msg_ts': C.parse_ts(session.last_msg_ts),
                    'scheduled_run_end': C.parse_ts(session.scheduled_run_end),
                    'scheduled_reply_ts': C.parse_ts(session.scheduled_reply_ts),
                    'scheduled_reply_continued': (C.parse_ts(session.last_asst_ts) or 0) >
                                               (C.parse_ts(session.scheduled_reply_ts) or float('inf'))}
            self.claude_tails = {p: tail for p, tail in self.claude_tails.items() if p in used_claude}
        tasks = read_claude(self.desktop_root, rows, now, self.cache, self.claude_logs) if self.desktop_root else []
        tasks.extend(read_codex(self.codex_dir, rows, now, self.cache, states))
        launchd, services = read_launchd(self.launchd_root, now, self.history, self.runner, self.cache)
        tasks.extend(launchd)
        used = set(paths.values())
        self.heartbeat_tails = {p: tail for p, tail in self.heartbeat_tails.items() if p in used}
        return {'ready': True, 'now': now, 'refresh_seconds': REFRESH_SECONDS, 'tasks': tasks, 'services': services}
