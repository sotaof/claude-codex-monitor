# Changelog

## Unreleased

- Read-only Scheduled tasks tab for user LaunchAgents, Claude desktop tasks and Codex automations, with
  per-run results, heartbeat turn detection, missed-run alerts, on-demand masked launchd logs and a separate
  KeepAlive service card. Scheduler collection runs independently every 45 seconds; launchd history starts
  when monitoring begins. Includes English and Chinese translations and made-up demo tasks.
- Scheduled run details include waiting items and next-step advice, reversible per-run handling and bulk handling
  of earlier issues. Session dismissals and run handling stay linked and expire on new messages. Handled runs keep
  their original result with an outlined check; only the latest unhandled issue counts toward Needs attention.
- Missed triggers are checked separately within the last 30 days and after task creation. Claude desktop scheduler
  dispatch logs distinguish automatic and manual runs, with labeled timing inference when logs are unavailable.
  Missed-run details read `pmset -g log` on demand and use cached, read-only `~/Library/Logs/Claude/main*.log` and
  monitor-owned launchd observations. Unknown evidence remains unknown; two or more consecutive misses show a warning.
  All handling state stays in the existing monitor-owned `dismissed.json`. No scheduler data is changed.
- Renamed the scheduled-run waiting state to **Needs you** / **待你处理**. Demo runs cover handled results, manual
  runs, consecutive misses, waiting items, network and sign-in failures, and launchd log guidance.

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
