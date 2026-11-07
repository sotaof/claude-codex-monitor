"""Read-only, injectable evidence for scheduled runs. Diagnostics are evaluated on demand."""
import datetime as dt
import glob
import os
import re
import subprocess
import threading
import time

from . import collect as C

WINDOW = 30 * 60
STAMP = re.compile(r'^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?: ?[+-]\d{4})?)')
EVENT = re.compile(r'\[CCDScheduledTasks\] (?:'
                   r'(?P<action>Spawning new session for scheduled task|Delaying dispatch for) (?P<task>[^\s{]+)'
                   r'|(?P<change>Updated|Saved|Disabled|Enabled) scheduled task: (?P<changed>[^\s]+))')
STATUS_EVENT = re.compile(r'\[CCDScheduledTasksApi\] updateScheduledTaskStatus:\s*'
                          r'scheduledTaskId=([^,\s]+),\s*status=(disabled|enabled)\b')
STATE_KINDS = ('updated', 'saved', 'disabled', 'enabled')


def period_at(periods, timestamp):
    return next((p for p in reversed(periods) if p['start'] <= timestamp and
                 (p['end'] is None or timestamp < p['end'] or
                  p.get('end_inclusive') and timestamp == p['end'])), None)


def log_timestamp(line):
    match = STAMP.match(line)
    if not match:
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r' ?([+-]\d{2})(\d{2})$', r'\1:\2', match[1])).timestamp()
    except ValueError:
        return None


def claude_event(line):
    ts = log_timestamp(line)
    if ts is None:
        return None
    status = STATUS_EVENT.search(line)
    if status:
        return {'at': ts, 'task': status[1], 'kind': status[2]}
    match = EVENT.search(line)
    if not match:
        return None
    event = {'at': ts, 'task': match['changed'] or match['task'],
             'kind': match['change'].lower() if match['change'] else
             'spawn' if match['action'].startswith('Spawning') else 'delay'}
    cron = re.search(r"cronExpression:\s*'([^']+)'", line)
    if cron:
        event['cron'] = cron[1]
    return event


def parse_pmset(text):
    events = []
    for line in text.splitlines():
        ts = log_timestamp(line)
        match = re.match(r'^\S+\s+\S+\s+(?:[+-]\d{4}\s+)?(Sleep|Wake|DarkWake)(?: {2,}|\t+)', line)
        if ts is not None and match:
            events.append((ts, match[1]))
    return sorted(set(events))


def asleep_at(events, timestamp):
    previous = next((event for event in reversed(events) if event[0] <= timestamp), None)
    return None if previous is None else previous[1] == 'Sleep'


