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
  - argv-parsing, NOT a fragile inline regex. The command string is tokenized
    with shlex (quotes respected, so `git commit -m "use git stash carefully"`
    is NOT a stash call), split into simple commands on shell operators
    (`&&`, `||`, `|`, `;`, `(`, `)`, redirections), and each `git ...` segment
    is evaluated by subcommand.
  - Blocks ONLY positively-identified destructive repo-global ops. Everything
    else — including all normal loop/auditor git — is allowed. Getting an
    allow-case wrong (breaking `git commit`) is worse than a missed block-case,
    so every ambiguous case errs toward ALLOW.

Blocked:
  git stash (push/save/pop/apply/drop/clear/branch/store, and bare `git stash`)
  git reset --hard | --merge | --keep
  git checkout -- <path> | git checkout . | git checkout <tree-ish> <path>
  git checkout -f / --force
  git switch --discard-changes / -f / --force
  git restore <path> (anything that touches the working tree)
  git clean with force (-f / --force, incl. -fd/-fdx), unless --dry-run/-n
  git worktree remove | git worktree move

  Long options are matched by git's own abbreviation rule, so `git reset --ha`
  and `git checkout --f` are blocked too. Short options are scanned as flag
  clusters that STOP at a value-taking letter, so `-bfix/thing` stays a branch
  name and is not read as a cluster containing `f`.

Allowed (verified): git add, git commit, git push, git reset (soft/mixed),
  git checkout <branch>, git checkout -b, git switch <branch>, git diff,
  git show, git log, git status, git ls-tree, git ls-remote, git stash list,
  git stash show, git stash create, git worktree add, git worktree list,
  git restore --staged <path> (index-only).

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
import re
import shlex
import sys

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# command wrappers that precede the real program
WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "exec", "builtin"}

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


def segments(command: str) -> list[list[str]]:
    """Split a bash command string into simple-command argv lists on shell operators."""
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


def git_args_of(seg: list[str]) -> list[str] | None:
    """If this simple command invokes git, return the args after `git`; else None."""
    i = 0
    while i < len(seg):
        core = seg[i].lstrip("$`(")
        if core == "":
            i += 1
            continue
        if ENV_ASSIGN_RE.match(core):  # FOO=bar prefix
            i += 1
            continue
        if core in WRAPPERS:  # sudo / env / command / ...
            i += 1
            continue
        break
    if i >= len(seg):
        return None
    prog = seg[i].lstrip("$`(").rstrip(")`")
    base = prog.rsplit("/", 1)[-1]
    if base != "git":
        return None
    return seg[i + 1:]


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


def check_checkout(rest: list[str]) -> str | None:
    if "--" in rest:
        return "`git checkout ... -- <path>` overwrites working-tree files from another commit."
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
    branch_flags = {"-b", "-B", "--orphan", "--detach", "-t", "--track"}
    if any(a in branch_flags or a.startswith("--track") for a in rest):
        return None  # branch create / switch
    if len(operands) >= 2:
        return "`git checkout <tree-ish> <path>` overwrites working-tree files from another commit."
    return None  # 0 or 1 operand, no `--`, not `.` -> treat as a branch switch


def check_switch(rest: list[str]) -> str | None:
    # -c/-C take an attached value, so `git switch -cfeat` is a branch name.
    if long_opt_abbrev(rest, "--discard-changes", "--force") or short_flag_has(rest, "f", "cC"):
        return "`git switch --discard-changes/-f` throws away uncommitted working-tree changes."
    return None


def check_restore(rest: list[str]) -> str | None:
    staged = any(a in ("--staged", "-S") for a in rest) or short_cluster_has(rest, "S")
    worktree = any(a in ("--worktree", "-W") for a in rest) or short_cluster_has(rest, "W")
    if worktree or not staged:  # default (and explicit -W) rewrites the working tree
        return "`git restore <path>` overwrites working-tree files from the index/a commit."
    return None  # --staged only -> unstages, working tree untouched


def check_clean(rest: list[str]) -> str | None:
    dry = any(a in ("-n", "--dry-run") for a in rest) or short_cluster_has(rest, "n")
    danger = ("--force" in rest) or short_cluster_has(rest, "fdxX")
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


CHECKS = {
    "stash": check_stash,
    "reset": check_reset,
    "checkout": check_checkout,
    "switch": check_switch,
    "restore": check_restore,
    "clean": check_clean,
    "worktree": check_worktree,
}


def evaluate_git(args: list[str]) -> str | None:
    sub = find_subcommand(args)
    if sub is None:
        return None
    check = CHECKS.get(sub)
    if check is None:
        return None
    # rest = everything after the subcommand token
    idx = args.index(sub)
    return check(args[idx + 1:])


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        allow()  # unparseable payload -> fail open
        return

    tool = data.get("tool_name")
    if tool not in (None, "Bash"):
        allow()
        return

    command = (data.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        allow()
        return

    try:
        segs = segments(command)
    except Exception:
        allow()  # unbalanced quotes / lexer failure -> fail open (never block normal git)
        return

    try:
        for seg in segs:
            git_args = git_args_of(seg)
            if git_args is None:
                continue
            reason = evaluate_git(git_args)
            if reason:
                block(reason)
    except Exception:
        allow()  # any logic error -> fail open
        return

    allow()


if __name__ == "__main__":
    main()
