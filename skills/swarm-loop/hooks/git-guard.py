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
  - Two questions are answered by ASKING git (read-only `rev-parse` /
    `config`, ~2s timeout): "is this checkout operand a ref or a path?" and
    "is this subcommand an alias?". If git cannot answer — no binary, not a
    repository, timeout — the answer is ALLOW.
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
  git worktree remove | git worktree move
  the plumbing equivalents: git checkout-index -f/-a, git read-tree -u,
    git apply -R, and git rm over a repo-wide pathspec (`.`, `:/`, a glob)

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
  - anything reached after a `cd` the guard cannot follow: the ref-vs-path
    question for `git checkout <operand>` is answered from the HOOK's cwd.
  This is a guard rail against accidents, not a sandbox against a determined
  caller. The real protection is per-agent worktree isolation.

Allowed (verified): git add, git commit, git push, git reset (soft/mixed),
  git checkout <branch>, git checkout -b, git switch <branch>, git diff,
  git show, git show HEAD:<path> > <path>, git log, git status, git ls-tree,
  git ls-remote, git stash list, git stash show, git stash create,
  git worktree add, git worktree list, git worktree prune,
  git restore --staged <path> (index-only), git clean -n / --dry-run,
  git rm <file> and git rm --cached (scoped, not repo-global),
  git merge / rebase / cherry-pick / revert / fetch / branch -D,
  and the sanctioned worktree teardown
  `rm -rf <worktree-path> && git worktree prune && git branch -D <branch>`.

Contract (Claude Code PreToolUse):
  stdin              JSON; tool_input.command holds the bash command string.
  exit 0             ALLOW (no output).
  exit 2 + stderr    BLOCK; stderr shown to the model.
Fails OPEN (exit 0) on any parse/logic error or unbalanced quotes — a broken
guard must never block all bash. Only a positively-identified destructive op
returns exit 2.
"""
from __future__ import annotations

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
    "  - Retiring a worktree? `git worktree remove` is blocked for everyone; the "
    "sanctioned path is `rm -rf <worktree-path>` then `git worktree prune` "
    "(both allowed).\n"
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


def is_operator(tok: str) -> bool:
    """A shell control/redirection operator token (&&, ||, |, ;, (, ), <, >, ...)."""
    return len(tok) > 0 and all(c in ";|&()<>" for c in tok)


def tokenize(command: str) -> list[str]:
    """shlex tokenization respecting quotes; operators come out as their own tokens.

    Raises on unbalanced quotes — caller treats that as fail-open.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


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


def segments(command: str) -> list[list[str]]:
    """Split a bash command string into simple-command argv lists on shell operators."""
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
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in toks:
        if is_operator(tok):
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


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
_ALIAS_CACHE: dict[str, str] | None = None


def _git_run(args: list[str]):
    """Run a READ-ONLY git query. Returns the CompletedProcess, or None if git
    could not be run at all (missing binary, timeout, ...) -> caller fails open."""
    try:
        return subprocess.run(
            ["git"] + args, capture_output=True, text=True,
            timeout=GIT_QUERY_TIMEOUT,
        )
    except Exception:
        return None


def git_aliases() -> dict[str, str]:
    """All `alias.*` entries visible from the cwd (repo + global + system).

    Loaded once per process, lazily: an alias is only consulted for a subcommand
    the guard does not already recognize. `co = checkout` in someone's dotfiles
    is boilerplate, and without this `git co .` walks straight past the guard."""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        out: dict[str, str] = {}
        p = _git_run(["config", "--get-regexp", "^alias\\."])
        if p is not None and p.returncode == 0:
            for line in p.stdout.splitlines():
                key, _, val = line.partition(" ")
                if key.startswith("alias.") and val:
                    out.setdefault(key[len("alias."):], val)
        _ALIAS_CACHE = out
    return _ALIAS_CACHE


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
    p = _git_run(["rev-parse", "--verify", "--quiet", name + "^{commit}"])
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
        if is_commit_ish(op) is False and os.path.lexists(op):
            # not a ref, and it names something on disk -> a real pathspec.
            # (not a ref and not on disk -> git itself errors out; harmless.)
            return PATHSPEC_REASON
    return None  # 0 operands, or an operand git resolves as a commit-ish


def check_switch(rest: list[str]) -> str | None:
    # -c/-C take an attached value, so `git switch -cfeat` is a branch name.
    if long_opt_abbrev(rest, "--discard-changes", "--force") or short_flag_has(rest, "f", "cC"):
        return "`git switch --discard-changes/-f` throws away uncommitted working-tree changes."
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
    if sc in ("remove", "move"):
        return (
            "`git worktree " + sc + "` tears down/relocates a worktree another agent "
            "may be working in (and drifts the run's worktree state)."
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
}

MAX_SHELL_DEPTH = 3
MAX_ALIAS_DEPTH = 3


def evaluate_git(args: list[str], depth: int = 0) -> str | None:
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
            scan(segments(payload), depth + 1)  # blocks (exits) if it finds one
        except Exception:
            pass  # unparseable alias body -> fail open for that alias only
        return None
    try:
        expanded = shlex.split(expansion) + rest
    except Exception:
        return None  # unparseable alias -> fail open
    return evaluate_git(expanded, depth + 1)


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


def scan(segs: list[list[str]], depth: int) -> None:
    """Evaluate every git segment; recurse into `bash -c` / `eval` payloads."""
    for seg in segs:
        git_args = git_args_of(seg)
        if git_args is None:
            git_args = var_git_args(seg)
        if git_args is not None:
            reason = evaluate_git(git_args, depth)
            if reason:
                block(reason)
            continue
        if depth >= MAX_SHELL_DEPTH:
            continue
        payload = shell_payload(seg) or eval_payload(seg)
        if not payload:
            continue
        try:
            inner = segments(payload)
        except Exception:
            continue  # unparseable payload -> fail open for that payload only
        scan(inner, depth + 1)


# shlex.read_token accumulates a token with string concatenation, so tokenizing
# ONE pathologically long word is quadratic in its length (`echo <200k chars>`
# burns ~1s). No real command carries a word this long; a base64/data blob does.
# Fail OPEN above the limit rather than stall the hook on every Bash call.
MAX_WORD_CHARS = 20000
WORD_SPLIT_RE = re.compile(r"[\s;|&()<>]+")  # shlex's own token boundaries


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

    tool = data.get("tool_name")
    if tool not in (None, "Bash"):
        allow()
        return

    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        allow()
        return

    if max((len(w) for w in WORD_SPLIT_RE.split(command)), default=0) > MAX_WORD_CHARS:
        allow()  # single-token blowup -> fail open (see MAX_WORD_CHARS)
        return

    try:
        segs = segments(command)
    except Exception:
        allow()  # unbalanced quotes / lexer failure -> fail open (never block normal git)
        return

    try:
        scan(segs, depth=0)
    except Exception:
        allow()  # any logic error -> fail open
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
