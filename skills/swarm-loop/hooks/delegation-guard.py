#!/usr/bin/env python3
"""delegation-guard.py — swarm-loop PreToolUse hook.

Counts CONSECUTIVE repo-mutating tool calls made by one session and resets the
count to zero on any delegation. It is a mechanism for the one rule in this
skill's operating model that has never been enforceable by prose: *delegate to
sub-agents by default*.

WHY A HOOK AND NOT A RULE. The instruction already exists, twice, and both
copies were loaded and read at session start — the global CLAUDE.md carries
"Delegate to sub-agents by default — this is the most-forgotten rule", and
`~/.claude/mistakes/MISTAKES.md` carries "Did delegable work solo and burned
orchestrator context (corrected 4x)". Neither prevented the failure they
describe. The measured incident: an orchestrator delegated correctly at every
PHASE BOUNDARY — seven build lanes, three auditors, a verifier, a report writer,
none of it prompted — and then, REACTIVELY MID-TURN, on one verifier finding,
personally read two modules, wrote a repro, patched two files, ran the suite,
found its own patch broke a property, reverted half, designed a second approach,
wrote two regression tests, ran an adversarial revert, fixed lint and committed:
~20 consecutive tool calls, every one of them delegable to a single lane with a
packet. The user's correction was "you're doing a lot of doing and not nearly
enough delegating."

The MECHANISM is the part worth encoding. Nobody decides to spend an hour solo;
it accretes one command at a time. At a phase boundary the unit of work is
visibly a task, so it gets a packet. Mid-turn each next step looks like "one
more command" and never like "a task". The standing rule fires on doubt — *when
in doubt, delegate* — and mid-turn there is no doubt, which is exactly why it
never fires. A principle keyed on a subjective state cannot bind an agent whose
subjective state is the thing that is wrong. So this guard asks nothing of the
agent's judgement: it counts, and past a threshold it refuses. That is the
git-guard's own discipline — it does not ask anyone to feel cautious about
`git stash`, it fails the call.

WHAT THE COUNTER MEASURES: how long since you last delegated. Not "how much
work you did", not "how destructive it was".

--------------------------------------------------------------------------
COUNTS (one per tool call, however many mutations that call contains)
  Edit / Write / NotebookEdit / MultiEdit whose target is inside the work area
    and is NOT under `.swarm-loop/` or `.claude/`.
  Bash whose command contains a simple command matching an AUTHORING shape:
    - a write redirect (`>`, `>>`, `>|`, `&>`, `&>>`) whose target resolves
      INSIDE the work area — the redirect IS the mutation, so what produced the
      bytes is irrelevant (`cat > src/x.py`, `python gen.py > src/x.py`)
    - `sed -i` / `perl -i` / `ruby -i` over an in-area file
    - `tee` / `truncate` / `dd of=` onto an in-area file
    - `rm` / `mv` / `cp` / `mkdir` / `touch` / `chmod` / `chown` / `ln` /
      `install` over an in-area path — the operand each program actually
      WRITES (`MUTATOR_SPECS`), so `cp <repo>/src/a.py /tmp/a.py` is a copy out
      and does not count
    - a package ADD (`npm|pnpm|yarn install|add <pkg>`, `pip install <pkg>`,
      `uv add`, `poetry add`, `cargo add`, `go get`, `gem install`,
      `apt-get install`, `brew install`)
    - `git commit`, `git add`, `git apply`, `git am`

NEVER COUNTS — and this half is the load-bearing half, for the same reason it is
in git-guard: a guard that fires on correct behaviour gets switched off, and a
switched-off guard enforces nothing.
  Read-only work, which is everything that matches no authoring shape. Named
    explicitly because they are what the reactive-solo pattern is made of, and a
    reader should not have to infer them from the absence of a rule:
    `git log/status/diff/rev-parse/show/branch/worktree list/ls-files/blame`,
    `ls`, `cat`, `wc`, `grep`, `find`, `head`, `tail`, `sed -n`, `pytest`,
    `python3 -m py_compile`, and every other command with no write shape.
  EVERY `swarmloop.py` invocation, whatever the subcommand and however it is
    piped or logged. This is HAZARD 1 and it is the case that decides whether
    the feature is worth having. Phase 7's evaluation sequence — `verify`,
    `measure`, `analyze`, `checkpoint`, run one at a time, in a row — is
    genuinely the orchestrator's own work and must never be delegated; so are
    the collection checks (`check-branch`, `check-wave`, `conflicts`, `reads`,
    `push-gate`, `red-check`), the scheduling reads (`frontier`, `status`,
    `resume`, `reachable`) and the ledger writes (`init`, `freeze`, `record`,
    `findings`, `dispatch`, `retire`, `distil`, `escalate`, `audit-loop-done`,
    `restore-harness`). Exempting the whole CLI rather than an enumerated subset
    is deliberate: a subcommand added later must not silently start counting,
    and there is no swarmloop.py subcommand a lane could be given.
  The GIT INTEGRATION VERBS: `merge`, `rebase`, `revert`, `cherry-pick`,
    `checkout` / `switch` (including `-b`), `branch`, `worktree`, `push`,
    `fetch`, `pull`, `tag`, `stash`, `reset`, `restore`, `clean`, `mv`, `rm`.
    This deviates from the brief that ordered this hook, which listed
    `git commit/add/merge/rebase/revert/checkout -b` as counting, and the
    deviation is on the brief's OWN governing instruction: `references/
    merge-protocol.md` step 4 is "merge one branch at a time, verify after
    each", so a seven-lane wave is seven consecutive `git merge --no-ff
    task/<id>` calls with nothing delegable in them, and step 9's landing adds
    `git checkout main` + `git merge --ff-only` + `git rebase` on top. Counting
    those trips the guard on Phase 6 of every single wave. It is also what the
    operating model itself says to keep: *"Keep for yourself only: the
    decisions, the integration, the merge of conflicting edits."* `commit` and
    `add` stay counted because they are how a solo patch gets recorded — and
    the collection case that needs them is covered by the next rule.
  ANYTHING while a merge / rebase / cherry-pick / revert / bisect is IN
    PROGRESS (`.git/MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`,
    `rebase-merge/`, `rebase-apply/`, `BISECT_LOG`). merge-protocol.md step 5
    says textual conflicts are resolved BY THE ORCHESTRATOR, in the primary
    checkout — `git show :2:<file> > <file>`, then `git add`, then
    `git commit`. That is three counted shapes per conflicted file, on a
    procedure this skill mandates. Reading one file to find out is cheaper than
    being wrong about it.
  ANY call whose work area is a LINKED WORKTREE (`<root>/.git` is a file, not a
    directory). Workers run in worktrees and a worker doing twenty consecutive
    edits is doing exactly what its packet asked. Without this the guard would
    deny lanes, i.e. it would enforce the opposite of its own rule. See SCOPE
    for the residual case this does not cover.
  Anything OUTSIDE the work area: `/tmp`, `/dev/null`, a sibling scratch dir. A
    path is "inside" if it resolves under the nearest ancestor of the payload's
    `cwd` that contains a `.git` — falling back to `cwd` itself when there is
    none.
  Heredoc BODIES, which are dropped as data before tokenization exactly as
    git-guard drops them. The loop mandates writing task packets by heredoc, and
    a packet's prose quotes `git commit` and `sed -i` constantly.

RESETS TO ZERO
  Any `Agent` / `Task` dispatch, and any `SendMessage` to a running agent. That
  is the whole point of the counter: it is a clock since the last delegation,
  not a budget.

--------------------------------------------------------------------------
THRESHOLDS — 5 (remind) and 8 (deny) are A STARTING POINT, NOT A MEASUREMENT.
Nobody has calibrated them; they are one reading of one incident (~20 calls) and
nothing else. They are overridable per run:

    SWARMLOOP_DELEGATION_REMIND=<n>   default 5   allow, but inject a reminder
    SWARMLOOP_DELEGATION_DENY=<n>     default 8   refuse, exit 2
    SWARMLOOP_DELEGATION_GUARD=off                disable entirely
    SWARMLOOP_DELEGATION_STATE_DIR=<dir>          where the counters live

If a run finds itself reminded during legitimate orchestration, RAISE THEM and
record what tripped it — that reading is worth more than the defaults are.

STATE — keyed by `session_id`, one small JSON file per session, under
`~/.claude/swarm-loop/delegation/` (or `$SWARMLOOP_DELEGATION_STATE_DIR`).
Deliberately OUTSIDE the measured repo: this skill has a measured incident about
files inside the repo being swept up by root-walking metric commands, and a
counter file that changes on every tool call is the worst possible thing to hand
a tree-walking metric. A payload carrying no `session_id` is not counted at all
— a consecutive run cannot be attributed to nobody.

FAIL OPEN, ALWAYS AND EVERYWHERE. Unreadable state, an unwritable directory, a
corrupt counter file, an unparseable payload, a tokenizer crash, a missing
`cwd`, a `.git` that cannot be read — every one of them ALLOWS. `main()` catches
BaseException and exits 0. There is no fail-closed rule in this file and there
should not be one: a hook that can wedge an unattended loop gets disabled, and a
disabled hook enforces nothing. This is the git-guard's discipline, minus its
single fail-closed exception (that one exists because an unclassified recursive
delete is unrecoverable; a missed delegation reminder is not).

SCOPE — session-scoped, like git-guard, and registered in the same
`<repo>/.claude/settings.local.json`. It needs its OWN PreToolUse entry with its
OWN matcher, because it routes two tools git-guard does not:

    "matcher": "Bash|Edit|Write|NotebookEdit|MultiEdit|Task|Agent|SendMessage"

Drop `Task`/`Agent`/`SendMessage` from that string and the RESET never fires,
which turns the guard from a delegation clock into a blanket cap on mutating
calls — strictly worse than not installing it. `REQUIRED_MATCHER` below is the
written statement of the correct value and the test battery asserts every routed
tool appears in it.

The residual hole, stated rather than left implicit: a subagent that runs in the
PRIMARY checkout rather than a worktree (a report writer, a doc auditor) may
share the parent's `session_id`, in which case its writes land on the
orchestrator's counter. Its first write follows an `Agent` dispatch that just
reset the count to zero, so it starts from 0 and would need REMIND consecutive
writes of its own to trip; the observed shape for those agents is one to three
files. Mutating lanes run in worktrees and are exempt by the rule above.

Contract (Claude Code PreToolUse):
  stdin              JSON; `tool_name`, `tool_input`, `cwd`, `session_id`.
  exit 0, no output  ALLOW (below the reminder threshold, or not counted).
  exit 0 + JSON      ALLOW with a reminder — `hookSpecificOutput.
                     additionalContext` (the model-facing half, best-effort:
                     PreToolUse support for it is version-dependent and this
                     claim is NOT version-stamped) plus `systemMessage` (the
                     operator-facing half). Never `permissionDecision: "allow"`
                     — that would bypass the user's own permission rules, which
                     is not this hook's business.
  exit 2 + stderr    DENY; stderr is shown to the model, which is why the deny
                     threshold is the half that actually binds.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

# --------------------------------------------------------------------------
# Configuration. Thresholds are UNCALIBRATED — see THRESHOLDS in the docstring.
# --------------------------------------------------------------------------

ENV_OFF = "SWARMLOOP_DELEGATION_GUARD"
ENV_REMIND = "SWARMLOOP_DELEGATION_REMIND"
ENV_DENY = "SWARMLOOP_DELEGATION_DENY"
ENV_STATE_DIR = "SWARMLOOP_DELEGATION_STATE_DIR"

DEFAULT_REMIND = 5
DEFAULT_DENY = 8

# Every tool this hook must be routed for. Missing FILE_TOOLS/Bash loses
# counting (the guard goes quiet); missing RESET_TOOLS loses the RESET, which is
# worse than quiet — it becomes a blanket cap. Asserted by the test battery.
REQUIRED_MATCHER = "Bash|Edit|Write|NotebookEdit|MultiEdit|Task|Agent|SendMessage"

FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
RESET_TOOLS = frozenset({"Task", "Agent", "SendMessage"})

# Paths whose contents are the RUN's own bookkeeping, not the product being
# built: the ledger, the frozen acceptance suite, worker preambles, and the
# session's settings/hook wiring. Editing them is orchestration.
EXEMPT_DIR_NAMES = (".swarm-loop", ".claude")


def _threshold(name: str, default: int) -> int:
    """An env override that is missing, empty, non-numeric or < 1 leaves the
    default in place. A malformed override must never become a threshold of 0,
    which would deny the very first mutating call of a run."""
    try:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        n = int(raw.strip())
        return n if n >= 1 else default
    except Exception:
        return default


def guard_disabled() -> bool:
    try:
        return (os.environ.get(ENV_OFF) or "").strip().lower() in (
            "0", "off", "no", "false", "disable", "disabled")
    except Exception:
        return False


# --------------------------------------------------------------------------
# Tokenizer. Same shape as hooks/git-guard.py's, trimmed to what this guard
# needs: quotes respected, heredoc BODIES dropped as data, backslash
# continuations joined, operators kept as their own tokens so redirect targets
# survive segmentation. Copied rather than imported on purpose — this hook runs
# on EVERY tool call, and it must not acquire a way to fail that lives in
# another file.
# --------------------------------------------------------------------------

PUNCTUATION_CHARS = "();<>|&`"
OPERATOR_CHARS = ";|&()<>`"
CONTINUATION_RE = re.compile(r"(?<!\\)\\\n")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FD_RE = re.compile(r"^[0-9]+$")

MAX_WORD_CHARS = 20000
LONG_WORD_RE = re.compile(r"[^\s;|&()<>`]{%d,}" % (MAX_WORD_CHARS + 1))
LONG_WORD_PLACEHOLDER = "swarmloop_elided_long_token"

WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "exec", "builtin",
            "timeout", "gtimeout", "xargs", "stdbuf", "setsid", "ionice", "doas"}
KEYWORDS = {
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "in", "select", "function", "!", "{", "}", "[[", "]]",
}
WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-I", "-i", "-L", "-l", "-d", "-E", "-e", "-P", "-s", "-a"},
    "time": {"-f", "-o", "--format", "--output"},
    "exec": {"-a"},
}
DURATION_WRAPPERS = {"timeout", "gtimeout"}
DURATION_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SHELL_VALUE_OPTS = {"-o", "+o", "--rcfile", "--init-file"}
MAX_SHELL_DEPTH = 3


def is_operator(tok: str) -> bool:
    return len(tok) > 0 and all(c in OPERATOR_CHARS for c in tok)


def is_write_redirect(tok: str) -> bool:
    """`>`, `>>`, `>|`, `&>`, `&>>` — an operator token that WRITES. `<`, `<<`,
    `<<<`, `|` and `||` carry no `>` and are not writes."""
    return (len(tok) > 0 and all(c in "<>&|" for c in tok) and ">" in tok)


def elide_long_tokens(command: str) -> str:
    """A pathologically long word (a base64 blob, minified JSON, a pasted
    lockfile) makes shlex quadratic. Elide the WORD, never bail on the command —
    git-guard measured that bailing on the whole command is worse than the
    problem it solves."""
    if len(command) <= MAX_WORD_CHARS:
        return command
    return LONG_WORD_RE.sub(LONG_WORD_PLACEHOLDER, command)


def tokenize(command: str) -> list:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION_CHARS)
        lexer.whitespace_split = True
        return list(lexer)
    except Exception:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=PUNCTUATION_CHARS)
        lexer.whitespace_split = True
        return list(lexer)


def _scan_line(line: str, quote):
    """Quote state at end-of-line, plus the heredoc delimiters opened here."""
    delims = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                quote = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        if line[i:i + 3] == "$((":          # arithmetic, not a heredoc
            end = line.find("))", i + 3)
            i = end + 2 if end != -1 else i + 3
            continue
        if line[i:i + 3] == "<<<":          # here-string: no body
            i += 3
            continue
        if line[i:i + 2] == "<<":
            j = i + 2
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j < n and line[j] in "'\"":
                q = line[j]
                j += 1
                start = j
                while j < n and line[j] != q:
                    j += 1
                delims.append(line[start:j])
                j += 1
            else:
                start = j
                while j < n and (line[j].isalnum() or line[j] in "_-.\\"):
                    j += 1
                word = line[start:j].replace("\\", "")
                if word and not word.isdigit():
                    delims.append(word)
            i = j
            continue
        i += 1
    return quote, delims


def logical_lines(command: str) -> list:
    """Physical lines -> logical lines, heredoc BODIES dropped as data.

    This is the rule that keeps a task packet from counting as authoring: the
    loop writes packets by heredoc, and a packet's prose is full of `git
    commit`, `sed -i` and `rm -rf`. The `cat > packet.md` on the OPENING line
    still counts if it targets the work area, which is right — a file really is
    being written."""
    command = CONTINUATION_RE.sub(" ", command)
    out = []
    pending = ""
    pending_delims = []
    awaiting = []
    quote = None
    for raw in command.split("\n"):
        line = raw.rstrip("\r")
        if awaiting:
            if line.strip() == awaiting[0]:
                awaiting.pop(0)
            continue
        quote, delims = _scan_line(line, quote)
        pending_delims.extend(delims)
        pending = line if not pending else pending + "\n" + line
        if quote is None:
            out.append(pending)
            pending = ""
            awaiting = pending_delims
            pending_delims = []
    if awaiting:
        return out                      # unterminated body: already dropped
    if quote is not None:
        raise ValueError("unterminated quote")
    if pending:
        out.append(pending)
    return out


def token_stream(command: str) -> list:
    toks = None
    if "\n" in command:
        try:
            relined = []
            for ln in logical_lines(command):
                try:
                    lt = tokenize(ln)
                except Exception:
                    continue
                if lt:
                    relined.extend(lt)
                    relined.append(";")
            if relined:
                toks = relined
        except Exception:
            toks = None
    if toks is None:
        toks = tokenize(command)
    return toks


def base_name(tok: str) -> str:
    return tok.lstrip("$`(").rstrip(")`").rsplit("/", 1)[-1]


def program_index(seg: list) -> int:
    """Index of the real program, skipping `FOO=bar`, shell reserved words, and
    wrappers with their own options / separate values / `timeout`'s duration."""
    i = 0
    while i < len(seg):
        core = seg[i].lstrip("$`(")
        if core == "":
            i += 1
            continue
        if ENV_ASSIGN_RE.match(core):
            i += 1
            continue
        if core in KEYWORDS:
            i += 1
            continue
        if core in WRAPPERS:
            value_opts = WRAPPER_VALUE_OPTS.get(core, frozenset())
            i += 1
            while i < len(seg) and seg[i].startswith("-") and seg[i] != "--":
                takes_value = seg[i] in value_opts
                i += 1
                if takes_value and i < len(seg):
                    i += 1
            if i < len(seg) and seg[i] == "--":
                i += 1
            if core in DURATION_WRAPPERS and i < len(seg) and DURATION_RE.match(seg[i]):
                i += 1
            continue
        break
    return i


def shell_payload(seg: list):
    """`bash -c '<payload>'` — the payload is one token here and is re-scanned,
    or `sh -c 'cat > src/x.py'` walks straight past every rule below."""
    i = program_index(seg)
    if i >= len(seg) or base_name(seg[i]) not in SHELLS:
        return None
    j = i + 1
    while j < len(seg) and seg[j].startswith("-") and seg[j] != "--":
        a = seg[j]
        if a == "--command" or (not a.startswith("--") and "c" in a[1:]):
            return seg[j + 1] if j + 1 < len(seg) else None
        if a in SHELL_VALUE_OPTS:
            j += 2
            continue
        j += 1
    return None


# --------------------------------------------------------------------------
# WHERE the command runs, and what counts as "inside the work area".
# --------------------------------------------------------------------------

MAX_ANCESTOR_WALK = 64


def work_root(cwd: str) -> str:
    """Nearest ancestor of `cwd` holding a `.git`, else `cwd` itself.

    Pure filesystem, no subprocess: this hook runs on every tool call and a
    `git rev-parse` per call is a tax on the whole session for an answer the
    directory tree already holds."""
    try:
        d = os.path.realpath(cwd)
    except Exception:
        return cwd
    for _ in range(MAX_ANCESTOR_WALK):
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return d
        except Exception:
            return d
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    try:
        return os.path.realpath(cwd)
    except Exception:
        return cwd


def is_linked_worktree(root: str) -> bool:
    """A linked worktree's `.git` is a FILE (`gitdir: ...`), a primary
    checkout's is a directory. This is the whole test for "am I a worker?", and
    it is the rule that stops this guard from denying the lanes it exists to
    encourage."""
    try:
        return os.path.isfile(os.path.join(root, ".git"))
    except Exception:
        return False


MERGE_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                 "rebase-merge", "rebase-apply", "BISECT_LOG")


