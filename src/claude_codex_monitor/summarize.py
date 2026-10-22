""""What is waiting for you" summaries: when a session stops with new text, a model reads the last 80 text
messages and lists what the user has not answered yet.

On by default (--summary auto: the local claude command, else codex); turned off in the page or with --summary off.
Backends: the local claude / codex command, an OpenAI-compatible API, or the Anthropic API.
Results are cached per session in <state dir>/summaries.json; while there is no summary (off, pending or failed)
the page shows the questions picked out of the reply text by rules (quick_asks).
"""
import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = 5                 # Bump after prompt or window changes to invalidate old summaries.
WINDOW_MSGS = 80            # Latest text messages from both roles, excluding tool output.
MAX_CHARS = 40000           # Drop oldest messages above this limit.
RETRY_AFTER = 600           # Retry delay after three consecutive failures.
RETRY_SOON = 30             # Retry after 30 seconds for the first two failures.

PROMPTS = {'zh': """你在帮用户盯着他和 AI 助手的会话。下面是这个会话最近几轮的文字，「用户」是他本人，「助手」是 AI。
判断：助手现在是不是在等用户回复、确认、做选择，或者等用户亲自去做某件事。

规则：
1. 只列用户还没回应的事。用户在后面的消息里已经回答、拒绝或安排过的，不要列。
2. 前面助手问过、用户一直没回的，也要列，哪怕之后话题已经转了（用户可能忘了）。
3. 「需要的话我可以…」「如果你想…我再…」这类可做可不做的建议，不算等用户，放进 optional。
4. 助手说自己接下来会做的事（「跑完马上告诉你」「我继续推进」），不列。
5. 每条用一句大白话写清楚要用户做什么、选什么，有选项就写出选项，不超过 45 个字；不要用「闭环」「赋能」「XX 驱动」这类词。默认用中文；如果用户自己的说明（比如 CLAUDE.md）规定了回复用什么语言，就按用户的规定。
6. 最多 6 条，按重要程度排。from 填这件事出自哪条消息的编号。
7. 助手如果写「上一条里请你确认的 4 件事」「前面那几个问题」这类指回前文的话，要回到那条消息，把每一件事分别写成一条（有选项就带上选项），不要照抄这句指代。

只输出 JSON，不要别的文字：
{"waiting": true 或 false, "items": [{"text": "…", "from": 编号}], "optional": ["…"]}

会话文字：
""", 'en': """You are helping a user monitor their conversation with an AI assistant. Below are recent text messages; "User" is the person and "Assistant" is the AI.
Decide whether the assistant is waiting for the user to reply, confirm, choose an option, or personally do something.

Rules:
1. List only things the user has not addressed. Omit anything answered, declined, or arranged in a later user message.
2. Include earlier requests the user never answered, even if the topic has since changed (they may have forgotten).
3. Optional offers such as "If needed, I can..." or "If you want, I can..." are not waiting on the user; put them in optional.
4. Omit things the assistant says it will do next ("I'll tell you when it finishes", "I'll keep going").
5. Write each item as one plain sentence, at most 25 words, stating what the user needs to do or choose; include any options and avoid jargon. Use the language the user writes in, unless the user's own instructions (such as a CLAUDE.md) say which language to reply in; then follow those.
6. List at most 6 items, most important first. Set from to the source message number.
7. If the assistant refers back to "the four things to confirm in my previous message" or "the questions above", find that message and list each thing separately, including options. Do not copy the pointer itself.

Output JSON only:
{"waiting": true or false, "items": [{"text": "...", "from": message_number}], "optional": ["..."]}

Conversation:
"""}
ROLES = {'zh': ('用户', '助手'), 'en': ('User', 'Assistant')}
OMITTED = {'zh': '\n…（中间省略）…\n', 'en': '\n...[middle omitted]...\n'}


def _clip_middle(text, head=800, tail=2400, lang='zh'):
    text = text.strip()
    if len(text) <= head + tail + 20:
        return text
    return text[:head] + OMITTED[lang] + text[-tail:]


