# Security

Claude & Codex Monitor reads your Claude Code and Codex session logs, serves a page on `127.0.0.1`, and, with model
summaries on, sends masked text from recent messages to a model. A security problem here can expose what you discussed
with your assistants, so please report it privately.

## Reporting a problem

Use **Report a vulnerability** on the repository's Security tab (GitHub private vulnerability reporting). Please do not
open a public issue, and do not attach real session logs: describe the problem, or reproduce it with
`python3 run.py --demo`, which shows made-up sessions only.

You can expect a first answer within a week.

## What it is designed to do

- It reads session logs and never writes to them.
- It listens on `127.0.0.1` only. The page and its data need an access token, kept in `~/.claude-codex-monitor/token`
  and readable only by you; `--no-token` turns this off. The browser keeps the token in a cookie for `127.0.0.1`,
  which browsers also send to other ports on `127.0.0.1`, so a program on another port that your browser opens can
  see the token.
- Its own files in `~/.claude-codex-monitor/` are readable only by you.
- It accepts changes only as JSON from its own page, and HTML files from a session folder are shown in a sandbox.
- Model summaries mask text that looks like keys, tokens, passwords, connection strings, cookies, private keys, email
  addresses and phone numbers before sending. Masking works by pattern, so an unusual format can get through.

The known limits are listed under "Risks" in the [README](README.md#risks).