def integration_in_progress(root: str) -> bool:
    """A merge / rebase / cherry-pick / revert / bisect is open in this tree.

    merge-protocol.md step 5 makes conflict resolution the ORCHESTRATOR's job,
    in the primary checkout — `git show :2:<file> > <file>`, `git add`,
    `git commit` — which is three counted shapes per conflicted file on a
    procedure this skill mandates. One directory listing is cheaper than being
    wrong about it."""
    try:
        gitdir = os.path.join(root, ".git")
        if not os.path.isdir(gitdir):
            return False
        for marker in MERGE_MARKERS:
            if os.path.exists(os.path.join(gitdir, marker)):
                return True
    except Exception:
        return False
    return False


def resolve(path: str, cwd: str):
    """Absolute, with symlinks resolved on the deepest EXISTING ancestor and the
    not-yet-existing tail rejoined.

    A plain `normpath` is not enough, and the way it fails is silent: `work_root`
    realpaths, so on macOS it returns `/private/var/folders/…/repo` while the
    payload says `/var/folders/…/repo/src/a.py`, and the ancestor test then says
    every file in the repo is OUTSIDE the work area — the guard counts nothing
    and looks like it is working. `/tmp` -> `/private/tmp` and any symlinked
    checkout do the same. Measured on this machine before the fix: a five-edit
    streak in a repo under `/tmp` produced no counter file at all.

    `realpath` on the WHOLE path is equally wrong in the other direction: the
    target of `> src/new.py` does not exist yet, and a missing leaf must still
    resolve."""
    try:
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        p = os.path.normpath(p)
        head, tail = p, []
        for _ in range(MAX_ANCESTOR_WALK):
            if os.path.exists(head):
                break
            head, part = os.path.split(head)
            if not part or not head:
                break
            tail.append(part)
        try:
            head = os.path.realpath(head)
        except Exception:
            pass
        return os.path.join(head, *reversed(tail)) if tail else head
    except Exception:
        return None