def recent_window(turns):
    """Latest WINDOW_MSGS text messages; drop oldest ones if their clipped length exceeds MAX_CHARS."""
    sel = list(turns)[-WINDOW_MSGS:]
    while len(sel) > 1 and sum(len(_clip_middle(t['text'])) for t in sel) > MAX_CHARS:
        sel = sel[1:]
    return sel


# Masked before anything leaves this machine: things that look like keys, tokens, passwords, emails, phone numbers.
# After a secret-looking name: 8 or more characters, or 6 or more with both letters and digits
SECRET_VALUE = r"""(?:[^\s"',;]{8,}|(?=[^\s"',;]*[A-Za-z])(?=[^\s"',;]*\d)[^\s"',;]{6,})"""
SECRET_NAME = (r'(?:api[_-]?key|secret|token(?!s)|passw(?:or)?d|passphrase|pwd|pass(?![a-z])|access[_-]?key|credentials?'
               r'|private[_-]?key|auth(?:orization)?(?![a-z])|cookie|dsn|webhook)')
REDACT = [
    # A private key block, even when its END line is missing (pasted in part)
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)',
                re.S), '[PRIVATE KEY]'),
    (re.compile(r'\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}'), '[TOKEN]'),
    (re.compile(r'\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}'), '[KEY]'),
    (re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{20,}|xox[abprs]-[\w-]{10,}|xapp-[\w-]{10,}'
                r'|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[\w-]{35}|ya29\.[\w-]{20,}|hf_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{30,}'
                r'|whsec_[A-Za-z0-9]{16,})'), '[KEY]'),
    (re.compile(r'https://hooks\.slack\.com/services/[\w/]+|https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+'),
     '[WEBHOOK]'),
    # user:password@ in any URL, for example postgres://admin:pw@db or redis://:pw@cache
    (re.compile(r'(?i)\b([a-z][a-z0-9+.-]{0,30}://)[^/\s:@]*:[^@\s/]+@'), r'\1[REDACTED]@'),
    (re.compile(r'(?i)\b(bearer)\s+[\w.~+/-]{16,}=*'), r'\1 [TOKEN]'),
    (re.compile(r'(?i)\b(authorization\s*:\s*basic)\s+[A-Za-z0-9+/]{8,}={0,2}'), r'\1 [TOKEN]'),
    (re.compile(r'(?i)\b((?:set-)?cookie\s*:\s*)[^\n]+'), r'\1[REDACTED]'),
    # Names are bounded in length: unbounded ones made masking a long message take seconds
    (re.compile(r'(?i)(?<![\w-])([\w-]{0,40}' + SECRET_NAME + r'[\w-]{0,40})' r"""(["']?\s*[:=：]\s*["']?)""" + SECRET_VALUE),
     r'\1\2[REDACTED]'),
    (re.compile(r'(密码|口令|密钥|令牌|私钥)(\s*(?:是|为|[:：=])\s*|\s+)[^\s，。；,;\[]{6,}'), r'\1\2[REDACTED]'),
    (re.compile(r'(?i)([?&](?:key|token|access_token|api_key|apikey|sig|signature|secret|password)=)[^&\s#]+'), r'\1[REDACTED]'),
    (re.compile(r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), '[EMAIL]'),
    (re.compile(r'(?<![\w+])(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)'), '[PHONE]'),
    (re.compile(r'(?<![\w+])\+\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?!\d)'), '[PHONE]'),
    (re.compile(r'(?<![\w(])\(?\d{3}\)?[ -]\d{3}[ -]\d{4}(?!\d)'), '[PHONE]'),
]

# Word boundaries and \w are ASCII-only here, so 「手机13812345678」 or 「密钥sk-…」 next to Chinese text still match
REDACT = [(re.compile(rx.pattern, (rx.flags & ~re.U) | re.A), rep) for rx, rep in REDACT]


def redact(text):
    for rx, rep in REDACT:
        text = rx.sub(rep, text)
    return text


# Left out when judging the language: pasted text, blocks the app attaches, code, links and paths
LANG_NOISE_RE = re.compile(r'<(pasted_content|system-reminder|session-mentions|command-[\w-]+)[^>]*>.*?</\1[^>]*>'
                           r'|```.*?```|`[^`\n]*`|\]\([^)]*\)|https?://\S+|(?:~|\.{0,2}/)[\w.@/-]+', re.S)


