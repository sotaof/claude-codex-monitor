"""Start the monitor when you log in: a LaunchAgent on macOS, a systemd user service on Linux.

`--install-autostart` saves the options it was given (and non-secret CCM_* settings from the environment) and starts
the monitor right away; `--remove-autostart` stops it and deletes the file. API keys are never copied: the files are
plain text. Use claude-cli / codex-cli summaries, or add the key to the file yourself.
"""
import os
import plistlib
import re
import subprocess
import sys
import urllib.parse

LABEL = 'io.github.sotaof.claude-codex-monitor'
SERVICE = 'claude-codex-monitor.service'
FLAGS = ('--install-autostart', '--remove-autostart')
PASS_ENV = ('CCM_PORT', 'CCM_DAYS', 'CCM_LANG', 'CCM_LOCK_LANG', 'CCM_STATE_DIR', 'CCM_SUMMARY', 'CCM_MODEL',
            'CCM_BASE_URL', 'CCM_NO_DESKTOP', 'CCM_NO_TOKEN', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'OPENAI_BASE_URL',
            'ANTHROPIC_BASE_URL')


def command(argv):
    """The command that starts the monitor with the same options, minus the autostart flags. It never opens a
    browser: it starts at every login and again after a crash."""
    args = [a for a in argv if a not in FLAGS]
    return [sys.executable, '-m', 'claude_codex_monitor'] + args + ([] if '--no-open' in args else ['--no-open'])


def environment():
    # The folder holding the package: src/ in a download, site-packages when installed
    env = {'PYTHONPATH': os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           'PATH': os.environ.get('PATH') or '/usr/local/bin:/usr/bin:/bin'}   # so claude / codex are found
    env.update({k: os.environ[k] for k in PASS_ENV if os.environ.get(k)})
    return env


def launch_agent(argv, log):
    # Restart only after a crash: a clean exit (for example "already running") must not loop every few seconds
    return {'Label': LABEL, 'ProgramArguments': command(argv), 'EnvironmentVariables': environment(),
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 30,
            'StandardOutPath': log, 'StandardErrorPath': log}


def _systemd_quote(s, command=True):
    s = s.replace('%', '%%')                            # specifiers, expanded everywhere
    if command:
        s = s.replace('$', '$$')                        # variables, expanded in ExecStart= only
    return '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"') if re.search(r'[\s"\'\\]', s) else s


def systemd_unit(argv, log):
    lines = ['[Unit]', 'Description=Claude & Codex Monitor', '', '[Service]',
             'ExecStart=' + ' '.join(_systemd_quote(a) for a in command(argv))]
    lines += ['Environment=' + _systemd_quote('%s=%s' % kv, command=False) for kv in environment().items()]
    lines += ['Restart=on-failure', 'RestartSec=30', 'StandardOutput=append:' + log, 'StandardError=append:' + log, '',
              '[Install]', 'WantedBy=default.target', '']
    return '\n'.join(lines)


def _run(*args):
    r = subprocess.run(list(args), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('claude-codex-monitor: %s failed: %s' % (' '.join(args[:3]), (r.stderr or r.stdout).strip()))


def _paths():
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/LaunchAgents/%s.plist' % LABEL)
    if sys.platform.startswith('linux'):
        return os.path.expanduser('~/.config/systemd/user/' + SERVICE)
    raise SystemExit('claude-codex-monitor: starting at login is supported on macOS and Linux only')


def check_urls(argv):
    """API addresses are saved in a plain-text file; refuse ones that carry a user name, password or query."""
    urls = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == '--base-url']
    urls += [a.split('=', 1)[1] for a in argv if a.startswith('--base-url=')]
    urls += [os.environ[k] for k in ('CCM_BASE_URL', 'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL') if os.environ.get(k)]
    for u in urls:
        p = urllib.parse.urlsplit(u)
        if p.username or p.password or p.query:
            raise SystemExit('claude-codex-monitor: not set to start at login: the API address %s://%s%s carries a user '
                             'name, password or query, which would be saved in a plain-text file. Use an address '
                             'without them and pass the key as CCM_API_KEY instead.' % (p.scheme, p.hostname or '', p.path))


def install(argv, state_dir):
    check_urls(argv)
    path = _paths()
    log = os.path.join(state_dir, 'monitor.log')
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if sys.platform == 'darwin':
        with open(path, 'wb') as f:
            plistlib.dump(launch_agent(argv, log), f)
        domain = 'gui/%d' % os.getuid()
        subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)   # replace an older copy
        _run('launchctl', 'bootstrap', domain, path)
    else:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(systemd_unit(argv, log))
        _run('systemctl', '--user', 'daemon-reload')
        _run('systemctl', '--user', 'enable', '--now', SERVICE)
    return path, log


def remove():
    path = _paths()
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', 'gui/%d/%s' % (os.getuid(), LABEL)], capture_output=True)
    else:
        subprocess.run(['systemctl', '--user', 'disable', '--now', SERVICE], capture_output=True)
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    if not sys.platform == 'darwin':
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    return path, existed