def _under(path: str, root: str) -> bool:
    try:
        if path == root:
            return True
        return path.startswith(root.rstrip(os.sep) + os.sep)
    except Exception:
        return False


def in_exempt_dir(abspath: str, root: str) -> bool:
    """`.swarm-loop/` and `.claude/` anywhere under the work area."""
    try:
        rel = os.path.relpath(abspath, root)
    except Exception:
        return False
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    return any(p in EXEMPT_DIR_NAMES for p in parts)


def counts_as_target(path, cwd: str, root: str) -> bool:
    """Is writing this path authoring work the run is building?

    Inside the work area, outside `.swarm-loop/` and `.claude/`. `/tmp/x`,
    `/dev/null` and `../scratch/notes` all fall out on the first test — they are
    not under the root — so no special-casing of temp directories is needed, and
    a fixture repo that itself lives under /tmp still classifies correctly."""
    if not isinstance(path, str) or not path.strip():
        return False
    if FD_RE.match(path.strip()):
        return False                    # `>&2` — a file descriptor, not a path
    ap = resolve(path, cwd)
    if ap is None:
        return False
    if not _under(ap, root):
        return False
    return not in_exempt_dir(ap, root)


# --------------------------------------------------------------------------
# Classification of one simple command.
# --------------------------------------------------------------------------