def conversation_lang(turns):
    """Language to summarize in: the conversation's, not the page's. The last 20 messages from the user and the last
    20 replies vote (replies follow the user's language and carry no pasted text), weighted by length up to 200, so
    many short progress notes do not outvote the real replies. Pasted text, attached blocks, code and paths are left
    out; very short messages ("ok", "好") do not vote.
    'zh' for Chinese; everything else gets the English instructions, which ask for the user's own language."""
    texts = [t['text'] for t in turns if t['role'] == 'user'][-20:] + [t['text'] for t in turns if t['role'] == 'assistant'][-20:]
    zh = other = 0
    for text in texts:
        text = LANG_NOISE_RE.sub(' ', text)
        han = len(re.findall(r'[\u4e00-\u9fff]', text))
        kana = len(re.findall(r'[\u3040-\u30ff\uac00-\ud7af]', text))      # Japanese, Korean
        words = len(re.findall(r'[A-Za-z]+', text))       # Chinese users mix in English terms; compare against words
        if han + kana + words < 3:
            continue
        weight = min(han + kana + words, 200)
        if han >= words and kana <= han * 0.2:
            zh += weight
        else:
            other += weight
    return 'zh' if zh and zh >= other else 'en'


def build_prompt(sel, fmt_ts, lang):
    """(instructions, conversation) for the model; every message is clipped and masked."""
    parts = []
    for i, t in enumerate(sel, 1):
        who = ROLES[lang][0 if t['role'] == 'user' else 1]
        # mask first, then clip: a secret cut in half by the clip would no longer be recognized
        parts.append('[#%d %s %s]\n%s' % (i, who, fmt_ts(t['ts']), _clip_middle(redact(t['text']), lang=lang)))
    return PROMPTS[lang], '\n\n'.join(parts)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the API key to whatever address it points at; fail instead."""
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_json(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                 headers=dict(headers, **{'Content-Type': 'application/json'}))
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode('utf-8', 'replace')
        e.close()
        raise RuntimeError('HTTP %d: %s' % (e.code, detail)) from None


class Backend:
    name = ''
    default_model = ''
    needs_model = True
    workers = 3                 # summaries run at the same time
    timeout = 120
    base = ''

    def __init__(self, cfg):
        self.model = cfg.model or self.default_model
        self.api_key = cfg.api_key
        if self.needs_model and not self.model:
            raise ValueError('--model is required for --summary %s' % self.name)

    @property
    def label(self):
        return '%s · %s' % (self.name, self.model or 'default model')

    def complete(self, system, prompt, timeout):
        raise NotImplementedError


class OpenAICompatible(Backend):
    """Any server that speaks POST {base}/chat/completions (OpenAI, OpenRouter, a local gateway...)."""
    name = 'openai'

    def __init__(self, cfg):
        super().__init__(cfg)
        self.base = cfg.base_url or os.environ.get('OPENAI_BASE_URL', '').rstrip('/') or 'https://api.openai.com/v1'

    def complete(self, system, prompt, timeout):
        headers = {'Authorization': 'Bearer ' + self.api_key} if self.api_key else {}
        d = _post_json(self.base + '/chat/completions', {'model': self.model, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}, headers, timeout)
        return ((d.get('choices') or [{}])[0].get('message') or {}).get('content') or ''


class AnthropicAPI(Backend):
    """POST {base}/v1/messages; base defaults to ANTHROPIC_BASE_URL, then https://api.anthropic.com."""
    name = 'anthropic'
    default_model = 'claude-sonnet-5-5'

    def __init__(self, cfg):
        super().__init__(cfg)
        if not self.api_key:
            raise ValueError('--summary anthropic needs ANTHROPIC_API_KEY (or CCM_API_KEY) in the environment')
        self.base = cfg.base_url or os.environ.get('ANTHROPIC_BASE_URL', '').rstrip('/') or 'https://api.anthropic.com'

    def complete(self, system, prompt, timeout):
        d = _post_json(self.base + '/v1/messages',
                       {'model': self.model, 'max_tokens': 8000, 'system': system,
                        'messages': [{'role': 'user', 'content': prompt}]},
                       {'x-api-key': self.api_key, 'anthropic-version': '2023-06-01'}, timeout)
        if d.get('stop_reason') == 'refusal':
            raise RuntimeError('the model declined this request')
        return ''.join(b.get('text', '') for b in d.get('content') or [] if b.get('type') == 'text')


