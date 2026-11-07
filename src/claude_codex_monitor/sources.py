"""Sources of things that need you, beyond the built-in agent sessions and scheduled tasks.

A source is a plugin installed as a separate Python package. It registers a factory under the entry point group
``claude_codex_monitor.sources``; the factory gets a :class:`Context` and returns a :class:`Source`. The monitor
polls each source in a thread of its own, keeps what you did with each item (done, later, not needed) in one place,
and shows the items on the Today page and, if the source has one, on a tab of its own.

Items are plain dicts::

    {'key': 'abc',              # unique within the source
     'kind': 'reply',           # one of KINDS: what you have to do next
     'title': 'Which partition is the latest?',
     'who': 'Alex', 'where': 'Direct message',
     'text': 'the message, or a short excerpt',
     'since': 1700000000,       # when it started waiting (seconds)
     'updated': 1700000500,     # last activity; new activity brings a "later" item back
     'due': None,               # deadline (seconds) or None
     'url': None,               # where to open it, if anywhere (http/https only)
     'open': True}              # False once the source can tell it is over

Nothing here imports a plugin unless it is installed, and a plugin that fails to load or to poll only shows an error
for itself.
"""
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback

ENTRY_POINT_GROUP = 'claude_codex_monitor.sources'
DAY_BOUNDARY_HOUR = 4          # "today" lasts until 04:00 the next morning
KINDS = ('reply', 'approve', 'deliver', 'waiting', 'alert', 'info')
ACTIONS = ('done', 'later', 'skip', 'restore')
NAME_RE = re.compile(r'^[a-z][a-z0-9-]{0,31}$')
STATIC_RE = re.compile(r'^[A-Za-z0-9_-]+\.(js|css|svg|png)$')
STATIC_TYPES = {'js': 'application/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8',
                'svg': 'image/svg+xml', 'png': 'image/png'}
MAX_ITEMS = 500


def day_window(now=None, boundary_hour=DAY_BOUNDARY_HOUR):
    """The local day "today" stands for, as (start, end): from that day's midnight to boundary_hour the next morning.

    Before boundary_hour it is still the previous day, so working until 1 a.m. does not start a new day, and the hours
    between midnight and boundary_hour belong to both days: nothing that arrives late at night is missed the next
    morning."""
    now = time.time() if now is None else now
    local = dt.datetime.fromtimestamp(now)
    day = local.date() - dt.timedelta(days=1) if local.hour < boundary_hour else local.date()
    start = dt.datetime.combine(day, dt.time())
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(boundary_hour))
    return start.timestamp(), end.timestamp()


class Context:
    """What a source gets from the monitor."""
    def __init__(self, name, state_dir, lang, demo=False):
        self.name = name
        self.state_dir = state_dir      # the source's own folder: <state dir>/ext/<name>, readable by you only
        self.lang = lang
        self.demo = demo


class Source:
    """Base class for plugins. Override items(); the rest is optional."""
    name = ''                 # lower case, digits and dashes; used in URLs and the state folder
    label = {}                # {'en': ..., 'zh': ...}: the tab and column title
    interval = 60             # seconds between polls
    static_dir = None         # folder with the files the tab needs, served at /ext/<name>/<file>
    tab_script = None         # file in static_dir that draws the tab, or None for no tab

    def __init__(self, context):
        self.context = context

    def items(self, now):
        """Return the current items (see the module docstring). Called from the source's own thread."""
        return []

    def status(self):
        """Optional: a dict shown with the source, for example which tools are missing."""
        return {}

    def handle(self, method, path, query, body):
        """Optional: answer /api/ext/<name>/<path>. Return (status code, JSON-able value) or None for 404."""
        return None


class EnvEntryPoint:
    """A source named in $CCM_SOURCES as name=module:factory, for a plugin on PYTHONPATH that is not pip-installed."""
    def __init__(self, name, value):
        self.name, self.value = name, value

    def load(self):
        import importlib
        module, _, attr = self.value.partition(':')
        obj = importlib.import_module(module)
        for part in filter(None, attr.split('.')):
            obj = getattr(obj, part)
        return obj


def _env_entry_points():
    found = []
    for part in os.environ.get('CCM_SOURCES', '').split(','):
        name, _, value = part.strip().partition('=')
        if name and value:
            found.append(EnvEntryPoint(name.strip(), value.strip()))
    return found