SWARMLOOP_SCRIPTS = ("swarmloop.py",)

# git verbs that RECORD MY OWN edits. Everything else git does is integration,
# landing, teardown or investigation — the orchestrator's own work by doctrine.
GIT_COUNTED = frozenset({"commit", "add", "apply", "am"})

# Named so a reader can see the read-only set is a decision and not an oversight.
# Nothing dispatches on this set — a program not in it is still NEUTRAL unless a
# rule below fires — but pinning it is what makes "read-only never counts" a
# claim the battery can test rather than a property of silence.
READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "wc", "grep", "egrep", "fgrep", "rg", "find", "head", "tail",
    "sed", "awk", "cut", "sort", "uniq", "tr", "diff", "stat", "file", "which",
    "pwd", "date", "echo", "printf", "jq", "tree", "du", "df", "basename",
    "dirname", "realpath", "readlink", "pytest", "python3", "python", "node",
    "npx", "make", "ruff", "mypy", "eslint", "tsc", "cargo", "go", "true",
    "false", "test", "sleep", "wait", "kill", "ps", "less", "more", "column",
})

INPLACE_PROGRAMS = frozenset({"sed", "perl", "ruby"})
SED_SCRIPT_OPTS = frozenset({"-e", "-f", "--expression", "--file"})