class ClaudeLogs:
    """Incremental index per log inode; no full log text or conversation content is retained."""
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.lock = threading.RLock()

    def update(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            paths = glob.glob(os.path.join(self.root, 'main*.log')) if self.root else []
            for path in paths:
                try:
                    st = os.stat(path)
                    old = self.files.get(path)
                    if not old or old['ino'] != st.st_ino or st.st_size < old['offset']:
                        old = {'ino': st.st_ino, 'offset': 0, 'events': [], 'first': None, 'last': None,
                               'activity': {}, 'minute': None}
                    if old['offset'] == st.st_size:
                        self.files[path] = old
                        continue
                    with open(path, 'rb') as f:
                        f.seek(old['offset'])
                        offset, last_line = old['offset'], None
                        while True:
                            raw = f.readline()
                            if not raw or not raw.endswith(b'\n'):
                                old['offset'] = offset
                                break
                            offset += len(raw)
                            line = raw.decode('utf-8', 'replace')
                            if STAMP.match(line):
                                last_line = line
                                if old['first'] is None:
                                    old['first'] = log_timestamp(line)
                                minute = line[:16]
                                if minute != old['minute']:
                                    ts = log_timestamp(line)
                                    old['minute'] = minute
                                    if ts is not None:
                                        old['activity'][minute] = ts
                            event = claude_event(line) if '[CCDScheduledTasks' in line else None
                            if event:
                                old['events'].append(event)
                        if last_line:
                            old['last'] = log_timestamp(last_line)
                    # Keep small state-change records so long pauses do not age out of the index.
                    old['events'] = [e for e in old['events']
                                     if e['at'] >= now - 31 * 86400 or e['kind'] in STATE_KINDS]
                    old['activity'] = {minute: ts for minute, ts in old['activity'].items()
                                       if ts >= now - 31 * 86400}
                    self.files[path] = old
                except OSError:
                    self.files.pop(path, None)
            self.files = {p: value for p, value in self.files.items() if p in paths}

    def events(self):
        with self.lock:
            return sorted((dict(e) for value in self.files.values() for e in value['events']), key=lambda e: e['at'])

    def available(self):
        with self.lock:
            return any(value['first'] is not None for value in self.files.values())

    def state_periods(self, task_id, enabled, since, now):
        events = [e for e in self.events()
                  if e['task'] == task_id and e['kind'] in STATE_KINDS and e['at'] <= now]
        start = min([since] + [e['at'] for e in events])
        periods = [{'start': start, 'end': None, 'enabled': enabled,
                    'inferred': True, 'reason': 'current_config'}]
        for event in events:
            previous = periods[-1]
            inferred = event['kind'] in ('updated', 'saved')
            if inferred and (not enabled or previous['enabled']):
                continue
            state = event['kind'] != 'disabled'
            if state == previous['enabled'] and inferred == previous['inferred']:
                continue
            previous.update(end=event['at'], end_inferred=inferred)
            periods.append({'start': event['at'], 'end': None, 'enabled': state,
                            'inferred': inferred, 'reason': event['kind']})
        return [p for p in periods if p['end'] is None or p['end'] > p['start']]

    def covers(self, start, end):
        with self.lock:
            ranges = sorted((v['first'], v['last']) for v in self.files.values() if v['first'] is not None)
        edge = start
        for first, last in ranges:
            if first > edge:
                break
            edge = max(edge, last)
            if edge >= end:
                return True
        return False

    def run_mode(self, task_id, start, previous, grace):
        if start is not None:
            if any(e['kind'] == 'spawn' and e['task'] == task_id and abs(e['at'] - start) <= 120
                   for e in self.events()):
                return 'automatic', False
            if self.covers(start - 120, start + 120):
                return 'manual', False
            due = previous(start)
            if due is not None and due <= start <= due + grace:
                return 'automatic', True
        return 'manual', True

    def cron_segments(self, task_id, expression, since):
        """Use recorded cron changes rather than applying today's hours to older triggers."""
        events = [e for e in self.events() if e['task'] == task_id]
        spawns = [e for e in events if e['kind'] == 'spawn' and e.get('cron')]
        segments = []
        previous = None
        for spawn in spawns:
            if not segments:
                segments.append([since, None, spawn['cron']])
            elif segments[-1][2] != spawn['cron']:
                changes = [e['at'] for e in events if e['kind'] == 'updated'
                           and previous['at'] < e['at'] <= spawn['at']]
                edge = max(changes) if changes else spawn['at']
                segments[-1][1] = edge
                segments.append([edge, None, spawn['cron']])
            previous = spawn
        if not segments:
            return [(since, None, expression)]
        if segments[-1][2] != expression:
            changes = [e['at'] for e in events if e['kind'] == 'updated' and e['at'] > previous['at']]
            if changes:
                edge = max(changes)
                segments[-1][1] = edge
                segments.append([edge, None, expression])
        return segments

    def diagnose(self, task_id, due, grace):
        all_events = self.events()
        events = [e for e in all_events if due - 120 <= e['at'] <= due + grace]
        own = [e for e in events if e['task'] == task_id]
        changes = [e for e in all_events if e['task'] == task_id
                   and e['kind'] in STATE_KINDS and e['at'] <= due]
        change = changes[-1] if changes else None
        covered = self.covers(due, due + grace)
        with self.lock:
            active = any(due - 120 <= ts <= due + grace for value in self.files.values()
                         for ts in value['activity'].values())
        return {
            'desktop_running': True if active else None,
            'dispatched': True if any(e['kind'] == 'spawn' for e in own) else False if covered else None,
            'delayed': True if any(e['kind'] == 'delay' for e in own) else False if covered else None,
            'modified': True if any(e['kind'] in STATE_KINDS for e in own)
                        else False if covered else None,
            'disabled': True if change and change['kind'] == 'disabled' else
                        False if change and change['kind'] == 'enabled' else None,
            'last_change_at': change['at'] if change else None,
            'last_change_kind': change['kind'] if change else None,
        }


def pmset_log():
    return subprocess.run(['pmset', '-g', 'log'], capture_output=True, text=True,
                          timeout=20, check=True).stdout


class Diagnostics:
    def __init__(self, logs, history, runner=None):
        self.logs, self.history = logs, history
        self.runner = runner or pmset_log
        self.power = None
        self.power_at = 0
        self.cache = {}
        self.lock = threading.RLock()

    def get(self, task, run, now=None):
        now = time.time() if now is None else now
        key = (task['source'], task['id'], run['id'])
        with self.lock:
            cached = self.cache.get(key)
            if cached and now - cached[0] < 300:
                return cached[1]
            if self.power is None or now - self.power_at >= 300:
                try:
                    self.power = parse_pmset(self.runner())
                except (OSError, subprocess.SubprocessError):
                    self.power = []
                self.power_at = now
            due = run['start']
            result = {'asleep': asleep_at(self.power, due), 'launchd_loaded': None,
                      'desktop_running': None, 'dispatched': None, 'delayed': None,
                      'modified': None, 'disabled': None}
            if task['source'] == 'claude':
                self.logs.update(now)
                result.update(self.logs.diagnose(task['id'], due, task.get('grace_seconds', WINDOW)))
                period = period_at(task.get('state_periods', []), due)
                if period and period['reason'] != 'current_config':
                    result['disabled'] = not period['enabled']
                    result['state_inferred'] = period['inferred']
            if task['source'] == 'launchd':
                observations = (self.history.labels.get(task['id']) or {}).get('observations', [])
                interval = next((o for o in reversed(observations) if o['at'] <= due <= o.get('last', o['at'])), None)
                before = next((o for o in reversed(observations) if o['at'] <= due), None)
                after = next((o for o in observations if o['at'] >= due), None)
                if interval:
                    result['launchd_loaded'] = interval['loaded']
                elif (before and after and after['at'] - before['at'] <= 90
                        and before['loaded'] == after['loaded']):
                    result['launchd_loaded'] = before['loaded']
            self.cache[key] = (now, result)
            self.cache = {k: value for k, value in self.cache.items() if now - value[0] < 300}
            return result