_work_dir = None


def work_dir():
    """One empty folder per process for the command line tools to run in, removed when the monitor exits."""
    global _work_dir
    if _work_dir is None:
        _work_dir = tempfile.mkdtemp(prefix='claude-codex-monitor-')
        atexit.register(shutil.rmtree, _work_dir, True)
    return _work_dir


class CliBackend(Backend):
    """A local command line tool, using the account it is signed in with. One call at a time (each call starts a
    process); it runs read-only, in an empty folder of its own."""
    exe = ''
    needs_model = False
    workers = 1
    timeout = 180

    def __init__(self, cfg):
        super().__init__(cfg)
        self.path = shutil.which(self.exe)
        if not self.path:
            raise ValueError('--summary %s needs the `%s` command on PATH' % (self.name, self.exe))
        self.cwd = work_dir()

    def _run(self, args, stdin, timeout):
        try:
            r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            raise TimeoutError('%s timed out after %d s' % (self.exe, timeout)) from None
        if r.returncode != 0:
            raise RuntimeError('%s exited with %d: %s' % (self.exe, r.returncode, (r.stderr or r.stdout).strip()[-300:]))
        return r.stdout


class ClaudeCli(CliBackend):
    """`claude -p` with Claude Code's own system prompt, tools, MCP servers, skills and hooks switched off, and no
    session log written. The prompt goes through stdin (--tools takes a list and would swallow it)."""
    name = 'claude-cli'
    exe = 'claude'
    default_model = 'sonnet'    # haiku is cheaper but missed sessions that were waiting in our tests

    def complete(self, system, prompt, timeout):
        out = self._run([self.path, '-p', '--no-session-persistence', '--output-format', 'json', '--model', self.model,
                         '--system-prompt', system, '--strict-mcp-config', '--disable-slash-commands',
                         '--settings', '{"disableAllHooks": true}', '--tools', ''], prompt, timeout)
        try:
            d = json.loads(out)
        except ValueError:
            raise RuntimeError('claude returned no JSON: ' + out[:200]) from None
        if d.get('is_error'):
            raise RuntimeError('claude: ' + str(d.get('result') or d.get('subtype'))[:200])
        return d.get('result') or ''


class CodexCli(CliBackend):
    """`codex exec` in a read-only sandbox, with notify hooks off and no rollout log written (--ephemeral)."""
    name = 'codex-cli'
    exe = 'codex'

    def complete(self, system, prompt, timeout):
        fd, out = tempfile.mkstemp(suffix='.md', dir=self.cwd)
        os.close(fd)
        args = [self.path, 'exec'] + (['-m', self.model] if self.model else []) + [
            '--ephemeral', '-s', 'read-only', '--skip-git-repo-check', '-c', 'model_reasoning_effort="low"',
            '-c', 'notify=[]', '-o', out, '-']
        try:
            self._run(args, system + '\n' + prompt, timeout)
            with open(out, encoding='utf-8') as f:
                return f.read()
        finally:
            os.unlink(out)


BACKENDS = {'openai': OpenAICompatible, 'anthropic': AnthropicAPI, 'claude-cli': ClaudeCli, 'codex-cli': CodexCli}
AUTO_ORDER = ('claude-cli', 'codex-cli')      # also what the page can switch between
CLAUDE_MODELS = ('sonnet', 'opus', 'haiku')   # claude's own aliases for the latest of each
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$')


def resolve(summary):
    """The backend a --summary value stands for right now, or None. 'auto' is the claude command if it is installed,
    then codex: every tool is switched off for claude, and each call costs far fewer extra tokens."""
    if summary == 'auto':
        return next((n for n in AUTO_ORDER if shutil.which(BACKENDS[n].exe)), None)
    return None if summary == 'off' else summary


