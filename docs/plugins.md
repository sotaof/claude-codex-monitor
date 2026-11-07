# Source plugins

The monitor shows Claude Code and Codex sessions and scheduled tasks on its own. A **source plugin** adds another
place that can need you — a chat, a ticket queue, an inbox — without changing this repository. Its items appear on
the **Today** page next to waiting sessions and failed scheduled tasks, and the plugin can add a tab of its own.

A plugin is an ordinary Python package installed next to the monitor. Nothing is loaded when none is installed, and
`--demo` never loads plugins.

## A minimal plugin

```python
# my_inbox/__init__.py
from claude_codex_monitor.sources import Source

class Inbox(Source):
    label = {'en': 'Inbox', 'zh': '收件箱'}
    interval = 60                      # seconds between polls

    def items(self, now):
        return [{'key': 'msg-42', 'kind': 'reply', 'title': 'Which partition is the latest?',
                 'who': 'Alex', 'where': 'Direct message', 'since': now - 600, 'updated': now - 600}]

def make(context):
    return Inbox(context)
```

```toml
# pyproject.toml of the plugin
[project.entry-points."claude_codex_monitor.sources"]
inbox = "my_inbox:make"
```

The entry point name (`inbox`) is the source's name: lower case letters, digits and dashes. It is used in URLs and in
the plugin's own state folder, `<state dir>/ext/<name>/`, which `context.state_dir` points to.

## Items

| Field | Meaning |
| --- | --- |
| `key` | Unique within the source; required |
| `kind` | What you have to do next: `reply`, `approve`, `deliver`, `waiting` (on someone else), `alert`, `info` |
| `title`, `text` | One line, and the message or an excerpt |
| `who`, `where` | Who it is from, and where (a chat, a group, a queue) |
| `since` | When it started waiting, in seconds |
| `updated` | Last activity; new activity brings an item you put off with **Later** back at once |
| `due` | Deadline in seconds, or `None` |
| `url` | Where to open it (`http` or `https` only) |
| `open` | `False` once the source can tell it is over |

`reply`, `approve`, `deliver` and `alert` count as needing you. `waiting` and `info` go to the
**Waiting on others · FYI** column; `info` items show only on the day they arrived.

**Done**, **Later** (until 9:00 the next morning) and **Not needed** are stored by the monitor in
`<state dir>/items.json`, not by the plugin, so every source behaves the same. Each item stays until you handle it,
whatever its date.

## What counts as "today"

The Today page covers the day from midnight until **04:00 the next morning**, and a new day starts only at 04:00.
Working until 1 a.m. is still the same day, and the hours between midnight and 04:00 belong to both days, so nothing
that arrives late at night is missed the next morning. Plugins can use the same window:
`claude_codex_monitor.sources.day_window(now)` returns `(start, end)`.

## A tab of its own

Set `static_dir` to a folder and `tab_script` to a file in it. The monitor serves the folder's `.js`, `.css`, `.svg`
and `.png` files at `/ext/<name>/<file>` and loads the script when the page starts. The script registers the tab:

```js
CCMTabs.register('inbox', {
  render(ctx) {      // ctx.source: the source and its items; ctx.itemLine(item): the standard row with its buttons
    return '<div class="tday">' + ctx.source.items.filter(i => i.open && !i.handled).map(ctx.itemLine).join('') + '</div>';
  },
  click(event, ctx) { return false; }      // optional; return true when the click was yours
});
```

The tab is at `#/x/<name>`. `ctx.api(path, options)` calls the plugin's `handle(method, path, query, body)` through
`/api/ext/<name>/<path>`, behind the same access token as the rest of the monitor.

## Rules

- A plugin that fails to load or to poll shows an error for itself only; the rest of the page keeps working.
- Keep the monitor's ground rules: nothing leaves the computer unless the plugin's README says so, and private data
  stays in the plugin's state folder.
- Private plugins live in their own repositories. `scripts/leak_guard.py` checks this repository for a list of
  words that must never appear in it (the list itself is kept outside the repository).
