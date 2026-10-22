# Changelog

## 0.1.0 (2026-10-23)

First public version.

- A local page that lists recent Claude Code and Codex sessions in five states: needs your reply, running,
  interrupted, completed and dismissed. A dismissed session comes back when it gets a new message.
- Session details with every subtask the session started, grouped by batch on a timeline: subagents, background
  commands, `codex exec` and `claude -p` runs, new sessions, workflows, Codex plan steps and child threads.
- Model summaries of what a session is waiting for, on by default through the local `claude` command, else `codex`;
  OpenAI-compatible and Anthropic APIs as well. Text is masked before sending. The page turns summaries off and picks
  the tool and model.
- An access token for the page and its data, `--no-token` to turn it off.
- English and Simplified Chinese, light and dark themes, a demo mode, and start at login on macOS and Linux.
- Python 3.9 or newer, standard library only.