def installed():
    """Which of the local command line tools the page can offer."""
    return {n: bool(shutil.which(BACKENDS[n].exe)) for n in AUTO_ORDER}


def make_backend(cfg):
    """None when summaries are off, or 'auto' finds neither command."""
    name = resolve(cfg.summary)
    return BACKENDS[name](cfg) if name else None


def parse_result(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if not m:
        raise ValueError('no JSON in the model reply: ' + (text or '')[:120])
    return json.loads(m.group(0))


QUESTION_RE = re.compile(r'[？?]|需要你|等你|请你|你来|要不要|是否|确认一下|你选|选哪'
                         r'|\b(?:should I|shall I|do you want|would you like|can you confirm|please confirm|let me know'
                         r'|which (?:one|option)|your call|need you to|waiting for you)\b', re.I)


SECTION_RE = re.compile(r'请你确认|需要你|等你处理|等你确认|要你定|你来定|待你|请你决定|需要确认'
                        r'|\b(?:needs? your (?:input|decision|confirmation)|please confirm|for you to decide|open questions'
                        r'|decisions? needed|next steps for you|action required|waiting on you)\b', re.I)
POINTER_RE = re.compile(r'(上一条|上面|前面|之前|刚才).{0,16}(\d+|几|这些|那些).{0,4}(件事|个问题|个选项|项)'
                        r'|\bthe (\d+|few|two|three) (things|questions|items|options) (above|in my (last|previous) (message|reply))\b', re.I)
OPTIONAL_RE = re.compile(r'^(?:If you want|If needed|Optionally|I can also)\b', re.I)


def _clean(ln):
    return re.sub(r'[*`>|]+', '', ln).strip(' -\t#')


def quick_asks(turns):
    """Verbatim fallback while model summaries are unavailable or failed:
    - Read all replies after the latest user message, so references to earlier requests can be resolved.
    - Extract numbered items in confirmation / decision sections, appending nested A/B/C/D options.
    - Also extract other question lines, skipping pointers to earlier messages."""
    turns = list(turns)
    users = [i for i, t in enumerate(turns) if t['role'] == 'user']
    assts = [t for t in turns[(users[-1] + 1 if users else 0):] if t['role'] == 'assistant']
    if not assts:
        last = next((t for t in reversed(turns) if t['role'] == 'assistant'), None)
        assts = [last] if last else []
    out = []
    for t in assts:
        in_sec = False
        for ln in t['text'].splitlines():
            raw, s = ln.rstrip(), _clean(ln)
            if not s:
                continue
            head = raw.lstrip().startswith('#') or (raw.strip().startswith('**') and raw.strip().endswith('**') and len(s) < 30)
            if head or ((s.endswith('：') and len(s) < 20 or s.endswith(':') and len(s) < 80) and not re.match(r'^\d', s)):
                in_sec = bool(SECTION_RE.search(s))
                continue
            if POINTER_RE.search(s):
                continue
            if OPTIONAL_RE.match(re.sub(r'^\d+[.、)）]\s*', '', s)):
                continue
            num = re.match(r'^(\d+)[.、)）]\s*(.+)', s)
            if in_sec and num and not raw.startswith((' ', '\t')):
                out.append(num.group(2)[:120])
            elif in_sec and out and raw.startswith((' ', '\t')):
                sub = re.split(r'[：:，,；;]', s)[0]
                sep = '' if out[-1].endswith(('：', ':')) else '；' if re.search(r'[\u4e00-\u9fff]', out[-1] + sub) else '; '
                out[-1] = (out[-1] + sep + sub)[:200]
            elif not in_sec:
                q = re.sub(r'^\d+[.、)）]\s*', '', s)
                if len(q) >= 8 and QUESTION_RE.search(q) and not q.startswith(('需要的话', '如果')) and not q.endswith(('的', '：', ':')):
                    out.append(q[:120])
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res[-6:]


class Summarizer:
    def __init__(self, path, backend):
        self.path = path
        self.active = False         # nothing is sent while summaries are off
        self.lock = threading.Lock()
        self.workers = 0
        self.pending = set()
        self.q = queue.PriorityQueue()
        try:
            with open(path, encoding='utf-8') as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        # One entry per session and summary language. Older caches were keyed by session only and written in Chinese.
        self.cache = {k if '|' in k else self._ck(k, v.get('lang', 'zh')): v for k, v in cache.items()}
        self.set_backend(backend)

    def set_backend(self, backend):
        """Switch tool or model while running. Each backend makes at most its own number of calls at a time, and
        summaries made by another model are redone."""
        with self.lock:
            self.backend = backend
            self.slots = threading.Semaphore(backend.workers if backend else 1)
            while backend and self.workers < backend.workers:
                threading.Thread(target=self._work, daemon=True).start()
                self.workers += 1

    @staticmethod
    def _ck(key, lang):
        return key + '|' + lang

    def get(self, key, lang):
        with self.lock:
            return self.cache.get(self._ck(key, lang))

    def is_pending(self, key, lang):
        with self.lock:
            return self._ck(key, lang) in self.pending

    def request(self, key, msg_key, turns, priority, fmt_ts, lang):
        if not self.backend or not self.active:
            return
        key = self._ck(key, lang)
        with self.lock:
            c = self.cache.get(key)
            if c and c.get('msg_key') == msg_key and c.get('v') == VERSION and c.get('model') == self.backend.label:
                wait = RETRY_SOON if c.get('fails', 1) < 3 else RETRY_AFTER
                if not c.get('error') or time.time() - c.get('at', 0) < wait:
                    return
            if key in self.pending:
                return
            self.pending.add(key)
        sel = recent_window(turns)
        self.q.put((priority, time.time(), key, msg_key, sel, fmt_ts, lang))

    def _work(self):
        while True:
            job = self.q.get()
            try:
                self._summarize(*job)
            except Exception as e:      # a dead worker would leave every later summary pending for good
                print('claude-codex-monitor: summary failed: %r' % e, file=sys.stderr, flush=True)
                with self.lock:
                    self.pending.discard(job[2])

    def _summarize(self, _priority, _at, key, msg_key, sel, fmt_ts, lang):
        with self.lock:
            backend, slots = self.backend, self.slots
        if not backend:
            with self.lock:
                self.pending.discard(key)
            return
        entry = {'msg_key': msg_key, 'v': VERSION, 'at': time.time(), 'model': backend.label, 'lang': lang}
        try:
            system, prompt = build_prompt(sel, fmt_ts, lang)
            with slots:
                res = parse_result(backend.complete(system, prompt, timeout=backend.timeout))
            items = []
            for it in res.get('items') or []:
                if isinstance(it, str):
                    it = {'text': it}
                n = it.get('from')
                src = sel[n - 1]['ts'] if isinstance(n, int) and 1 <= n <= len(sel) else None
                if it.get('text'):
                    items.append({'text': str(it['text']).strip(), 'from_ts': src})
            entry.update(waiting=bool(res.get('waiting')) and bool(items), items=items[:6],
                         optional=[str(x) for x in res.get('optional') or []][:4])
        except Exception as e:      # Fall back to original text; count failures and retry the first two after 30 seconds.
            entry['error'] = repr(e)[:200]
            prev = self.cache.get(key) or {}
            entry['fails'] = prev.get('fails', 0) + 1 if prev.get('msg_key') == msg_key and prev.get('error') else 1
        with self.lock:
            self.cache[key] = entry
            self.pending.discard(key)
            try:
                self._save()
            except OSError as e:        # keep summarizing even when the cache cannot be written this time
                print('claude-codex-monitor: could not save summaries: %r' % e, file=sys.stderr, flush=True)

    def prune(self, before):
        """Drop summaries made before the scanned range began."""
        with self.lock:
            old = [k for k, v in self.cache.items() if v.get('at', 0) < before]
            for k in old:
                del self.cache[k]
            if old:
                try:
                    self._save()
                except OSError as e:
                    print('claude-codex-monitor: could not save summaries: %r' % e, file=sys.stderr, flush=True)

    def _save(self):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, self.path)
