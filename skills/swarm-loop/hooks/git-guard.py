#!/usr/bin/env python3
"""git-guard.py — swarm-loop PreToolUse Bash hook.

Blocks the *repo-global, destructive* git operations that revert EVERY parallel
agent's uncommitted work when a swarm shares one working tree. Born from a live
incident: one fix-agent ran `git stash` + `git stash pop` to check whether some
type errors were pre-existing; the stash reverted five sibling agents'
in-progress uncommitted edits repo-wide, and the pop aborted on a conflict,
leaving their work only in a dangling stash. This guard is the belt-and-suspenders
layer under the real fix (each mutating agent runs in its own isolation).

Design:
  - argv-parsing, NOT a fragile inline regex. The command string is split into
    logical lines (backslash continuations joined, quoted strings kept whole,
    heredoc BODIES dropped as data), tokenized with shlex (quotes respected, so
    `git commit -m "use git stash carefully"` is NOT a stash call), split into
    simple commands on shell operators (`&&`, `||`, `|`, `;`, `(`, `)`,
    newlines, redirections), and each `git ...` segment is evaluated by
    subcommand.
  - A simple command is read past what precedes the real program: `FOO=bar`
    prefixes, shell reserved words (`then`, `do`, `else`, `{`, ...), and command
    wrappers with their own options and values (`sudo -u root`, `nice -n 5`,
    `timeout 30`, `xargs -I{}`, `env -u VAR`, `stdbuf -oL`, ...).
  - `bash -c '<payload>'` (sh/zsh/dash/ksh, `-lc`/`-euc` clusters too) and
    `eval <payload>` have their payload re-tokenized and re-evaluated, up to 3
    levels deep.
  - A subcommand the guard does not recognize is resolved against git's ALIASES
    (inline `-c alias.x=...` first, then `git config`); the expansion is
    re-evaluated, and a `!shell` alias body is re-scanned as a command line.
    `co = checkout` in a developer's dotfiles is boilerplate, so this path needs
    no adversarial intent at all.
  - Questions are answered by ASKING git (read-only `rev-parse` / `worktree
    list` / `config`, ~2s timeout, one longer retry): "is this checkout operand
    a ref or a path?", "is this subcommand an alias?", "what repo/worktree does
    this path belong to?". If git cannot answer — no binary, not a repository,
    timeout — the answer is ALLOW, with ONE exception documented under
    FAIL-OPEN below.
  - Git is asked FROM THE OPERAND, not from this process's cwd. A path names
    the thing it is about, so `git -C <that path>` is what gets asked. Asking
    from the process cwd is what made a cwd outside any repository disarm every
    path rule at once: `rm -rf <repo>` was exit 2 from the repo and exit 0 from
    one directory up. The command's own directory is tracked too — the payload's
    `cwd`, then any `git -C <path>` global, then any `cd <path>` segment.
  - Blocks ONLY positively-identified destructive repo-global ops. Everything
    else — including all normal loop/auditor git — is allowed. Getting an
    allow-case wrong (breaking `git commit`, or blocking a task packet that
    merely *quotes* `git stash`) is worse than a missed block-case, so every
    ambiguous case errs toward ALLOW.

Blocked:
  git stash (push/save/pop/apply/drop/clear/branch/store, and bare `git stash`)
  git reset --hard | --merge | --keep
  git checkout -- <path> | git checkout . | git checkout <tree-ish> <path>
  git checkout <pathspec> — ONE operand that git does not resolve as a ref and
    that names something on disk, plus the shapes no refname can take
    (`:/` = the whole repo, `src/`, a glob) | --pathspec-from-file
  git checkout -f / --force
  git switch --discard-changes / -f / --force
  git restore <path> (anything that touches the working tree)
  git clean with force (-f / --force, incl. -fd/-fdx), unless --dry-run/-n
  git worktree remove --force (the CHECKED `git worktree remove` is allowed —
    see below) | git worktree move
  the plumbing equivalents: git checkout-index -f/-a, git read-tree -u,
    git apply -R, and git rm over a repo-wide pathspec (`.`, `:/`, a glob)
  the ref/object destroyers, none of which the loop uses: git push --force
    (NOT --force-with-lease), git update-ref, git reflog expire|delete,
    git gc --prune=<not never>, git prune, git repack -ad, git filter-branch,
    git symbolic-ref <ref> <target>, git submodule deinit -f,
    git sparse-checkout set|add|init|reapply|disable
  the same effects under their OTHER spellings, which used to be exit 0 while
    the three above were exit 2 — the guard reasoned about spellings, not
    effects: git branch -f (moves a branch ref; `-D`/`--delete` stays allowed,
    it is the documented teardown), git checkout -B and git switch -C (force-
    reset an existing branch; `-b`/`-c` create and stay allowed),
    git push --mirror, git push --delete, git push <remote> :<ref> (the
    empty-source refspec = delete), git push <remote> +<src>:<dst> (`+` is
    --force for one ref), and git fetch <remote> +<src>:<local ref> (a forced
    fetch INTO a local branch; `+…:refs/remotes/…` and `+…:refs/tags/…` are the
    ordinary refspecs and stay allowed)

Blocked WITHOUT a git verb (rules keyed on the PATH, added after a live audit
found the guard had no path awareness at all):
  rm -r of the repo root, a worktree root, a directory CONTAINING worktree
    roots, `/`, or `$HOME` — including the collapsed form, since
    `rm -rf <worktree-root>/$ID` with `$ID` unset really is `rm -rf
    <worktree-root>/` and takes every live worktree's uncommitted work — and
    the VARIABLE-ROOT/LITERAL-TAIL form beside it, `rm -rf "$WT/T-010"`, which
    collapses to a harmless-looking `/T-010` and so walked past the rule above
    while naming a live worktree out loud
  rm / mv of anything under `.swarm-loop/` (the run's frozen goals, acceptance
    suite, manifest and history — deleting it destroys the DETECTOR, not just
    the data), and `find <under .swarm-loop> ... -delete`
  in-place rewrites of the run's LEDGER (`state.json`, `goals.json`,
    `manifest.json`, `history.csv`, `freeze-log.jsonl`, `escalations.jsonl`,
    `ESCALATIONS.md`): `> <ledger>`, `truncate`, `sed -i`, `dd of=`, `tee`, and
    an Edit/Write/NotebookEdit/MultiEdit payload naming one

  ...reached however the shell dresses them up: on a later line of a multi-line
  command, after a shell keyword (`if ...; then git stash; fi`), behind a
  wrapper (`timeout 30 git stash`, `nice -n 5 git stash`,
  `... | xargs git checkout --`), inside `bash -c '...'` or `eval '...'`,
  through a git alias (`git co .`), or via `$GIT stash`.

  Long options are matched by git's own abbreviation rule on BOTH sides, so
  `git reset --ha`, `git checkout --f` and `git clean --for` are blocked, while
  `git clean --dry -d` and `git restore --stag <path>` are recognized as the
  harmless ops they really are. A longer option that merely shares a prefix
  (`--force-with-lease`) is not an abbreviation of `--force` and does not match.
  Short options are scanned as flag clusters that STOP at a value-taking letter,
  so `-bfix/thing` stays a branch name and is not read as a cluster containing
  `f`.

NOT covered (by design — coverage is bounded by what the tokenizer can see as
argv, and widening it would cost false blocks, which are worse):
  - heredoc bodies fed to a shell (`bash <<EOF ... EOF`): a heredoc body is
    treated as DATA everywhere, because the loop mandates writing task packets
    by heredoc whose text quotes `git stash` / `git reset --hard`. False-blocking
    the loop's own dispatch step would wedge the run.
  - payloads that are only strings at RUN time: `eval "$cmd"` (the literal
    `eval 'git stash'` IS caught; the variable's contents are not),
    `python3 -c 'os.system("git stash")'`, a destructive op inside a script file.
  - a program named by a variable that does not mention git (`$X stash` where
    `X=git`); `$GIT`-style names are caught, the rest would cost false blocks.
  - the INTERIOR of a `$(...)` or backtick substitution that sits after a `cd`
    on the same line. A plain `cd <path> && ...` IS honoured now, and so is
    `git -C <path> ...`; but substitution bodies are scanned off the RAW string
    (single quotes decide whether bash runs them at all, and posix tokenization
    erases quotes), so their interiors are judged from the directory the line
    STARTED in. That errs toward BLOCK, which is the survivable direction, and
    it is what remains of the older, wider "anything after a `cd`" limitation.
  - `find . ... -delete` outside `.swarm-loop/`, and `... | xargs rm`. Narrow on
    purpose: `find . -name '*.pyc' -delete` is ordinary housekeeping, and a false
    block on housekeeping is worse than this miss.
  - EVERY file-tool write except a LEDGER path. The Edit/Write/NotebookEdit/
    MultiEdit branch checks `protected_write_reason`, which asks one question:
    is this the ledger? So a frozen acceptance test edited with `Edit` passes
    this hook, and so does any other file in the repo. That is deliberate — the
    frozen suite is tamper-EVIDENT via the manifest hash, and the sanctioned
    `freeze --amend` repair writes into that directory — but it means "the hook
    is armed for the file tools" is NOT "the file tools are covered". Widening
    the matcher arms exactly one rule, not a file-write policy.
  This is a guard rail against accidents, not a sandbox against a determined
  caller. The real protection is per-agent worktree isolation.

ONE FAIL-CLOSED RULE, and only one. A RECURSIVE `rm` whose target the guard
could not classify — because git could not be RUN at all (no binary, or a
timeout that survived the longer retry) — is REFUSED. Everything else in this
file still fails open. The reason it is the exception: an empty protected set
used to mean two different things at once, "nothing is protected here" and "I
could not find out", and a recursive delete is the one operation where guessing
the second is unrecoverable. Git ANSWERING "not a git repository" is not this
case — that is determined-and-empty, and it allows. If git really is missing,
the harness cannot run anyway.

FAIL-OPEN otherwise, and what it costs (stated plainly because it is a real
limit, not an oversight): `main()` catches BaseException and exits 0. Any
command that crashes the parser is therefore allowed WHOLESALE — not just the
part that crashed.
That is deliberate: a guard that blocks all bash when it hits a bug wedges an
unattended run, and a false block is worse than a missed block. Two reachable
crash paths were found and closed rather than left to this net — `$'…'` ANSI-C
quoting (retried with `posix=False` in `tokenize`) and a >20 KB token (elided
per-token in `elide_long_tokens` instead of bailing on the whole command, which
is what it used to do: one long word anywhere disarmed the guard for every
segment, across lines). Unbalanced quotes still fail open, by design.

One asymmetry, deliberate: `tool_name` ABSENT entirely is treated as Bash and can
be blocked, while `tool_name` present-but-unrecognized is allowed. A payload with
no tool name is either a probe or a harness bug; a payload naming some other tool
is a tool this guard has no rules for.

SCOPE — the guard is SESSION-scoped, not repo-scoped and not global. It is
registered in exactly one place, `<repo>/.claude/settings.local.json`. The
REQUIRED matcher string, verbatim, is

    "matcher": "Bash|Edit|Write|NotebookEdit|MultiEdit"

and it must list EVERY member of `FILE_TOOLS` (below) or the missing tool's
payloads never reach this hook at all. `MultiEdit` is the one that was armed in
code and omitted from the shipped settings template, so a MultiEdit write to
`.swarm-loop/state.json` was inspected by nobody — the guard's rule existed and
could not fire. Registering only `Bash` leaves every path rule for the file
tools disarmed in the same way. Two consequences:
every Bash call from that session is inspected regardless of target directory
(annoying, not damaging), and — the real hole — a worktree has no `.claude/` of
its own, so any `claude` process STARTED with cwd inside a worktree runs
completely unguarded. Guarded status depends on workers being Agent-tool
subagents of the parent session; that is a property of how the run is launched,
not something this file can enforce.

Allowed (verified): git add, git commit, git push, git reset (soft/mixed),
  git checkout <branch>, git checkout -b, git switch <branch>, git diff,
  git show, git show HEAD:<path> > <path>, git log, git status, git ls-tree,
  git ls-remote, git stash list, git stash show, git stash create,
  git worktree add, git worktree list, git worktree prune,
  git restore --staged <path> (index-only), git clean -n / --dry-run,
  git rm <file> and git rm --cached (scoped, not repo-global),
  git merge / rebase (incl. -i) / cherry-pick / revert / fetch / branch -D,
  git push --force-with-lease, git gc, git reflog show, git mv,
  writing/reading `.swarm-loop/acceptance/**` and `.swarm-loop/worker-preamble.md`
  (the frozen suite is tamper-EVIDENT via the manifest hash, and the only
  amendment path observed to work in a live run writes into that directory),
  running the frozen suite (`pytest .swarm-loop/acceptance ...`) — a blocked
  VERIFICATION while mutation is permitted is exactly backwards,
  and the sanctioned worktree teardown
  `git worktree remove <path> && git worktree prune && git branch -D <branch>`.

Contract (Claude Code PreToolUse):
  stdin              JSON; `tool_input.command` holds the bash command string;
                     `cwd` holds the directory the command will run in, and is
                     honoured — without it the ref-vs-path question for
                     `git checkout <operand>` was answered from the SESSION root,
                     so a worker could destructively check out any file that
                     existed only in its own worktree. `cwd` is the STARTING
                     point only: `git -C <path>` and `cd <path>` move it, and
                     path rules additionally ask git about the operand itself,
                     so no single directory decides a verdict any more.
  exit 0             ALLOW (no output).
  exit 2 + stderr    BLOCK; stderr shown to the model.
Fails OPEN (exit 0) on any parse/logic error or unbalanced quotes — a broken
guard must never block all bash. Only a positively-identified destructive op
returns exit 2.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# command wrappers that precede the real program
WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "exec", "builtin",
            "timeout", "gtimeout", "xargs", "stdbuf", "setsid", "ionice", "doas"}

# shell reserved words / grouping tokens that may precede the real program.
# Kept SEPARATE from WRAPPERS because a keyword takes no options of its own:
# `then -x git ...` is not a thing, and skipping options after a keyword would
# swallow the git command's own flags.
KEYWORDS = {
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "in", "select", "function", "!", "{", "}", "[[", "]]",
}

# per-wrapper options that consume the FOLLOWING token as their value, so the
# value is not mistaken for the program (`nice -n 5 git stash` -> prog `5`).
# Under-listing an option only loses a block (fail-open); it can never false-block.
WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t", "--user", "--group",
             "--prompt", "--close-from", "--host", "--other-user", "--role", "--type"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-n", "-I", "-i", "-L", "-l", "-d", "-E", "-e", "-P", "-s", "-a",
              "--max-args", "--replace", "--max-lines", "--delimiter", "--eof",
              "--max-procs", "--max-chars", "--arg-file", "--process-slot-var"},
    "time": {"-f", "-o", "--format", "--output"},
    "exec": {"-a"},
}

# wrappers taking a bare DURATION positional before the program. `timeout 30 git
# stash` hides git behind a token that is not dash-prefixed at all, so an
# option-only skip would walk out at `30` and silently miss the block.
DURATION_WRAPPERS = {"timeout", "gtimeout"}
DURATION_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")

# shells whose `-c <payload>` argument is itself a command line
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SHELL_VALUE_OPTS = {"-o", "+o", "--rcfile", "--init-file"}

# builtins whose ARGUMENTS are re-joined into a command line and executed
EVAL_WORDS = {"eval"}

# a program token that is a shell variable naming git: `GIT=${GIT:-git}; $GIT stash`
VAR_NAME_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# characters a git refname can never contain -> the operand is certainly a pathspec
GLOB_RE = re.compile(r"[*?\[]")

# git global options that consume the FOLLOWING token as their value (separate form)
GIT_VALUE_GLOBALS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--super-prefix", "--config-env",
}

SAFE_HINT = (
    "WHY: this git op is repo-global — in a shared working tree it reverts or "
    "destroys EVERY parallel agent's uncommitted work, not just yours "
    "(refs/stash is one ref shared by all worktrees).\n"
    "SAFE ALTERNATIVES:\n"
    "  - Compare your change vs the committed baseline with READ-ONLY "
    "`git show HEAD:<file>` or `git diff HEAD -- <file>` (never stash).\n"
    "  - Need a clean tree in isolation? Each mutating agent must run in its own "
    "worktree (`git worktree add ...` or the Agent tool's isolation: 'worktree'), "
    "never the shared primary tree.\n"
    # This hint used to prescribe `rm -rf <worktree-path>` — the one teardown the
    # guard did not cover at all — while blocking the checked one. That is
    # reversed now, and merge-protocol.md step 10 / task-packets.md must say the
    # same thing or a worker will follow a doc into a block.
    "  - Retiring a worktree? `git worktree remove <path>` then `git worktree "
    "prune` (both allowed). Do NOT `rm -rf` it: `remove` refuses a worktree with "
    "uncommitted changes, `rm -rf` does not, and an empty variable in the path "
    "(`rm -rf <root>/$ID`) takes every live worktree with it.\n"
    "If you genuinely must run this, do it yourself outside the swarm, or ask the "
    "orchestrator to run it once the wave has been collected and committed."
)


def allow() -> None:
    sys.exit(0)


def block(reason: str) -> None:
    print(
        "BLOCKED (swarm-loop git-guard): " + reason + "\n\n" + SAFE_HINT,
        file=sys.stderr,
    )
    sys.exit(2)


# Operator characters that separate simple commands. The backtick is here for
# the reason in PUNCTUATION_CHARS below: with it split off as punctuation,
# `` `git stash` `` segments cleanly instead of fusing the closing backtick onto
# `stash`. shlex GROUPS runs of punctuation, so `` (` `` and `` `) `` arrive as
# single tokens and must still read as operators — hence the all-chars test.
OPERATOR_CHARS = ";|&()<>`"


def is_operator(tok: str) -> bool:
    """A shell control/redirection operator token (&&, ||, |, ;, (, ), <, >, `)."""
    return len(tok) > 0 and all(c in OPERATOR_CHARS for c in tok)


# shlex's own `punctuation_chars=True` set is `();<>|&` — it does NOT include the
# backtick, so the closing backtick of a command substitution fused onto the LAST
# token: `` `git stash` `` tokenized as ['`git', 'stash`'], and `base_name` strips
# the leading backtick from the PROGRAM token only. Measured before this fix:
# `` `git stash` ``, `` `git reset --hard` `` and ``X=`git stash` `` were all
# ALLOWED while `` `git stash pop` `` was blocked — a silent, shape-dependent hole,
# which is worse than a uniform miss. Backticks inside quotes are untouched
# (shlex applies punctuation only outside quoted regions), so
# ``echo "do not run `git stash`"`` and ``grep '`git stash`' docs/`` stay ALLOWED.
PUNCTUATION_CHARS = "();<>|&`"


def tokenize(command: str) -> list[str]:
    """shlex tokenization respecting quotes; operators come out as their own tokens.

    Raises on unbalanced quotes — caller treats that as fail-open.

    A POSIX-mode failure is retried with `posix=False` before giving up, because
    `$'…'` ANSI-C quoting is valid bash that shlex's POSIX mode cannot lex:
    `echo $'don\\'t' ; git stash` is `bash -n` clean but raised ValueError, and the
    whole command — including the `git stash` — was allowed. Non-POSIX mode keeps
    quotes attached to their tokens, so quoted prose (`git commit -m "git stash"`)
    stays one inert token and cannot become a false block. Genuinely unbalanced
    quotes raise in BOTH modes (verified: `echo "unterminated`,
    `git stash 'unbalanced`), so the fail-open contract for those is unchanged.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION_CHARS)
        lexer.whitespace_split = True
        return list(lexer)
    except Exception:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=PUNCTUATION_CHARS)
        lexer.whitespace_split = True
        return list(lexer)  # still raises for a truly unbalanced quote -> fail open


# shlex.read_token accumulates a token with string concatenation, so tokenizing
# ONE pathologically long word is quadratic in its length (`echo <200k chars>`
# burns ~1s). No real command carries a word this long; a base64/data blob,
# minified JSON, a data URI or a pasted lockfile fragment does.
#
# The FIRST version of this defence bailed out of the whole command above the
# limit, and that was measurably worse than the problem: reproduced against the
# live hook, `git status⏎echo <20001 A's>⏎git stash` exited 0 — one oversized word
# ANYWHERE disarmed the guard for EVERY segment, across lines, silently. So elide
# only the offending word and evaluate everything else.
MAX_WORD_CHARS = 20000
LONG_WORD_RE = re.compile(r"[^\s;|&()<>`]{%d,}" % (MAX_WORD_CHARS + 1))
# An inert placeholder: not `git`, not a path, not an option, not a pathspec —
# so wherever the blob sat, the remaining argv reads as "some ordinary word".
LONG_WORD_PLACEHOLDER = "swarmloop_elided_long_token"


def elide_long_tokens(command: str) -> str:
    """Replace every >MAX_WORD_CHARS run of non-delimiter characters with an inert
    placeholder, so the rest of the command is still parsed and evaluated.

    Quoting is preserved when the blob is a whole quoted word (the quotes ride
    along in the match and are replaced together). When a blob straddles a quote
    boundary the substitution can leave the quote unbalanced, at which point
    `tokenize` raises and the caller fails open — exactly the old behaviour, and
    never a false block."""
    if len(command) <= MAX_WORD_CHARS:
        return command  # cheap path: no word can exceed the whole string
    return LONG_WORD_RE.sub(LONG_WORD_PLACEHOLDER, command)


CONTINUATION_RE = re.compile(r"(?<!\\)\\\n")


def _scan_line(line: str, quote: str | None) -> tuple[str | None, list[str]]:
    """Scan one physical line outside/inside quotes.

    Returns the quote state at end-of-line and the heredoc delimiters opened on
    this line (in order). A `<<<` here-string opens no body. Only used to decide
    which following lines are heredoc BODY (data, never commands)."""
    delims: list[str] = []
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
            break  # comment runs to end of line
        if line[i:i + 3] == "$((":
            # arithmetic expansion: `$((1<<2))` is a SHIFT, not a heredoc. Read
            # as a heredoc it would swallow every following line as body and
            # silently drop a real `git stash` on the next line.
            end = line.find("))", i + 3)
            i = end + 2 if end != -1 else i + 3
            continue
        if line[i:i + 3] == "<<<":
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
                # an all-digit "delimiter" is a shift/fd, not a heredoc word
                if word and not word.isdigit():
                    delims.append(word)
            i = j
            continue
        i += 1
    return quote, delims


def logical_lines(command: str) -> list[str]:
    """Physical lines -> logical lines: backslash continuations joined, lines
    inside a quoted string kept together, heredoc BODIES dropped (they are data).

    Needed because shlex treats "\\n" as plain whitespace, so a multi-line
    command otherwise fuses into ONE argv list: `git status` + `git stash`
    becomes `git status git stash` (never seen as a stash), and
    `git checkout main` + `git status` becomes a 2-operand checkout (a FALSE
    block). Continuations must be joined FIRST, or `git reset \\<newline>--hard`
    would split into two harmless-looking segments and lose a block that the
    naive per-line split would otherwise have caught.

    Raises if a heredoc body never terminates - caller then falls back to
    whole-command tokenization rather than parsing document text as commands."""
    command = CONTINUATION_RE.sub(" ", command)
    out: list[str] = []
    pending = ""
    pending_delims: list[str] = []
    awaiting: list[str] = []
    quote: str | None = None
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
        # Body never terminated (truncated command). Everything from the `<<`
        # onward has already been dropped as data, which is the fail-open answer:
        # re-tokenizing document prose is how `| 1 | git checkout . |` in a
        # markdown table becomes a FALSE BLOCK.
        return out
    if quote is not None:
        raise ValueError("unterminated quote")
    if pending:
        out.append(pending)
    return out


def token_stream(command: str) -> list[str]:
    """Every token of a command string, operators included.

    Operators are KEPT, because the redirection rules need to see what a plain
    segmentation throws away: `> .swarm-loop/state.json` leaves only the bare
    path behind once `>` has been consumed as a boundary. `_scan_command` walks
    this stream once, cutting simple commands at the operators and checking
    redirect targets as it passes them, so a `cd` earlier on the line is
    already in effect when a later target is judged."""
    toks: list[str] | None = None
    if "\n" in command:
        try:
            relined: list[str] = []
            for ln in logical_lines(command):
                try:
                    lt = tokenize(ln)
                except Exception:
                    continue  # unparseable line: skip it, never block on it
                if lt:
                    relined.extend(lt)
                    relined.append(";")
            if relined:
                toks = relined
        except Exception:
            toks = None  # fall back to whole-command tokenization
    if toks is None:
        toks = tokenize(command)
    return toks


def program_index(seg: list[str]) -> int:
    """Index of the real program token, skipping `FOO=bar` prefixes, shell
    reserved words (`then`, `do`, `{`, ... — which take no options), and command
    wrappers together with the wrapper's OWN options, their separate values, a
    `--` end-of-options, and timeout's bare DURATION positional. Skipping too
    little only loses a block (the value reads as the program -> not git ->
    allow); it can never turn a legitimate command into a block."""
    i = 0
    while i < len(seg):
        core = seg[i].lstrip("$`(")
        if core == "":
            i += 1
            continue
        if ENV_ASSIGN_RE.match(core):  # FOO=bar prefix
            i += 1
            continue
        if core in KEYWORDS:  # if / then / do / else / { ... (no options of their own)
            i += 1
            continue
        if core in WRAPPERS:  # sudo / env / command / timeout / xargs / ...
            value_opts = WRAPPER_VALUE_OPTS.get(core, frozenset())
            i += 1
            while i < len(seg) and seg[i].startswith("-") and seg[i] != "--":
                takes_value = seg[i] in value_opts
                i += 1
                if takes_value and i < len(seg):
                    i += 1  # its separate value, e.g. `nice -n 5`
            if i < len(seg) and seg[i] == "--":
                i += 1
            if core in DURATION_WRAPPERS and i < len(seg) and DURATION_RE.match(seg[i]):
                i += 1  # `timeout 30 git ...`
            continue
        break
    return i


def base_name(tok: str) -> str:
    return tok.lstrip("$`(").rstrip(")`").rsplit("/", 1)[-1]


def git_args_of(seg: list[str]) -> list[str] | None:
    """If this simple command invokes git, return the args after `git`; else None."""
    i = program_index(seg)
    if i >= len(seg):
        return None
    if base_name(seg[i]) != "git":
        return None
    return seg[i + 1:]


def shell_payload(seg: list[str]) -> str | None:
    """If this simple command is `bash -c '<payload>'` (sh/zsh/dash/ksh; `-lc`,
    `-euc` clusters too), return the payload — it is one shlex token here, and
    without re-tokenizing it `bash -c 'git stash'` walks straight past the guard.
    Only tokens BEFORE the first non-option are considered, so the argument of
    `bash script.sh --msg 'git stash'` is never re-scanned."""
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


def find_subcommand(args: list[str]) -> str | None:
    """First non-option token after skipping git's global options."""
    j = 0
    while j < len(args):
        a = args[j]
        if a.startswith("-"):
            if "=" in a:
                j += 1
            elif a in GIT_VALUE_GLOBALS:
                j += 2
            else:
                j += 1
            continue
        return a
    return None


def first_operand(args: list[str]) -> str | None:
    for a in args:
        if not a.startswith("-"):
            return a
    return None


def short_cluster_has(args: list[str], letters: str) -> bool:
    """True if any single-dash (non `--`) token contains one of `letters` after the dash."""
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if any(c in a[1:] for c in letters):
                return True
    return False


def short_flag_has(args: list[str], letters: str, value_taking: str = "") -> bool:
    """Like short_cluster_has, but scans a cluster as FLAGS and stops at the first
    `value_taking` letter, because git lets a value attach straight to it:
    `-bfix/thing` is branch `fix/thing`, not the flag cluster b,f,i,x. Without
    that stop, any branch name containing the letter reads as a force flag."""
    for a in args:
        if not a.startswith("-") or a.startswith("--"):
            continue
        for ch in a[1:]:
            if ch in letters:
                return True
            if ch in value_taking:
                break  # rest of the token is this option's value
    return False


def long_opt_abbrev(args: list[str], *full_names: str) -> bool:
    """True if a token is a prefix git would expand to one of `full_names`.
    Git accepts any unambiguous abbreviation, so `git reset --ha` really does
    run `--hard`; exact-match checks leak every shortened spelling. A longer
    option that merely starts with the same text (`--force-with-lease`) is not
    a prefix of `--force` and so does not match."""
    for a in args:
        if not a.startswith("--") or len(a) <= 2:
            continue
        if any(full.startswith(a) for full in full_names):
            return True
    return False


GIT_QUERY_TIMEOUT = 2.0
_ALIAS_CACHE: dict[str, dict[str, str]] = {}


def _git_run(args: list[str], timeout: float | None = None):
    """Run a READ-ONLY git query. Returns the CompletedProcess, or None if git
    could not be run at all (missing binary, timeout, ...) -> caller fails open."""
    try:
        return subprocess.run(
            ["git"] + args, capture_output=True, text=True,
            timeout=GIT_QUERY_TIMEOUT if timeout is None else timeout,
        )
    except Exception:
        return None


# --- WHERE the command will actually run -----------------------------------
#
# Every question this guard asks git ("is this operand a ref?", "what is the
# repo root?") and every relative path it resolves has an implicit answer-from
# location. `_main` chdir's to the payload's declared `cwd`, and that was the
# whole of the previous fix. But a process cwd is ONE location and a command
# names others out loud, in argv: `git -C <worktree> checkout f` and
# `cd <worktree> && git checkout f` both say exactly where they mean, and both
# returned exit 0 for a file that exists only in that worktree.
#
# `_CWD` is that location. It starts at the payload's cwd and is re-pointed for
# the extent of a `git -C` segment, or from a `cd` onward. Re-pointing is a
# variable, never a real `os.chdir`: the hook is one process evaluating many
# segments, and a chdir would leak into all of them.
try:
    _CWD = os.getcwd()
except Exception:                      # cwd deleted out from under the process
    _CWD = os.sep


def _init_cwd() -> None:
    global _CWD
    try:
        _CWD = os.getcwd()
    except Exception:
        pass


def _set_cwd(d: str | None) -> str:
    """Point resolution at `d`, resolved relative to the current `_CWD` (so
    `git -C ../x` and `cd ..` compose). A directory that is not usable changes
    NOTHING — an unresolvable `cd` must never become a block of its own.
    Returns the previous value so the caller can restore it."""
    global _CWD
    prev = _CWD
    if d:
        try:
            cand = os.path.realpath(os.path.join(_CWD, os.path.expanduser(d)))
            if os.path.isdir(cand):
                _CWD = cand
        except Exception:
            pass
    return prev


def _restore_cwd(prev: str) -> None:
    global _CWD
    _CWD = prev


def _git_at(d: str | None, args: list[str]):
    """A read-only git query asked FROM directory `d`, via `git -C`.

    Retried once with a longer timeout when the first attempt could not run at
    all. That retry is load-bearing: "git could not answer" is now a
    fail-CLOSED signal for recursive deletes, and a flaky 2-second timer must
    not be allowed to read as one."""
    pre = ["-C", d] if d else []
    p = _git_run(pre + args)
    if p is None:
        p = _git_run(pre + args, timeout=GIT_QUERY_TIMEOUT * 2)
    return p


def git_aliases() -> dict[str, str]:
    """All `alias.*` entries visible from the cwd (repo + global + system).

    Loaded once per process, lazily: an alias is only consulted for a subcommand
    the guard does not already recognize. `co = checkout` in someone's dotfiles
    is boilerplate, and without this `git co .` walks straight past the guard."""
    hit = _ALIAS_CACHE.get(_CWD)
    if hit is None:
        out: dict[str, str] = {}
        p = _git_at(_CWD, ["config", "--get-regexp", "^alias\\."])
        if p is not None and p.returncode == 0:
            for line in p.stdout.splitlines():
                key, _, val = line.partition(" ")
                if key.startswith("alias.") and val:
                    out.setdefault(key[len("alias."):], val)
        _ALIAS_CACHE[_CWD] = out
        hit = out
    return hit


def inline_aliases(args: list[str]) -> dict[str, str]:
    """Aliases defined on the command line itself: `git -c alias.wipe='!git stash' wipe`.
    They never reach `git config`, so they must be read out of argv."""
    out: dict[str, str] = {}
    for k, tok in enumerate(args):
        val = None
        if tok == "-c" and k + 1 < len(args):
            val = args[k + 1]
        elif tok.startswith("-c") and len(tok) > 2 and "=" in tok:
            val = tok[2:]
        if val and val.startswith("alias.") and "=" in val:
            name, _, definition = val.partition("=")
            out.setdefault(name[len("alias."):], definition)
    return out


def is_commit_ish(name: str) -> bool | None:
    """True if git resolves `name` to a commit, False if it definitively does not,
    None if the question could not be answered (no git, not a repo, ...).
    None means fail open: an unanswerable question must never produce a block."""
    p = _git_at(_CWD, ["rev-parse", "--verify", "--quiet", name + "^{commit}"])
    if p is None:
        return None
    if p.returncode == 0:
        return True
    if p.returncode == 1:  # --quiet: "not a valid object name"
        return False
    return None  # 128 = not a repository / other failure -> unknown


def certainly_pathspec(op: str) -> bool:
    """Shapes a git refname can NEVER take, so no git query is needed: pathspec
    magic (`:/` = the whole repo, `:(top)`), a trailing `/`, `.`/`..`, or a glob."""
    return (
        op.startswith(":")
        or op.endswith("/")
        or op in (".", "..")
        or GLOB_RE.search(op) is not None
    )


def check_stash(rest: list[str]) -> str | None:
    sc = first_operand(rest)
    if sc in ("list", "show", "create"):
        return None  # read-only / non-destructive
    label = "git stash " + sc if sc else "git stash"
    return (
        "`" + label + "` pushes/pops the SHARED refs/stash and rewrites the "
        "working tree for the whole repo."
    )


def check_reset(rest: list[str]) -> str | None:
    # --merge and --keep also rewrite the working tree (they only refuse SOME
    # conflicting local edits); no loop workflow needs either. Matched by git's
    # abbreviation rule so `--ha`/`--kee`/`--mer` cannot walk past this.
    for mode, why in (
        ("--hard", "discards all uncommitted working-tree changes"),
        ("--merge", "rewrites working-tree files to match the target commit"),
        ("--keep", "rewrites working-tree files to match the target commit"),
    ):
        if long_opt_abbrev(rest, mode):
            return "`git reset " + mode + "` " + why + "."
    return None  # soft/mixed reset leaves the working tree intact


PATHSPEC_REASON = (
    "`git checkout <pathspec>` overwrites those working-tree files from the "
    "index/a commit — a single non-ref operand is a PATH, not a branch."
)


def _lexists(op: str) -> bool:
    """Does this operand name something on disk WHERE THE COMMAND WILL RUN?
    Asked against `_CWD`, not this process's cwd: `git -C <worktree> checkout f`
    and `cd <worktree> && git checkout f` both returned exit 0 for a file that
    exists only in that worktree, because the question was asked from the
    session root where the file does not exist."""
    try:
        p = os.path.expanduser(op)
        if not os.path.isabs(p):
            p = os.path.join(_CWD, p)
        return os.path.lexists(p)
    except Exception:
        return False


def check_checkout(rest: list[str]) -> str | None:
    if "--" in rest:
        return "`git checkout ... -- <path>` overwrites working-tree files from another commit."
    # --pathspec-from-file / --pathspec-file-nul put checkout in path mode exactly
    # like `--`, with the paths read from a file instead of argv.
    if any(a.startswith("--pathspec") for a in rest):
        return "`git checkout --pathspec-from-file` overwrites the listed working-tree files."
    # A bare integer is an fd left behind by `2>/dev/null`: the lexer splits the
    # redirection off but does not absorb the fd number, and counting it as an
    # operand blocks an ordinary `git checkout main 2>/dev/null`.
    operands = [a for a in rest if not a.startswith("-") and not a.isdigit()]
    if "." in operands:
        return "`git checkout .` discards ALL uncommitted working-tree changes."
    # -f/--force must be checked BEFORE the branch-create early-return below:
    # `git checkout -f <branch>` throws away uncommitted work exactly like the
    # ops above, and `-f` is not a branch flag so it would otherwise fall
    # through as a plain 1-operand branch switch. Mirrors check_switch.
    # -b/-B take an attached value, so stop the cluster scan there.
    if long_opt_abbrev(rest, "--force") or short_flag_has(rest, "f", "bB"):
        return "`git checkout -f/--force` throws away uncommitted working-tree changes."
    # -b/-B arrive CLUSTERED with other short flags all the time (`git checkout
    # -qb task/T-010 main`), so they are matched with the same cluster scan `-f`
    # uses above, not by exact token match: an exact match misses `-qb`, and the
    # command then falls through to the two-operand `<tree-ish> <path>` veto
    # below — a FALSE BLOCK on the branch create the swarm dispatches constantly.
    # -b/-B take an attached value, so the scan stops there (`-bfix` = branch fix).
    # -B is NOT -b: it force-RESETS an existing branch to the start point,
    # exactly as `git branch -f` does. It has to be tested before the
    # branch-create early-return below, which used to swallow it.
    if short_flag_has(rest, "B", "bB"):
        return ("`git checkout -B <branch>` force-resets an existing branch to the "
                "start point (`-b` creates without clobbering, and is allowed).\n"
                + 'If the branch really must move, delete and re-create it explicitly (`git branch -D <b>` is allowed) so the move is a deliberate two-step, not a flag.')
    branch_flags = {"-b", "-B", "--orphan", "--detach", "-t", "--track"}
    if short_flag_has(rest, "bB", "bB") or any(
        a in branch_flags or a.startswith("--track") for a in rest
    ):
        return None  # branch create / switch
    if len(operands) >= 2:
        return "`git checkout <tree-ish> <path>` overwrites working-tree files from another commit."
    if len(operands) == 1:
        # ONE operand is NOT automatically a branch switch: git resolves a
        # non-ref argument as a PATHSPEC and overwrites those files
        # (`git checkout a.txt`, `git checkout src/`, `git checkout :/` == the
        # whole repo). Ask git whether it is a ref, and block only when the
        # answer is a definite no. Unanswerable (no git / not a repo) -> ALLOW.
        op = operands[0]
        if certainly_pathspec(op):
            return PATHSPEC_REASON
        if is_commit_ish(op) is False and _lexists(op):
            # not a ref, and it names something on disk -> a real pathspec.
            # (not a ref and not on disk -> git itself errors out; harmless.)
            return PATHSPEC_REASON
    return None  # 0 operands, or an operand git resolves as a commit-ish


def check_switch(rest: list[str]) -> str | None:
    # -c/-C take an attached value, so `git switch -cfeat` is a branch name.
    if long_opt_abbrev(rest, "--discard-changes", "--force") or short_flag_has(rest, "f", "cC"):
        return "`git switch --discard-changes/-f` throws away uncommitted working-tree changes."
    # -C is to -c what checkout's -B is to -b: it force-resets an existing branch.
    if short_flag_has(rest, "C", "cC"):
        return ("`git switch -C <branch>` force-resets an existing branch to the "
                "start point (`-c` creates without clobbering, and is allowed).\n"
                + 'If the branch really must move, delete and re-create it explicitly (`git branch -D <b>` is allowed) so the move is a deliberate two-step, not a flag.')
    return None


def check_restore(rest: list[str]) -> str | None:
    # abbreviations both ways: `git restore --stag <path>` really is --staged
    # (index-only, explicitly an allowed op) and must not be blocked.
    staged = long_opt_abbrev(rest, "--staged") or short_cluster_has(rest, "S")
    worktree = long_opt_abbrev(rest, "--worktree") or short_cluster_has(rest, "W")
    if worktree or not staged:  # default (and explicit -W) rewrites the working tree
        return "`git restore <path>` overwrites working-tree files from the index/a commit."
    return None  # --staged only -> unstages, working tree untouched


def check_clean(rest: list[str]) -> str | None:
    # abbreviations on BOTH sides: `--f`/`--for` really is --force (a missed
    # block), and `--dry` really is --dry-run (a false block on a legit dry run).
    dry = long_opt_abbrev(rest, "--dry-run") or short_cluster_has(rest, "n")
    danger = long_opt_abbrev(rest, "--force") or short_cluster_has(rest, "fdxX")
    if danger and not dry:
        return "`git clean -f/-d/-x` permanently deletes untracked files/directories."
    return None


def check_worktree(rest: list[str]) -> str | None:
    sc = first_operand(rest)
    if sc == "remove":
        # This used to be an unconditional block, and it was the guard's single
        # worst rule: `git worktree remove` is the ONE teardown that validates the
        # target really is a worktree and refuses one with uncommitted changes.
        # Blocking it pushed everybody onto `rm -rf <path>` — which the guard did
        # not cover at all — and the SAFE_HINT said so in as many words. So: allow
        # the checked form, block only the check-defeating `--force`.
        if long_opt_abbrev(rest, "--force") or short_flag_has(rest, "f"):
            return (
                "`git worktree remove --force` removes the worktree even with "
                "uncommitted changes in it — that work exists nowhere else. Drop "
                "`--force`: without it the command refuses exactly the case you "
                "would regret, and it is allowed."
            )
        return None
    if sc == "move":
        return (
            "`git worktree move` relocates a worktree another agent may be working "
            "in (and drifts the run's recorded worktree state)."
        )
    return None


# --- working-tree-destroying PLUMBING -------------------------------------
# Blocking the porcelain while leaving the plumbing open blocks the spelling,
# not the operation; an agent that hits the block message once can find these.

def check_checkout_index(rest: list[str]) -> str | None:
    if long_opt_abbrev(rest, "--force", "--all") or short_flag_has(rest, "fa"):
        return (
            "`git checkout-index -f/-a` overwrites working-tree files straight "
            "from the index (the plumbing under `git checkout -- .`)."
        )
    return None


def check_read_tree(rest: list[str]) -> str | None:
    if long_opt_abbrev(rest, "--update") or short_cluster_has(rest, "u"):
        return (
            "`git read-tree -u` rewrites working-tree files to match a tree — "
            "this is literally what `git reset --hard` does underneath."
        )
    return None


def rm_repo_wide(op: str) -> bool:
    """True only for a `git rm` pathspec that reaches the WHOLE repo.

    `git rm` cannot touch a file it does not name, so a glob that still carries a
    literal path prefix (`tests/legacy_*.py`) is scoped and must be ALLOWED — the
    allow-list above says so in as many words. Treating every glob metacharacter
    as repo-global also false-blocks a literal filename that merely contains one
    (`src/data[1].json`). Blocked shapes are the ones anchored at the repo top:
    pathspec magic (`:/`, `:(top)`), `.`/`./`/`..`, a first path segment that is
    nothing but wildcards (`*`, `**`, `**/*.py`), and an unanchored top-level
    glob (`*.py`, which git's wildmatch spreads across every directory)."""
    if op.startswith(":"):  # :/ , :/* , :(top) , :!... — pathspec magic
        return True
    s = op.rstrip("/")  # "./" -> "." , "*/" -> "*"
    if s in ("", ".", ".."):
        return True
    head = s.split("/", 1)[0]  # first path segment
    if head and all(c in "*?" for c in head):  # `*`, `**`, `*/x`, `**/*.py`
        return True
    if "/" not in s and s[0] in "*?[":  # `*.py` — no directory component at all
        return True
    return False  # a literal path, or a glob scoped by a literal prefix


def check_rm(rest: list[str]) -> str | None:
    # Scoped `git rm <file>` is a legitimate worker edit and stays allowed; only
    # the repo-WIDE shapes are blocked, which is this guard's actual charter.
    if long_opt_abbrev(rest, "--cached"):
        return None  # index-only, working tree untouched
    for op in rest:
        if op.startswith("-") and op != "-":
            continue
        if rm_repo_wide(op):
            return "`git rm` over a repo-wide pathspec deletes tracked files for every agent."
    return None


def check_apply(rest: list[str]) -> str | None:
    if long_opt_abbrev(rest, "--reverse") or short_flag_has(rest, "R"):
        return "`git apply -R/--reverse` un-applies a patch against the shared working tree."
    return None


# --- destructive verbs that had no checker at all -------------------------
# Every one of these was measured ALLOWED. None of them appears anywhere in
# SKILL.md, references/ or swarmloop.py (grepped), so a checker here costs the
# loop nothing. Two of them close direct contradictions with the loop's own
# documentation: `task-packets.md` tells a worker a clobbered stash is
# "recoverable only from a dangling stash commit BEFORE `git gc`", which made the
# guard's whole residual-recovery story one unblocked `git gc --prune=now` away.
#
# Deliberately NOT added, though both appear on the same "no checker" list:
#   - `git branch -D` — the sanctioned teardown chain ends with it, and it is on
#     the verified false-positive-safe list. The merge-protocol's "keep rejected
#     branches" rule is a review rule, not a blast-radius one; enforcing it here
#     would false-block the loop's own cleanup.
#   - `git rebase -i` — the docstring allows `git rebase`, rebase refuses to run
#     with a dirty tree, and it touches only the current branch.
# A false block on a loop-mandated command is worse than either miss.

def check_push(rest: list[str]) -> str | None:
    # `--force-with-lease` is NOT an abbreviation of `--force` (it is longer), so
    # long_opt_abbrev leaves the loop's allowed lease-push alone. `-o` takes an
    # attached value, so the cluster scan stops there.
    if long_opt_abbrev(rest, "--force") or short_flag_has(rest, "f", "o"):
        return ("`git push --force` overwrites the remote branch, discarding "
                "commits other agents may have already pushed.")
    # Same effects, different spellings — all of these were exit 0 while
    # `--force` above was exit 2. `--mirror` makes the remote match this repo
    # EXACTLY, deleting every remote ref this repo does not have.
    if long_opt_abbrev(rest, "--mirror"):
        return ("`git push --mirror` makes the remote match this repo exactly — "
                "every remote ref this repo does not have is DELETED.")
    if long_opt_abbrev(rest, "--delete") or short_flag_has(rest, "d", "o"):
        return "`git push --delete` deletes the named ref on the remote."
    for a in rest:
        if a.startswith("-") or ":" not in a:
            continue
        s, _, d = a.partition(":")
        if not s.strip():
            return ("`git push <remote> :" + d + "` is the empty-source refspec: "
                    "it DELETES `" + d + "` on the remote.")
        if s.startswith("+"):
            return ("`git push <remote> +" + s.lstrip("+") + ":" + d + "` is a "
                    "forced refspec — the `+` is `--force` for that ref, which is "
                    "blocked above under its other spelling.")
    return None


def check_branch(rest: list[str]) -> str | None:
    """`git branch -f <b> <commit>` MOVES an existing branch ref to another
    commit — the plumbing under it is `git update-ref`, which this guard already
    blocks. Deleting a branch stays allowed: `git branch -D task/<id>` is the
    documented teardown step and `-D` is `--delete --force`, so the force test
    has to run after the delete test or it would false-block the runbook.
    -m/-c/-u/-t take values that can attach, so the cluster scan stops there."""
    if long_opt_abbrev(rest, "--delete") or short_flag_has(rest, "dD", "umcMC"):
        return None
    if long_opt_abbrev(rest, "--force") or short_flag_has(rest, "f", "umcMC"):
        return ("`git branch -f/--force` moves an existing branch to another "
                "commit, orphaning everything reachable only from where it was — "
                "the same effect as the `git update-ref` this guard blocks.\n"
                + 'If the branch really must move, delete and re-create it explicitly (`git branch -D <b>` is allowed) so the move is a deliberate two-step, not a flag.')
    return None


def check_fetch(rest: list[str]) -> str | None:
    """A `+` refspec is `--force` for one ref. Fetching into `refs/remotes/…`
    (or a tag) that way is the ORDINARY default refspec and must stay allowed;
    fetching into a LOCAL branch overwrites it with no merge and no reflog
    warning, which is `git reset --hard` for the ref."""
    for a in rest:
        if a.startswith("-") or ":" not in a:
            continue
        s, _, d = a.partition(":")
        if not s.startswith("+"):
            continue
        d = d.strip()
        if not d or d.startswith("refs/remotes/") or d.startswith("refs/tags/"):
            continue
        return ("`git fetch <remote> +<src>:" + d + "` force-updates the LOCAL ref "
                "`" + d + "`, discarding whatever it pointed at.\n" + 'If the branch really must move, delete and re-create it explicitly (`git branch -D <b>` is allowed) so the move is a deliberate two-step, not a flag.')
    return None


def check_update_ref(rest: list[str]) -> str | None:
    # Every form of update-ref mutates a ref, including the shared refs/stash;
    # nothing in the loop uses this plumbing at all.
    return ("`git update-ref` rewrites a ref directly, bypassing every branch "
            "protection the run relies on.")


def check_reflog(rest: list[str]) -> str | None:
    sc = first_operand(rest)
    if sc in ("expire", "delete"):
        return ("`git reflog " + sc + "` destroys the reflog — the last recovery "
                "route for a clobbered branch tip or a dropped stash.")
    return None  # `git reflog` / `git reflog show` are read-only


def check_gc(rest: list[str]) -> str | None:
    # Bare `git gc` keeps unreachable objects for two weeks, which is what makes
    # "recoverable from a dangling stash commit" true. `--prune=now` is what makes
    # it false.
    for a in rest:
        if a.startswith("--prune=") and a[len("--prune="):] not in ("never",):
            return ("`git gc --prune=" + a[len("--prune="):] + "` immediately deletes "
                    "unreachable objects — including the dangling stash commit that "
                    "is the documented recovery route.")
    return None


def check_prune(rest: list[str]) -> str | None:
    return ("`git prune` deletes unreachable objects — the dangling commits that "
            "are the only residue of a clobbered stash or a deleted branch.")


def check_repack(rest: list[str]) -> str | None:
    # `-d` alone drops redundant packs (harmless). `-a`/`-A` WITH `-d` is the
    # combination that discards unreachable objects.
    if short_cluster_has(rest, "aA") and short_cluster_has(rest, "d"):
        return "`git repack -ad` discards unreachable objects repo-wide."
    return None


def check_filter_branch(rest: list[str]) -> str | None:
    return ("`git filter-branch` rewrites history repo-wide; every other agent's "
            "branch point stops existing.")


def check_symbolic_ref(rest: list[str]) -> str | None:
    ops = [a for a in rest if not a.startswith("-")]
    if long_opt_abbrev(rest, "--delete") or short_flag_has(rest, "d") or len(ops) >= 2:
        return ("`git symbolic-ref <ref> <target>` repoints HEAD without touching the "
                "working tree — every subsequent commit lands on the wrong branch.")
    return None  # one operand = the read form


def check_submodule(rest: list[str]) -> str | None:
    if first_operand(rest) == "deinit" and (
            long_opt_abbrev(rest, "--force") or short_flag_has(rest, "f")):
        return ("`git submodule deinit -f` discards uncommitted work inside the "
                "submodule checkout (without `-f` it refuses, which is fine).")
    return None


def check_sparse_checkout(rest: list[str]) -> str | None:
    sc = first_operand(rest)
    if sc in ("set", "add", "init", "reapply", "disable"):
        return ("`git sparse-checkout " + sc + "` adds or REMOVES files across the "
                "whole working tree — a repo-global rewrite wearing a config hat.")
    return None  # `git sparse-checkout list` is read-only


# --- non-git destruction: rules keyed on PATHS, not on git verbs -----------
# The guard used to be keyed on git subcommands only, with no path awareness
# anywhere — and its own SAFE_HINT prescribed `rm -rf <worktree-path>`, a command
# it did not cover AT ALL. Measured ALLOW before this section existed:
#   rm -rf ../<repo>-worktrees/$ID && git worktree prune     (with $ID unset:
#       every live worktree, uncommitted work included, no prompt — `Bash(rm:*)`
#       is allowlisted for the run)
#   rm -rf /$ID  ·  rm -rf <repo root>  ·  rm -rf .swarm-loop
#   > .swarm-loop/state.json  ·  truncate -s 0 .swarm-loop/state.json
#   sed -i '' 's/x/y/' .swarm-loop/goals.json  ·  mv .swarm-loop /tmp/

SWARM_DIR = ".swarm-loop"

# The run's LEDGER: bookkeeping the harness writes from python (`swarmloop.py`),
# never from a shell. A truncating redirect into any of these voids the run's
# scores with no detector left to notice.
#
# `acceptance/` is deliberately NOT here. Tampering with a frozen test is already
# tamper-EVIDENT — `verify` re-hashes it against the manifest — and the only
# amendment path that was observed to work in a live run was the `Edit` tool
# writing into that directory under a user-approved `freeze --amend`. Blocking
# writes there would break the sanctioned repair while adding nothing the hash
# manifest does not already catch. Deleting the directory outright IS blocked,
# below, because that destroys the detector rather than tripping it.
LEDGER_NAMES = frozenset({
    "state.json", "goals.json", "manifest.json", "history.csv",
    "freeze-log.jsonl", "escalations.jsonl", "ESCALATIONS.md",
})

FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

# The PreToolUse matcher this hook requires, verbatim — `Bash` plus every member
# of FILE_TOOLS above. The two must agree or the rules are silently disarmed for
# whichever tool is missing: `MultiEdit` was armed in FILE_TOOLS and absent from
# the shipped settings template, so a MultiEdit write to `.swarm-loop/state.json`
# never reached this process and the rule that would have blocked it never ran.
# Deliberately NOT asserted: a failed import-time assert exits 1, which is not
# the fail-open contract. Kept as one string so a reader can copy it.
REQUIRED_MATCHER = "Bash|Edit|Write|NotebookEdit|MultiEdit"

def abs_path(op: str) -> str | None:
    """Absolute, symlink-resolved form of an operand, relative to `_CWD` — the
    directory the command will actually run in, not this process's. Purely
    lexical about existence: the target of an `rm` is exactly the thing that may
    not survive, and a rule that only fires on paths that exist right now is not
    a rule."""
    try:
        p = os.path.expanduser(op)
        if not os.path.isabs(p):
            p = os.path.join(_CWD, p)
        return os.path.realpath(p)
    except Exception:
        return None


# --- git context, resolved FROM THE OPERAND -------------------------------
#
# THE BUG THIS REPLACES. `repo_root()` shelled `git rev-parse --show-toplevel`
# and cached **None** on failure; `worktree_roots()` shelled `git worktree list
# --porcelain` and cached **[]**. Both asked git from the PROCESS cwd, and every
# path rule then fell through on the falsy value — `if root and ...` skipped,
# `for wt in worktree_roots()` never entered, an empty protected set read as
# "nothing is protected". Measured, one identical command string:
#
#     rc=2  cwd=<repo>              rm -rf <repo>
#     rc=0  cwd=<worktrees parent>  rm -rf <repo>      *** same string ***
#     rc=0  cwd=/tmp                rm -rf <repo>
#     rc=2  cwd=<repo>              rm -rf <worktrees parent>/T-010
#     rc=0  cwd=<worktrees parent>  rm -rf <worktrees parent>/T-010
#
# The run's worktree PARENT (`../<repo>-worktrees`) is exactly such a directory:
# `git rev-parse` there says "not a git repository", the run GRANTS it with
# `--add-dir`, and it is the natural place to stand for a teardown loop.
#
# The design intent was already right — "ask git what is live" beats hard-coding
# a path convention. Git was just asked from the wrong place. An operand NAMES
# the thing it is about, so that is where it is asked now.
_CTX_CACHE: dict[str, tuple] = {}

MAX_ANCESTOR_WALK = 64
MAX_SCAN_ENTRIES = 64
MAX_CHILD_PROBES = 4
MAX_CHILD_DEPTH = 2


def _ctx_at(d: str | None) -> tuple:
    """(repo root or None, [worktree roots], determined) as git sees it FROM `d`.

    `determined` is False ONLY when git could not be run or could not answer:
    no binary, a timeout that survived the retry, an unexpected failure. A clean
    "not a git repository" is git ANSWERING, so it is determined-and-empty.
    That distinction is the whole of the fail-closed rule below — today both
    outcomes were the same empty list, which is why "nothing is protected" and
    "I could not find out" were indistinguishable."""
    if not d:
        return (None, [], False)
    hit = _CTX_CACHE.get(d)
    if hit is not None:
        return hit
    root, wts, determined = None, [], True
    p = _git_at(d, ["rev-parse", "--show-toplevel"])
    if p is None:
        determined = False
    elif p.returncode == 0:
        out = p.stdout.strip()
        if out:
            try:
                root = os.path.realpath(out)
            except Exception:
                determined = False
        else:
            determined = False         # exit 0 with no answer is not an answer
    # a non-zero exit is git saying "no repository here" -> determined, empty
    if root is not None:
        q = _git_at(d, ["worktree", "list", "--porcelain"])
        if q is None or q.returncode != 0:
            determined = False
        else:
            for line in q.stdout.splitlines():
                if line.startswith("worktree "):
                    try:
                        wts.append(os.path.realpath(line[len("worktree "):].strip()))
                    except Exception:
                        determined = False
    _CTX_CACHE[d] = (root, wts, determined)
    return _CTX_CACHE[d]


def _nearest_existing_dir(ap: str | None) -> str | None:
    """Deepest directory that EXISTS at or above `ap`.

    `git -C` needs a real directory and an `rm` operand very often is not one
    (`rm -rf dist`, `rm -rf /tmp/scratch-$USER`). Asking from its nearest real
    ancestor is what turns those into a determined "nothing protected here"
    rather than an unanswered question — which, under the rule below, is the
    difference between allowed and blocked."""
    if not ap:
        return None
    d = ap
    for _ in range(MAX_ANCESTOR_WALK):
        try:
            if os.path.isdir(d):
                return d
        except Exception:
            return None
        nd = os.path.dirname(d)
        if not nd or nd == d:
            return None
        d = nd
    return None


def _linked_worktrees_under(d: str | None) -> list[str]:
    """Directories at or under `d` (<= MAX_CHILD_DEPTH levels) that are LINKED
    worktrees — their `.git` is a FILE (`gitdir: ...`), not a directory.

    This is the only way to see `rm -rf ../<repo>-worktrees`: that operand is
    not IN a repository, is not UNDER one, and has none above it. The worktrees
    are BELOW it. One hit is usually enough — `git worktree list` from any
    member returns the whole set, siblings and main tree included.

    Deliberately restricted to the LINKED shape. A plain clone's `.git` is a
    DIRECTORY, so a vendored checkout under `node_modules` is not probed and
    `rm -rf node_modules` stays allowed; that false block would be far worse
    than this miss. Bounded on every axis — entries scanned, probes issued,
    depth — because this runs on `rm`."""
    if not d:
        return []
    found: list[str] = []
    frontier = [(d, 0)]
    scanned = 0
    while frontier and len(found) < MAX_CHILD_PROBES:
        cur, depth = frontier.pop(0)
        if depth > MAX_CHILD_DEPTH or scanned >= MAX_SCAN_ENTRIES * 4:
            continue
        entries = []
        try:
            with os.scandir(cur) as it:
                for e in it:
                    scanned += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            entries.append(e.path)
                    except Exception:
                        pass
                    if len(entries) >= MAX_SCAN_ENTRIES or scanned >= MAX_SCAN_ENTRIES * 4:
                        break
        except Exception:
            continue
        for child in entries:
            try:
                if os.path.isfile(os.path.join(child, ".git")):
                    found.append(child)
                    if len(found) >= MAX_CHILD_PROBES:
                        break
                    continue
            except Exception:
                pass
            frontier.append((child, depth + 1))
    return found


def _resolve(ap: str | None, scan_below: bool = False) -> tuple:
    """(repo roots, worktree roots, determined) relevant to operand `ap`.

    Three probe points, deliberately a UNION rather than a replacement:
      - `_CWD`, so no verdict that already worked can be lost;
      - the operand's own nearest existing directory — the fix;
      - for a recursive delete, the linked worktrees BELOW it.
    """
    roots: list[str] = []
    wts: list[str] = []
    determined = True
    base = _nearest_existing_dir(ap)
    probes = [_CWD, base]
    if scan_below:
        probes.extend(_linked_worktrees_under(base))
    seen = set()
    for d in probes:
        if not d or d in seen:
            continue
        seen.add(d)
        r, w, det = _ctx_at(d)
        if not det:
            determined = False
        if r and r not in roots:
            roots.append(r)
        for x in w:
            if x not in wts:
                wts.append(x)
    return roots, wts, determined


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# `$VAR`, `${VAR…}`, and the special one-character parameters. NOT `$(…)`, which
# is a command whose output nothing here can predict.
VAR_REF_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[@*#?$!0-9-]")


def collapse_unexpanded(op: str) -> str:
    """What this path becomes if every unexpanded `$…` expands to nothing.

    The reproduced disaster, not a hypothetical: `rm -rf <worktree-root>/$ID`
    with `$ID` unset is `rm -rf <worktree-root>/`, which takes every live
    worktree and all their uncommitted work.

    The SEPARATORS are kept, and that is the whole subtlety — `"$A/$B"` really
    does become `/`, while a lone `"$V"` becomes the empty string, which bash
    passes to `rm` verbatim and `rm` refuses. Emptying the components instead of
    deleting them is what tells those two apart; an earlier draft dropped the
    components and turned `rm -rf "$(mktemp -d)"` and `rm -rf "$VENV"` into false
    blocks on the repo root. And only the VARIABLE REFERENCE is emptied, not the
    whole component: `scratch-$USER` expands to `scratch-`, so blanking the
    component turned `rm -rf /tmp/scratch-$USER` into `rm -rf /tmp/` and blocked
    it. A `$(…)` that this cannot evaluate blanks its component, which is the
    conservative reading."""
    out = []
    for c in op.split("/"):
        if "$" in c:
            c = VAR_REF_RE.sub("", c)
            if "$" in c:  # `$(...)` and friends — unknowable, so drop it whole
                c = ""
        out.append(c)
    return "/".join(out)


def swarm_dirs(ap: str | None) -> list[str]:
    """`<repo-root>/.swarm-loop` for every repo root in scope for this operand.
    Plural because the operand and the cwd can name different repositories —
    `> <repo>/.swarm-loop/state.json` issued from the worktrees parent is the
    case that used to escape entirely, since the cwd had no repo root at all.

    Worktree roots are included as well as repo roots, and that is not padding:
    inside a worker's worktree `rev-parse --show-toplevel` answers with the
    WORKTREE, so the main tree — where the run's real `.swarm-loop` lives —
    only ever shows up in the worktree list."""
    roots, wts, _det = _resolve(ap, scan_below=True)
    seen, out = set(), []
    for r in roots + wts:
        sd = os.path.join(r, SWARM_DIR)
        if sd not in seen:
            seen.add(sd)
            out.append(sd)
    return out


def in_swarm_tree(op: str) -> bool:
    """Is this operand inside (or exactly) some `<repo-root>/.swarm-loop`?
    The `SWARM_DIR in op` prefilter keeps the git queries off the hot path: no
    ordinary command mentions the directory at all."""
    if SWARM_DIR not in op:
        return False
    ap = abs_path(op)
    if not ap:
        return False
    return any(_under(ap, sd) for sd in swarm_dirs(ap))


def is_ledger(op: str) -> bool:
    if SWARM_DIR not in op:
        return False
    ap = abs_path(op)
    if not ap:
        return False
    for sd in swarm_dirs(ap):
        if _under(ap, sd) and ap != sd:
            if os.path.relpath(ap, sd) in LEDGER_NAMES:
                return True
    return False


def critical_dir_reason(op: str) -> str | None:
    """Why a RECURSIVE delete of this path is repo-global destruction.

    Covers the repo/worktree root itself, any ancestor of one (which is how
    `rm -rf <worktree-parent>` takes them all), and the run-state directory."""
    if in_swarm_tree(op):
        return ("`" + SWARM_DIR + "` holds the run's frozen goals, acceptance suite, "
                "manifest and history — deleting it destroys the evidence AND the "
                "detector that would have noticed")
    ap = abs_path(op)
    if not ap:
        return None
    if ap == os.sep:
        return "that is the filesystem root"
    home = os.path.expanduser("~")
    if home and home != os.sep and ap == os.path.realpath(home):
        return "that is the home directory"
    roots, wts, _det = _resolve(ap, scan_below=True)
    for wt in wts:
        if ap == wt:
            return ("that is a live worktree root — another agent may be working in "
                    "it, and its uncommitted work exists nowhere else")
    for root in roots:
        if _under(root, ap):
            return "that is the repository root (or an ancestor of it)"
    for wt in wts:
        if _under(wt, ap):
            return ("that directory CONTAINS live worktrees — every one of them, and "
                    "all their uncommitted work, goes with it")
    return None


# --- globs and brace expansion: an operand that names no single path ------
#
# Every rule above resolves the LITERAL operand to a real path, and a glob never
# names one: `rm -rf <worktree-root>/*` realpaths to a path whose last component
# is a literal `*`, which is neither equal to nor under any live worktree root,
# so it fell through every check. Measured before this: `rm -rf ../worktrees/*`
# ALLOWED while `rm -rf ../worktrees` and `rm -rf ../worktrees/T-010` were both
# blocked — i.e. the ONE shape that got through was the natural bulk-teardown
# spelling, and it is the exact catastrophe the worktree rules exist to prevent.
#
# Evaluated LEXICALLY. The glob is never expanded against the filesystem: a
# pattern matching nothing right now still matches the worktree created a second
# later, and `glob.glob()` would call that safe. What it is matched against
# instead is what git says is live (`git worktree list`, `rev-parse`), which is
# authoritative and cannot go stale mid-command.
GLOB_META_RE = re.compile(r"[*?\[]")

MAX_BRACE_ALTS = 64


def _split_top_commas(body: str) -> list[str]:
    """`a,b{c,d}` -> ['a', 'b{c,d}'] — commas at brace depth 0 only."""
    out, cur, depth = [], "", 0
    for c in body:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += c
    out.append(cur)
    return out


def expand_braces(op: str) -> list[str]:
    """`{a,b}` alternatives, expanded lexically — `../wt/{T-1,T-2}` is two paths.

    `${VAR}` is NOT a brace expansion and is left alone (that is what the `$`
    lookbehind is for), and a brace group with no top-level comma is literal
    text to bash too. Capped, because `{a,b}{a,b}{a,b}...` is exponential and
    this runs on every `rm` the session issues."""
    i = op.find("{")
    while i != -1:
        if i == 0 or op[i - 1] != "$":
            depth, j, commas = 0, i, 0
            while j < len(op):
                c = op[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif c == "," and depth == 1:
                    commas += 1
                j += 1
            if j < len(op) and commas:
                out: list[str] = []
                for alt in _split_top_commas(op[i + 1:j]):
                    out.extend(expand_braces(op[:i] + alt + op[j + 1:]))
                    if len(out) >= MAX_BRACE_ALTS:
                        return out[:MAX_BRACE_ALTS]
                return out
        i = op.find("{", i + 1)
    return [op]


def _component_matches(pat: str, name: str) -> bool:
    """One glob COMPONENT against one path component, with bash's dotfile rule:
    a leading `.` is matched only by a literal `.` in the pattern, so `rm -rf *`
    does not "match" `.swarm-loop` and cannot be blocked for the wrong reason."""
    if name.startswith(".") and not pat.startswith("."):
        return False
    try:
        return fnmatch.fnmatchcase(name, pat)
    except Exception:
        return False


def _protected_paths(ap: str | None = None) -> list[str]:
    """The concrete directories the path rules protect, as seen from the operand.
    Ancestors are reached by TRIMMING these, so `../worktree*` still resolves to
    "contains worktrees"."""
    roots, wts, _det = _resolve(ap, scan_below=True)
    out: list[str] = []
    for r in roots + wts:
        if r and r not in out:
            out.append(r)
    # via swarm_dirs(), not a second join, so "where the run state lives" has
    # exactly one definition. It was two, and the copy here silently covered
    # for the other — which is how a rule ends up shipped and never exercised.
    for sd in swarm_dirs(ap):
        if sd not in out:
            out.append(sd)
    return out


def glob_reason(op: str) -> str | None:
    """Why a recursive delete of this GLOB is repo-global destruction.

    Two independent readings, because a glob can be dangerous either way:

      1. The pattern NAMES a protected directory (or an ancestor of one).
         `../worktrees/T-0*`, `../worktrees/[Tt]-010`, `../*/T-010` all match a
         live worktree root component-for-component. Matching is per COMPONENT,
         never with a `*` that crosses `/`, so `../worktrees/*/node_modules`
         (build junk inside each worktree) stays allowed.
      2. A catch-all LAST component (`*`, `**`, `?*`) names the entire CONTENTS
         of its parent, so its blast radius is the parent's. `rm -rf <parent>/*`
         is then judged exactly as `rm -rf <parent>` already is. Restricted to
         catch-alls so `rm -rf *.pyc` — a pattern with literal text, whose
         parent is merely the cwd — is not read as "delete the repo root", and
         to the LAST component so the `*/node_modules` shape above survives.
    """
    comps = op.split("/")
    k = None
    for idx, c in enumerate(comps):
        if GLOB_META_RE.search(c):
            k = idx
            break
    if k is None:
        return None

    ap = abs_path(op)
    if ap:
        pcomps = ap.split(os.sep)
        for cand in _protected_paths(ap):
            ccomps = cand.split(os.sep)
            if len(ccomps) < len(pcomps):
                continue
            trimmed = ccomps[:len(pcomps)]
            if all(_component_matches(p, c) for p, c in zip(pcomps, trimmed)):
                hit = os.sep.join(trimmed) or os.sep
                why = critical_dir_reason(hit)
                if why:
                    return "that pattern matches `" + hit + "`, and " + why

    tail = comps[k + 1:]
    pat = comps[k]
    if all(not c for c in tail) and "*" in pat and set(pat) <= set("*?"):
        prefix = "/".join(comps[:k])
        if not prefix:
            prefix = os.sep if op.startswith("/") else "."
        why = critical_dir_reason(prefix)
        if why:
            return ("that glob names everything inside `" + prefix + "`, and "
                    + why)
    return None


def rm_target_reason(op: str) -> str | None:
    """Why a recursive `rm` of this operand is repo-global destruction — the
    literal path, each brace alternative, and each of those read as a glob."""
    alts = expand_braces(op)
    for alt in alts:
        why = critical_dir_reason(alt) or glob_reason(alt)
        if why:
            if alt != op:
                return "it expands to `" + alt + "`, and " + why
            return why
    return None


def operands_after_options(args: list[str]) -> list[str]:
    """Non-option arguments, honouring a `--` end-of-options marker."""
    out: list[str] = []
    seen_ddash = False
    for a in args:
        if not seen_ddash and a == "--":
            seen_ddash = True
            continue
        if not seen_ddash and a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def var_tail_reason(op: str) -> str | None:
    """`rm -rf "$WT/T-010"` — a VARIABLE root with a LITERAL tail.

    `collapse_unexpanded` only catches the FULLY collapsed spelling: `"$WT/$ID"`
    becomes `/`, which is the filesystem root and is blocked. Leave one literal
    component and `"$WT/T-010"` collapses to `/T-010`, which is not a protected
    path and was ALLOWED — the same delete, one variable less. `${WT}/T-010` and
    the unquoted form were allowed too.

    But the guard already knows the NAME of every live worktree, and this
    operand states it verbatim. If the literal tail is the tail of a protected
    path, then every value of `$WT` that makes the command mean anything at all
    is a value that makes it destructive; the variable is not hiding the target,
    only its prefix."""
    comps = op.split("/")
    k = None
    for idx, c in enumerate(comps):
        if "$" in c:
            k = idx
    if k is None or k == len(comps) - 1:
        return None                      # no variable, or nothing literal after it
    tail = [c for c in comps[k + 1:] if c]
    if not tail or any("$" in c or GLOB_META_RE.search(c) for c in tail):
        return None
    ap = abs_path(op)
    for cand in _protected_paths(ap):
        ccomps = [c for c in cand.split(os.sep) if c]
        if len(ccomps) < len(tail) or ccomps[-len(tail):] != tail:
            continue
        why = critical_dir_reason(cand)
        if why:
            return ("its literal tail `" + "/".join(tail) + "` names `" + cand
                    + "`, and " + why)
    return None


RM_HINT = ("Retire a worktree with `git worktree remove <path>` (now allowed): it "
           "verifies the path really is a worktree and REFUSES one with uncommitted "
           "changes, which is exactly the check `rm -rf` does not have.")


UNDETERMINED_HINT = (
    "This is the ONE rule in this guard that fails CLOSED, and it is deliberate: "
    "an empty protected set used to mean two different things — `nothing is "
    "protected here` and `I could not find out` — and a recursive delete is the "
    "one operation where guessing the second is unrecoverable. Everything else "
    "in this hook still fails OPEN. If git really is unavailable, the run cannot "
    "work anyway; fix git, or delete the path with a non-recursive command."
)


def check_rm_paths(args: list[str]) -> str | None:
    """Non-git `rm`. This is the rule the guard was missing entirely."""
    recursive = long_opt_abbrev(args, "--recursive") or short_cluster_has(args, "rRd")
    unknown: str | None = None
    for op in operands_after_options(args):
        if is_ledger(op) or in_swarm_tree(op):
            return ("`rm " + op + "` deletes the run's own state under `" + SWARM_DIR
                    + "` — frozen goals, acceptance suite, manifest, history.")
        if not recursive:
            continue  # a non-recursive rm cannot take a directory
        why = rm_target_reason(op)
        if why:
            return "`rm -r " + op + "` — " + why + ".\n" + RM_HINT
        if "$" in op:
            collapsed = collapse_unexpanded(op)
            if collapsed:
                why = rm_target_reason(collapsed)
                if why:
                    return ("`rm -r " + op + "` — if that variable expands to nothing "
                            "the target is `" + collapsed + "`, and " + why + ".\n"
                            + RM_HINT)
            why = var_tail_reason(op)
            if why:
                return "`rm -r " + op + "` — " + why + ".\n" + RM_HINT
            if not collapsed:
                continue  # `rm -rf "$V"` with V unset is `rm -rf ''` — rm refuses
        # Nothing matched. Was that an ANSWER, or a failure to ask? Only the
        # second is a reason to refuse.
        if unknown is None and not _resolve(abs_path(op), scan_below=True)[2]:
            unknown = op
    if recursive and unknown is not None:
        return ("`rm -r " + unknown + "` — the guard could not ask git whether that "
                "path is a repository root, a live worktree, or a directory holding "
                "live worktrees: the query did not run (no git binary, or it timed "
                "out twice).\n" + UNDETERMINED_HINT)
    return None


def check_mv_paths(args: list[str]) -> str | None:
    for op in operands_after_options(args):
        if is_ledger(op) or in_swarm_tree(op):
            return ("`mv " + op + "` moves the run's own state out from under the "
                    "harness — every later `verify`/`measure` reads a hole.")
    return None


def _ledger_write(args: list[str], verb: str) -> str | None:
    for op in operands_after_options(args):
        if is_ledger(op):
            return ("`" + verb + " " + op + "` rewrites the run's ledger in place. "
                    "Only `swarmloop.py` may write it; a hand-edited ledger voids "
                    "every score computed from it.")
    return None


def check_truncate_paths(args: list[str]) -> str | None:
    # `truncate -s 0 <file>`: -s takes a separate value that is not a path.
    cleaned = [a for i, a in enumerate(args)
               if not (i > 0 and args[i - 1] in ("-s", "--size"))]
    return _ledger_write(cleaned, "truncate")


def check_sed_paths(args: list[str]) -> str | None:
    if not (short_cluster_has(args, "i") or long_opt_abbrev(args, "--in-place")):
        return None  # sed without -i writes to stdout
    return _ledger_write(args, "sed -i")


def check_dd_paths(args: list[str]) -> str | None:
    for a in args:
        if a.startswith("of=") and is_ledger(a[3:]):
            return _ledger_write([a[3:]], "dd of=")
    return None


def check_tee_paths(args: list[str]) -> str | None:
    return _ledger_write(args, "tee")


def check_find_paths(args: list[str]) -> str | None:
    # Narrow on purpose: only a find ROOTED IN the run-state directory. A blanket
    # rule on `find . ... -delete` would false-block `find . -name '*.pyc'
    # -delete`, which is ordinary housekeeping — and a false block on ordinary
    # housekeeping is worse than this miss.
    if not ("-delete" in args or ("-exec" in args and "rm" in args)):
        return None
    for a in args:
        if a.startswith("-"):
            break  # the start paths all precede the first predicate
        if in_swarm_tree(a):
            return ("`find " + a + " ... -delete` deletes the run's own state under `"
                    + SWARM_DIR + "`.")
    return None


NON_GIT_CHECKS = {
    "rm": check_rm_paths,
    "mv": check_mv_paths,
    "truncate": check_truncate_paths,
    "gtruncate": check_truncate_paths,
    "sed": check_sed_paths,
    "gsed": check_sed_paths,
    "dd": check_dd_paths,
    "tee": check_tee_paths,
    "find": check_find_paths,
    "gfind": check_find_paths,
}


REDIRECT_OPS = (">", ">|", ">>", "&>", "&>>")


def check_redirect_target(target: str) -> None:
    """`> <ledger file>` truncates it to nothing before a byte is written; `>>`
    corrupts it just as thoroughly, one appended line at a time.

    Scoped to the LEDGER, not to `.swarm-loop/` as a whole, because
    `cat > .swarm-loop/worker-preamble.md <<'EOF'` is how the loop writes the
    preamble every worker reads, and `git show HEAD:<frozen test> > <frozen
    test>` is the documented harness restore. Both stay allowed. shlex groups
    punctuation, so `>&` / `2>&1` (fd duplication, not a write) arrive as their
    own tokens and are never mistaken for a redirect.

    Known narrow imprecision: posix tokenization erases quotes, so a token that
    is EXACTLY `>` reads as an operator even when it was quoted — `grep ">"
    .swarm-loop/state.json` would block. It needs a bare-quoted `>` immediately
    followed by a ledger path, the block message says exactly what it saw, and
    any other spelling of the grep works; that is a better trade than carrying a
    second quote-aware scanner for it.

    Called from INSIDE the token walk rather than in a pass of its own. As two
    passes, every redirect on the line was resolved from the cwd the line
    STARTED in, so `cd <repo> && > .swarm-loop/state.json` was allowed from
    anywhere outside the repo and false-blocked from inside it. Neither verdict
    can be reproduced from a single cwd, which is why no case in a 482-case
    battery had ever disagreed with it."""
    if is_ledger(target):
        block("`> " + target + "` writes the run's ledger from a shell. Only "
              "`swarmloop.py` may write it; a hand-written ledger voids every "
              "score computed from it.")


def protected_write_reason(path: str) -> str | None:
    """Path rule for the non-Bash file tools (Edit/Write/NotebookEdit/MultiEdit
    — every member of FILE_TOOLS, all of which must appear in REQUIRED_MATCHER).

    It covers the LEDGER and nothing else. A frozen acceptance test edited with
    `Edit` passes this hook, deliberately: that tampering is already tamper-
    EVIDENT (`verify` re-hashes the file against the manifest), and the only
    amendment path observed to work in a live run writes into that directory
    under a user-approved `freeze --amend`. Do not read a quiet hook as "the
    file tools are covered" — they are covered for the ledger only."""
    if is_ledger(path):
        return ("writing `" + path + "` edits the run's ledger by hand. Only "
                "`swarmloop.py` may write it; a hand-edited ledger voids every "
                "score computed from it.")
    return None


CHECKS = {
    "stash": check_stash,
    "reset": check_reset,
    "checkout": check_checkout,
    "switch": check_switch,
    "restore": check_restore,
    "clean": check_clean,
    "worktree": check_worktree,
    "checkout-index": check_checkout_index,
    "read-tree": check_read_tree,
    "rm": check_rm,
    "apply": check_apply,
    "push": check_push,
    "branch": check_branch,
    "fetch": check_fetch,
    "update-ref": check_update_ref,
    "reflog": check_reflog,
    "gc": check_gc,
    "prune": check_prune,
    "repack": check_repack,
    "filter-branch": check_filter_branch,
    "symbolic-ref": check_symbolic_ref,
    "submodule": check_submodule,
    "sparse-checkout": check_sparse_checkout,
}

MAX_SHELL_DEPTH = 3
MAX_ALIAS_DEPTH = 3


def dash_c_dir(args: list[str]) -> str | None:
    """The directory git is told to run in by its own `-C <path>` global.
    Repeatable and cumulative in git, so they compose in order. This is the
    command NAMING its target: `git -C <worktree> checkout <file>` was exit 0
    for a file existing only in that worktree."""
    d = None
    j = 0
    while j < len(args):
        a = args[j]
        if a == "-C" and j + 1 < len(args):
            nxt = args[j + 1]
            d = nxt if d is None else os.path.join(d, nxt)
            j += 2
            continue
        if not a.startswith("-"):
            break                        # the subcommand; globals are over
        j += 1
    return d


def evaluate_git(args: list[str], depth: int = 0) -> str | None:
    prev = _set_cwd(dash_c_dir(args))
    try:
        return _evaluate_git(args, depth)
    finally:
        _restore_cwd(prev)


def _evaluate_git(args: list[str], depth: int = 0) -> str | None:
    sub = find_subcommand(args)
    if sub is None:
        return None
    check = CHECKS.get(sub)
    if check is not None:
        # rest = everything after the subcommand token
        idx = args.index(sub)
        return check(args[idx + 1:])
    if depth >= MAX_ALIAS_DEPTH:
        return None
    # Not a builtin the guard knows: it may be an ALIAS, defined inline with
    # `-c alias.x=...` or sitting in ~/.gitconfig / .git/config. `co = checkout`
    # is dotfile boilerplate, so this needs zero adversarial intent.
    expansion = inline_aliases(args).get(sub) or git_aliases().get(sub)
    if not expansion:
        return None
    rest = args[args.index(sub) + 1:]
    if expansion.startswith("!"):
        # `!shell` alias: the body is a command line, not git args. Re-scan it
        # rather than blocking outright — plenty of harmless aliases are `!git
        # ls-files ...`, and a blanket block on them would be a false block.
        payload = expansion[1:]
        if rest:
            payload += " " + " ".join(shlex.quote(a) for a in rest)
        try:
            scan_command(payload, depth + 1)  # blocks (exits) if it finds one
        except Exception:
            pass  # unparseable alias body -> fail open for that alias only
        return None
    try:
        expanded = shlex.split(expansion) + rest
    except Exception:
        return None  # unparseable alias -> fail open
    return _evaluate_git(expanded, depth + 1)


def var_git_args(seg: list[str]) -> list[str] | None:
    """Args of a segment whose PROGRAM is a shell variable naming git —
    `GIT=${GIT:-git}; $GIT stash` is what an agent-written script looks like.
    Restricted to variables whose NAME mentions git, because failing closed on
    every `$VAR ...` program would false-block `$PYTHON x.py`, `$DOTNET restore`
    and friends — and a false block is worse than a miss."""
    i = program_index(seg)
    if i >= len(seg) or "$" not in seg[i]:
        return None
    m = VAR_NAME_RE.search(seg[i])
    if not m or "GIT" not in m.group(1).upper():
        return None
    return seg[i + 1:]


def eval_payload(seg: list[str]) -> str | None:
    """`eval "git stash"` — eval's arguments are re-joined into a command line
    and executed, so they must be re-scanned as one."""
    i = program_index(seg)
    if i >= len(seg) or base_name(seg[i]) not in EVAL_WORDS:
        return None
    rest = seg[i + 1:]
    return " ".join(rest) if rest else None


def non_git_reason(seg: list[str]) -> str | None:
    """Path rules for the non-git destroyers (`rm`, `mv`, `sed -i`, ...)."""
    i = program_index(seg)
    if i >= len(seg):
        return None
    check = NON_GIT_CHECKS.get(base_name(seg[i]))
    return check(seg[i + 1:]) if check is not None else None


# `$(...)` that survives as part of ONE shlex token, i.e. sits inside double
# quotes: `echo "$(git reset --hard)"` really does run the reset, and nothing
# re-scanned the token's interior. Bash does NOT expand it inside single quotes,
# and posix tokenization has already thrown the quote characters away — so this
# only fires on tokens shlex produced from a DOUBLE-quoted or bare region, which
# is what `unquoted_substitutions` below establishes from the raw string.
DOLLAR_SUBST_RE = re.compile(r"\$\(")


def _balanced_subst(text: str, start: int) -> tuple[str, int] | None:
    """Interior of the `$(` beginning at `start`, and the index just past its `)`."""
    depth, i, n = 0, start + 1, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 2:i], i + 1
        i += 1
    return None


def _balanced_backtick(text: str, start: int) -> tuple[str, int] | None:
    """Interior of the backtick pair opening at `start`, and the index past its
    closing backtick. Backticks do not nest — an inner one must be escaped — so
    the first UNESCAPED backtick closes it."""
    i, n = start + 1, len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == "`":
            return text[start + 1:i], i + 1
        i += 1
    return None


def expandable_substitutions(command: str) -> list[str]:
    """Interiors of `$(...)` and `` `...` `` that bash would actually EXECUTE.

    Scanned off the raw string, not off tokens, because the one thing that
    decides the answer — single quotes — is exactly what posix tokenization
    erases. `echo 'never run $(git stash)'` is inert text and must stay allowed;
    `echo "$(git stash)"` runs it.

    Backticks are handled here for the DOUBLE-QUOTED case only. Unquoted ones
    are already split off as operators by `PUNCTUATION_CHARS`, so their bodies
    arrive as ordinary segments — but shlex applies punctuation only OUTSIDE
    quotes, so a backtick pair inside double quotes survived as inert-looking
    token text while bash executes it exactly as it executes `$(...)` there.
    Verified against real bash: `echo "`id`"` prints the uid. That union case —
    the intersection of the two holes each of which had been closed on its own —
    was the last executable substitution the guard could not see."""
    out: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\" and quote != "'" and i + 1 < n:
            i += 2
            continue
        if quote == '"':
            if c == '"':
                quote = None
                i += 1
                continue
        elif c in "'\"":
            quote = c
            i += 1
            continue
        if command[i:i + 2] == "$(" and command[i:i + 3] != "$((":
            found = _balanced_subst(command, i)
            if found is None:
                break
            inner, i = found
            out.append(inner)
            out.extend(expandable_substitutions(inner))  # nested `$( $( ) )`
            continue
        if c == "`" and quote == '"':
            found = _balanced_backtick(command, i)
            if found is None:
                break  # unpaired backtick in prose -> nothing executes, fail open
            inner, i = found
            out.append(inner)
            out.extend(expandable_substitutions(inner))
            continue
        i += 1
    return out


def cd_target(seg: list[str]) -> str | None:
    """`cd <dir>` as a simple command. Its effect on the rest of the line is
    real — `cd <worktree> && git checkout <file>` names its target as plainly as
    `git -C` does, and both were exit 0 — so it is honoured from here on.
    Only the unambiguous one-operand form: `cd` with no operand (HOME), `cd -`
    and `cd a b` all change nothing rather than guess."""
    i = program_index(seg)
    if i >= len(seg) or base_name(seg[i]) != "cd":
        return None
    ops = [a for a in seg[i + 1:] if not a.startswith("-")]
    return ops[0] if len(ops) == 1 else None


def scan_command(command: str, depth: int) -> None:
    """Evaluate one command string: redirections, then every simple command,
    recursing into `bash -c` / `eval` payloads and executed `$(...)` bodies.

    `_CWD` is saved and restored around the whole scan: a `cd` inside a payload
    (or a `!shell` alias body) must not leak out into the segments that follow
    it at the caller's level."""
    entry_cwd = _CWD
    try:
        _scan_command(command, depth)
    finally:
        _restore_cwd(entry_cwd)


def _scan_command(command: str, depth: int) -> None:
    try:
        toks = token_stream(command)
    except Exception:
        return  # unparseable -> fail open for this level only

    if depth < MAX_SHELL_DEPTH:
        # Scanned off the command with heredoc BODIES removed, never off the raw
        # string. The loop writes task packets by heredoc whose prose quotes the
        # banned ops; a packet line reading "never run $(git stash)" is document
        # text, and re-scanning it would false-block the dispatch step — the one
        # failure this guard is least allowed to have.
        try:
            body = "\n".join(logical_lines(command)) if "\n" in command else command
        except Exception:
            body = None
        if body is not None:
            for inner in expandable_substitutions(body):
                scan_command(inner, depth + 1)

    cur: list[str] = []
    for k, tok in enumerate(toks):
        if is_operator(tok):
            if cur:
                _scan_segment(cur, depth)
                cur = []
            # AFTER flushing, so a `cd` on this line is already in effect.
            if tok in REDIRECT_OPS and k + 1 < len(toks):
                check_redirect_target(toks[k + 1])
            continue
        cur.append(tok)
    if cur:
        _scan_segment(cur, depth)


def _scan_segment(seg: list[str], depth: int) -> None:
    """One simple command. Split out of the token walk so redirect targets and
    simple commands can be interleaved in source order — see
    `check_redirect_target`."""
    target = cd_target(seg)
    if target is not None:
        _set_cwd(target)
        return
    git_args = git_args_of(seg)
    if git_args is None:
        git_args = var_git_args(seg)
    if git_args is not None:
        reason = evaluate_git(git_args, depth)
        if reason:
            block(reason)
        return
    reason = non_git_reason(seg)
    if reason:
        block(reason)
    if depth >= MAX_SHELL_DEPTH:
        return
    payload = shell_payload(seg) or eval_payload(seg)
    if not payload:
        return
    scan_command(payload, depth + 1)




def _main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        allow()  # unparseable payload -> fail open
        return

    # A payload that is not a JSON object (a list, a bare string, a number,
    # null) has no .get(); reading it unguarded raised AttributeError and exited
    # 1, which is NOT fail-open. Any shape but an object -> allow.
    if not isinstance(data, dict):
        allow()
        return

    # The command runs where the AGENT is, not where this hook process was
    # started. Measured differential, payload byte-identical, always declaring
    # cwd = a worker's worktree, target present in the worktree and absent from
    # the primary tree: hook cwd = primary -> ALLOWED; hook cwd = worktree ->
    # BLOCKED; control (a file present in BOTH, from the primary) -> BLOCKED.
    # The control is what makes it conclusive — the rule works, so the allow was
    # a failed CLASSIFICATION: `is_commit_ish` and `os.path.lexists` were asking
    # about the SESSION's root. Net effect: a worker could destructively
    # `git checkout` any file it had created, i.e. most of its own output.
    # Guarded, and a bad cwd changes nothing — an unusable cwd must never turn
    # into a block.
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        try:
            if os.path.isdir(cwd):
                os.chdir(cwd)
        except Exception:
            pass  # unreadable/vanished cwd -> evaluate from here, never block on it
    _init_cwd()   # `_CWD` tracks the payload cwd, and is re-pointed by -C / cd

    tool = data.get("tool_name")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}

    # Non-Bash file-writing tools. These payloads only arrive if the hook is
    # registered with the full matcher `Bash|Edit|Write|NotebookEdit|MultiEdit`
    # (see SCOPE in the docstring) — every member of FILE_TOOLS must be named
    # there, and a tool that is missing from it is not "guarded but quiet", it
    # is unguarded. The guard also used to self-exempt (`tool not in (None,
    # "Bash") -> allow`), which meant belt AND braces in the wrong direction:
    # widening the matcher alone would have changed nothing. Measured then, on
    # one `git stash` payload: Bash exit 2, Edit/Write/NotebookEdit exit 0
    # (MultiEdit was added to FILE_TOOLS afterwards and is not a fourth reading
    # of that measurement — it is covered by the same branch). Handling them
    # here is what makes the matcher string above actually do something, and
    # what it arms is `protected_write_reason` — the LEDGER rule, only.
    # `tool_name` absent ENTIRELY is still treated as Bash — see the docstring.
    if tool is not None and tool != "Bash":
        if tool in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            reason = protected_write_reason(path) if isinstance(path, str) else None
            if reason:
                block(reason)
        allow()
        return

    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        allow()
        return

    command = elide_long_tokens(command)

    try:
        scan_command(command, depth=0)
    except Exception:
        allow()  # unbalanced quotes / any logic error -> fail open
        return

    allow()


def main() -> None:
    """Fail-open wrapper. The docstring's contract is exit 0 on ANY parse/logic
    error, so nothing may escape as a traceback + exit 1. allow()/block() raise
    SystemExit, which is a BaseException and must pass through untouched."""
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        pass  # unexpected failure -> fail open, never block all bash
    allow()


if __name__ == "__main__":
    main()