# WHICH operands a mutator actually writes, per program. Reading "every
# non-option token" instead cost two false counts in one pass:
# `cp {repo}/src/a.py /tmp/a.py` counted because the SOURCE is in the repo, and
# `truncate -s 0 /tmp/x` counted because `0` resolved to `<repo>/0`. A false
# count is the failure mode this guard cannot afford, so the operand a program
# writes is named rather than inferred.
#   "all"  every operand is a target        (rm, mkdir, touch, truncate)
#   "tail" every operand but the first      (chmod MODE file..., chown OWNER)
#   "dest" the last operand only            (cp, mv, ln, install)
MUTATOR_SPECS = {
    "rm":       (frozenset(), "all"),
    "rmdir":    (frozenset(), "all"),
    "unlink":   (frozenset(), "all"),
    "touch":    (frozenset({"-r", "--reference", "-d", "--date", "-t"}), "all"),
    "mkdir":    (frozenset({"-m", "--mode"}), "all"),
    "truncate": (frozenset({"-s", "--size", "-r", "--reference"}), "all"),
    "shred":    (frozenset({"-n", "--iterations", "-s", "--size"}), "all"),
    "tee":      (frozenset(), "all"),
    "chmod":    (frozenset({"--reference"}), "tail"),
    "chown":    (frozenset({"--reference"}), "tail"),
    "cp":       (frozenset({"-t", "--target-directory"}), "dest"),
    "mv":       (frozenset({"-t", "--target-directory"}), "dest"),
    "ln":       (frozenset({"-t", "--target-directory"}), "dest"),
    "install":  (frozenset({"-m", "-o", "-g", "--mode", "--owner", "--group",
                            "-t"}), "dest"),
    "rsync":    (frozenset(), "dest"),
}

