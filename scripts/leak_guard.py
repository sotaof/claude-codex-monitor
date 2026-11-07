#!/usr/bin/env python3
"""Stop words that must not reach this public repository: names of private plugins, company tools, internal hosts.

The word list is never part of the repository. It is read from, in order:
  $CCM_LEAK_WORDS          comma or newline separated (CI reads it from a repository secret)
  $CCM_LEAK_WORDS_FILE     a file, one word per line, # for comments
  ~/.config/claude-codex-monitor/leak-words.txt

  python3 scripts/leak_guard.py                 check every tracked file and the last 50 commit messages
  python3 scripts/leak_guard.py --install-hook  run the check before every git push from this clone

Matches are reported by file, line and the word's number in the list, never the word itself, so a public CI log does
not give the list away. Without a word list it says so and passes.
"""
import argparse
import os
import re
import subprocess
import sys

HOOK = '#!/bin/sh\n# installed by scripts/leak_guard.py\nexec python3 "$(git rev-parse --show-toplevel)/scripts/leak_guard.py"\n'


def words():
    raw = os.environ.get('CCM_LEAK_WORDS', '')
    path = os.environ.get('CCM_LEAK_WORDS_FILE') or os.path.expanduser('~/.config/claude-codex-monitor/leak-words.txt')
    if not raw.strip() and os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            raw = f.read()
    found = []
    for part in re.split(r'[,\n]', raw):
        part = part.split('#', 1)[0].strip()
        if part and part.lower() not in found:
            found.append(part.lower())
    return found


def git(*args):
    return subprocess.run(['git'] + list(args), capture_output=True, text=True, check=True).stdout


def check(word_list, root):
    pattern = re.compile('|'.join(r'(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])' % re.escape(w) for w in word_list), re.I)
    index = {w: i + 1 for i, w in enumerate(word_list)}
    hits = []
    for name in git('-C', root, 'ls-files', '-z').split('\0'):
        path = os.path.join(root, name)
        if not name or not os.path.isfile(path):
            continue
        with open(path, 'rb') as f:
            data = f.read()
        if b'\0' in data[:4096]:
            continue                    # binary
        for n, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            for m in pattern.finditer(line):
                hits.append('%s:%d: word #%d' % (name, n, index[m.group(0).lower()]))
    log = git('-C', root, 'log', '-50', '--format=%H%x00%B%x01')
    for entry in log.split('\x01'):
        sha, _, body = entry.strip().partition('\0')
        for m in pattern.finditer(body):
            hits.append('commit %s: word #%d' % (sha[:10], index[m.group(0).lower()]))
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--install-hook', action='store_true', help='run this check before every git push')
    a = ap.parse_args()
    root = git('rev-parse', '--show-toplevel').strip()
    if a.install_hook:
        hook = os.path.join(git('rev-parse', '--git-path', 'hooks').strip(), 'pre-push')
        hook = hook if os.path.isabs(hook) else os.path.join(root, hook)
        if os.path.exists(hook):
            with open(hook, encoding='utf-8') as f:
                if 'leak_guard.py' not in f.read():
                    sys.exit('leak guard: %s already exists; add this line to it:\n  python3 scripts/leak_guard.py' % hook)
        with open(hook, 'w', encoding='utf-8') as f:
            f.write(HOOK)
        os.chmod(hook, 0o755)
        print('leak guard: runs before every push (%s)' % hook)
        return
    word_list = words()
    if not word_list:
        print('leak guard: no word list (CCM_LEAK_WORDS, CCM_LEAK_WORDS_FILE or '
              '~/.config/claude-codex-monitor/leak-words.txt); skipped')
        return
    hits = check(word_list, root)
    if hits:
        print('leak guard: %d match(es) of private words:' % len(hits))
        print('\n'.join('  ' + h for h in hits[:200]))
        sys.exit(1)
    print('leak guard: %d words checked, nothing found' % len(word_list))


if __name__ == '__main__':
    main()
