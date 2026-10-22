import os
import tempfile
import unittest

from claude_codex_monitor import collect as C


class ClaudeCliDetection(unittest.TestCase):
    """A shell command counts as calling `claude -p` only when claude itself runs, not when it is quoted text."""

    CALLS = [
        'claude -p "summarise this"',
        'claude --print "x"',
        'claude --model haiku -p "x"',
        'cd /tmp/work && claude -p "y"',
        'FOO=1 BAR=2 claude -p z',
        'env A=1 claude -p z',
        '/usr/local/bin/claude -p x',
        '~/.local/bin/claude -p x',
        'cat prompt.md | claude -p',
        'out=$(claude -p "x")',
        'npm test; claude -p "explain the failure"',
        'claude -p --output-format json < prompt.txt',
        'claude --print=true "x"',
    ]
    NOT_CALLS = [
        'grep "claude -p" notes.txt',
        "echo 'claude -p hello'",
        'python3 -c "import os; os.system(\'claude -p x\')"',
        'cat <<EOF > run.sh\nclaude -p x\nEOF',
        'claude --version',
        'claude-code -p x',
        'claude mcp list',
    ]

    def test_calls(self):
        for cmd in self.CALLS:
            with self.subTest(cmd=cmd):
                self.assertTrue(C.mentions_claude(cmd, '/nonexistent'))

    def test_not_calls(self):
        for cmd in self.NOT_CALLS:
            with self.subTest(cmd=cmd):
                self.assertFalse(C.mentions_claude(cmd, '/nonexistent'))

    def test_script_that_calls_claude(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'run.sh'), 'w') as f:
                f.write('#!/bin/sh\nset -e\nclaude -p "$(cat prompt.md)" > out.md\n')
            self.assertTrue(C.mentions_claude('bash run.sh', d))
            self.assertFalse(C.mentions_codex('bash run.sh', d))


class CodexExecDetection(unittest.TestCase):
    def test_codex_exec(self):
        self.assertTrue(C.mentions_codex('codex exec --full-auto "port the workflow"', '/nonexistent'))
        self.assertTrue(C.mentions_codex('cd repo && codex exec - < brief.md', '/nonexistent'))
        self.assertFalse(C.mentions_codex('codex --version', '/nonexistent'))


class ProjectName(unittest.TestCase):
    def setUp(self):
        C._project_cache.clear()

    def test_git_root_and_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, 'shop')
            os.makedirs(os.path.join(repo, '.git', 'worktrees', 'wt1'))
            os.makedirs(os.path.join(repo, 'src', 'api'))
            wt = os.path.join(d, 'elsewhere', 'wt1')
            os.makedirs(wt)
            with open(os.path.join(wt, '.git'), 'w') as f:
                f.write('gitdir: %s/.git/worktrees/wt1\n' % repo)
            plain = os.path.join(d, 'notes', 'week1')
            os.makedirs(plain)
            self.assertEqual(C.project_of(os.path.join(repo, 'src', 'api')), 'shop')
            self.assertEqual(C.project_of(wt), 'shop')
            self.assertEqual(C.project_of(plain), 'week1')

    def test_removed_worktree(self):
        self.assertEqual(C.project_of('/nonexistent/acme/.claude/worktrees/fix-login'), 'acme')
        self.assertEqual(C.project_of(''), '')


if __name__ == '__main__':
    unittest.main()