# A package ADD, never a manifest SYNC. merge-protocol.md step 8 mandates
# `npm ci` / `uv sync` / `uv pip install -e '.[dev]'` as the orchestrator's
# environment re-sync before measurement; counting those would trip the guard on
# an explicitly required step.
PKG_ADD = {
    "npm": ("install", "i", "add"),
    "pnpm": ("install", "i", "add"),
    "yarn": ("add",),
    "bun": ("add",),
    "pip": ("install",),
    "pip3": ("install",),
    "poetry": ("add",),
    "uv": ("add",),
    "cargo": ("add",),
    "gem": ("install",),
    "brew": ("install",),
    "apt": ("install",),
    "apt-get": ("install",),
    "go": ("get",),
}
# operands that mean "sync from the manifest", not "add this package".
# `-U`/`--upgrade` is deliberately NOT here: upgrading a named package is an
# add, and exempting it would exempt `pip install -U requests`.
PKG_SYNC_OPERANDS = ("-r", "--requirement", "-e", "--editable", ".", "./")


def operands(args: list, value_opts=frozenset()) -> list:
    """Non-option tokens, skipping the VALUE of an option that takes one
    separately (`truncate -s 0 f` -> `f`, not `0` and `f`)."""
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            if a in value_opts:
                i += 2
                continue
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def mutator_targets(prog: str, args: list) -> list:
    value_opts, mode = MUTATOR_SPECS[prog]
    ops = operands(args, value_opts)
    if not ops:
        return []
    if mode == "dest":
        return ops[-1:]
    if mode == "tail":
        return ops[1:]
    return ops


def inplace_targets(prog: str, args: list) -> list:
    """`sed -i 's/a/b/' f` — the FIRST operand is the script unless `-e`/`-f`
    supplied one. Without this, `sed -i 's/a/b/' /tmp/x` counted because
    `s/a/b/` resolves under the repo root."""
    ops = operands(args, SED_SCRIPT_OPTS)
    if prog == "sed" and ops and not any(
            a.split("=")[0] in SED_SCRIPT_OPTS for a in args if a.startswith("-")):
        ops = ops[1:]
    return ops


def has_inline_i(args: list) -> bool:
    """`-i`, `-i.bak`, `-pi`, `--in-place`. Stops at nothing: `sed -n` has no
    `i`, and `sed -e 's/i//' f` keeps the `i` inside a quoted token, which is
    one token and not an option."""
    for a in args:
        if a.startswith("--"):
            if a.split("=")[0] in ("--in-place",):
                return True
            continue
        if a.startswith("-") and len(a) > 1:
            if "i" in a[1:]:
                return True
    return False


def find_subcommand(args: list):
    j = 0
    while j < len(args):
        a = args[j]
        if a.startswith("-"):
            if "=" in a:
                j += 1
            elif a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                j += 2
            else:
                j += 1
            continue
        return a
    return None


def git_reason(args: list, cwd: str, root: str):
    sub = find_subcommand(args)
    if sub not in GIT_COUNTED:
        return None
    if sub == "add":
        # `git add .swarm-loop` is Phase 7 committing the run's own evidence.
        # `git add -A` names nothing, so it is not exempt.
        ops = [o for o in operands(args) if o != "add"]
        if ops and all(not counts_as_target(o, cwd, root) for o in ops):
            return None
    return "git " + sub


