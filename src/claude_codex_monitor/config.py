"""Runtime settings. Command-line flags override environment variables, which override defaults."""
import argparse
import os
from dataclasses import dataclass

from . import __version__

BACKENDS = ('auto', 'off', 'openai', 'anthropic', 'claude-cli', 'codex-cli')
LANGS = ('en', 'zh')


@dataclass
class Config:
    port: int = 8767
    days: int = 7                 # how many days of sessions to scan
    lang: str = ''                # page language; '' follows the browser. The page can switch and remembers it
    state_dir: str = ''           # dismissed sessions and the summary cache live here
    claude_dir: str = ''          # Claude Code data (honours CLAUDE_CONFIG_DIR)
    codex_dir: str = ''           # Codex data (honours CODEX_HOME)
    summary: str = 'auto'         # which model summarizes "what is waiting for you"; auto: local claude, then codex
    model: str = ''
    summary_fixed: bool = False   # --summary or --model given at startup: the page cannot change them then
    base_url: str = ''
    desktop: bool = True          # read Claude desktop app files: "start a new session" cards, open-in-app links
    lock_lang: bool = False       # always use `lang` and hide the language menu
    demo: bool = False            # show made-up sessions instead of this computer's
    open: bool = True             # open the page in the browser after starting
    token: bool = True            # the page and its data need an access token
    install_autostart: bool = False
    remove_autostart: bool = False

    @property
    def api_key(self):
        """Only ever read from the environment, never from flags or files."""
        own = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}.get(self.summary)
        return os.environ.get('CCM_API_KEY') or (os.environ.get(own) if own else None) or ''


def _env_bool(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def parse_args(argv=None):
    home = os.path.expanduser('~')
    env = os.environ.get
    ap = argparse.ArgumentParser(
        prog='claude-codex-monitor', allow_abbrev=False,
        description='Local dashboard for your Claude Code and Codex sessions: which ones are waiting for you, '
                    'and how their subtasks are doing.',
        epilog='Most options can also be set with environment variables (CCM_PORT, CCM_DAYS, CCM_SUMMARY, ...); '
               'see the README. API keys are only read from CCM_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY.')

    def env_int(name, default):
        try:
            return int(env(name) or default)
        except ValueError:
            ap.error('%s must be a whole number, not %r' % (name, env(name)))

    ap.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    ap.add_argument('--port', type=int, default=env_int('CCM_PORT', 8767), help='port on 127.0.0.1 (default 8767)')
    ap.add_argument('--days', type=int, default=env_int('CCM_DAYS', 7), help='days of sessions to scan (default 7)')
    ap.add_argument('--lang', choices=LANGS, default=env('CCM_LANG') or '',
                    help="page language (default: the browser's language, English if it is not Chinese)")
    ap.add_argument('--state-dir', default=env('CCM_STATE_DIR') or os.path.join(home, '.claude-codex-monitor'),
                    help='where dismissed sessions, the summary cache and settings are stored '
                         '(default ~/.claude-codex-monitor)')
    ap.add_argument('--claude-dir', default=env('CLAUDE_CONFIG_DIR') or os.path.join(home, '.claude'),
                    help='Claude Code data folder (default $CLAUDE_CONFIG_DIR or ~/.claude)')
    ap.add_argument('--codex-dir', default=env('CODEX_HOME') or os.path.join(home, '.codex'),
                    help='Codex data folder (default $CODEX_HOME or ~/.codex)')
    ap.add_argument('--summary', choices=BACKENDS,
                    help='how to summarize what each session is waiting for (default auto: the local claude '
                         'command, else codex; off: built-in text rules only, nothing is sent). Given here, it '
                         'overrides the choice made in the page.')
    ap.add_argument('--model', help='model for the summary (default: sonnet for claude-cli, claude-sonnet-5-5 for '
                                    'anthropic, your codex default for codex-cli; required for openai). Given here, '
                                    'it overrides the page.')
    ap.add_argument('--base-url', default=env('CCM_BASE_URL') or '', help='API base URL for openai / anthropic')
    ap.add_argument('--lock-lang', action='store_true', default=_env_bool('CCM_LOCK_LANG'),
                    help='always show the page in --lang and hide the language menu')
    ap.add_argument('--demo', action='store_true', default=_env_bool('CCM_DEMO'),
                    help="show made-up sessions instead of this computer's (nothing is read or sent)")
    ap.add_argument('--install-autostart', action='store_true',
                    help='start the monitor now and whenever you log in, with the other options given (macOS, Linux)')
    ap.add_argument('--remove-autostart', action='store_true',
                    help='remove the login item and stop the monitor it started')
    ap.add_argument('--no-desktop', action='store_true', default=_env_bool('CCM_NO_DESKTOP'),
                    help="don't read Claude desktop app files (no new-session cards, no open-in-app buttons)")
    ap.add_argument('--no-open', action='store_true', default=_env_bool('CCM_NO_OPEN'),
                    help="don't open the page in the browser after starting")
    ap.add_argument('--no-token', action='store_true', default=_env_bool('CCM_NO_TOKEN'),
                    help='let anyone who can reach 127.0.0.1 on this computer open the page, without the access token '
                         '(only for a computer you do not share)')
    a = ap.parse_args(argv)
    if a.days < 1:
        ap.error('--days must be at least 1')
    if a.lang and a.lang not in LANGS:
        ap.error('CCM_LANG must be en or zh, not %r' % a.lang)
    if a.lock_lang and not a.lang:
        ap.error('--lock-lang needs --lang')
    summary = a.summary or env('CCM_SUMMARY') or 'auto'
    if summary not in BACKENDS:
        ap.error('CCM_SUMMARY must be one of: ' + ', '.join(BACKENDS))
    model = a.model if a.model is not None else env('CCM_MODEL') or ''
    fixed = a.summary is not None or a.model is not None or bool(env('CCM_SUMMARY') or env('CCM_MODEL'))
    return Config(port=a.port, days=a.days, lang=a.lang, state_dir=os.path.expanduser(a.state_dir),
                  claude_dir=os.path.expanduser(a.claude_dir), codex_dir=os.path.expanduser(a.codex_dir),
                  summary=summary, model=model, summary_fixed=fixed, base_url=a.base_url.rstrip('/'), desktop=not a.no_desktop,
                  lock_lang=a.lock_lang, demo=a.demo, open=not a.no_open, token=not a.no_token,
                  install_autostart=a.install_autostart,
                  remove_autostart=a.remove_autostart)
