# Contributing

Thanks for helping. A few things keep this project easy to run and safe to share.

## Running it and the tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 run.py --demo --port 8769          # made-up sessions, nothing read or sent
```

The tests run on Python 3.9 to 3.14 in CI, on Linux and macOS.

## Ground rules

- Standard library only. A change that needs a third-party package needs a very good reason.
- Never commit real session logs, screenshots of real sessions, or anything from `~/.claude`, `~/.codex` or
  `~/.claude-codex-monitor`. When Claude Code or Codex changes their log format, add a made-up example to
  `src/claude_codex_monitor/demo.py` or to a test instead.
- Every text shown on the page goes through `static/i18n.js`, with the same key in `en` and `zh`. A test checks that
  both languages have the same keys and no key appears twice.
- Session titles and text stay in the language they were written in; only the page's own words are translated.
- Anything that sends data off the computer must stay off until masking runs, and must be described in the README.

## Pull requests

Keep a pull request to one change, say what you checked, and add a test for a bug fix. If the change is visible on the
page, add a screenshot taken with `--demo`.
