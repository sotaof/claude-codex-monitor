"""Cursor agent chats: ~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl, one per chat.

Each line is a user message ({"role": "user", "message": {"content": [...]}}, the text wrapped in <user_query> and
preceded by a <timestamp>), an assistant message (text and tool_use blocks), or {"type": "turn_ended", "status": ...}.
Assistant lines carry no time of their own: they get the time of the user message they answer, and the last one gets
the file's modification time.

The command line agent keeps its chats elsewhere (~/.cursor/chats/*/store.db, a content-addressed store); those are
not read.
"""
import datetime as dt
import glob
import os
import re

HOME = os.path.expanduser('~')
CURSOR_PROJECTS = os.path.join(HOME, '.cursor', 'projects')
TS_RE = re.compile(r'<timestamp>\s*(.+?)\s*\(UTC([+-]\d{1,2})(?::?(\d{2}))?\)\s*</timestamp>')
QUERY_RE = re.compile(r'<user_query>\s*(.*?)\s*</user_query>', re.S)
TEMP_PREFIXES = ('var-folders-', 'private-var-folders-', 'private-tmp-', 'tmp-')
# The empty folders this monitor and its plugins run model tools in (tempfile.mkdtemp: prefix + 8 characters)
OWN_WORK_RE = re.compile(r'(?:^|-)claude-codex-monitor-[A-Za-z0-9-]{8}$')


def configure(cursor_dir):
    global CURSOR_PROJECTS
    CURSOR_PROJECTS = os.path.join(cursor_dir, 'projects')


def chat_files():
    """Top-level chat logs (not subagents), skipping projects in temporary folders that no longer exist."""
    out = []
    for path in glob.glob(os.path.join(CURSOR_PROJECTS, '*', 'agent-transcripts', '*', '*.jsonl')):
        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        # Tools run the agent in throwaway folders (ours among them); skip those once the folder is gone
        if OWN_WORK_RE.search(project) or project.startswith(TEMP_PREFIXES) and not cwd_of(project):
            continue
        if os.path.splitext(os.path.basename(path))[0] == os.path.basename(os.path.dirname(path)):
            out.append(path)
    return out


def parse_stamp(text):
    """'Saturday, Nov 7, 2026, 10:14 PM (UTC+8)' -> ISO time, or None."""
    m = TS_RE.search(text or '')
    if not m:
        return None
    try:
        local = dt.datetime.strptime(m.group(1).strip(), '%A, %b %d, %Y, %I:%M %p')
    except ValueError:
        return None
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    tz = dt.timezone(dt.timedelta(hours=hours, minutes=minutes if hours >= 0 else -minutes))
    return local.replace(tzinfo=tz).isoformat()


_cwd_cache = {}


def _dashed(name):
    """A folder name as Cursor writes it in a project name: everything but letters and digits becomes '-'."""
    return re.sub(r'[^A-Za-z0-9\u0080-\uffff]', '-', name)


def cwd_of(project):
    """The folder a project name stands for. Cursor turns every '/', '.', '_' and space of the path into '-', so walk
    down from / comparing each level's real folder names, written the same way. None when nothing matches."""
    if project in _cwd_cache:
        return _cwd_cache[project]

    def walk(base, rest):
        if not rest:
            return base
        try:
            names = os.listdir(base)
        except OSError:
            return None
        for name in sorted(names, key=len, reverse=True):          # the longest match first: my-app before my
            d = _dashed(name)
            if (rest == d or rest.startswith(d + '-')) and os.path.isdir(os.path.join(base, name)):
                hit = walk(os.path.join(base, name), rest[len(d) + 1:])
                if hit:
                    return hit
        return None
    found = walk('/', project) if project and project != 'empty-window' else None
    _cwd_cache[project] = found
    return found


def _texts(content):
    return [b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text']


class CursorChat:
    """What a chat log says so far: its turns, whether a turn is running, and how the last one ended."""

    def __init__(self):
        self.turns = []              # {'role', 'ts', 'text'} like the Claude and Codex readers
        self.first_user = ''
        self.first_ts = None
        self.last_user_ts = None
        self.in_turn = False
        self.last_status = None      # status of the last turn_ended: success, error, aborted...
        self.last_tool = None
        self.tools = 0
        self.mtime = 0
        self._clock = None           # time of the latest user message, for the assistant lines after it

    @staticmethod
    def want(raw):
        return True

    def feed(self, d, raw):
        if d.get('type') == 'turn_ended':
            self.in_turn = False
            self.last_status = d.get('status')
            return
        role, content = d.get('role'), (d.get('message') or {}).get('content') or []
        if not isinstance(content, list):
            return
        if role == 'user':
            text = '\n'.join(_texts(content))
            ts = parse_stamp(text) or self._clock
            q = QUERY_RE.search(text)
            text = (q.group(1) if q else TS_RE.sub('', text)).strip()
            if not text:
                return
            self._clock = ts
            self.first_user = self.first_user or text
            self.first_ts = self.first_ts or ts
            self.last_user_ts = ts
            self.in_turn, self.last_status = True, None
            self.turns.append({'role': 'user', 'ts': ts, 'text': text})
        elif role == 'assistant':
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'tool_use':
                    self.tools += 1
                    self.last_tool = b.get('name')
            text = '\n'.join(t for t in _texts(content) if t.strip()).strip()
            if text:
                self.turns.append({'role': 'assistant', 'ts': self._clock, 'text': text})

    def timed_turns(self):
        """The turns, with the last assistant reply stamped with the log's modification time."""
        turns = [dict(t) for t in self.turns]
        if turns and turns[-1]['role'] == 'assistant' and self.mtime:
            turns[-1]['ts'] = dt.datetime.fromtimestamp(self.mtime, dt.timezone.utc).isoformat()
        return turns