def pkg_reason(prog: str, args: list):
    subs = PKG_ADD.get(prog)
    if not subs:
        return None
    ops = operands(args)
    # `uv pip install …` and `poetry run pip install …` are the manifest-sync
    # spellings; only the top-level ADD verb (`uv add`) is in `subs`, so they
    # fall out here rather than needing a rule of their own.
    if not ops or ops[0] not in subs:
        return None
    rest = ops[1:]
    if not rest:
        return None                     # `npm install` alone = manifest sync
    if any(a in PKG_SYNC_OPERANDS for a in args):
        return None
    return prog + " " + ops[0] + " " + rest[0]


def segment_reason(seg: list, cwd: str, root: str, depth: int = 0):
    """(exempt, reason) for ONE simple command.

    `exempt` short-circuits the whole tool call — see `scan_command`."""
    if not seg:
        return False, None

    # A swarmloop.py invocation anywhere in the segment, however it is spelled:
    # `swarmloop.py measure`, `python3 <skill>/scripts/swarmloop.py verify`,
    # `uv run scripts/swarmloop.py analyze`.
    for tok in seg:
        if base_name(tok) in SWARMLOOP_SCRIPTS:
            return True, None

    i = program_index(seg)
    if i >= len(seg):
        return False, None
    prog = base_name(seg[i])
    args = seg[i + 1:]

    payload = shell_payload(seg)
    if payload is not None and depth < MAX_SHELL_DEPTH:
        try:
            return scan_command(payload, cwd, root, depth + 1)
        except Exception:
            return False, None

    if prog == "git":
        return False, git_reason(args, cwd, root)

    if prog in INPLACE_PROGRAMS and has_inline_i(args):
        for o in inplace_targets(prog, args):
            if counts_as_target(o, cwd, root):
                return False, prog + " -i " + o
        return False, None

    if prog == "dd":
        for a in args:
            if a.startswith("of=") and counts_as_target(a[3:], cwd, root):
                return False, a
        return False, None

    if prog in MUTATOR_SPECS:
        for o in mutator_targets(prog, args):
            if counts_as_target(o, cwd, root):
                return False, prog + " " + o
        return False, None

    reason = pkg_reason(prog, args)
    if reason:
        return False, reason

    return False, None


def scan_command(command: str, cwd: str, root: str, depth: int = 0):
    """(exempt, reason) for a whole Bash command string.

    EXEMPT WINS over a mutating segment, deliberately and asymmetrically: an
    orchestrator command that is piped or logged (`swarmloop.py measure 2>&1 |
    tee /tmp/m.log`, `swarmloop.py analyze > report.md`) must not count, and the
    cost is that a mutation chained onto one is missed. That is the cheap
    direction — a missed count delays a reminder, a false count teaches the
    operator to switch the hook off."""
    toks = token_stream(elide_long_tokens(command))
    seg = []
    reason = None
    i = 0
    while i < len(toks):
        tok = toks[i]
        if is_write_redirect(tok):
            target = toks[i + 1] if i + 1 < len(toks) else None
            if target is not None and not is_operator(target):
                if reason is None and counts_as_target(target, cwd, root):
                    reason = "redirect > " + target
                i += 2
                continue
            i += 1
            continue
        if is_operator(tok):
            exempt, r = segment_reason(seg, cwd, root, depth)
            if exempt:
                return True, None
            reason = reason or r
            seg = []
            i += 1
            continue
        seg.append(tok)
        i += 1
    exempt, r = segment_reason(seg, cwd, root, depth)
    if exempt:
        return True, None
    return False, (reason or r)


# --------------------------------------------------------------------------
# State: one counter per session, outside the measured repo.
# --------------------------------------------------------------------------

SESSION_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_KEY_CHARS = 80
PRUNE_AFTER_SECONDS = 14 * 24 * 3600
PRUNE_WHEN_OVER = 200


def state_dir() -> str:
    d = os.environ.get(ENV_STATE_DIR)
    if d and d.strip():
        return os.path.expanduser(d.strip())
    return os.path.join(os.path.expanduser("~"), ".claude", "swarm-loop", "delegation")


def state_path(session_id: str):
    """`<state dir>/<sanitized session>.json`.

    Sanitized rather than trusted: a session id is payload data, and a payload
    field that reaches `os.path.join` unfiltered is a path-traversal write."""
    key = SESSION_KEY_RE.sub("_", session_id)[:MAX_KEY_CHARS]
    if not key:
        return None
    return os.path.join(state_dir(), key + ".json")


def read_count(path) -> int:
    """A missing, unreadable, corrupt or nonsensical counter reads as 0. There
    is no state fault that can produce a refusal."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        n = data.get("count")
        return n if isinstance(n, int) and n >= 0 else 0
    except Exception:
        return 0


def write_count(path, count: int, reason) -> None:
    """Best effort. An unwritable state directory means the counter never rises,
    which is the fail-open answer: the guard goes quiet instead of wedging."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"count": count, "updated": int(time.time()),
                       "last": reason or ""}, fh)
        os.replace(tmp, path)
    except Exception:
        return
    if count <= 1:
        _prune()


