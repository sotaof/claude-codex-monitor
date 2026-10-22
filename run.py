#!/usr/bin/env python3
"""Start Claude & Codex Monitor straight from a download, without installing anything:

    python3 run.py            # then open http://127.0.0.1:8767/
    python3 run.py --demo     # made-up sessions, to look around first
    python3 run.py --help     # every option
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from claude_codex_monitor.server import main  # noqa: E402

main()