def _entry_points():
    try:
        from importlib import metadata
    except ImportError:         # pragma: no cover - Python < 3.8 is not supported anyway
        return _env_entry_points()
    eps = metadata.entry_points()
    if hasattr(eps, 'select'):
        installed = list(eps.select(group=ENTRY_POINT_GROUP))
    else:
        installed = list(eps.get(ENTRY_POINT_GROUP, []))         # Python 3.9
    return installed + _env_entry_points()


def load(state_dir, lang='', demo=False, entry_points=None):
    """Load the installed sources. Returns (sources, errors); errors are {'name', 'error'} for the page."""
    found, errors, seen = [], [], set()
    for ep in _entry_points() if entry_points is None else entry_points:
        name = getattr(ep, 'name', '?')
        try:
            if not NAME_RE.match(name):
                raise ValueError('source name must be lower case letters, digits and dashes: %r' % name)
            if name in seen:
                raise ValueError('another source already uses the name %r' % name)
            folder = os.path.join(state_dir, 'ext', name)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            src = ep.load()(Context(name, folder, lang, demo))
            if not isinstance(src, Source):
                raise TypeError('%s did not return a Source' % name)
            src.name = name
            seen.add(name)
            found.append(src)
        except Exception as e:
            print('claude-codex-monitor: source %s did not load: %r' % (name, e), file=sys.stderr, flush=True)
            errors.append({'name': name, 'error': repr(e)})
    return found, errors


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v, limit):
    return v[:limit] if isinstance(v, str) else ''


def clean(name, raw):
    """Keep only well-formed fields of an item from a plugin, so the page can trust their types."""
    if not isinstance(raw, dict) or not isinstance(raw.get('key'), str) or not raw['key']:
        return None
    url = raw.get('url')
    return {'key': name + ':' + raw['key'][:200], 'source': name,
            'kind': raw.get('kind') if raw.get('kind') in KINDS else 'info',
            'title': _text(raw.get('title'), 300), 'who': _text(raw.get('who'), 100),
            'where': _text(raw.get('where'), 200), 'text': _text(raw.get('text'), 2000),
            'since': _num(raw.get('since')), 'updated': _num(raw.get('updated')), 'due': _num(raw.get('due')),
            'url': url if isinstance(url, str) and re.match(r'^https?://', url) else None,
            'open': raw.get('open') is not False}


class Poller:
    """Polls one source in its own thread and keeps the last good result."""
    def __init__(self, source):
        self.source = source
        self.items = []
        self.error = None
        self.polled = None
        self.stop = threading.Event()

    def poll(self, now=None):
        now = time.time() if now is None else now
        try:
            raw = self.source.items(now) or []
            items = [i for i in (clean(self.source.name, r) for r in list(raw)[:MAX_ITEMS]) if i]
            self.items, self.error, self.polled = items, None, now
        except Exception as e:
            if repr(e) != self.error:
                traceback.print_exc()
            self.error = repr(e)

    def run(self):
        while not self.stop.is_set():
            self.poll()
            if self.stop.wait(max(5, int(getattr(self.source, 'interval', 60) or 60))):
                return

    def start(self):
        threading.Thread(target=self.run, daemon=True, name='source-' + self.source.name).start()


class ItemState:
    """What you did with each plugin item: done, later (until a time) or skipped (not needed). One file for all."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding='utf-8') as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def set(self, key, action, until=None, now=None):
        now = time.time() if now is None else now
        if action not in ACTIONS:
            raise ValueError(action)
        with self.lock:
            previous = self.data.get(key)
            if action == 'restore':
                self.data.pop(key, None)
            else:
                entry = {'state': action, 'at': now}
                if action == 'later':
                    until = _num(until)
                    if until is None or until <= now:
                        raise ValueError(until)
                    entry['until'] = until
                self.data[key] = entry
            self.save()
            return previous

    def put_back(self, key, previous):
        """Undo: put back what set() replaced."""
        with self.lock:
            if previous is None:
                self.data.pop(key, None)
            else:
                self.data[key] = previous
            self.save()

    def state(self, item, now):
        """'done', 'skip', 'later' or None (it needs you). A "later" item comes back when its time comes or when
        something new happens in it; done and skipped items stay put."""
        entry = self.data.get(item['key'])
        if not entry:
            return None
        if entry.get('state') == 'later':
            if now >= entry.get('until', 0) or (item.get('updated') or 0) > entry.get('at', 0):
                return None
        return entry.get('state')

    def prune(self, keep, before):
        """Forget items no source reports any more, once they are old."""
        with self.lock:
            old = [k for k, v in self.data.items() if k not in keep and v.get('at', 0) < before]
            for k in old:
                del self.data[k]
            if old:
                self.save()
