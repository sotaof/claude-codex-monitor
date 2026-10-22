import atexit
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from claude_codex_monitor import autostart


# Everything these tests create goes into one folder, removed when they finish
tempfile.tempdir = tempfile.mkdtemp(prefix='claude-codex-monitor-tests-')
atexit.register(shutil.rmtree, tempfile.tempdir, True)


class Autostart(unittest.TestCase):
    """Only the files are checked here; nothing is installed."""

    ARGV = ['--summary', 'claude-cli', '--port', '8770', '--install-autostart']

    def test_command_keeps_options_but_not_the_autostart_flags(self):
        cmd = autostart.command(self.ARGV)
        self.assertEqual(cmd[:3], [sys.executable, '-m', 'claude_codex_monitor'])
        self.assertEqual(cmd[3:], ['--summary', 'claude-cli', '--port', '8770', '--no-open'])

    def test_command_never_opens_a_browser(self):
        self.assertEqual(autostart.command(['--no-open', '--install-autostart']).count('--no-open'), 1)

    def test_api_keys_are_never_copied(self):
        env = {'CCM_API_KEY': 'k1', 'OPENAI_API_KEY': 'k2', 'ANTHROPIC_API_KEY': 'k3', 'CCM_SUMMARY': 'openai',
               'CCM_BASE_URL': 'https://example.com/v1', 'PATH': '/opt/bin:/usr/bin'}
        with mock.patch.dict(os.environ, env, clear=True):
            got = autostart.environment()
        self.assertEqual(got['CCM_SUMMARY'], 'openai')
        self.assertEqual(got['PATH'], '/opt/bin:/usr/bin')
        self.assertTrue(os.path.isdir(os.path.join(got['PYTHONPATH'], 'claude_codex_monitor')))
        for secret in ('CCM_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            self.assertNotIn(secret, got)

    def test_launch_agent(self):
        data = plistlib.loads(plistlib.dumps(autostart.launch_agent(self.ARGV, '/tmp/monitor.log')))
        self.assertEqual(data['Label'], autostart.LABEL)
        self.assertTrue(data['RunAtLoad'])
        self.assertEqual(data['KeepAlive'], {'SuccessfulExit': False})    # a clean exit is not restarted
        self.assertEqual(data['ThrottleInterval'], 30)
        self.assertNotIn('--install-autostart', data['ProgramArguments'])
        self.assertEqual(data['StandardErrorPath'], '/tmp/monitor.log')

    def test_systemd_unit_quotes_spaces_and_percent(self):
        with mock.patch.object(sys, 'executable', '/home/me/My Tools/python3'):
            unit = autostart.systemd_unit(['--state-dir', '/home/me/100% done'], '/tmp/monitor.log')
        self.assertIn('ExecStart="/home/me/My Tools/python3" -m claude_codex_monitor --state-dir "/home/me/100%% done"', unit)
        self.assertIn('WantedBy=default.target', unit)
        self.assertIn('StandardOutput=append:/tmp/monitor.log', unit)
        self.assertIn('RestartSec=30', unit)
        with mock.patch.dict(os.environ, {'CCM_STATE_DIR': '/home/me/$state'}):
            unit = autostart.systemd_unit(['--state-dir', '/home/me/$HOME'], '/tmp/monitor.log')
        self.assertIn('--state-dir /home/me/$$HOME', unit)                  # ExecStart= expands $
        self.assertIn('Environment=CCM_STATE_DIR=/home/me/$state', unit)       # Environment= does not

    def test_install_and_remove_on_macos(self):
        plist = os.path.join(tempfile.mkdtemp(), 'LaunchAgents', 'x.plist')
        calls = []

        def run(args, **kwargs):
            calls.append(list(args[:2]))
            return mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(autostart, '_paths', return_value=plist), \
                mock.patch.object(autostart.subprocess, 'run', side_effect=run), mock.patch.dict(os.environ, {}, clear=True):
            path, log = autostart.install(['--port', '8770', '--install-autostart'], tempfile.mkdtemp())
            with open(plist, 'rb') as f:
                self.assertEqual(plistlib.load(f)['ProgramArguments'][-3:], ['--port', '8770', '--no-open'])
            self.assertIn(['launchctl', 'bootstrap'], calls)
            self.assertEqual(autostart.remove(), (plist, True))
            self.assertEqual(autostart.remove(), (plist, False))       # removing twice is fine
        self.assertFalse(os.path.exists(plist))

    def test_refuses_api_addresses_with_secrets(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            autostart.check_urls(['--base-url', 'https://api.example.com/v1'])          # fine
            for bad in (['--base-url', 'https://user:pw@api.example.com/v1'], ['--base-url=https://api.example.com/v1?key=x']):
                with self.assertRaises(SystemExit):
                    autostart.check_urls(bad)
        with mock.patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://u:p@gw.example.com/v1'}, clear=True):
            with self.assertRaises(SystemExit):
                autostart.check_urls([])


if __name__ == '__main__':
    unittest.main()