def _prune() -> None:
    """One counter file per session accumulates forever otherwise. Runs only at
    the start of a fresh streak, and never raises."""
    try:
        d = state_dir()
        names = os.listdir(d)
        if len(names) <= PRUNE_WHEN_OVER:
            return
        cutoff = time.time() - PRUNE_AFTER_SECONDS
        for name in names:
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                continue
    except Exception:
        return


# --------------------------------------------------------------------------
# Verdicts.
# --------------------------------------------------------------------------

WHY = (
    "WHY: this fires on a COUNT, not on a judgement, because the judgement is "
    "the thing that fails. Nobody decides to spend an hour building solo — it "
    "accretes one command at a time, and mid-turn each next step looks like "
    "'one more command' rather than 'a task'. The standing rule says 'when in "
    "doubt, delegate', and mid-turn there is no doubt."
)

WHAT_TO_DO = (
    "WHAT TO DO: stop, write a task packet for the remaining thread of work "
    "(goal, file scope, verify command, done-when) and dispatch a lane with the "
    "Agent tool. Keep for yourself only the decisions, the integration, and the "
    "merge of conflicting edits. Any Agent dispatch resets this counter to zero."
)


def allow() -> None:
    sys.exit(0)


def remind(count: int, deny_at: int, reason) -> None:
    """Allow, and say so out loud. `additionalContext` is the model-facing half
    and is best-effort — PreToolUse support for it is version-dependent, and
    that claim is not version-stamped here. `systemMessage` reaches the
    operator either way, and the DENY threshold is what actually binds."""
    msg = (
        "swarm-loop delegation-guard: %d CONSECUTIVE repo-mutating tool calls "
        "with no delegation (latest: %s). At %d this hook refuses.\n%s\n%s"
        % (count, reason or "unnamed", deny_at, WHY, WHAT_TO_DO)
    )
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": msg,
            },
            "systemMessage": msg,
        }))
    except Exception:
        pass
    sys.exit(0)


def deny(count: int, remind_at: int, reason) -> None:
    print(
        "BLOCKED (swarm-loop delegation-guard): %d CONSECUTIVE repo-mutating "
        "tool calls with no delegation (latest: %s). The reminder at %d was "
        "not acted on.\n%s\n%s\n"
        "This refusal is about WHO does the work, not about the command — the "
        "same command run by a dispatched lane is fine. If this run has "
        "genuinely reached a stretch of orchestrator-only work that the "
        "classifier is reading wrong, raise the thresholds for the run "
        "(%s / %s) and record what tripped it: the defaults are a starting "
        "point, not a measurement."
        % (count, reason or "unnamed", remind_at, WHY, WHAT_TO_DO,
           ENV_REMIND, ENV_DENY),
        file=sys.stderr,
    )
    sys.exit(2)


def _main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        allow()
        return
    if not isinstance(data, dict):
        allow()
        return
    if guard_disabled():
        allow()
        return

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        allow()                          # a streak cannot be attributed to nobody
        return
    path = state_path(session_id.strip())
    if not path:
        allow()
        return

    tool = data.get("tool_name")

    # THE RESET. First, and before any classification: this is the whole point
    # of the counter, and a fault in the counting path must never be able to
    # swallow the delegation that clears it.
    if tool in RESET_TOOLS:
        if read_count(path):
            write_count(path, 0, "delegated: " + str(tool))
        allow()
        return

    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        try:
            cwd = os.getcwd()
        except Exception:
            allow()
            return
    root = work_root(cwd)

    # A worker's own worktree. Twenty consecutive edits there is a lane doing
    # exactly what its packet asked.
    if is_linked_worktree(root):
        allow()
        return
    # merge-protocol.md step 5: conflict resolution is the orchestrator's job.
    if integration_in_progress(root):
        allow()
        return

    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}

    reason = None
    if tool in FILE_TOOLS:
        p = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(p, str) and counts_as_target(p, cwd, root):
            reason = str(tool) + " " + p
    elif tool is None or tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            try:
                exempt, reason = scan_command(command, cwd, root)
                if exempt:
                    reason = None
            except Exception:
                reason = None            # tokenizer fault -> not counted
    else:
        allow()                          # a tool this guard has no rules for
        return

    if not reason:
        allow()
        return

    remind_at = _threshold(ENV_REMIND, DEFAULT_REMIND)
    deny_at = _threshold(ENV_DENY, DEFAULT_DENY)

    count = read_count(path) + 1
    write_count(path, count, reason)

    if count >= deny_at:
        deny(count, remind_at, reason)
    if count >= remind_at:
        remind(count, deny_at, reason)
    allow()


def main() -> None:
    """Fail-open wrapper — the contract is exit 0 on ANY fault, so nothing may
    escape as a traceback. allow()/deny() raise SystemExit, which is a
    BaseException and must pass through untouched."""
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        pass
    allow()


if __name__ == "__main__":
    main()
