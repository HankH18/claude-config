#!/usr/bin/env python3
"""swarmloop.py — deterministic harness for the swarm-loop skill.

Owns the numeric/state side of the loop so the orchestrating agent never has to
eyeball a trend or hand-compute a regression. Pure stdlib. All state lives in
.swarm-loop/ at the repo root.

Subcommands:
  init        Create .swarm-loop/ state (cycle budget, autonomy, stall limit)
  freeze      Hash goals.json + protected paths into manifest.json (goal lock)
  verify      Re-hash and compare against manifest; nonzero exit on tampering
  restore-harness  Rewrite protected files back to their frozen bytes from git
  check-branch  Flag a git branch that touches protected paths (pre-merge veto)
  measure     Run every metric command, record values+errors for a cycle
  record      Manually record one metric value for a cycle
  analyze     Per-metric OLS over cycle history: slope, projection, verdict
  checkpoint  Record HEAD for the cycle; drive the no-change kill switch
  audit-loop-done  Record a completed audit-fix loop (exit 2 when terminal)
  escalate    Ask the USER to change something frozen/blocked (records ONLY)
  frontier    Ready set from the ticket graph (readiness + blast radius)
  check-wave  Refuse a wave whose members could own the same file
  conflicts   Pairwise TEXTUAL conflict between task branches (git merge-tree)
  push-gate   Score the mechanical rungs of the Phase 7 push gate
  reads       Read edges the graph does not declare (gates freeze)
  findings    Record checker findings durably, then mint tickets from them
  red-check   Prove each ticket's verify command FAILS before it is built
  reachable   Wiring check: is a symbol defined, called, exported, reachable
  dispatch    Record a dispatched agent / close one (append-only)
  retire      Reclaim worktrees whose work LANDED on main (plain remove, never --force)
  resume      Reconstruct an interrupted run from artifacts alone
  status      Print loop state summary

A freeze is a LOCKED DOOR, not a wall. Three levels, which must never blur:
  WORKERS       hard wall. Never touch anything frozen; report upward through
                the packet's NEEDS channel. The mechanical veto is UNCHANGED —
                check-branch still refuses any branch touching a protected path
                (and .swarm-loop/ at large, where escalations live), so a worker
                cannot file one from its own branch. That veto is the anti-gaming
                property of the whole harness and nothing here weakens it.
  ORCHESTRATOR  a permission boundary, not a dead end. When something frozen
                turns out to be wrong the obligation is to ESCALATE — surface it
                loudly, with the evidence and a specific proposed change — and
                never to silently work around it, silently re-freeze it, or keep
                scoring against a goal it believes is broken.
  USER          the only one who can authorize a change. `freeze --amend
                "<reason>"` remains the only mechanism that alters a frozen thing.

INVARIANT: `escalate` CHANGES NOTHING. It appends to escalations.jsonl and
regenerates the ESCALATIONS.md mirror; it touches no goal, manifest, metric,
history row or state field. An agent may ask; only the user may grant. That
inertness is what keeps "the door can be opened" from becoming a goalpost-moving
hole.

INVARIANT: an ask nobody sees is an ask nobody filed. `escalate --open-asks`
prints every open request and NOTHING AT ALL when there are none, so the end of
every user-facing turn can run it unconditionally; that silence is what makes it
runnable that often. `--resolve` records what the USER decided and demands a
note; `--withdraw` is the loop retracting a question that became moot, is
recorded as NEVER ANSWERED, and is refused outright for a blocking one.

Exit codes: 0 ok · 1 error/violation · 2 kill switch or terminal condition ·
3 usage error (bad flag/subcommand — never a terminal signal; fix and re-run) ·
130 interrupted (Ctrl-C/SIGINT) — nothing was concluded, re-run the command.
"""

import argparse
import ast
import csv
import difflib
import fnmatch
import io
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

STATE_DIR = ".swarm-loop"
STATE_FILE = "state.json"
GOALS_FILE = "goals.json"
MANIFEST_FILE = "manifest.json"
FREEZE_LOG_FILE = "freeze-log.jsonl"
ESCALATIONS_FILE = "escalations.jsonl"
ESCALATIONS_MD = "ESCALATIONS.md"
LEARNINGS_MD = "learnings.md"      # per-run, project-specific (Phase 8 appends)
LEARNED_MD = "LEARNED.md"          # the SKILL's cross-run memory (Termination distils into it)
HISTORY_FILE = "history.csv"
ANALYSIS_DIR = "analysis"

# 'source' is load-bearing, not decoration: analyze keeps the LAST row per cycle
# in file order, so a hand-entered 'record' silently outranks the real 'measure'
# for that cycle. The column is what lets analyze say so out loud. Files written
# by older versions have only the first five columns and are still readable.
# `selected`/`deselected` are the DURABLE half of the deselection fix. Without
# them the only trace of a metric whose number came from a filter was a WARNING
# on measure's stdout: history.csv gained no column, analysis/cycle-N.json gained
# no key, and a later reader could not tell a deselected green from a real one.
# Measured on this branch before they existed — honest baseline, then a marker
# filter added to pytest.ini with src/ still EMPTY: `grep -ic desel` over
# history.csv, analysis/cycle-1.json and analysis/cycle-1.md returned 0 / 0 / 0
# while analyze printed ALL TARGETS MET. Appended, never inserted: _history_reader
# validates the first five columns only, so files written by older versions (5 or
# 6 columns) stay readable and read as "selection unknown", which is NOT the same
# as zero and is never compared as if it were.
HISTORY_HEADER = ["cycle", "metric_id", "value", "error", "timestamp", "source",
                  "selected", "deselected"]

SLOPE_EPS = 1e-9  # below this magnitude, treat the error trend as flat
# A metric command that sends its own output to /dev/null. Matches the shapes a
# shell actually uses: `>/dev/null`, `> /dev/null`, `2>/dev/null`, `&>/dev/null`,
# and `>/dev/null 2>&1`. Deliberately NOT matching `2>/dev/null` alone would be
# wrong — stderr is exactly where a failing runner says why.
DEVNULL_RE = re.compile(r"(?:\d*|&)\s*>+\s*/dev/null")

# ---- anchoring the redirect above to the command that actually runs the tests.
#
# DEVNULL_RE against a WHOLE command string is right for a METRIC command (one
# expression whose last line IS the number, refused at intake where the fix costs
# a flag) and WRONG for a ticket's `verify` string, which is a shell pipeline the
# user wrote in their build docs. Measured false positive:
#     command -v jq >/dev/null 2>&1 || exit 3; pytest -q tests/test_t1.py
# scored SWALLOWED. Under `red-check --close` that is exit 1, and the stamp it
# writes says `swallowed`, which check-branch --ticket then reads as a permanent
# VETO — while the remedy check-branch prints (re-run red-check --close) re-runs
# the same string and re-stamps `swallowed`. A false refusal that the printed
# remedy cannot clear, on the loop's own merge step, discovered after the work is
# built. This repo's standard is explicit that a false refusal there is strictly
# worse than the gap being closed.
#
# So: split the command into shell segments, and decide per segment.
#
# QUOTE-AWARE, hand-rolled, because `re.split(r"\|\||&&|;|\||\n")` splits inside
# quotes and that cuts BOTH ways here: `pytest -q -k "a;b" >/dev/null 2>&1;
# echo '1 failed'` splits into `pytest -q -k "a` and `b" >/dev/null 2>&1`, so the
# redirect ends up on a fragment with no runner in it while the runner ends up on
# a fragment with no redirect — and the false NEGATIVE hides exactly the shape
# the check exists for. Not a shell parser and not trying to be: quotes and
# backslash escapes only, which is all a `verify` string has ever needed.
_SEGMENT_SEPS = (("||", 2), ("&&", 2), (";", 1), ("|", 1), ("\n", 1))


def _shell_segments(cmd):
    """`cmd` split on UNQUOTED ; && || | and newline. Empty pieces dropped."""
    out, buf = [], []
    quote = None
    i, n = 0, len(cmd or "")
    while i < n:
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        hit = next((ln for sep, ln in _SEGMENT_SEPS if cmd.startswith(sep, i)), None)
        if hit:
            out.append("".join(buf))
            buf = []
            i += hit
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s for s in out if s.strip()]
# A segment that RUNS TESTS. Positive evidence, deliberately: if the redirect is
# on this, the count parsed downstream cannot have come from it.
TEST_RUNNER_RE = re.compile(
    r"(?:^|[\s/=(`$])(?:py\.?test|unittest|nose2?|tox|jest|vitest|mocha|jasmine|karma|"
    r"ava|cypress|playwright|rspec|minitest|phpunit|ctest|bats|behave|cucumber|gotestsum)\b"
    r"|\b(?:go|cargo|dotnet|swift|mix|flutter)\s+test\b"
    r"|\b(?:npm|yarn|pnpm|bun|npx)\s+(?:run\s+)?(?:test|t)\b"
    r"|\b(?:make|just)\s+(?:test|tests|check|verify|ci)\b"
    r"|\bgradlew?\b[^\n]*\b(?:test|check)\b|\bmvn\b[^\n]*\b(?:test|verify)\b"
    r"|[\w./-]*(?:test|spec|check|verify)[\w.-]*\.(?:sh|bash|zsh|py|js|mjs|ts|rb)\b", re.I)
# A segment that is a PRECONDITION or SETUP step, not the test run. These are the
# idioms whose output a competent verify string legitimately discards.
PRECONDITION_RE = re.compile(
    r"^\s*(?:command\s+-v|which|type|hash|test\s|\[\[?\s|cd\s|pushd|popd|mkdir|rm\s|"
    r"touch|export|source|\.\s|set\s|unset|umask|true|:|echo|printf|sleep|"
    r"git(?:\s|$)|docker(?:\s|$)|apt-get|apk|brew|nvm|pyenv|conda|asdf|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:ci|install|i|add)\b|"
    r"(?:pip3?|uv|poetry|pipenv)\s+(?:install|sync|add|pip)\b|"
    r"go\s+mod\b|cargo\s+(?:fetch|build)\b|make\s+(?:install|build|deps|setup)\b)")


def _swallowing_redirect(cmd):
    """The /dev/null redirect that could be hiding THE TEST RUN, or None.

    Flags a redirect only when the segment carrying it either (a) runs a
    recognised test runner, or (b) is unrecognised — neither a runner nor a known
    precondition — AND no other segment runs a test runner with its output
    visible. Clause (b) is what keeps the teeth for the shape that motivated the
    check, `./gate.sh >/dev/null 2>&1; echo '1 failed'; exit 1`, where the runner
    is behind a script name this function cannot know; clause (b)'s second half
    is what stops `./setup.sh >/dev/null; pytest -q tests` from being refused.

    Returns (redirect_text, segment) so the message can quote BOTH — the old
    message quoted only the redirect, which is precisely what made the false
    positive unreadable: `'>/dev/null'` says nothing about which command's output
    went there.
    """
    segments = _shell_segments(cmd or "")
    redirected = [s for s in segments if DEVNULL_RE.search(s)]
    if not redirected:
        return None
    visible_runner = any(TEST_RUNNER_RE.search(s) and not DEVNULL_RE.search(s)
                         for s in segments)
    for seg in redirected:
        s = seg.strip()
        if TEST_RUNNER_RE.search(s):
            return DEVNULL_RE.search(s).group(0), s[:120]
        if PRECONDITION_RE.match(s):
            continue
        if not visible_runner:
            return DEVNULL_RE.search(s).group(0), s[:120]
    return None
# Relative floor under which a change in a metric's error is float noise rather
# than progress. Scaled by the previous error so a metric measured in millions
# and one measured in percent get the same treatment.
PROGRESS_EPS = 1e-9

# D3: the two protected paths that are NOT negotiable. goals.json holds every
# target; .swarm-loop/acceptance holds the tests that grade them. `init
# --protect-path` used to REPLACE the whole list, so one flag silently dropped
# goals.json — the frozen targets became freely editable while verify, measure
# and analyze went on asserting the harness was intact. No forgery required.
MANDATORY_PROTECTED = (f"{STATE_DIR}/acceptance", f"{STATE_DIR}/{GOALS_FILE}")

# The keys freeze STAMPS INTO goals.json. They move on every freeze even when
# nothing about the goals moves, which is why a raw digest of the file cannot
# answer "did a target move?" — see _goals_canon.
GOALS_VOLATILE_KEYS = ("frozen", "frozen_at", "amendments", "last_amended_at",
                       "last_amend_reason")


# ---------------------------------------------------------------- utilities

def repo_root():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        die("not inside a git repository (swarm-loop requires git)")


def sd(root):
    return os.path.join(root, STATE_DIR)


def die(msg, code=1):
    print(f"swarmloop: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path, what):
    if not os.path.exists(path):
        die(f"{what} not found at {path} — run the earlier stage first")
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    # The tmp name carries the pid: checkpoint and audit-loop-done both
    # read-modify-write state.json, and with a SHARED "state.json.tmp" two
    # concurrent writers clobber each other's half-written file before either
    # rename lands. Uniquifying makes each write atomic on its own; _state_lock
    # below is what makes the read-modify-write pair atomic.
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _state_lock(object):
    """Advisory lock around a read-modify-write of state.json.

    P-14: `checkpoint` and `audit-loop-done` both load state.json, mutate one
    field and save it. Run concurrently (an orchestrator and a recovery command,
    say) one update is lost entirely — silently, since both exit 0. This is an
    O_EXCL lockfile, not a kernel lock, because the writers are separate
    short-lived processes on one machine.

    It NEVER blocks the run: after the timeout it proceeds with a warning. A
    harness that refuses to checkpoint because a stale lockfile survived a
    SIGKILL would be worse than the race it is preventing.
    """

    def __init__(self, d, timeout=10.0, stale=120.0):
        self.path = os.path.join(d, ".state.lock")
        self.timeout, self.stale, self.held = timeout, stale, False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, ("%d\n" % os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue
                if age > self.stale:
                    # The holder died. Breaking a stale lock is safe here: the
                    # writes it guards are single-file os.replace renames.
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    print("swarmloop: WARNING: another swarmloop process appears to be "
                          f"writing {STATE_DIR}/{STATE_FILE} (lock held >{self.timeout:g}s); "
                          "proceeding anyway — check for a concurrent orchestrator")
                    return self
                time.sleep(0.1)
            except OSError:
                return self  # unwritable state dir: the caller's own write will say so

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _goals_canon(obj):
    """sha256 of goals.json with freeze's OWN stamps removed — the target digest.

    D1, and it is the hole A1 was built to close. The manifest's raw digest for
    goals.json can never be reconciled against the freeze log's, ever: freeze
    stamps frozen_at/amendments/last_amend_reason into the file, so the digest
    moves on every freeze even when no goal moves, and an amendment that touches
    no goal writes no goals.json line into the log at all — the replayed raw
    digest is stale by construction. That is why _authenticate_manifest excluded
    goals.json outright, and the exclusion left the P-2/H-31 manifest-forgery
    attack wide open on the ONE file that holds every target: edit a target,
    recompute that single digest into manifest.json, and `verify` authenticated
    it as clean forever.

    Hashing the STAMP-FREE payload gives the two files something comparable. The
    result is written onto every freeze-log record (`goals_canon`) and into the
    manifest, so "was a target moved without an amendment?" becomes a hash
    comparison against an append-only, git-anchored file. Both keys are optional
    additions: a run frozen before they existed simply cannot be reconciled on
    this axis and is told so (a note), never accused.
    """
    if not isinstance(obj, dict):
        return None
    payload = {k: v for k, v in obj.items() if k not in GOALS_VOLATILE_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def _goals_canon_file(path):
    """_goals_canon of the goals.json at `path`, or None if it will not parse."""
    try:
        with open(path) as f:
            return _goals_canon(json.load(f))
    except (OSError, ValueError):
        return None


IGNORED_DIRS ={"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}


def is_artifact_path(rel):
    """True for tool-CACHE paths, which iter_files skips.

    check-branch needs the SAME filter freeze/verify use: running the frozen
    acceptance suite (which task-packets.md explicitly invites workers to do)
    writes .swarm-loop/acceptance/__pycache__/*.pyc, and without this the two
    halves of the same protection model disagree — verify says intact, while
    check-branch vetoes the branch as a harness tamper.

    P-6/H-9: this used to ALSO drop any *.pyc/*.pyo by SUFFIX, wherever it sat,
    and that was the one addition NEITHER integrity path could see. A sourceless
    .swarm-loop/acceptance/helpers.pyc — no helpers.py beside it — is imported by
    CPython's SourcelessFileLoader as an ordinary sibling module, so it changes
    what the frozen suite DOES while freeze never hashes it, verify's ADDED scan
    never flags it, and .gitignore's '*.py[co]' keeps it out of every diff.
    Keeping only the DIRECTORY rule preserves the worker flow (a legitimate
    __pycache__/*.pyc is still ignored by all three) and closes the hole (a .pyc
    outside any cache dir is now frozen by freeze, flagged ADDED by verify and
    vetoed by check-branch)."""
    parts = rel.replace("\\", "/").split("/")
    return any(p in IGNORED_DIRS for p in parts[:-1]) or parts[-1] in IGNORED_DIRS


class HarnessSymlink(Exception):
    """A symlink under a protected path. Fatal — see iter_files."""


def _symlink_msg(root, exc):
    path = str(exc)
    try:
        dest = os.readlink(path)
    except OSError:
        dest = "?"
    rel = os.path.relpath(path, root)
    if os.path.isdir(path):
        return (f"symlinked directory under a protected path: {rel} "
                f"-> {dest!r}. os.walk does not descend a symlinked directory, so freeze "
                "records ZERO hashes for that whole subtree and verify's ADDED scan cannot "
                "see behind it either — the acceptance tests it points at could be gutted "
                "with nothing noticing. Replace the symlink with the real files.")
    # P-14: the file case used to be followed SILENTLY. The manifest then claims
    # to protect an in-repo path while hashing bytes that live outside the repo,
    # where nothing on the branch shows an edit and restore-harness cannot
    # rewrite them from git either.
    return (f"symlinked FILE under a protected path: {rel} -> {dest!r}. freeze would "
            "hash bytes that live OUTSIDE the repo while the manifest claims to protect "
            "an in-repo path: the real bytes can be edited with nothing to see it, and "
            "restore-harness cannot restore what git never held. Replace the symlink "
            "with the real file.")


def iter_files(base):
    """Walk files under base, skipping tool CACHE DIRECTORIES — build artifacts
    are not tampering, and including them makes every real run a false positive.

    Bytecode OUTSIDE a cache directory is deliberately NOT skipped; see
    is_artifact_path for the sourceless-.pyc hole that closes.

    A symlink — directory or file — is a hard error, not a skip: it is invisible
    to both halves of the integrity check, so silently walking past one would
    hand back a manifest that protects less than it claims to."""
    if os.path.isfile(base) or os.path.islink(base):
        if os.path.islink(base):
            raise HarnessSymlink(base)
        yield base
        return
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in IGNORED_DIRS]
        for name in dirs:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                raise HarnessSymlink(p)
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                raise HarnessSymlink(p)
            yield p


# ------------------------------------------------------------------- init

def cmd_init(args):
    root = repo_root()
    d = sd(root)
    if os.path.exists(os.path.join(d, STATE_FILE)) and not args.force:
        die(f"{d} already initialized (use --force to reset state, keeping goals/history is NOT supported)")

    # --force on top of a prior run must not mix that run's rows into this one:
    # stale history/analysis made 'analyze' report a false "all targets met" at
    # cycle 1 in a real run. Archive them and start the new run clean.
    if args.force:
        hist = os.path.join(d, HISTORY_FILE)
        adir = os.path.join(d, ANALYSIS_DIR)
        rdir = os.path.join(d, "reports")
        stale_history = os.path.exists(hist) and sum(1 for _ in open(hist)) > 1
        analysis_files = sorted(os.listdir(adir)) if os.path.isdir(adir) else []
        # reports/ must be archived too, and final-report.md above all: resume's
        # highest-priority branch keys on that file, so a surviving one told a
        # brand-new run "this run is closed; start a new run with init --force" —
        # a recommendation with no exit, because --force did not remove it.
        report_files = sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []
        if stale_history or analysis_files or report_files:
            # Uniquify: two force-inits in the same second must not overwrite
            # each other's archives (exist_ok would silently merge them).
            base = os.path.join(d, "archive", "run-" + time.strftime("%Y%m%d-%H%M%S"))
            arch, n = base, 1
            while True:
                try:
                    os.makedirs(arch, exist_ok=False)
                    break
                except FileExistsError:
                    n += 1
                    arch = f"{base}-{n}"
            n_moved = 0
            if os.path.exists(hist):
                os.replace(hist, os.path.join(arch, HISTORY_FILE))
                n_moved += 1
            for name in analysis_files:
                os.replace(os.path.join(adir, name), os.path.join(arch, name))
                n_moved += 1
            if report_files:
                os.makedirs(os.path.join(arch, "reports"), exist_ok=True)
                for name in report_files:
                    os.replace(os.path.join(rdir, name), os.path.join(arch, "reports", name))
                    n_moved += 1
            # goals.json is COPIED, not moved: it is an input the user edits, and
            # the next run almost always starts from it. The archive keeps the
            # record of what the previous run was actually scored against.
            gsrc = os.path.join(d, GOALS_FILE)
            if os.path.exists(gsrc):
                with open(gsrc, "rb") as a, open(os.path.join(arch, GOALS_FILE), "wb") as b:
                    b.write(a.read())
                n_moved += 1
            print(f"archived {n_moved} stale run file(s) (history.csv + analysis/ + reports/ "
                  f"+ a copy of goals.json) to {os.path.relpath(arch, root)}; starting fresh history")
        # A new run must be freshly frozen. Leaving frozen=true + the previous
        # run's manifest in place makes this run's `freeze` look like a goalpost
        # move to the amendment gate below (which would refuse it outright), and
        # leaves 'verify' checking the PREVIOUS run's file hashes in the meantime.
        gpath = os.path.join(d, GOALS_FILE)
        mpath = os.path.join(d, MANIFEST_FILE)
        if os.path.exists(gpath):
            g = load_json(gpath, "goals")
            if g.get("frozen"):
                g["frozen"], g["frozen_at"] = False, None
                # amendment bookkeeping belongs to the run that made it
                g.pop("amendments", None)
                g.pop("last_amended_at", None)
                g.pop("last_amend_reason", None)
                save_json(gpath, g)
                print("goals un-frozen for the new run — re-run 'freeze' after confirming goals")
        if os.path.exists(mpath):
            os.replace(mpath, os.path.join(d, MANIFEST_FILE + ".prev-run"))
            print(f"previous run's manifest moved aside to {STATE_DIR}/{MANIFEST_FILE}.prev-run")
        flog = os.path.join(d, FREEZE_LOG_FILE)
        if os.path.exists(flog):
            os.replace(flog, os.path.join(d, FREEZE_LOG_FILE + ".prev-run"))
        acc = os.path.join(d, "acceptance")
        try:
            has_acceptance = os.path.isdir(acc) and any(True for _ in iter_files(acc))
        except HarnessSymlink:
            has_acceptance = True  # freeze will refuse and name it
        if has_acceptance:
            print(f"note: {STATE_DIR}/acceptance/ carries over UNCHANGED into the new run — "
                  "review it against the new goals before freezing (a stale acceptance test "
                  "re-freezes into this run's manifest and counts for the whole run)")
    # Where task worktrees live. SKILL.md Phase 4 puts them in a SIBLING
    # directory: a worktree INSIDE the repo sits inside every root-walking metric
    # command's scan, so the mandatory lint/type-error metric ends up counting
    # orchestration noise as product code. The in-tree path stays as the
    # documented fallback for an unwritable parent. It is recorded in state.json
    # rather than hard-coded in resume, so recovery never has to guess where a
    # given run actually put them.
    prev_state = {}
    spath_prev = os.path.join(d, STATE_FILE)
    if os.path.exists(spath_prev):
        try:
            with open(spath_prev) as f:
                prev_state = json.load(f)
        except (ValueError, OSError):
            prev_state = {}
    # --force resets the run but must NOT silently relocate its worktrees: an
    # explicit flag wins, else the previous run's choice, else the default.
    wt_root = args.worktree_root or prev_state.get("worktree_root")
    if not wt_root:
        parent = os.path.dirname(root) or root
        if os.access(parent, os.W_OK):
            wt_root = os.path.join(os.pardir, os.path.basename(root) + "-worktrees")
        else:
            wt_root = f"{STATE_DIR}/worktrees"
            print(f"note: {parent} is not writable — task worktrees fall back to the in-tree "
                  f"{STATE_DIR}/worktrees/; exclude it from every root-walking metric command")

    # Where each lane's PRIVATE scratch/temp directory lives: one subdirectory
    # per task id, <scratch-root>/<task-id>/. Optional, and written into
    # state.json only when it was actually chosen — a run that never names one
    # keeps exactly the state.json shape it had before this flag existed.
    #
    # Why the harness carries this at all. Every parallel agent in a wave used
    # to be handed the SAME scratch path, with nothing anywhere saying it was
    # shared. Two agents wrote a probe script at the same filename and one of
    # them executed it with ANOTHER agent's worktree as its working directory.
    # It died on a path that did not exist and no damage was done, which was
    # luck rather than design: the same collision against a live path is one
    # lane's script mutating another lane's tree, which is precisely what the
    # worktrees exist to prevent.
    #
    # And why the ROOT is recorded rather than merely handed out at dispatch:
    # so the subdivision survives a compaction or a resume. That is the same
    # failure class --worktree-root was added for — a run that never recorded
    # where its worktrees lived resumed, reported ZERO live worktrees, and
    # re-dispatched every in-flight ticket into a second set of trees.
    #
    # The honest limit, stated here because nothing downstream can state it:
    # this harness can store and print the path, and it can NEVER enforce that
    # a lane uses its private subdirectory. It never observes the inside of a
    # lane's turn. This is bookkeeping that survives a restart, not a guarantee.
    #
    # Carried across --force exactly like worktree_root: an explicit flag wins,
    # else the previous run's choice, else nothing at all is recorded.
    scratch_root = args.scratch_root or prev_state.get("scratch_root")
    scratch_abs = (os.path.normpath(os.path.join(root, scratch_root))
                   if scratch_root else None)
    if scratch_abs and (scratch_abs == root or scratch_abs.startswith(root + os.sep)):
        # WARN, never refuse — the operator may have no writable parent, and an
        # in-tree path still beats every lane sharing one directory. But it is
        # the expensive choice and the cost is not visible from the flag: metric
        # commands run with cwd = the repo root, so a scratch directory parked
        # inside the repo sits in every tree-walking command's search path. A
        # file-count metric once read 1, then 4, then 10 purely as worktrees
        # appeared, and 'analyze' reported that as a regression with no product
        # code having changed. Phase 2 freezes the metric commands' exclusions
        # at cycle 0, so a root chosen badly here cannot be excluded later.
        print(f"WARNING: --scratch-root {scratch_root} is INSIDE the measured repo "
              f"({os.path.relpath(scratch_abs, root)}). Metric commands run with cwd = the "
              "repo root, so it sits in every tree-walking command's search path: a "
              "file-count metric once read 1, then 4, then 10 purely as lane directories "
              "appeared, and 'analyze' scored that as a regression with no product code "
              "changed. Phase 2 freezes those exclusions at cycle 0, so this root cannot be "
              "excluded later — prefer a sibling directory outside the repo. Recorded anyway.")

    os.makedirs(os.path.join(d, ANALYSIS_DIR), exist_ok=True)
    os.makedirs(os.path.join(d, "reports"), exist_ok=True)
    os.makedirs(os.path.join(d, "acceptance"), exist_ok=True)

    # P-7: --force resets the RUN, it does not redesign the protection model.
    # This used to rebuild protected_paths from the two hard-coded defaults while
    # deliberately carrying worktree_root forward — so a second `init --force` in
    # a repo whose run had added Makefile, scripts/verify.sh and
    # scripts/check_verify_contracts.py silently unprotected all three: freeze
    # hashed fewer files, verify returned OK forever over a freely editable
    # verification script, and check-branch stopped vetoing it. Carry the list
    # forward exactly like worktree_root; --protect-path replaces it explicitly.
    #
    # D3: --protect-path REPLACED the list wholesale, mandatory members and all.
    # `init --protect-path src/` silently dropped .swarm-loop/goals.json, so the
    # frozen targets stayed freely editable for the whole run while verify,
    # measure and analyze went on asserting the harness was intact — the same
    # end state as a successful forgery, reached with no forgery at all, and
    # neither init nor freeze said a word. The mandatory two are unioned in
    # whatever the caller passes, and said out loud when they had to be added.
    default_protected = list(MANDATORY_PROTECTED)
    if args.protect_path:
        protected = list(dict.fromkeys(args.protect_path))
        forced = [p for p in default_protected if p not in protected]
        if forced:
            protected.extend(forced)
            print("note: --protect-path did not list " + ", ".join(forced)
                  + " — added, because it is not optional: it holds the targets and tests "
                    "this run is graded on, and a freeze that does not lock it leaves "
                    "'verify' asserting integrity over a harness anyone can edit")
    else:
        protected = list(prev_state.get("protected_paths") or default_protected)
        for p in default_protected:
            if p not in protected:
                protected.append(p)
    carried = [p for p in protected if p not in default_protected]
    if args.force and carried and not args.protect_path:
        print("carried this repo's extra protected path(s) forward into the new run: "
              + ", ".join(carried) + "  (--protect-path replaces the whole list)")

    state = {
        "project": args.project or os.path.basename(root),
        "max_cycles": args.max_cycles,
        "audit_loops": args.audit_loops,
        "audit_loops_completed": 0,
        "clean_audit": False,
        "current_cycle": 0,
        "autonomy": args.autonomy,
        "push_remote": args.push_remote,
        "stall_limit": args.stall_limit,
        "stall_count": 0,
        # The second stall counter, on the question the run is actually for.
        # Defaults to the same limit; both feed the same kill switch, and they
        # are separate because they measure different things — the codebase one
        # cannot see a pinned metric, and this one cannot see a dead loop that
        # still measures.
        "goal_stall_limit": args.goal_stall_limit or args.stall_limit,
        "goal_stall_count": 0,
        "head_history": [],
        "worktree_root": wt_root,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "protected_paths": protected,
    }
    # Optional and carried forward when already set: where the ticket graph
    # lives. Absent means "search the default locations" (see _load_graph), so
    # every run written before this key existed still works.
    graph = args.ticket_graph or prev_state.get("ticket_graph")
    if graph:
        state["ticket_graph"] = graph
    # Optional and carried forward when already set, exactly like ticket_graph:
    # ABSENT means "this run never recorded whether its lanes were subdivided",
    # which every reader has to be able to say out loud rather than guess at —
    # so nothing downstream may require the key, and every run written before
    # this flag existed keeps working untouched.
    if scratch_root:
        state["scratch_root"] = scratch_root
    save_json(os.path.join(d, STATE_FILE), state)

    goals_path = os.path.join(d, GOALS_FILE)
    if not os.path.exists(goals_path):
        save_json(goals_path, {"frozen": False, "frozen_at": None, "metrics": []})

    hist = os.path.join(d, HISTORY_FILE)
    if not os.path.exists(hist):
        with open(hist, "w", newline="") as f:
            csv.writer(f).writerow(HISTORY_HEADER)

    # Keep worktrees and archived stale runs out of the tree — and tool caches
    # too: running the frozen acceptance suite writes .pyc under a PROTECTED
    # path, which check-branch would otherwise read as a harness tamper.
    gi = os.path.join(root, ".gitignore")
    existing = open(gi).read() if os.path.exists(gi) else ""
    # .swarm-loop/worktrees/ is kept even under the sibling default: it is still
    # the correct entry for the in-tree fallback, and costs nothing otherwise.
    markers = [f"{STATE_DIR}/worktrees/", f"{STATE_DIR}/archive/",
               "__pycache__/", "*.py[co]", ".pytest_cache/", ".mypy_cache/"]
    wt_abs = os.path.normpath(os.path.join(root, wt_root))
    if wt_abs.startswith(root + os.sep):  # only a root git can actually see
        wt_rel = os.path.relpath(wt_abs, root).replace(os.sep, "/") + "/"
        if wt_rel not in markers:
            markers.append(wt_rel)
    # Same treatment for an in-repo scratch root. It does NOT undo the warning
    # above — git's ignore list is not a metric command's exclusion list — but
    # it keeps the lane directories out of `git status` and out of the codebase
    # fingerprint the stall counter is computed from.
    if scratch_abs and scratch_abs.startswith(root + os.sep):
        sc_rel = os.path.relpath(scratch_abs, root).replace(os.sep, "/") + "/"
        if sc_rel not in markers:
            markers.append(sc_rel)
    for marker in markers:
        if marker not in existing:
            with open(gi, "a") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + marker + "\n")
            existing += ("" if existing.endswith("\n") or not existing else "\n") + marker + "\n"

    print(f"initialized {d}")
    print(f"  project      : {state['project']}")
    print(f"  max cycles   : {state['max_cycles']}")
    print(f"  autonomy     : {state['autonomy']}")
    print(f"  push remote  : {state['push_remote']}")
    print(f"  stall limit  : {state['stall_limit']} cycles with an unchanged codebase")
    print(f"  goal stall   : {state['goal_stall_limit']} measured cycles with no improvement "
          "in any graded metric")
    print(f"  audit loops  : {state['audit_loops']} after the build converges")
    print("  protected    : " + ", ".join(state["protected_paths"]))
    print(f"next: define metrics in {STATE_DIR}/{GOALS_FILE} and acceptance tests in {STATE_DIR}/acceptance/, then run 'freeze'")


# --------------------------------------------------- metric command runner

def _kill_process_group(proc):
    """SIGTERM then SIGKILL the command's whole process group.

    SIGTERM first so a suite's fixtures get a chance to close DB connections and
    drop temp schemas; SIGKILL after a short grace for anything that ignores it.
    Falls back to signalling just the direct child if the group is already gone.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    # Never signal our own group: that would kill the orchestrator too. Only
    # reachable if start_new_session somehow failed to take effect.
    if pgid is None or pgid == os.getpgid(0):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                proc.send_signal(sig)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        pass


def _run_metric_command(command, cwd, timeout):
    """Run one metric command under a timeout, killing the WHOLE process group on expiry.

    subprocess.run(shell=True, timeout=...) signals only the DIRECT child. A
    simple command is exec-optimized by the shell, so the child IS the runner and
    dies correctly — but a NON-simple command (a pipeline, 'a; b', '( ... )',
    'a && b') leaves /bin/sh as the child and the actual test suite as a
    grandchild, which survives the timeout as an orphan (reparented to pid 1)
    still holding postgres/neo4j/redis connections and contaminating the NEXT
    metric's reading. start_new_session=True puts the command in its own process
    group so one killpg reaps the whole tree.

    That same detachment removes the command from the terminal's foreground
    process group, so a Ctrl-C at the orchestrator no longer reaches it — hence
    the bare `except BaseException`, which catches KeyboardInterrupt too and
    reaps the group before re-raising. Without it an interrupted 'measure' would
    leak the entire tree forever: strictly worse than the bug being fixed.

    Returns a CompletedProcess; raises subprocess.TimeoutExpired (after the kill)
    exactly as subprocess.run did, so callers are unchanged.
    """
    proc = subprocess.Popen(
        command, shell=True, cwd=cwd, text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:  # TimeoutExpired, KeyboardInterrupt, anything
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(command, proc.returncode, out, err)


def _metric_timeout(m, default):
    """Per-metric 'timeout' override; the CLI flag is the default for the rest.

    Never fatal at measure time — a malformed field must not discard the rows of
    every metric that already ran this epoch. freeze validates it properly.
    """
    t = m.get("timeout")
    if t is None:
        return default
    try:
        t = int(t)
        if t <= 0:
            raise ValueError
    except (TypeError, ValueError):
        print(f"  {m.get('id', '?'):<28} WARNING: bad 'timeout' field ({t!r}); using {default}s")
        return default
    return t


# ------------------------------------------------------- freeze/git helpers

def _git(root, *a, **kw):
    """git in the repo. binary=True returns bytes (blob content must not be decoded)."""
    binary = kw.pop("binary", False)
    if kw:
        raise TypeError("unexpected kwargs: %r" % sorted(kw))
    return subprocess.run(["git", *a], capture_output=True, cwd=root, text=not binary)


def _protected_hashes(root, state):
    """({relpath: sha256}, [protected paths that do not exist]).

    P-9: the missing list used to be a silent `continue`. A protected path not
    yet authored at freeze time (scripts/verify.sh written in cycle 1, say) was
    then unprotected for the WHOLE run — freeze reported "locked", verify's ADDED
    scan skipped the absent directory, and check-branch went on vetoing the path
    by prefix, so the two halves of the protection model disagreed about a file
    the whole verification contract rested on. The caller decides what to do; it
    must not be invisible."""
    files, missing = {}, []
    try:
        for rel in state["protected_paths"]:
            base = os.path.join(root, rel)
            if not os.path.exists(base) and not os.path.islink(base):
                missing.append(rel)
                continue
            for path in iter_files(base):
                files[os.path.relpath(path, root)] = sha256_file(path)
    except HarnessSymlink as e:
        # Fatal, not skippable: hashing around it would produce a manifest that
        # protects less than it claims to, and nothing downstream would know.
        die(_symlink_msg(root, e))
    return files, missing


def _graph_protected_rel(root, state):
    """(rel, outside) — the ticket graph's path for protected_paths, or (None, None).

    G-1: the ticket graph was the LAST UNGUARDED INPUT TO A GATE. `red-check`
    exempts a ticket from the must-go-red requirement when the graph says
    `bootstrap: true`, and it reads that field from the graph — so a worker could
    add `bootstrap: true` to its OWN ticket and exempt itself from the gate that
    would have caught its defect.

    The git-guard cannot close this and it was right not to try. The canonical
    graph is repo-root `tickets.json`, OUTSIDE `.swarm-loop/` entirely, and the
    guard's ledger rule only ever fires on a name under the state dir; a
    `LEDGER_NAMES` entry would have covered the `.swarm-loop/tickets.json`
    variant alone — half a rule that reads as a whole one, which is worse than
    none.

    So this is the answer the skill already found once, for the frozen
    acceptance suite: tamper-EVIDENCE, not prevention. The suite is editable on
    purpose (the guard deliberately does not block `Edit` there) and is held by
    manifest hashing — `verify` fails if a byte moved, and the sanctioned way to
    move one is `freeze --amend`, which is logged, prints old -> new hashes, and
    is a user-awake action. The graph gets the same treatment for the same
    reason: the orchestrator legitimately amends it (and `findings` mints into
    it every audit loop), so BLOCKING is wrong; an unhashed input to a gate is a
    gate a worker can edit around, so SILENCE is wrong too.

    `outside` is the relpath of a graph that lives outside the repo, which
    cannot be protected and must not be pretended into the manifest: freeze
    would hash bytes git never held, no branch diff could show an edit to them,
    and restore-harness could not repair one. Reported loudly by the caller
    rather than protected wrongly.

    A run with no graph — greenfield, or plain-description intake — returns
    (None, None) and every caller is a silent no-op, exactly like the read gate.
    """
    p, _tickets = _load_graph(root, state, None, required=False)
    if not p:
        return None, None
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + "/"):
        return None, rel
    return rel, None


def _reseal_protected(root, d, rel, old_digest, new_digest, action, reason, extra=None):
    """Re-hash ONE protected path into the manifest and record the delta in the
    append-only freeze log. Returns the new log seq.

    This is how a SANCTIONED write to a protected file that is not a goalpost
    move gets recorded — `findings` minting tickets into the frozen ticket
    graph, and nothing else so far. It is deliberately the same machinery
    `freeze --amend` uses, so the reconciliation in _authenticate_manifest keeps
    working with no special case: the record is an ordinary forward delta, so
    _replay_freeze_log picks the new digest up, and

      * `frozen_at` is the FREEZE'S OWN timestamp, never `now()`. This record is
        a delta INSIDE that freeze, not a new freeze; _freeze_log_match_index
        resolves a manifest to a record by (frozen_at, seq), so a fresh
        timestamp here would leave the manifest naming a freeze that never
        happened and `verify` printing [UNLOGGED] over a clean tree.
      * `log_seq` moves with it, so that same lookup lands on THIS record and
        the replay runs through it rather than stopping one short.
      * `action` is NOT "initial" (replay resets on those) and there is no
        `goals_canon` key, so the goals digest in force carries forward
        untouched — this says nothing about goals.json, because it changed
        nothing about goals.json.
      * `amendments` is NOT incremented. A minted ticket is not a moved
        goalpost, and inflating that counter would put "GOALPOSTS MOVED" in
        every report of every audit loop until the word meant nothing.
    """
    mpath = os.path.join(d, MANIFEST_FILE)
    manifest = load_json(mpath, "manifest")
    recs, _exists = _freeze_log_records(d)
    seq = len(recs)
    files = dict(manifest.get("files") or {})
    files[rel] = new_digest
    manifest["files"] = files
    manifest["log_seq"] = seq
    save_json(mpath, manifest)
    rec = {
        "action": action,
        "amendment": int(manifest.get("amendments", 0) or 0),
        "reason": reason,
        "frozen_at": manifest.get("frozen_at"),
        "prev_frozen_at": manifest.get("frozen_at"),
        "seq": seq,
        "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
        "modified": [{"path": rel, "old": old_digest, "new": new_digest}],
        "added": [],
        "removed": [],
    }
    rec.update(extra or {})
    _append_freeze_log(d, rec)
    return seq


def _dupe_metric_ids(goals, warn_only=False):
    """Metric ids used more than once. P-14: nothing rejected them, and the
    consequences compound silently — `measure` appends a row per definition,
    `analyze`'s by_cycle[c] keeps only the LAST, so one metric becomes invisible
    while `n_at_target == len(results)` still counts it as met. A metric nobody
    can see, inside the count that decides ALL TARGETS MET."""
    seen, dupes = set(), []
    for m in goals.get("metrics", []):
        mid = m.get("id")
        if mid in seen and mid not in dupes:
            dupes.append(mid)
        seen.add(mid)
    if dupes and warn_only:
        print("WARNING: duplicate metric id(s) in %s/%s: %s — only the LAST definition of "
              "each is scored, and the others are invisible in the verdict count. Fix "
              "goals.json and re-freeze with --amend." % (STATE_DIR, GOALS_FILE, ", ".join(dupes)))
    return dupes


def _manifest_delta(old, new):
    """(added, removed, changed) between two {relpath: sha256} maps."""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(r for r in (set(old) & set(new)) if old[r] != new[r])
    return added, removed, changed


def _commit_with_blob(root, rel, digest, limit=None):
    """Newest commit reachable from HEAD whose blob for <rel> hashes to <digest>.

    Deliberately NOT the freeze-time HEAD: .swarm-loop/ is committed *after*
    freeze + baseline (goal-setting.md, "Baseline"), so the freeze-time commit
    does not contain the frozen files at all and recording it would be useless.
    Matching on the manifest digest finds the right commit regardless — and
    proves the bytes it holds are the frozen ones rather than assuming it.

    `limit` caps how many commits are opened. restore-harness wants the whole
    history (it is a repair, run once); the verify-time anchoring check passes a
    cap because it runs on every measure and its answer is advisory."""
    p = _git(root, "rev-list", "HEAD", "--", rel)
    if p.returncode != 0:
        return None
    commits = p.stdout.split()
    if limit is not None:
        commits = commits[:limit]
    for commit in commits:
        blob = _git(root, "cat-file", "blob", commit + ":" + rel, binary=True)
        if blob.returncode != 0:
            continue
        if hashlib.sha256(blob.stdout).hexdigest() == digest:
            return commit, blob.stdout
    return None


def _print_unified(root, rel, old_digest, new_bytes=None, indent="      "):
    """Unified diff frozen-bytes -> on-disk bytes, when the frozen bytes are
    recoverable from git and both sides are text. Hashes alone otherwise."""
    found = _commit_with_blob(root, rel, old_digest)
    if not found:
        print(indent + "(frozen bytes not found in git history — hash change only;"
                       " was the harness ever committed?)")
        return
    _commit, blob = found
    try:
        old_text = blob.decode()
        if new_bytes is None:
            with open(os.path.join(root, rel), "rb") as f:
                new_bytes = f.read()
        new_text = new_bytes.decode()
    except (UnicodeDecodeError, OSError):
        print(indent + "(binary or unreadable — hash change only)")
        return
    diff = list(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                     fromfile="frozen/" + rel, tofile="ondisk/" + rel))
    for line in diff[:200]:
        print(indent + line.rstrip("\n"))
    if len(diff) > 200:
        print(indent + "... (%d more diff lines)" % (len(diff) - 200))


def _append_freeze_log(d, rec):
    with open(os.path.join(d, FREEZE_LOG_FILE), "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _freeze_log_records(d):
    """(records, log_exists). Malformed lines are skipped, never fatal."""
    path = os.path.join(d, FREEZE_LOG_FILE)
    if not os.path.exists(path):
        return [], False
    recs = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    recs.append(rec)
    except OSError:
        return [], True
    return recs, True


def _freeze_log_state(d, frozen_at):
    """'logged' | 'not-logged' | 'no-log' — how freeze-log.jsonl answers for this freeze.

    freeze writes the manifest, renames goals.json into place, and only then
    appends its log line — so a manifest whose frozen_at never reached the log is
    the signature of a freeze that died mid-write (resume's 'a freeze died
    between the two writes' branch). Nothing was ever locked in that state, and
    re-running freeze is the documented repair, so the goalpost gate lets exactly
    that case through.

    P-3: the three states used to collapse into one boolean that returned False
    when the log FILE WAS ABSENT — i.e. it failed OPEN on the single condition an
    attacker controls most easily. `rm .swarm-loop/freeze-log.jsonl` was the
    whole of step 2 in a four-step laundering sequence. A missing log over a
    manifest that records a freeze is not evidence of a crash; it is the
    destruction of the only permanent record that a goalpost ever moved, and it
    is now reported as its own violation."""
    if not frozen_at:
        return "not-logged"
    recs, exists = _freeze_log_records(d)
    if not exists:
        return "no-log"
    for rec in recs:
        if rec.get("frozen_at") == frozen_at:
            return "logged"
    return "not-logged"


def _staged_goals_ok(d):
    """Is goals.json.tmp REALLY the staged goals of a freeze that died mid-write?

    A2/P-3: the crash hatch used to accept the mere EXISTENCE of the file, so
    `touch .swarm-loop/goals.json.tmp` was step 4 of laundering a tamper. freeze
    writes that file with json.dump of a goals object that already has
    frozen=True and frozen_at stamped, so requiring it to parse and to look like
    that costs an attacker the whole shortcut and costs a genuine crash nothing."""
    path = os.path.join(d, GOALS_FILE) + ".tmp"
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            staged = json.load(f)
    except (OSError, ValueError):
        return False
    return (isinstance(staged, dict) and isinstance(staged.get("metrics"), list)
            and bool(staged.get("metrics")) and bool(staged.get("frozen"))
            and bool(staged.get("frozen_at")))


def _freeze_log_match_index(recs, manifest):
    """Index of the freeze-log record this manifest was written alongside.

    D2: the replay used to stop at the FIRST record whose `frozen_at` equalled
    the manifest's, and `frozen_at` is `%H:%M:%S` — ONE-SECOND resolution. Two
    freezes inside one wall-clock second (a scripted `freeze` then `freeze
    --amend`, or any repair that does both) and the replay stopped on the wrong
    record: every digest the second freeze recorded was missing from the
    expected map, so `verify` printed [FORGED] over a perfectly clean tree —
    permanently, and with no command that could clear it.

    Identity is the whole record now, not the timestamp. `seq` is the monotonic
    position freeze stamps into BOTH the manifest (`log_seq`) and the record, so
    it settles the question exactly when both sides carry it; the amendment
    counter settles it for a manifest written before `seq` existed; and the
    tie-break of last resort is the LAST record sharing the timestamp rather
    than the first, because the manifest on disk is the one the last freeze
    wrote. Every key consulted is optional — a pre-change manifest still
    resolves. Returns None when the log contains no record for this freeze at
    all, which is the [UNLOGGED] violation and not a tie to break.
    """
    frozen_at = manifest.get("frozen_at")
    cands = [i for i, r in enumerate(recs) if r.get("frozen_at") == frozen_at]
    if not cands:
        return None
    seq = manifest.get("log_seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        exact = [i for i in cands if recs[i].get("seq") == seq]
        if exact:
            return exact[-1]
    amd = manifest.get("amendments")
    if amd is not None:
        exact = [i for i in cands if recs[i].get("amendment") == amd]
        if exact:
            return exact[-1]
    return cands[-1]


def _replay_freeze_log(recs, upto_index=None):
    """{relpath: sha256} the freeze log says should be frozen, replayed forward.

    Each record is a delta (`added`/`modified`/`removed` with their new hashes);
    an `initial` record starts a run, so it resets the map rather than merging
    into a previous run's (init --force rotates the log aside, but a hand-merged
    one must not silently inherit). Stops AFTER `upto_index` (inclusive) — a
    manifest restored from an older commit must reconcile against the log AS OF
    ITS OWN FREEZE, not against later amendments. The caller resolves that index
    with _freeze_log_match_index; it used to be resolved here, by timestamp
    alone, which is D2."""
    exp = {}
    for i, rec in enumerate(recs):
        if upto_index is not None and i > upto_index:
            break
        if rec.get("action") == "initial":
            exp = {}
        for e in rec.get("added", []) or []:
            if isinstance(e, dict) and e.get("path"):
                exp[e["path"]] = e.get("new")
        for e in rec.get("modified", []) or []:
            if isinstance(e, dict) and e.get("path"):
                exp[e["path"]] = e.get("new")
        for e in rec.get("removed", []) or []:
            if isinstance(e, dict) and e.get("path"):
                exp.pop(e["path"], None)
    return exp


def _replay_goals_canon(recs, upto_index=None):
    """The canonical goals digest in force at `upto_index`, or None if unlogged.

    Carried forward like the file map: `goals_canon` is stamped on EVERY record
    (not only the ones whose delta happens to include goals.json), because an
    amendment can move a target without goals.json appearing in the delta at
    all — freeze re-stamps and re-hashes it after the delta is computed. Records
    written before this key existed return None, which the caller reports as
    "cannot be reconciled", never as a tamper."""
    canon = None
    for i, rec in enumerate(recs):
        if upto_index is not None and i > upto_index:
            break
        if rec.get("action") == "initial":
            canon = None
        if rec.get("goals_canon"):
            canon = rec["goals_canon"]
    return canon


def _anchor_initial(recs, upto_index=None):
    """The `initial` record that starts the run the manifest at `upto_index`
    belongs to, or None if the log has none (a log written before `action`
    existed — BC, and the caller must skip its checks rather than accuse).

    This is the record every replay resets on, so it is the record that DEFINES
    what "frozen" means for that manifest. Whoever writes it writes the whole
    protected map."""
    end = len(recs) - 1 if upto_index is None else upto_index
    for i in range(min(end, len(recs) - 1), -1, -1):
        if recs[i].get("action") == "initial":
            return recs[i]
    return None


def _log_git_check(root, d, recs, anchor=None):
    """Was freeze-log.jsonl REWRITTEN since it was last committed — and is the
    record the whole freeze hangs from actually IN that committed copy?

    The log is documented as append-only in five places and nothing enforced it.
    Git does: the newest committed copy must be a PREFIX of the copy on disk.
    Compared as records rather than bytes so a trailing-newline difference is not
    an accusation.

    A prefix rule alone is not enough, and that gap was the whole forgery.
    APPENDING preserves the prefix, and replay RESETS on `action: "initial"` —
    so one appended `initial` record, plus a manifest edited to agree with it,
    redefines every frozen digest and every target while the committed prefix
    stays untouched. Reproduced end to end: a gutted acceptance test and a goal
    moved 100 -> 0 both verified as "OK — frozen harness intact". So the anchor
    `initial` record must be a MEMBER of the committed copy, not merely
    compatible with it. Continuation says nothing about the record that starts
    the run; membership does.

    Returns (violation | None, note | None). A committed copy that does not even
    START this run — the previous run's log, left behind because `init --force`
    rotates the file aside — is a NOTE, not a violation: there is nothing
    append-only about two different runs. A log that has never been committed at
    all stays a NOTE too (that is the normal state between `freeze` and the
    baseline commit), so the membership assertion only ever runs where git has
    something to say."""
    rel = os.path.relpath(os.path.join(d, FREEZE_LOG_FILE), root)
    p = _git(root, "rev-list", "-n", "1", "HEAD", "--", rel)
    if p.returncode != 0 or not p.stdout.split():
        return None, ("the freeze log has never been committed — git cannot corroborate "
                      "it; commit %s with the baseline (goal-setting.md, 'Baseline')" % rel)
    commit = p.stdout.split()[0]
    blob = _git(root, "cat-file", "blob", commit + ":" + rel, binary=True)
    if blob.returncode != 0:
        return None, None
    committed = []
    for line in blob.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            committed.append(json.loads(line))
        except ValueError:
            continue
    if not committed:
        return None, None
    first_disk = recs[0].get("frozen_at") if recs else None
    if not any(c.get("frozen_at") == first_disk for c in committed):
        return None, ("the committed freeze log (%s) is from an earlier run — this run's "
                      "log is not yet in git" % commit[:10])
    start = next(i for i, c in enumerate(committed) if c.get("frozen_at") == first_disk)
    tail = committed[start:]
    if len(tail) > len(recs) or any(
            json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)
            for a, b in zip(tail, recs)):
        return (f"[REWRITTEN] {rel} — the append-only freeze log on disk is NOT a "
                f"continuation of the copy committed at {commit[:10]}. Records that were "
                "committed have been altered or removed; that is the permanent record of "
                "every moved goalpost. Recover it with "
                f"`git show {commit[:10]}:{rel}`"), None
    if anchor is not None:
        want = json.dumps(anchor, sort_keys=True)
        if not any(json.dumps(c, sort_keys=True) == want for c in committed):
            return (f"[FORGED] {rel} — the `initial` record this freeze hangs from is NOT "
                    f"in the copy committed at {commit[:10]}; it was APPENDED afterwards. "
                    "Replay resets on `initial`, so that one record redefines every frozen "
                    "digest and every target — appending it preserves the committed prefix "
                    "while replacing the entire harness it is supposed to attest to. "
                    f"Recover the real log with `git show {commit[:10]}:{rel}` and restore "
                    "the frozen bytes with 'restore-harness'."), None
    return None, None


def _authenticate_manifest(root, d, manifest, disk_ok):
    """(violations, notes) — is manifest.json itself telling the truth?

    P-2/H-31: manifest.json defines what "frozen" means and nothing authenticated
    it. It is a plain unsigned JSON file, it is not among the digests it carries,
    and both detection paths took its word. Weaken a frozen test, recompute that
    one digest into the manifest, and `verify` printed "OK — frozen harness
    intact" forever: no freeze-log line, `amendments` unchanged,
    resume/status/analyze all endorsing a clean run.

    Two independent files must now agree. freeze-log.jsonl records every digest
    it ever froze, is append-only, and is committed — so the manifest is
    reconciled against the log (HARD), and the log against git (HARD).

    `disk_ok` is the set of paths whose bytes on disk already matched the
    manifest. Only those get the git-anchoring walk: a mismatch is caught by the
    cheap byte comparison, so the git walk is the CONFIRMATION step, not the
    primary one (H-31's own wording).

    A-7 TRAP, and it is a real one: the freeze log's RAW goals.json hash can
    never equal the manifest's, because frozen_at/amendments are stamped into
    goals.json after the delta that the log records is computed, and an
    amendment that moves a target need not put goals.json in that delta at all.
    Reconciling the raw digests reports a tamper on a clean tree — verified
    against the live ProxyShop run, where every one of the other 15 files agrees
    and goals.json alone differs.

    D1: the old answer was to skip goals.json entirely, which excluded the ONE
    file holding every target from the check built to protect targets. Edit a
    target, recompute that digest into manifest.json, and this returned clean.
    The raw digest stays excluded — it is genuinely incomparable — and the
    CANONICAL digest (_goals_canon: the payload with freeze's own stamps
    removed) is reconciled in its place. It is recorded on every freeze-log
    record and in the manifest, so a moved target now has to survive a
    comparison against an append-only, git-anchored file.
    """
    violations, notes = [], []
    frozen_at = manifest.get("frozen_at")
    recs, exists = _freeze_log_records(d)
    logrel = f"{STATE_DIR}/{FREEZE_LOG_FILE}"
    if not exists:
        violations.append(
            f"[MISSING] {logrel} — the manifest records a freeze at {frozen_at} but the "
            "append-only freeze log that must corroborate it is GONE. That log is the only "
            "permanent record of every goalpost this run moved; its absence is a violation, "
            "not a missing nicety. Recover it from git (`git show <commit>:" + logrel + "`).")
        return violations, notes
    # One run, one `initial`. Replay resets on `action: "initial"` and NOTHING
    # counted them, so a second one appended under the SAME frozen_at silently
    # became the definition of the freeze — the map, the goals digest, all of
    # it — with the original still sitting above it in the log looking honest.
    # `init --force` rotates the log aside rather than appending, so two
    # `initial` records sharing one timestamp are never something freeze wrote.
    dup = sorted(set(
        r.get("frozen_at") for r in recs
        if r.get("action") == "initial"
        and sum(1 for x in recs
                if x.get("action") == "initial"
                and x.get("frozen_at") == r.get("frozen_at")) > 1))
    if dup:
        violations.append(
            "[FORGED] %s — %d `initial` record(s) share the freeze timestamp %s. A run has "
            "exactly one, and replay RESETS the whole protected map on each one, so the "
            "later record silently redefines every frozen digest and every target while "
            "the earlier one stays in the log looking untouched. One of these was appended; "
            "recover the log from git and restore the frozen bytes with 'restore-harness'."
            % (logrel,
               sum(1 for r in recs
                   if r.get("action") == "initial" and r.get("frozen_at") in dup),
               ", ".join(str(x) for x in dup)))

    idx = _freeze_log_match_index(recs, manifest)
    if idx is None:
        violations.append(
            f"[UNLOGGED] {STATE_DIR}/{MANIFEST_FILE} claims a freeze at {frozen_at} that "
            f"appears nowhere in {logrel}. Either the manifest was rewritten by something "
            "other than 'freeze', or the log was truncated. Scores against it are void.")
        return violations, notes

    expected = _replay_freeze_log(recs, idx)
    grel = os.path.relpath(os.path.join(d, GOALS_FILE), root)

    # --- goals.json, the file that holds every target (D1) -------------------
    # Its RAW digest is incomparable (see the docstring), so the canonical
    # stamp-free digest is what gets reconciled. When goals.json's bytes already
    # matched the manifest, the bytes on disk ARE what the manifest attests to,
    # so hashing them canonically answers the question directly and cannot be
    # dodged by also editing the manifest's own goals_canon. When they did not
    # match, the byte comparison has already reported it and the manifest's
    # recorded value is the best available witness.
    want_canon = _replay_goals_canon(recs, idx)
    if grel in disk_ok:
        got_canon = _goals_canon_file(os.path.join(root, grel))
    else:
        got_canon = manifest.get("goals_canon")
    if want_canon and got_canon and want_canon != got_canon:
        violations.append(
            f"[FORGED] {grel} — the goals in force ({got_canon[:16]}…) are NOT the goals "
            f"{logrel} recorded at freeze time ({want_canon[:16]}…). A target, metric or "
            "command was changed and the manifest was rewritten to match it, with no "
            "amendment on the append-only record. Every score measured against these goals "
            "is void; restore goals.json, or record the change with "
            "'freeze --amend \"<reason>\"'.")
    elif want_canon is None and manifest.get("files", {}).get(grel):
        notes.append(
            "%s cannot be reconciled against %s — this run was frozen before the canonical "
            "goals digest existed, so a moved target is caught only by %s's own digest. The "
            "next 'freeze --amend' records it and closes the gap."
            % (grel, logrel, MANIFEST_FILE))

    for rel in sorted(set(expected) | set(manifest.get("files", {}))):
        if rel == grel:
            continue                      # raw digest incomparable; canon above
        want, got = expected.get(rel), manifest.get("files", {}).get(rel)
        if want == got:
            continue
        if got is None:
            violations.append(
                f"[DROPPED] {rel} — {logrel} froze this file and the manifest no longer "
                "lists it, with no 'removed' record. A file was taken out of protection "
                "without the amendment that authorizes it.")
        elif want is None:
            violations.append(
                f"[UNLOGGED] {rel} — the manifest protects it but no freeze record ever "
                "added it. The manifest was written by something other than 'freeze'.")
        else:
            violations.append(
                f"[FORGED] {rel} — the manifest's digest ({got[:16]}…) is not the one "
                f"{logrel} recorded at freeze time ({want[:16]}…). The manifest was edited "
                "to match altered bytes; that is the P-2 tamper this check exists for.")

    v, n = _log_git_check(root, d, recs, _anchor_initial(recs, idx))
    if v:
        violations.append(v)
    if n:
        notes.append(n)

    # Advisory: are the frozen bytes recoverable at all? Not a violation — at
    # freeze time .swarm-loop/ is legitimately uncommitted (the baseline commit
    # comes after), and making this fatal would refuse every freshly frozen run.
    #
    # goals.json is excluded, and that is the fix for a real defect: freeze
    # REWRITES it (frozen_at, amendments) as the last act of every freeze, so
    # its current bytes cannot be in history until someone commits AFTER the
    # freeze. It was therefore named by this advisory on every freshly frozen
    # and every freshly amended run — a permanent false positive on the very
    # file whose real anchoring failure matters most, which trains operators to
    # scroll past the one line that is ever going to be true. Its integrity is
    # carried by the manifest digest and by the canonical reconciliation above;
    # restore-harness still finds it in history once the run is committed.
    anchor_pool = [rel for rel in sorted(disk_ok) if rel != grel]
    unanchored = [rel for rel in anchor_pool
                  if _commit_with_blob(root, rel, manifest["files"][rel], limit=40) is None]
    if unanchored:
        # "commit .swarm-loop/" was the whole of this advice, and G-1 made it
        # wrong: the ticket graph is a protected path and its canonical home is
        # repo-root tickets.json, so the one command the note prints does not
        # cover the file the note is complaining about. It names the outside
        # paths too now — an unrecoverable file is exactly the case where the
        # operator follows the printed remedy literally.
        outside = sorted(set(r for r in unanchored
                             if not (r == STATE_DIR or r.startswith(STATE_DIR + "/"))))
        notes.append("%d of %d frozen file(s) are not in git history yet — commit %s so "
                     "'restore-harness' can repair a tamper (first: %s)"
                     % (len(unanchored),
                        len([r for r in manifest.get("files", {}) if r != grel]),
                        ", ".join([STATE_DIR + "/"] + outside), unanchored[0]))
    return violations, notes


# ------------------------------------------------------------- escalations
#
# A freeze is a LOCKED DOOR, not a wall. This is the knocker.
#
# The guidance this replaces treated a frozen thing that turns out to be wrong as
# a dead end — "note it and stop" — which in practice bought the two worst
# outcomes available: a loop that kept scoring against a target it had already
# concluded was broken, or one that quietly routed around it. Neither ever
# reached the one person allowed to fix it. An escalation is the third option:
# surface it, loudly, with a specific proposed change, and wait.
#
# Everything in this section is deliberately INERT. escalations.jsonl is
# append-only and sits INSIDE .swarm-loop/ but OUTSIDE the protected paths.
# Inside, because check-branch's existing swarm_hits veto then refuses any worker
# branch that writes one — workers keep the hard wall and report through their
# packet's NEEDS channel, and the anti-gaming property is untouched. Outside,
# because filing one must not itself read as an integrity violation.

ESCALATION_KINDS = ("frozen-goal", "frozen-test", "harness", "blocked-ticket", "other")


def _oneline(s, limit=200):
    """Collapse a reason/proposal onto one line for a summary block."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


def _esc_path(d):
    return os.path.join(d, ESCALATIONS_FILE)


def _load_escalations(d):
    """Fold the append-only log into current records, oldest first.

    Every run that exists today predates this file, so a missing or unreadable
    log is an EMPTY LIST and never an error: no pre-escalation command may start
    failing because a run never filed one. A malformed line is skipped for the
    same reason — a half-written record must not take down 'resume', which is the
    command that has to work when everything else is broken.

    'resolve' and 'block' are recorded as their own appended lines rather than by
    rewriting an earlier one. The log keeps the freeze-log's property: what was
    asked, and what was decided, both stay on the record forever.
    """
    path = _esc_path(d)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []
    recs, order = {}, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        rid, action = rec["id"], rec.get("action", "open")
        if action == "open":
            if rid not in recs:
                order.append(rid)
            base = dict(rec)
            base.setdefault("status", "open")
            recs[rid] = base
        elif rid in recs:
            cur = recs[rid]
            if action == "resolve":
                cur["status"] = "resolved"
                cur["resolved_at"] = rec.get("timestamp")
                cur["resolved_cycle"] = rec.get("cycle")
                cur["resolution_note"] = rec.get("note")
            elif action == "withdraw":
                # Closed, but NOT decided. status is "resolved" so every reader
                # that already asks "is this still open?" keeps working
                # unchanged; the `withdrawn` flag is what stops it ever being
                # rendered as an answer the user never gave.
                cur["status"] = "resolved"
                cur["withdrawn"] = True
                cur["resolved_at"] = rec.get("timestamp")
                cur["resolved_cycle"] = rec.get("cycle")
                cur["resolution_note"] = rec.get("note")
            elif action == "block":
                cur["blocking"] = True
                cur["blocked_at"] = rec.get("timestamp")
    return [recs[i] for i in order]


def _open_escalations(d):
    return [r for r in _load_escalations(d) if r.get("status") != "resolved"]


def _blocking_escalations(d):
    return [r for r in _open_escalations(d) if r.get("blocking")]


def _append_escalation(d, rec):
    with open(_esc_path(d), "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _next_esc_id(records):
    """ESC-001, ESC-002, ... Highest id on file + 1.

    Deliberately not len()+1: the log holds 'resolve' and 'block' lines too, so
    counting lines would hand the next escalation an id that is already in use
    and already quoted in a cycle report."""
    n = 0
    for r in records:
        m = re.match(r"^ESC-(\d+)$", str(r.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return "ESC-%03d" % (n + 1)


def _norm_esc_id(raw):
    """Accept ESC-007, esc-7 or 7. This id is typed by a human reading
    ESCALATIONS.md, usually at the wrong end of a long night."""
    m = re.match(r"^(?:ESC[-_ ]?)?(\d+)$", str(raw).strip().upper())
    return "ESC-%03d" % int(m.group(1)) if m else None


def _esc_grant_commands(rec):
    """(preamble, [commands]) — exactly what the USER runs to grant this request.

    The mechanism differs by kind, but every path ends at the same two facts:
    'freeze --amend' is the only thing that alters a frozen object, and the
    escalation stays open until somebody records a decision on it."""
    rid = rec.get("id")
    kind = rec.get("kind")
    subject = rec.get("subject") or "<subject>"
    resolve = 'swarmloop.py escalate --resolve %s --note "<what you decided>"' % rid
    if kind == "frozen-goal":
        return ('edit the "%s" metric in %s/%s, then:' % (subject, STATE_DIR, GOALS_FILE),
                ['swarmloop.py freeze --amend "<why you approved this goal change>"', resolve])
    if kind in ("frozen-test", "harness"):
        return ("edit %s, then re-lock the harness around the new bytes:" % subject,
                ['swarmloop.py freeze --amend "<why you approved this harness change>"', resolve])
    return ("decide, then record the decision (nothing frozen moves on its own):", [resolve])


def _print_escalation(rec, prefix=""):
    print(prefix + str(rec.get("id"))
          + ("  [BLOCKING]" if rec.get("blocking") else "")
          + "  %s \u00b7 %s" % (rec.get("kind"), rec.get("subject")))
    print(prefix + "   filed    : %s (cycle %s, HEAD %s)"
          % (rec.get("timestamp"), rec.get("cycle"), (rec.get("head") or "\u2014")[:10]))
    print(prefix + "   wrong    : " + _oneline(rec.get("reason")))
    print(prefix + "   proposed : " + _oneline(rec.get("proposed")))
    pre, cmds = _esc_grant_commands(rec)
    print(prefix + "   grant it : " + pre)
    for c in cmds:
        print(prefix + "              " + c)


def _print_ask(rec, prefix=""):
    """One open ask in its end-of-turn form: what was asked, about what, what
    change was proposed, when, whether it blocks, and the command that closes it.

    Deliberately shorter than _print_escalation, which spells out the whole grant
    recipe. This block prints at the end of EVERY user-facing turn, and a block
    that outgrows a screen is a block that gets skipped \u2014 which is the defect it
    exists to fix."""
    print(prefix + str(rec.get("id"))
          + ("  [BLOCKING]" if rec.get("blocking") else "")
          + "  %s \u00b7 %s" % (rec.get("kind"), rec.get("subject")))
    print(prefix + "   asked    : %s (cycle %s)" % (rec.get("timestamp"), rec.get("cycle")))
    print(prefix + "   wrong    : " + _oneline(rec.get("reason"), 160))
    print(prefix + "   proposed : " + _oneline(rec.get("proposed"), 160))
    print(prefix + '   answer   : swarmloop.py escalate --resolve %s --note "<what you decided>"'
          % rec.get("id"))


def _escalate_open_asks(d):
    """THE END-OF-TURN BLOCK \u2014 every open ask, newest first, blocking on top, and
    NOTHING AT ALL (exit 0) when none are open.

    Filing an escalation gives a question a durable home on disk and no path to
    the eyes of the one person allowed to answer it. The non-blocking case is
    where that bites: the loop is told to file it and keep going, so the ask is
    recorded, hours of output scroll past, and the user never sees the thing the
    run is waiting on. A mechanism that looks like it is doing its job with no
    path by which it can fire is this project's recurring defect in a new hat.

    The silence is therefore load-bearing, not a nicety. This can only be run
    unconditionally at the end of every turn if the empty case costs the reader
    nothing: a command that prints "no open escalations" two hundred times trains
    the eye to skip the block, and the one turn carrying a real question scrolls
    past with the rest of it.

    It reads and prints. It writes nothing \u2014 not even the state directory.
    """
    open_recs = list(reversed(_open_escalations(d)))               # newest first
    if not open_recs:
        return                                                     # silence, exit 0
    open_recs.sort(key=lambda r: 0 if r.get("blocking") else 1)    # stable: blocking on top
    blocking = [r for r in open_recs if r.get("blocking")]
    bar = "=" * 74
    print(bar)
    print("OPEN ASKS \u2014 %d awaiting YOUR decision%s"
          % (len(open_recs), ", %d BLOCKING" % len(blocking) if blocking else ""))
    print("Nothing here has been changed by the loop. Escalating is asking, never granting.")
    for r in open_recs:
        print("")
        _print_ask(r, prefix="  ")
    print("")
    print("  full text, with the exact command that grants each: %s/%s"
          % (STATE_DIR, ESCALATIONS_MD))
    print(bar)
    if blocking:
        print("AWAITING USER DECISION \u2014 %s BLOCKING. The run is WAITING ON A HUMAN:"
              % ", ".join(r["id"] for r in blocking))
        print("neither done nor failed, and it must never be reported as either.")
        sys.exit(2)


def _rewrite_escalations_md(d, root, project):
    """Regenerate the human mirror. This file is for the USER to read first thing,
    so it is ordered the way a just-woken person reads: open items on top,
    blocking above the rest, newest first, each carrying the exact command that
    grants it. Resolved items sink below."""
    records = _load_escalations(d)
    open_recs = [r for r in records if r.get("status") != "resolved"]
    done_recs = [r for r in records if r.get("status") == "resolved"]
    open_recs = list(reversed(open_recs))                       # newest first
    open_recs.sort(key=lambda r: 0 if r.get("blocking") else 1)  # stable: blocking on top
    done_recs.sort(key=lambda r: str(r.get("resolved_at") or ""), reverse=True)
    n_block = sum(1 for r in open_recs if r.get("blocking"))

    L = ["# ESCALATIONS \u2014 " + project, ""]
    L += ["The loop writes here when it hits something frozen (or otherwise locked) that it",
          "believes is **wrong**. A freeze is a locked door, not a wall: the loop's obligation is",
          "to bring the problem to you rather than work around it, silently re-freeze it, or keep",
          "scoring against a goal it thinks is broken.",
          "",
          "**Nothing in this file has been changed by the loop. Escalating is asking. It is never",
          "granting.** Every entry is a request awaiting your decision. Only you can grant one:",
          "`freeze --amend \"<reason>\"` is the only mechanism that alters a frozen goal or test,",
          "and `escalate --resolve <ID>` records what you decided.",
          "",
          "<sub>Mirror of `%s/%s` (append-only \u2014 that file is the source of truth). Regenerated by"
          % (STATE_DIR, ESCALATIONS_FILE),
          "every `escalate`; editing this file by hand changes nothing.</sub>",
          ""]
    L.append("## OPEN (%d%s)" % (len(open_recs), " \u2014 %d BLOCKING" % n_block if n_block else ""))
    L.append("")
    if not open_recs:
        L += ["_None. Nothing is waiting on you._", ""]
    for r in open_recs:
        L.append("### %s \u00b7 %s%s \u00b7 `%s`"
                 % (r.get("id"), "**BLOCKING** \u00b7 " if r.get("blocking") else "",
                    r.get("kind"), r.get("subject")))
        L.append("")
        L.append("- **filed**: %s \u00b7 cycle %s \u00b7 HEAD `%s`"
                 % (r.get("timestamp"), r.get("cycle"), (r.get("head") or "\u2014")[:10]))
        if r.get("blocking"):
            L.append("- **blocking**: the loop reports that no further progress is possible until "
                     "this is decided")
        L.append("- **what is wrong**: " + str(r.get("reason", "")))
        L.append("- **proposed change**: " + str(r.get("proposed", "")))
        pre, cmds = _esc_grant_commands(r)
        L += ["- **to grant it** \u2014 " + pre, "", "  ```"]
        L += ["  " + c for c in cmds]
        L += ["  ```",
              "- **to decline it**:", "", "  ```",
              '  swarmloop.py escalate --resolve %s --note "declined \u2014 <why>"' % r.get("id"),
              "  ```", ""]
    L.append("## RESOLVED (%d)" % len(done_recs))
    L.append("")
    if not done_recs:
        L += ["_None yet._", ""]
    for r in done_recs:
        L.append("### %s \u00b7 %s \u00b7 `%s` \u2014 %s %s"
                 % (r.get("id"), r.get("kind"), r.get("subject"),
                    "WITHDRAWN by the loop" if r.get("withdrawn") else "resolved",
                    r.get("resolved_at")))
        L.append("")
        L.append("- filed %s (cycle %s)%s"
                 % (r.get("timestamp"), r.get("cycle"),
                    " \u00b7 was BLOCKING" if r.get("blocking") else ""))
        L.append("- **what was wrong**: " + str(r.get("reason", "")))
        L.append("- **proposed**: " + str(r.get("proposed", "")))
        if r.get("withdrawn"):
            # Said in the reading order the user actually uses: nobody answered
            # this one, and a reader skimming RESOLVED must never mistake a
            # retraction for a decision.
            L.append("- **decision**: _NONE \u2014 NEVER ANSWERED. The loop withdrew the "
                     "question_: " + str(r.get("resolution_note") or ""))
        else:
            L.append("- **decision**: " + (str(r.get("resolution_note"))
                                           if r.get("resolution_note") else "_(no note recorded)_"))
        L.append("")
    path = os.path.join(d, ESCALATIONS_MD)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(L).rstrip("\n") + "\n")
    os.replace(tmp, path)


def _escalate_list(d, show_all=False):
    """OPEN escalations by default \u2014 the question an orchestrator is actually
    asking here is "is anything waiting on the user?", and a resolved item is
    noise against it. --all adds the decided ones for the record."""
    all_recs = _load_escalations(d)
    open_recs = list(reversed([r for r in all_recs if r.get("status") != "resolved"]))
    blocking = [r for r in open_recs if r.get("blocking")]
    if not open_recs:
        print("escalate: no open escalations \u2014 nothing is waiting on the user")
    else:
        print("%d OPEN escalation(s)%s, newest first \u2014 escalating is asking, it is never "
              "granting:"
              % (len(open_recs), " \u2014 %d BLOCKING" % len(blocking) if blocking else ""))
        for r in open_recs:
            print("")
            _print_escalation(r, prefix="  ")
    if show_all:
        done = [r for r in all_recs if r.get("status") == "resolved"]
        done.sort(key=lambda r: str(r.get("resolved_at") or ""), reverse=True)
        print("")
        print("%d RESOLVED escalation(s), newest first:" % len(done))
        for r in done:
            print("  %s  %s \u00b7 %s \u2014 %s %s: %s"
                  % (r.get("id"), r.get("kind"), r.get("subject"),
                     "WITHDRAWN, never answered," if r.get("withdrawn") else "resolved",
                     r.get("resolved_at"),
                     _oneline(r.get("resolution_note")) or "(no note)"))
    print("")
    print("full text: %s/%s   (append-only log: %s/%s)"
          % (STATE_DIR, ESCALATIONS_MD, STATE_DIR, ESCALATIONS_FILE))
    if blocking:
        print("AWAITING USER DECISION \u2014 a BLOCKING escalation is open; the loop must not "
              "continue past it.")
        sys.exit(2)


def _escalate_close(args, d, root, state, project, withdraw=False):
    """Close an open ask. Two doors, and they are NOT the same door.

    --resolve records what the USER decided. --withdraw records that the LOOP
    retracted a question which became moot \u2014 the route pivoted, the ticket was
    dropped \u2014 and it is written down as never answered, forever, because it was.
    The asymmetry is the whole point: an agent may ask, only the user may grant,
    and an ask closed with no recorded decision cannot be told apart from an agent
    quietly retiring its own inconvenient question. Which is why both doors demand
    a note, and why a BLOCKING ask has no withdraw door at all: it stopped the run
    to wait for a person, so the loop does not get to answer it by closing it.

    The withdraw door exists because the alternative is worse. An ask that can
    never be closed reappears in every turn's block forever, a block that repeats
    forever gets ignored, and an ignored block is the original defect with extra
    steps \u2014 so a loop with no honest way out of a moot question has every
    incentive to file a dishonest --resolve instead.
    """
    raw = args.withdraw if withdraw else args.resolve
    rid = _norm_esc_id(raw)
    if rid is None:
        die("'%s' is not an escalation id (expected e.g. ESC-003)" % raw, 3)
    note = str(args.note or "").strip()
    if not note:
        if withdraw:
            die("--withdraw needs --note \"<why this question no longer needs an answer>\": a "
                "retraction with no stated reason is exactly how an unanswered ask disappears "
                "quietly, which is the failure this ledger exists to prevent.", 3)
        die("--resolve needs --note \"<what the user decided>\": an ask closed with no recorded "
            "decision cannot be told apart from the loop retiring its own unanswered question. "
            "Declining IS a decision \u2014 record it: --note \"declined \u2014 <why>\".", 3)
    recs = {r["id"]: r for r in _load_escalations(d)}
    rec = recs.get(rid)
    if rec is None:
        die("unknown escalation id %s (on file: %s)"
            % (rid, ", ".join(sorted(recs)) or "none"))
    if rec.get("status") == "resolved":
        # The log is append-only and a decision is not re-taken: silently
        # "re-resolving" would let a second answer overwrite the first with no
        # trace of which one the run actually proceeded under.
        die("%s was already %s at %s (%s). File a NEW escalation if the question is "
            "open again." % (rid, "withdrawn" if rec.get("withdrawn") else "resolved",
                             rec.get("resolved_at"),
                             _oneline(rec.get("resolution_note")) or "no note"))
    if withdraw and rec.get("blocking"):
        die("%s is BLOCKING and the loop may not withdraw it. A blocking escalation stopped the "
            "run to wait for a person; retracting it would be the loop answering its own "
            "question. Only the user closes this one: "
            "escalate --resolve %s --note \"<what you decided>\"." % (rid, rid))
    cycle = args.cycle if args.cycle is not None else int(state.get("current_cycle", 0) or 0)
    _append_escalation(d, {
        "id": rid, "action": "withdraw" if withdraw else "resolve", "status": "resolved",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "cycle": cycle,
        "note": note, "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
    })
    _rewrite_escalations_md(d, root, project)
    if withdraw:
        print("%s WITHDRAWN by the loop \u2014 %s \u00b7 %s"
              % (rid, rec.get("kind"), rec.get("subject")))
        print("  reason: " + note)
        print("  recorded as NEVER ANSWERED. Withdrawing is not a decision and grants nothing;")
        print("  if the question still needs an answer, file it again.")
    else:
        print("%s resolved \u2014 %s \u00b7 %s" % (rid, rec.get("kind"), rec.get("subject")))
        print("  decision: " + note)
    print("  moved to the RESOLVED section of %s/%s" % (STATE_DIR, ESCALATIONS_MD))
    still = _blocking_escalations(d)
    if still:
        print("NOTE: a BLOCKING escalation is still open: "
              + ", ".join(r["id"] for r in still) + " \u2014 the loop is still stopped.")


def cmd_escalate(args):
    """Ask the USER to change something frozen or blocked. Records the request only.

    CHANGES NOTHING. It appends to escalations.jsonl and regenerates the
    ESCALATIONS.md mirror; it does not touch goals.json, manifest.json,
    history.csv, state.json or any metric value, and it cannot flip a verdict.
    That inertness is precisely what makes the reframe safe: an agent may knock
    on the door as often as it likes and the door does not move. Only the user,
    awake, running 'freeze --amend', opens it.
    """
    root = repo_root()
    d = sd(root)
    # Answered before anything below can run: the end-of-turn block is a READ,
    # it is the one form of this command that fires on EVERY turn, and it must
    # not so much as create a directory to print what is already on file.
    if args.open_asks:
        return _escalate_open_asks(d)
    # Never die for want of an initialized run: an escalation filed from a
    # half-set-up repo is exactly the kind that most needs to reach the user.
    os.makedirs(d, exist_ok=True)
    state = {}
    spath = os.path.join(d, STATE_FILE)
    if os.path.exists(spath):
        try:
            with open(spath) as f:
                state = json.load(f)
        except (ValueError, OSError):
            state = {}
    project = state.get("project") or os.path.basename(root)

    if args.do_list:
        return _escalate_list(d, args.show_all)
    if args.resolve is not None and args.withdraw is not None:
        die("--resolve and --withdraw are not two names for the same thing: one records what "
            "the USER decided, the other records that the LOOP retracted a question nobody "
            "answered. Pass exactly one.", 3)
    if args.resolve is not None:
        return _escalate_close(args, d, root, state, project)
    if args.withdraw is not None:
        return _escalate_close(args, d, root, state, project, withdraw=True)

    missing = [name for name, v in (("--kind", args.kind), ("--subject", args.subject),
                                    ("--reason", args.reason), ("--proposed", args.proposed))
               if not str(v or "").strip()]
    if missing:
        die("escalate needs " + " ".join(missing) + " \u2014 an escalation the user cannot act on "
            "is worse than none. Name the thing, the evidence that it is wrong, and the exact "
            "change you are asking for. (Or use --open-asks / --list / --resolve <ID> / "
            "--withdraw <ID>.)", 3)
    subject = args.subject.strip()
    cycle = args.cycle if args.cycle is not None else int(state.get("current_cycle", 0) or 0)
    head = _git(root, "rev-parse", "HEAD").stdout.strip() or None

    dup = next((r for r in _open_escalations(d)
                if r.get("kind") == args.kind
                and (r.get("subject") or "").strip() == subject), None)
    if dup:
        # An unattended loop re-files the same escalation every cycle unless
        # something stops it, and 200 identical entries bury the one the user
        # actually has to read. The request is already on the record; say so.
        print("escalate: %s is ALREADY OPEN for %s \u00b7 %s \u2014 nothing appended (the request "
              "stands and is still awaiting a decision)." % (dup["id"], args.kind, subject))
        if args.blocking and not dup.get("blocking"):
            # Not a duplicate of the same thing: the same problem has BECOME
            # blocking since it was filed, and the record must say so or the
            # user reads a stale severity.
            _append_escalation(d, {"id": dup["id"], "action": "block",
                                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "cycle": cycle, "head": head})
            _rewrite_escalations_md(d, root, project)
            dup["blocking"] = True
            print("escalate: %s UPGRADED to BLOCKING \u2014 no further progress is possible."
                  % dup["id"])
        _print_escalation(dup, prefix="  ")
        if dup.get("blocking"):
            # Deliberate: re-asking about a blocking escalation does not make it
            # less terminal. Exiting 0 here would tell a literal-minded
            # orchestrator to carry on past the exact wall it just hit.
            print("AWAITING USER DECISION \u2014 this escalation is BLOCKING and still open.")
            sys.exit(2)
        return

    rid = _next_esc_id(_load_escalations(d))
    rec = {
        "id": rid, "action": "open", "status": "open",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cycle": cycle, "kind": args.kind, "subject": subject,
        # H-1: structured, so freeze --amend can LINK this to the amendment that
        # answers it without parsing prose out of `subject`. Optional — records
        # filed before these existed simply lack them, and the linker still tries
        # the subject (and its nodeid head) for those.
        "path": (args.path or "").strip() or None,
        "test": (args.test or "").strip() or None,
        "reason": args.reason.strip(), "proposed": args.proposed.strip(),
        "blocking": bool(args.blocking), "head": head,
    }
    _append_escalation(d, rec)
    _rewrite_escalations_md(d, root, project)

    bar = "!" * 74
    print(bar)
    print("!!! ESCALATION %s FILED \u2014 AWAITING USER DECISION%s"
          % (rid, "   [BLOCKING]" if args.blocking else ""))
    print(bar)
    _print_escalation(rec, prefix="  ")
    print("")
    print("  recorded in %s/%s (append-only) and mirrored into %s/%s for the user to read"
          % (STATE_DIR, ESCALATIONS_FILE, STATE_DIR, ESCALATIONS_MD))
    print("  first thing. NOTHING ELSE CHANGED \u2014 no goal, no manifest, no metric, no history")
    print("  row. Do not work around this, do not re-freeze it, and do not score as though it")
    print("  were settled.")
    print("")
    # The exact moment an agent is tempted to read its own filing as permission,
    # so it is said here rather than only in the docs.
    print("  AN ESCALATION YOU FILED IS NOT AN AMENDMENT YOU WERE GRANTED.")
    print("  Escalating is asking. It is never granting. Only the user, awake, running")
    print("  'freeze --amend \"<reason>\"' changes a frozen thing.")
    if args.blocking:
        print(bar)
        print("!!! BLOCKING \u2014 no further progress is possible without a decision. Stop the")
        print("!!! loop, surface this, and do not resume until it is resolved.")
        print(bar)
        sys.exit(2)


# ------------------------------------------------------------------ freeze

def cmd_freeze(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")

    # D3, the second half. init now unions the mandatory paths in, so the only
    # way to arrive here without them is a state.json that was edited by hand —
    # which IS the tamper. Freezing over it would produce a manifest that
    # protects less than every downstream command claims it does, and nothing
    # would ever say so again.
    absent_mandatory = [p for p in MANDATORY_PROTECTED
                        if p not in (state.get("protected_paths") or [])]
    if absent_mandatory:
        die("refusing to freeze: %s missing from protected_paths in %s/%s. That path holds "
            "the targets and acceptance tests this freeze exists to lock; without it 'verify' "
            "would report an intact harness over files anyone may edit. Add it back (or run "
            "'init --force', which unions it in and carries the rest of the list forward)."
            % (", ".join(absent_mandatory), STATE_DIR, STATE_FILE))

    if not goals.get("metrics"):
        die("goals.json has no metrics — define goals before freezing")
    dupes = _dupe_metric_ids(goals)
    if dupes:
        die("duplicate metric id(s) in goals.json: %s. analyze keeps only the LAST row per "
            "(cycle, id), so every earlier definition is scored invisibly while the "
            "at-target count still counts it — one metric can be permanently red and "
            "ALL TARGETS MET still fire. Give each metric its own id."
            % ", ".join(dupes))
    for m in goals["metrics"]:
        for key in ("id", "command", "target", "direction"):
            if key not in m:
                die(f"metric {m.get('id', '?')} missing required field '{key}'")
        if m["direction"] not in ("maximize", "minimize"):
            die(f"metric {m['id']}: direction must be 'maximize' or 'minimize'")
        # 'target' and 'tolerance' must PARSE as finite numbers, and this is the
        # only place that checks. Without it a "tolerance": null (or a string
        # target) freezes clean, measures clean, and then crashes 'analyze' with
        # a TypeError on every retry — and resume's recommendation for a measured
        # but unanalyzed cycle is "run analyze --cycle N", i.e. straight back into
        # the same crash. Catch it while goals are still editable.
        try:
            if not math.isfinite(float(m["target"])):
                raise ValueError
        except (TypeError, ValueError):
            die(f"metric {m['id']}: 'target' must be a finite number (got {m['target']!r})")
        if not isinstance(m["target"], (int, float)) or isinstance(m["target"], bool):
            # P-10: a STRING that parses ("120") used to sail through here and
            # then die on `:g` mid-measure, after the command had run and before
            # a single row was written. Every use site coerces now (_target), so
            # this is no longer fatal — but goals.json is frozen bytes and the
            # user is editing it right now, which is the one moment it is cheap
            # to make it a real number.
            print(f"  WARNING: metric {m['id']}: 'target' is {m['target']!r} (a "
                  f"{type(m['target']).__name__}, not a JSON number). It is read as "
                  f"{float(m['target']):g}; quote-free numbers are what goals.json should "
                  "carry — fix it before this freeze locks the bytes.")
        if "tolerance" in m:
            try:
                if not math.isfinite(float(m["tolerance"])):
                    raise ValueError
            except (TypeError, ValueError):
                die(f"metric {m['id']}: 'tolerance' must be a finite number "
                    f"(got {m['tolerance']!r}) — use 0, or omit the field entirely")
        if "timeout" in m:
            try:
                if int(m["timeout"]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                die(f"metric {m['id']}: timeout must be a positive integer number of seconds")
        # goal-setting.md's rule, enforced instead of merely written. A metric
        # that throws its own failure output away leaves a zero with no artifact
        # anywhere: measured cost on one run, four false zeros in six cycles,
        # four manual re-runs, four different causes, every one of them
        # rediscovered by hand because the measurement destroyed the evidence.
        # A freeze is the last cheap moment to say so — after it, changing the
        # command needs a user-approved amendment.
        #
        # WHOLE-STRING here, deliberately, and _swallowing_redirect's anchored
        # match is deliberately NOT used: a metric command is one expression
        # whose LAST LINE is the number, so any /dev/null in it is on the path to
        # that number, and the refusal lands at intake where the remedy is an
        # edit. red-check's identical-looking check runs at MERGE, on a shell
        # pipeline the user wrote in their build docs, where the same whole-string
        # match produced a false refusal the printed remedy could not clear.
        # Same regex, different cost of being wrong, so different anchoring.
        cmd = str(m.get("command") or "")
        if DEVNULL_RE.search(cmd):
            die(f"metric {m['id']}: the command discards its own failure output "
                f"({DEVNULL_RE.search(cmd).group(0)!r}). A zero from this metric will leave no "
                f"artifact to explain it, and this is the last moment it is cheap to fix — "
                f"after the freeze the command is frozen bytes and needs an amendment. "
                f"Redirect to a file instead:  > {STATE_DIR}/reports/<id>-last.log 2>&1")

    # B5/H-10: the read-closure check runs HERE — before the goalpost gate, before
    # a single byte is written — because its whole value is that it fires at
    # intake. At dispatch time the answer is useless: the live instance (T-065
    # importing T-062's scoring module) cannot be fixed by adding the missing
    # edge, since T-062 already depends on T-065 and the reverse edge would close
    # a cycle. It needs a re-marked test or a split ticket, and both are cheap
    # only while the suite is still unfrozen. Total no-op on a project with no
    # ticket graph, which is every project the existing regression suite builds.
    #
    # D4: and it BLOCKS only at that first freeze. On an already-frozen run the
    # goals are not malleable any more — the remedy the refusal proposes
    # (re-mark the test, split the ticket) is itself an amendment, so refusing
    # the amend command is refusing the only door out. It also broke backward
    # compatibility outright: a run frozen by an earlier version, whose state
    # this gate never saw, could no longer be amended at all. Advisory there,
    # loudly; blocking where it is still cheap to act on.
    _freeze_read_gate(root, d, state, args.allow_read_violations,
                      already_frozen=(os.path.exists(os.path.join(d, MANIFEST_FILE))
                                      or bool(goals.get("frozen"))))

    # G-1: the ticket graph joins the protected set, when there is one. See
    # _graph_protected_rel for why the guard could not do this and why evidence
    # beats prevention here. Added to the IN-MEMORY list now, so every hash
    # below covers it, and persisted to state.json only once the manifest has
    # actually been written — a freeze that refuses must leave check-branch and
    # verify agreeing about what is protected, which is P-9's whole lesson.
    graph_rel, graph_outside = _graph_protected_rel(root, state)
    graph_newly_protected = False
    if graph_outside:
        bar = "!" * 74
        print(bar)
        print(f"!!! THE TICKET GRAPH LIVES OUTSIDE THIS REPO ({graph_outside}) — IT CANNOT BE FROZEN")
        print("!!! `red-check` reads `bootstrap: true` from it to exempt a ticket from the")
        print("!!! must-go-red gate, so it is an INPUT TO A GATE. freeze can only hash files")
        print("!!! git can hold: outside the repo nothing shows an edit in a branch diff and")
        print("!!! 'restore-harness' has nothing to restore from. Move the graph into the repo")
        print("!!! (and re-point it with 'init --ticket-graph <path>'), or accept that the")
        print("!!! bootstrap exemption is ungraded for this whole run and say so at cycle 0.")
        print(bar)
    elif graph_rel and graph_rel not in state["protected_paths"]:
        state["protected_paths"].append(graph_rel)
        graph_newly_protected = True
        print(f"note: the ticket graph {graph_rel} joins protected_paths — `red-check` reads "
              "`bootstrap: true` from it to exempt a ticket from the must-go-red gate, and an "
              "unhashed input to a gate is a gate a worker can edit around. It is FROZEN, not "
              "blocked: amend it with 'freeze --amend', and 'findings' re-seals and logs its "
              "own mints.")

    # --- goalpost gate -------------------------------------------------------
    # A bare re-freeze over an ALREADY-frozen harness re-locks whatever bytes are
    # on disk. After tampering that silently turns 'verify' green and erases the
    # only record that the goal moved. Freezing twice is legitimate exactly once:
    # when the user, awake, amends a frozen goal (references/goal-setting.md) —
    # which is what --amend records.
    mfpath = os.path.join(d, MANIFEST_FILE)
    # Gate on goals["frozen"] AND on the manifest. `rm manifest.json` must not
    # become a way to launder a re-freeze — and neither must `"frozen": false`,
    # which is one field of the very file this gate protects and which silently
    # re-locked a TAMPERED harness at exit 0 (verify then passed, amendments
    # still read 0). The manifest's existence, not a self-reported boolean, is
    # the authority; init --force moves it aside, so a genuinely new run in the
    # same repo is unaffected.
    frozen_before = bool(goals.get("frozen"))
    prior = load_json(mfpath, "manifest") if os.path.exists(mfpath) else None
    # The one manifest that does NOT mean "locked": a freeze that died between
    # writing the manifest and flipping the flag — no closing freeze-log line AND
    # the leftover goals.json.tmp that resume names as the confirming evidence.
    # Nothing was ever locked in that state and re-freezing is the documented
    # repair, so the gate lets exactly that combination through.
    log_state = _freeze_log_state(d, (prior or {}).get("frozen_at"))
    if prior is not None and log_state == "no-log":
        # A2/P-3. The old hatch turned on `_freeze_logged` returning False, which
        # it did UNCONDITIONALLY when the log file was absent — fail-open on the
        # one condition an attacker controls with a single `rm`. The full
        # laundering sequence was: edit an acceptance test → rm freeze-log.jsonl
        # → flip "frozen" to false inside the very file the gate protects →
        # touch goals.json.tmp → freeze. locked_before came out False, the whole
        # refusal block below was skipped, the delta was never even computed, and
        # a fresh `action:"initial", amendment:0` line was written over the
        # tampered bytes. A missing log over a manifest that records a freeze is
        # not evidence of a crash; it is the destruction of the record.
        print(f"freeze: REFUSED — {STATE_DIR}/{MANIFEST_FILE} records a freeze at "
              f"{prior.get('frozen_at')} but {STATE_DIR}/{FREEZE_LOG_FILE} is GONE.")
        print("That log is the append-only record of every goalpost this run ever moved, and")
        print("its absence is an integrity violation in its own right — not a reason to")
        print("re-freeze, and never evidence of a half-written freeze.")
        print(f"  restore it:  git show <commit>:{STATE_DIR}/{FREEZE_LOG_FILE} > "
              f"{STATE_DIR}/{FREEZE_LOG_FILE}")
        print("  or, if this repo is genuinely starting a new run:  swarmloop.py init --force")
        sys.exit(1)
    crashed_freeze = (prior is not None and not frozen_before
                      and _staged_goals_ok(d)
                      and log_state == "not-logged")
    locked_before = frozen_before or (prior is not None and not crashed_freeze)
    if args.amend is not None and not args.amend.strip():
        die("--amend requires a reason: the user's own words for why this frozen goal moves", 3)
    initial_reason = None
    if args.amend and not locked_before:
        # P-11: this used to discard the user's approved reason on one
        # informational print, exit 0, and leave `amendments` unincremented — so
        # the artifact documented as the permanent record of a moved goalpost was
        # the artifact whose absence caused the move to go unrecorded.
        if prior is not None or frozen_before:
            print("freeze: REFUSED — --amend was passed with a reason the user approved, but "
                  "this harness does not read as locked (a crashed freeze, or a cleared "
                  "'frozen' flag).")
            print("  reason given: " + args.amend)
            print("Silently downgrading that to an unrecorded initial freeze is how an "
                  "approved goalpost move goes unlogged. Repair the state first:")
            print("  swarmloop.py resume        # says which of the two states this is")
            print("  swarmloop.py freeze        # re-runs a freeze that died mid-write")
            sys.exit(1)
        # Nothing was ever frozen here: an initial freeze is correct, but the
        # reason still goes on the record rather than into a print statement.
        print("note: nothing has ever been frozen in this repo — this is the INITIAL freeze, "
              "not an amendment; your reason is recorded on the initial freeze-log record")
        initial_reason, args.amend = args.amend, None
    # An amendment is recorded when a LOCKED harness is re-frozen; a manifest
    # with no delta and no --amend is just an ordinary (re)freeze.
    amending = locked_before and (frozen_before or bool(args.amend))
    added = removed = changed = None
    pre_bytes = {}
    # Hashed BEFORE the smoke run, for two jobs: the goalpost delta below, and
    # A6's tripwire on what the smoke run itself does to protected bytes.
    pre, missing_protected = _protected_hashes(root, state)
    if missing_protected:
        # P-9: this was a silent `continue`. A protected path that does not exist
        # yet is unprotected for the whole run while check-branch keeps vetoing
        # it by prefix — the two halves of the protection model disagreeing about
        # the file the verification contract rests on.
        bar = "!" * 74
        print(bar)
        print("!!! PROTECTED PATH(S) DO NOT EXIST — they will NOT be protected by this freeze:")
        for rel in missing_protected:
            print(f"!!!   {rel}")
        print("!!! freeze hashes what is on disk. A path authored later is unprotected for the")
        print("!!! whole run — verify's ADDED scan skips a directory that is not there — while")
        print("!!! check-branch still vetoes any branch touching it. Author them first, or drop")
        print("!!! them from state.json's protected_paths.")
        print(bar)
        if not args.allow_missing_protected:
            die("refusing to freeze with %d protected path(s) absent — author them, or pass "
                "--allow-missing-protected to freeze anyway (and say so in the cycle-0 report)"
                % len(missing_protected))
    if locked_before:
        added, removed, changed = _manifest_delta((prior or {}).get("files", {}), pre)
        if not args.amend and (frozen_before or changed or added or removed):
            if frozen_before:
                print(f"freeze: REFUSED — goals were already frozen at {goals.get('frozen_at')}.")
            else:
                print(f"freeze: REFUSED — {STATE_DIR}/{GOALS_FILE} says frozen=false, but "
                      f"{STATE_DIR}/{MANIFEST_FILE} (frozen at {prior.get('frozen_at')}) says "
                      "this harness IS locked and the files on disk no longer match it. "
                      "Clearing the flag inside the protected file is not un-freezing.")
            if prior is None:
                print(f"{STATE_DIR}/{MANIFEST_FILE} is MISSING — that is itself an integrity "
                      "violation, not a reason to re-freeze. Restore it from the commit that "
                      "holds it (`git show <commit>:" + STATE_DIR + "/" + MANIFEST_FILE + " > "
                      + STATE_DIR + "/" + MANIFEST_FILE + "`).")
            elif changed or added or removed:
                print("The harness on disk no longer matches the frozen manifest:")
                for rel in changed:
                    print(f"  [MODIFIED] {rel}")
                for rel in added:
                    print(f"  [ADDED]    {rel}")
                for rel in removed:
                    print(f"  [MISSING]  {rel}")
                print("A bare re-freeze would re-lock exactly these bytes and turn "
                      "'verify' green with no record that the goal moved.")
            else:
                print("The harness on disk still matches the manifest — nothing but "
                      "frozen_at would change.")
            print("")
            print("If the harness was altered, RESTORE it, do not re-freeze:")
            print("  swarmloop.py restore-harness")
            print("Only the user, awake, amends a frozen goal. With their approval:")
            print('  swarmloop.py freeze --amend "<reason the user approved this goal change>"')
            print("Starting a genuinely new run instead? 'init --force' un-freezes goals first.")
            sys.exit(1)
        # freeze rewrites goals.json below; snapshot the bytes the hashes above
        # describe so the printed diff and the printed hashes agree.
        for rel in changed:
            try:
                with open(os.path.join(root, rel), "rb") as f:
                    pre_bytes[rel] = f.read()
            except OSError:
                pass

    # Smoke-run every metric command exactly as 'measure' will run it, BEFORE
    # locking anything — a non-portable command that freezes fine only explodes
    # at measure time, when fixing it requires editing goals and re-freezing.
    # This is a probe, not a measurement: values are NOT recorded in history.
    if not args.skip_smoke:
        print("smoke-running metric commands (probe only — nothing is recorded):")
        smoke_failures, already_met = [], []
        deselecting, selects_zero = [], []
        for m in goals["metrics"]:
            tmo = _metric_timeout(m, args.smoke_timeout)
            try:
                proc = _run_metric_command(m["command"], root, tmo)
            except subprocess.TimeoutExpired:
                smoke_failures.append(m["id"])
                print(f"  {m['id']:<28} FAIL: timeout after {tmo}s")
                continue
            # stdout AND stderr: the value is parsed from stdout's last line, but
            # a runner under `2>&1`-less invocation prints its summary wherever it
            # likes, and the selection facts are worth strictly more than the
            # convenience of parsing one stream.
            smoke_out = (proc.stdout or "") + (proc.stderr or "")
            sm_sel, sm_desel, sm_pytest = _selection_facts(smoke_out)
            sm_note = _selection_note(sm_sel, sm_desel, sm_pytest)
            if sm_desel:
                deselecting.append((m["id"], sm_note, smoke_out))
            elif sm_pytest and not sm_sel:
                selects_zero.append((m["id"], sm_note, smoke_out))
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            try:
                value = float(lines[-1])
                # nan/inf parse fine and would be blessed here, then recorded by
                # measure and read downstream as at_target. Reject at the gate.
                if not math.isfinite(value):
                    raise ValueError("non-finite")
            except (IndexError, ValueError):
                smoke_failures.append(m["id"])
                bad = repr(lines[-1]) if lines else "<no output>"
                if proc.returncode != 0:
                    print(f"  {m['id']:<28} FAIL: nonzero exit ({proc.returncode}), "
                          f"no finite numeric last line ({bad})")
                else:
                    print(f"  {m['id']:<28} FAIL: no finite numeric last line ({bad})")
                if proc.stderr.strip():
                    print("      stderr: " + proc.stderr.strip().splitlines()[-1][:200])
                continue
            # Print target and error, not just the value: the smoke-run is the
            # only moment the harness sees a metric BEFORE any implementation
            # exists, and "already at target" is the signature of a vacuous
            # acceptance test (assert True, no assertion, a wrapper that prints a
            # number without running the suite) — which would inflate the frozen
            # score for the whole run with nothing checking it.
            err = _error(value, _target(m), m["direction"])
            tol = _tol(m)
            tail = f"  [{sm_note}]" if sm_note else ""
            if err <= tol:
                already_met.append(m["id"])
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {_target(m):g}, "
                      f"error {err:g})  ** ALREADY AT TARGET **{tail}")
            else:
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {_target(m):g}, "
                      f"error {err:g}){tail}")
        if smoke_failures:
            print(f"freeze: {len(smoke_failures)} metric command(s) failed the smoke-run: {', '.join(smoke_failures)}")
            print("manifest NOT written; goals remain unfrozen — fix the command(s) and re-run freeze")
            sys.exit(1)
        # The deselection gate, and it has TEETH HERE AND ONLY HERE. freeze fires once
        # per run, never on the dispatch path, and is the last moment the command is
        # still editable text rather than frozen bytes needing a user-approved
        # amendment — the same "last moment it is cheap to fix" the DEVNULL
        # refusal above is built on. A false refusal costs one flag; the same
        # check on the dispatch path would wedge a live run.
        #
        # What it is refusing: a metric whose own smoke run throws tests away
        # before running them, or runs none at all. Both make the metric's number
        # a statement about the SELECTION rather than about the code — and
        # selection is editable from pytest.ini, pyproject.toml or a conftest,
        # none of which are protected paths, none of which appear in any test
        # diff, and all of which check-branch is deliberately forbidden from
        # vetoing. Measured: honest red baseline (2 failed), marker filter added
        # to pytest.ini, src/ still EMPTY, and the run reads ALL TARGETS MET.
        #
        # INTAKE ONLY, and this is the D4 rule, not a nicety. A run frozen by an
        # earlier version has a deselecting metric this gate never inspected;
        # every remedy for it — editing the frozen command — IS an amendment, so
        # refusing `freeze --amend` would refuse the only door out of the state
        # being complained about and leave the operator with a run that can never
        # be amended again. On an already-frozen harness the finding is reported
        # loudly, by name, and the freeze proceeds.
        if (deselecting or selects_zero) and not args.allow_deselection and not locked_before:
            bar = "!" * 74
            print(bar)
            print("!!! freeze REFUSED: a metric command's score is decided by TEST SELECTION")
            for mid, note, out in deselecting:
                print(f"!!!   {mid}: {note}")
                for ln in out.splitlines():
                    if DESELECTED_RE.search(ln):
                        print(f"!!!       {ln.strip()[:120]}")
                        break
            for mid, note, out in selects_zero:
                print(f"!!!   {mid}: {note} — the command ran pytest and pytest ran nothing")
            print(bar)
            die("freeze REFUSED: %d metric command(s) above deselect their own tests or select "
                "none at all. A metric that deselects is not measuring the code, it is measuring "
                "a filter — and the filter lives in pytest.ini / pyproject.toml / conftest.py, "
                "which no protected path covers, which appears in no test diff, and which "
                "check-branch reports as an ADVISORY `?` it may never veto on. Measured end to "
                "end: an honest 2-failed baseline became `2 deselected`, measure recorded "
                "value=0 error=0, and analyze printed ALL TARGETS MET with src/ EMPTY. "
                "THE WAY FORWARD, in order of preference: (1) point the command at the frozen "
                "acceptance tests directly so no filter applies; (2) drop the -m / -k / "
                "--ignore / addopts filter from the command AND from the config it inherits; "
                "(3) if the deselection is a deliberate project convention (a `-m 'not slow'` "
                "house rule), re-run `freeze --allow-deselection` and record at the cycle-0 "
                "checkpoint which tests the metric will never run — because from then on "
                "nothing in this harness can tell you again. Manifest NOT written; goals "
                "remain unfrozen."
                % (len(deselecting) + len(selects_zero)))
        if deselecting or selects_zero:
            for mid, note, _out in deselecting + selects_zero:
                print(f"  !! {mid}: {note} — this metric's score is decided by test SELECTION")
            print("freeze: %d metric(s) whose score depends on test SELECTION accepted (%s). "
                  "Say at the cycle-0 checkpoint which tests they never run."
                  % (len(deselecting) + len(selects_zero),
                     "--allow-deselection passed" if args.allow_deselection
                     else "already frozen — the remedy is itself an amendment, so this "
                          "cannot be a refusal here"))
        if already_met:
            # Warn, never block: a brownfield hold-the-line metric (build_ok=1,
            # lint=0 on an existing clean repo) legitimately starts green.
            bar = "!" * 74
            print(bar)
            print("!!! WARNING: METRIC(S) ALREADY AT TARGET AT FREEZE TIME: "
                  + ", ".join(already_met))
            print("!!! goal-setting.md expects acceptance metrics to FAIL at baseline — before")
            print("!!! any implementation exists. A metric that is already met either measures")
            print("!!! nothing (a vacuous 'assert True' suite, a test whose imports are stubs, a")
            print("!!! wrapper printing a number without running the suite) or is a deliberate")
            print("!!! hold-the-line goal on brownfield code. Confirm which, and say so at the")
            print("!!! cycle-0 checkpoint — a vacuous metric inflates the frozen score forever.")
            print(bar)

    # A6/P-8: the smoke run above is ARBITRARY SHELL (shell=True, cwd = repo
    # root), and it used to sit between the hashes the delta was computed from
    # and the hashes actually written to the manifest. So a metric command of the
    # form `sed -i '' 's/assert x == 5/pass/' <a frozen test>; pytest …` was
    # silently locked in: the refusal, the GOALPOSTS MOVED block and the freeze
    # log all described the PRE-smoke bytes while the manifest recorded the POST
    # ones, and the log's own hash did not match the manifest it was recording.
    # Re-hash and hard-fail; nothing a metric command does may reach the frozen
    # bytes.
    files, missing_after = _protected_hashes(root, state)
    smoke_added, smoke_removed, smoke_changed = _manifest_delta(pre, files)
    if smoke_added or smoke_removed or smoke_changed:
        bar = "!" * 74
        print(bar)
        print("!!! A METRIC COMMAND MODIFIED THE FILES BEING FROZEN, DURING THE SMOKE RUN")
        for rel in smoke_changed:
            print(f"!!!   [MODIFIED] {rel}")
        for rel in smoke_added:
            print(f"!!!   [ADDED]    {rel}")
        for rel in smoke_removed:
            print(f"!!!   [REMOVED]  {rel}")
        print("!!! The manifest is NOT written. A measurement command that edits the harness")
        print("!!! it measures would lock its own edit in as the frozen baseline — the exact")
        print("!!! goalpost move the freeze exists to prevent. Read the command(s) in")
        print(f"!!! {STATE_DIR}/{GOALS_FILE}, restore the files, and freeze again.")
        print(bar)
        sys.exit(1)

    prev_frozen_at = goals.get("frozen_at") or (prior or {}).get("frozen_at")
    goals["frozen"] = True
    goals["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if amending:
        # The manifest's count is a floor: goals.json is editable by whoever
        # tampered with it, so its own 'amendments' cannot be trusted to be
        # higher than the one recorded alongside the hashes.
        goals["amendments"] = max(int(goals.get("amendments", 0)),
                                  int((prior or {}).get("amendments", 0))) + 1
        goals["last_amended_at"] = goals["frozen_at"]
        goals["last_amend_reason"] = args.amend

    if not files:
        die("nothing to freeze — no files found under protected paths")

    # Write order matters. goals["frozen"] is the flag measure/analyze/checkpoint
    # gate on; manifest.json is what verify needs. Flipping the flag FIRST (as
    # this once did) meant a process death in between left a run that happily
    # recorded scores while 'verify' hard-errored on a missing manifest, with no
    # sanctioned way back — and unattended overnight runs are exactly where a
    # command dies mid-flight. So: stage the new goals.json, hash the staged
    # bytes into the manifest, write the manifest, and only then let the flag
    # become visible with one atomic rename.
    gpath = os.path.join(d, GOALS_FILE)
    gtmp = gpath + ".tmp"
    with open(gtmp, "w") as f:
        json.dump(goals, f, indent=2, sort_keys=False)
        f.write("\n")
    grel = os.path.relpath(gpath, root)
    if grel in files:
        files[grel] = sha256_file(gtmp)

    # D2/H-09: which targets this amendment actually moved. Recorded in the
    # manifest as a new OPTIONAL key so the NEXT amendment can answer the
    # question exactly; a manifest written before this key existed cannot, so
    # the fallback is conservative — if goals.json's bytes moved at all, treat
    # the baseline as owed a re-measure. Wrong in the harmless direction.
    now_targets = {m["id"]: _target(m) for m in goals["metrics"]}
    target_moves = []
    if amending:
        prior_targets = (prior or {}).get("targets")
        if prior_targets is None:
            if grel in (changed or []) or grel in (added or []):
                target_moves = ["(unknown — the previous manifest predates target recording; "
                                "goals.json changed, so the baseline is treated as owed)"]
        else:
            for mid, tgt in sorted(now_targets.items()):
                try:
                    if mid not in prior_targets or float(prior_targets[mid]) != tgt:
                        target_moves.append(mid)
                except (TypeError, ValueError):
                    target_moves.append(mid)

    # Two new OPTIONAL keys, written to the manifest and to the freeze-log
    # record this freeze is about to append, so the pair can be matched and
    # reconciled exactly:
    #   log_seq / seq  — the record's monotonic position. frozen_at has
    #                    one-second resolution and two freezes can share it (D2);
    #                    this cannot be shared.
    #   goals_canon    — the stamp-free digest of the goals themselves, the only
    #                    form of goals.json the two files can ever compare (D1).
    # Both are computed BEFORE the manifest is written and reused verbatim
    # below; a manifest lacking either still resolves by the older rules.
    log_seq = len(_freeze_log_records(d)[0])
    goals_canon = _goals_canon(goals)
    save_json(os.path.join(d, MANIFEST_FILE), {
        "frozen_at": goals["frozen_at"],
        "amendments": int(goals.get("amendments", 0)),
        "files": files,
        "targets": now_targets,
        "goals_canon": goals_canon,
        "log_seq": log_seq,
    })
    os.replace(gtmp, gpath)
    head = _git(root, "rev-parse", "HEAD").stdout.strip() or None

    # Only now — the manifest exists, so the two halves of the protection model
    # (verify's manifest and check-branch's prefix veto) start protecting the
    # graph in the same breath. Persisting it above would have left a refused
    # freeze with a state.json naming a path no manifest covers.
    if graph_newly_protected:
        with _state_lock(d):
            spath = os.path.join(d, STATE_FILE)
            live = load_json(spath, "state")
            paths = list(live.get("protected_paths") or [])
            if graph_rel not in paths:
                paths.append(graph_rel)
                live["protected_paths"] = paths
                save_json(spath, live)

    if target_moves:
        with _state_lock(d):
            spath = os.path.join(d, STATE_FILE)
            live = load_json(spath, "state")
            cyc = int(live.get("current_cycle", 0) or 0)
            live["pending_remeasure"] = sorted(set(_pending_remeasure(live)) | {cyc})
            save_json(spath, live)
        bar = "!" * 74
        print(bar)
        print("!!! THIS AMENDMENT MOVED A TARGET — THE BASELINE IT MOVED IS NOW INVALID")
        print("!!!   " + ", ".join(target_moves))
        print(f"!!! Every row already in {STATE_DIR}/{HISTORY_FILE} for cycle {cyc} was measured")
        print("!!! against the old target (and possibly an older frozen suite). 'analyze' will")
        print("!!! WITHHOLD all-targets-met until you re-run:")
        print(f"!!!   swarmloop.py measure --cycle {cyc}")
        print("!!! Nothing else clears it — a hand-entered 'record' does not.")
        print(bar)

    if amending:
        old_files = (prior or {}).get("files", {})
        print("")
        print(f"GOALPOSTS MOVED — amendment #{goals['amendments']} to a frozen harness")
        print(f"  reason         : {args.amend}")
        print(f"  previous freeze: {prev_frozen_at}   new freeze: {goals['frozen_at']}")
        for rel in changed:
            print(f"  [MODIFIED] {rel}")
            print(f"      {old_files[rel][:16]}… -> {pre[rel][:16]}…")
            _print_unified(root, rel, old_files[rel], pre_bytes.get(rel))
        for rel in added:
            print(f"  [ADDED]    {rel}  (now frozen at {pre[rel][:16]}…)")
        for rel in removed:
            print(f"  [REMOVED]  {rel}  (was {old_files[rel][:16]}…, no longer protected)")
        if not (changed or added or removed):
            print("  (no protected file changed — only frozen_at moves)")
        # An amendment is almost always the GRANT of a request filed earlier, so
        # link any open escalation that named what this amend actually touched.
        # The freeze-log is the permanent record of a moved goalpost; it is worth
        # far more with the reason the user was asked for it attached, and the
        # link is what lets 'resume' stop reporting a decision that has been made.
        touched = set(changed) | set(added) | set(removed)
        basenames = {os.path.basename(p) for p in touched}
        # A metric id only counts as touched when goals.json itself moved —
        # otherwise every open frozen-goal escalation would link to an amend that
        # never went near it.
        metric_ids = ({m["id"] for m in goals.get("metrics", [])}
                      if grel in touched else set())

        def _names_a_touched_path(s):
            s = (s or "").strip()
            if not s:
                return False
            return (s in touched or s in basenames or s in metric_ids
                    or any(p.endswith("/" + s) for p in touched))

        linked = []
        for r in _open_escalations(d):
            # H-1, and it is the most serious linker defect: matching was exact
            # string equality of `subject` against a path, its basename, a metric
            # id or "/"+subject — and a pytest NODEID
            # (test_spec_criteria.py::test_name), which is the canonical way to
            # name ONE frozen test and the only form that says WHICH test is at
            # issue, matched none of the four. Live: ESC-002 was filed with a
            # nodeid and answered by amendment 2; the record carries
            # "escalations": [], the ANSWERS block never fired, and it was closed
            # only because the orchestrator did it by hand. Filed --blocking, the
            # loop would have stayed halted waiting on a decision the user had
            # already made. The real fix is the structured --path/--test captured
            # at escalate time; splitting the nodeid is the stopgap for the
            # escalations already on file.
            subj = (r.get("subject") or "").strip()
            cands = [subj, subj.split("::")[0], r.get("path"), r.get("test")]
            if r.get("test"):
                cands.append(str(r["test"]).split("::")[0])
            if any(_names_a_touched_path(c) for c in cands):
                linked.append(r)
        _append_freeze_log(d, {
            "action": "amend",
            "amendment": goals["amendments"],
            "reason": args.amend,
            "prev_frozen_at": prev_frozen_at,
            "frozen_at": goals["frozen_at"],
            "seq": log_seq,
            "goals_canon": goals_canon,
            "head": head,
            "escalations": [r["id"] for r in linked],
            "targets_moved": target_moves,
            # A6: the NEW hashes come from `files` — the bytes the manifest
            # actually recorded — not from `pre`, which described the tree before
            # the smoke run. Writing `pre` here is why the log's goals.json hash
            # could never match the manifest's (A-7): goals.json is re-hashed
            # post-stamp into `files`, and the log was quoting the pre-stamp one.
            "modified": [{"path": r, "old": old_files[r], "new": files.get(r, pre[r])}
                         for r in changed],
            "added": [{"path": r, "new": files.get(r, pre[r])} for r in added],
            "removed": [{"path": r, "old": old_files[r]} for r in removed],
        })
        print(f"  recorded in {STATE_DIR}/{FREEZE_LOG_FILE} (append-only) — this amendment MUST")
        print("  appear in the cycle report and the final report; the regression before and")
        print("  after it is not measured against the same target.")
        if linked:
            print("")
            print("  OPEN ESCALATION(S) THIS AMENDMENT ANSWERS — linked in the freeze-log record:")
            for r in linked:
                print(f"    {r['id']}  {r.get('kind')} · {r.get('subject')}"
                      + ("  [BLOCKING]" if r.get("blocking") else ""))
                print(f"        proposed: {_oneline(r.get('proposed'))}")
            print("  CLOSE EACH ONE NOW — until you do, 'resume', 'status' and 'analyze' keep")
            print("  reporting it as awaiting a decision (and a BLOCKING one keeps the loop stopped):")
            for r in linked:
                print(f'    swarmloop.py escalate --resolve {r["id"]} --note "granted: {args.amend}"')
    else:
        _append_freeze_log(d, {
            "action": "initial",
            "amendment": 0,
            # P-11: normally None. Carries the user's words when --amend was
            # passed to what turned out to be the initial freeze, so an approved
            # reason is never lost to a print statement.
            "reason": initial_reason,
            "prev_frozen_at": None,
            "frozen_at": goals["frozen_at"],
            "seq": log_seq,
            "goals_canon": goals_canon,
            "head": head,
            "modified": [], "added": [{"path": r, "new": h} for r, h in sorted(files.items())],
            "removed": [],
        })

    print(f"frozen: {len(files)} file(s) under protection")
    for rel in sorted(files):
        print(f"  {rel}")
    print("goals and acceptance tests are now locked; 'verify' will detect any change")
    # B4/H-01: a freeze (and above all an amendment) is exactly when the ticket
    # graph and the ledger drift — amendment 1 added two tickets to the graph and
    # backlog.md never followed. Reported, never a veto: a stale ledger is not a
    # reason to refuse a lock the user approved.
    for line in _graph_reconcile_lines(root, d, state):
        print("  ticket graph: " + line)


# ------------------------------------------------------------------ verify

def cmd_verify(args):
    root = repo_root()
    d = sd(root)
    # load_json for the message it dies with; the check itself is _verify_quiet's,
    # so verify and the automatic gates can never disagree again (P-4).
    manifest = load_json(os.path.join(d, MANIFEST_FILE), "manifest (run 'freeze' first)")
    state = load_json(os.path.join(d, STATE_FILE), "state")
    status, bad, notes = _verify_quiet(root, d, state)
    if status == "VIOLATED":
        print("INTEGRITY VIOLATION — the frozen goal harness was altered:")
        for b in bad:
            print(f"  {b}")
        print("scores against a modified harness are void. Restore the frozen bytes with")
        print("  swarmloop.py restore-harness")
        print("(`git checkout -- <path>` / `git restore <path>` are blocked by this skill's own")
        print(" git-guard hook; `git show <commit>:<path> > <path>` is the allowed manual form.)")
        print("Do NOT re-run 'freeze' — that re-locks the tampered bytes; freeze refuses without --amend.")
        sys.exit(1)
    for n in notes:
        print(f"note: {n}")
    print("verify: OK — frozen harness intact (%d file(s); manifest reconciled against "
          "%s/%s)" % (len(manifest.get("files", {})), STATE_DIR, FREEZE_LOG_FILE))


# --------------------------------------------------------- restore-harness

def cmd_restore_harness(args):
    """Rewrite every protected file back to the exact bytes recorded in the
    manifest, sourced from git history. This exists because the repair the docs
    sanction — `git checkout -- <path>`, `git restore <path>`, `git checkout
    HEAD -- <dir>` — is blocked by this skill's own PreToolUse git-guard for
    everyone, orchestrator included, leaving 'verify: VIOLATED' unfixable."""
    root = repo_root()
    d = sd(root)
    manifest = load_json(os.path.join(d, MANIFEST_FILE), "manifest (run 'freeze' first)")
    state = load_json(os.path.join(d, STATE_FILE), "state")

    ok, restored, failed = [], [], []
    for rel, digest in sorted(manifest["files"].items()):
        path = os.path.join(root, rel)
        if os.path.exists(path) and sha256_file(path) == digest:
            ok.append(rel)
            continue
        found = _commit_with_blob(root, rel, digest)
        if not found:
            failed.append(rel)
            continue
        commit, blob = found
        existed = os.path.exists(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        if not existed:
            # a deleted file loses its mode too; recover the executable bit
            ls = _git(root, "ls-tree", commit, "--", rel).stdout.split()
            if ls and ls[0] == "100755":
                os.chmod(path, 0o755)
        restored.append((rel, commit[:10]))

    extra = []
    try:
        for relbase in state["protected_paths"]:
            base = os.path.join(root, relbase)
            if not os.path.isdir(base):
                continue
            for path in iter_files(base):
                rel = os.path.relpath(path, root)
                if rel not in manifest["files"]:
                    extra.append(rel)
    except HarnessSymlink as e:
        die(_symlink_msg(root, e))

    for rel, commit in restored:
        print(f"  [RESTORED] {rel}  (frozen bytes from {commit})")
    for rel in failed:
        print(f"  [UNRECOVERABLE] {rel} — no commit reachable from HEAD holds the frozen bytes")
    for rel in sorted(extra):
        if args.remove_added:
            os.remove(os.path.join(root, rel))
            print(f"  [REMOVED] {rel}  (added under protection after the freeze)")
        else:
            print(f"  [ADDED] {rel} — not in the manifest; re-run with --remove-added to delete it")
    if ok and not (restored or failed or extra):
        print(f"restore-harness: nothing to do — all {len(ok)} protected file(s) already match the manifest")
        return
    print(f"restore-harness: {len(restored)} restored, {len(ok)} already correct, "
          f"{len(failed)} unrecoverable, {len(extra)} extra")
    if failed or (extra and not args.remove_added):
        print("harness NOT fully restored — 'verify' will still fail. The frozen state must have")
        print("been committed at freeze time (goal-setting.md, 'Baseline'); if it never was, this")
        print("is a terminal condition for autonomy: stop and surface it to the user.")
        sys.exit(1)
    print("run 'verify' to confirm")


# ------------------------------------------------------------- check-branch

# Files that commonly steer a metric command's behaviour without being part of
# the frozen manifest: tooling config, test-collection hooks, task runners.
# A change here is NOT a veto — it is legitimate work most of the time, and a
# veto would deadlock the very scaffold ticket whose job is authoring these
# files — but it is the one class of change that can move a frozen metric
# without touching a line of product or test code, so it gets flagged for a
# human/orchestrator read rather than passing silently.
METRIC_CONFIG_BASENAMES = {
    "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py",
    ".coveragerc", "coverage.rc", ".flake8", "mypy.ini", ".mypy.ini",
    "ruff.toml", ".ruff.toml", "pylintrc", ".pylintrc",
    "package.json", "Makefile", "makefile", "GNUmakefile", "justfile",
    "tsconfig.json", ".babelrc", "babel.config.js",
}
METRIC_CONFIG_PREFIXES = ("jest.config", "vitest.config", ".eslintrc", "karma.conf")


def _ticket_dispatched_at(d, tid):
    """Earliest recorded dispatch timestamp for a ticket, or None.

    None is the answer that matters: the live run has no dispatch.jsonl at all
    (see _dispatch_holder), so 'I cannot date this ticket' must stay a first-class
    answer rather than collapsing into 'it is new'. The gate below reads None as
    'predates the ledger', which is the direction that cannot wedge a run.
    """
    path = os.path.join(d, DISPATCH_FILE)
    if not os.path.exists(path):
        return None
    stamps = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("dispatched_at"):
                    continue
                branch = str(rec.get("branch") or "")
                if (tid == _ticket_of_branch(branch)
                        or tid in str(rec.get("agent_ref") or "")
                        or tid in str(rec.get("subject") or "")):
                    stamps.append(str(rec["dispatched_at"]))
    except OSError:
        return None
    return min(stamps) if stamps else None


def _redcheck_gate(root, d, tid, base, branch, fork):
    """Refuse a branch whose ticket has no `red` stamp at an ancestor of its
    merge parent. Returns True to veto.

    Nothing prevented a ticket being marked done on a gate that cannot go
    red. `dispatch --close` is append-only bookkeeping about an AGENT — it never
    reads the ticket's verify, cannot fail, and records a close even for a ref
    that was never open. check-branch takes no ticket id, so it could not know
    which gate it was judging. From the live run's findings.jsonl:120, three
    tickets were reported CLOSED TWICE with their defects live; red-check would
    have caught all three and nothing required anyone to run it.

    THE D4 SHAPE, and it is the whole reason this is safe to land on a live run.
    A resumed or in-flight run has no stamps for tickets already dispatched, so a
    hard refusal would wedge the build the moment it merged. So: advisory for a
    ticket that predates the ledger (or that cannot be dated at all), blocking
    from the first stamp onward. `--ticket` itself stays OPTIONAL, so every
    existing check-branch invocation keeps working verbatim.
    """
    stamps, ledger = _redcheck_stamps(d)
    cmd = (f"  python3 <skill>/scripts/swarmloop.py red-check --close --ticket {tid} "
           f"--base $(git merge-base {base} {branch})")
    if ledger in ("empty", "unreadable"):
        # A PRESENT ledger that yields nothing is not the same fact as no ledger,
        # and treating them alike was the whole exploit: `: > redcheck.jsonl`
        # cleared every veto in the run and the gate printed "no redcheck.jsonl
        # exists" about a file it had just opened. _append_redcheck cannot
        # produce this state, so something outside the harness did.
        print(f"VETO: {STATE_DIR}/{REDCHECK_FILE} EXISTS but holds no usable verdict "
              f"({ledger}). That is not the same as a run that has never stamped: this "
              "harness only ever appends whole records to that file and never creates it "
              f"empty, so it was TRUNCATED or rewritten outside the harness. {tid} cannot "
              "be cleared against a ledger that has been emptied.")
        print("  Restore it from git (`git checkout -- %s/%s`) or re-establish the verdict:"
              % (STATE_DIR, REDCHECK_FILE))
        print(cmd)
        return True
    if not stamps:
        print(f"ADVISORY (not a veto) — no {STATE_DIR}/{REDCHECK_FILE} exists, so nothing "
              f"here can say whether {tid}'s gate was ever red:")
        print(cmd)
        print("    Until the first stamp lands this is unenforceable, and a run that has "
              "never stamped must not be wedged by a gate it predates. From the first "
              "stamp onward a ticket dispatched after it and carrying no red stamp is "
              "REFUSED here.")
        return False
    mine = stamps.get(tid) or []
    reds = [r for r in mine if r.get("verdict") == "red"]
    anc = [(r, _is_ancestor(root, str(r.get("base_sha") or ""), fork or branch))
           for r in reds]
    good = [r for r, a in anc if a is True]
    if good:
        last = good[-1]
        print(f"red-check: {tid} was RED at {str(last.get('base_sha'))[:12]} "
              f"({last.get('timestamp')}, {last.get('mode')}) — an ancestor of this "
              "branch's merge parent")
        # ADVISORY, never a veto, and the reason it exists: a `red` stamped over
        # a gate whose grading tests could not be COLLECTED at all reads, in the
        # ledger, exactly like a `red` whose grading tests were observed running.
        # The terminal that printed the difference is long gone by the time this
        # gate runs; the stamp is what survives, so the stamp has to carry it.
        sel = str(last.get("selection") or "")
        if sel and sel != "ok":
            print(f"  ADVISORY — that red was stamped with selection `{sel}`: the probe did "
                  "NOT positively observe this ticket's own frozen tests being collected at "
                  "that base, so the red is unverified rather than confirmed. It is not a "
                  "finding either — the commonest causes are a ticket with no "
                  "`@pytest.mark.ticket` grading test (`unknown`) and a throwaway worktree "
                  "with no venv (`uncollectable`). Read the gate before you rely on it.")
        elif not sel:
            print("  (that stamp carries no selection field — written before red-check "
                  "recorded one, so what its gate actually selected is not on file)")
        return False
    # `_is_ancestor` returns None when git cannot answer — a rewritten history, a
    # sha that no longer exists. Its own contract is that unknown must keep a
    # tree, and it holds here too: a red stamp whose ancestry is unanswerable is
    # not evidence the gate was never red.
    if any(a is None for _, a in anc):
        print(f"ADVISORY (not a veto) — {tid} has a `red` stamp whose base sha git cannot "
              "place relative to this branch's merge parent (a rewritten or pruned history). "
              "Unknown ancestry is not a finding; confirm by hand which tree it was red "
              "against.")
        return False
    # An undated record must not be allowed to set the ledger's start: "" sorts
    # below every real timestamp and would push every ticket into the blocking
    # branch at once, which is the wedge the D4 shape exists to prevent.
    first = min((r["timestamp"] for recs in stamps.values() for r in recs
                 if str(r.get("timestamp") or "").strip()), default="")
    when = _ticket_dispatched_at(d, tid)
    if not mine and (not first or when is None or when < first):
        print(f"ADVISORY (not a veto) — {tid} has no red-check stamp, and it "
              + (f"was dispatched {when}, before this ledger's first stamp ({first})"
                 if when and first else "cannot be dated from "
                 + f"{STATE_DIR}/{DISPATCH_FILE}")
              + ". A ticket that predates the ledger is not refused by it. Run it anyway "
                "before you merge:")
        print(cmd)
        return False
    print(f"VETO: {tid} has no red-check verdict of `red` at an ancestor of this branch's "
          f"merge parent, and it is not a ticket this ledger predates.")
    if mine:
        print("  What IS stamped for it:")
        for r in mine[-5:]:
            print("    %-11s %s  base %s  (%s)"
                  % (r.get("verdict"), r.get("timestamp"),
                     str(r.get("base_sha") or "?")[:12], r.get("mode")))
        print("  A verdict other than `red` means the gate proved nothing about this "
              "ticket; a `red` at a base that is NOT an ancestor of the merge parent is a "
              "red about a tree this branch never forked from.")
        # Saying "re-run red-check" over an unchanged verify string is advice
        # that cannot work: the same command re-stamps the same verdict, so an
        # operator following it literally loops. The stamp names WHY, and the
        # why is what has to change.
        print("  RE-RUNNING red-check ALONE WILL NOT CLEAR THIS. The verdict above is a "
              "statement about the ticket's `verify` command (it discards its own output, "
              "or its own grading tests are not what went red); an unchanged command "
              "re-stamps the same verdict. Fix the cause the verdict names — in the ticket "
              "graph, not in the ledger — and then:")
    else:
        print(f"  Nothing is stamped for {tid} at all, and {STATE_DIR}/{REDCHECK_FILE} "
              f"already carries stamps from {first} onward.")
        print("  Establish it, then re-run this check:")
    print(cmd)
    return True


def _metric_config_hits(root, changed):
    """Paths a metric command could plausibly read. Advisory only, never a veto."""
    tokens = set()
    gp = os.path.join(sd(root), GOALS_FILE)
    if os.path.exists(gp):
        try:
            with open(gp) as fh:
                goals = json.load(fh)
        except (ValueError, OSError):
            goals = {}
        # Before the freeze there is no target to game: config churn is scaffolding.
        if not goals.get("frozen"):
            return []
        for m in goals.get("metrics", []):
            for tok in re.findall(r"[A-Za-z0-9_./-]+", str(m.get("command", ""))):
                if "/" in tok or "." in tok:
                    tokens.add(tok.lstrip("./"))
    hits = []
    for path in changed:
        base = os.path.basename(path)
        if base in METRIC_CONFIG_BASENAMES or base.startswith(METRIC_CONFIG_PREFIXES):
            hits.append(path)
        elif path in tokens or any(t == path or t.startswith(path + "/") for t in tokens):
            hits.append(path)
    return hits


def _lane_commits(root, base, branch):
    """Commit COUNTS across the base/branch fork. -> dict, or None if git cannot answer.

    {"ahead": commits on branch not on base, "behind": commits on base since the
     fork, "fork": the merge-base sha, "base_files": files base changed since it}

    Every question the harness asked about a branch was ancestry-shaped -- "is it
    merged", "does it merge clean" -- and ancestry cannot count. A branch with
    ZERO commits is an ancestor of base, merges clean, and reads MERGED, so a
    lane that was provisioned and never dispatched is indistinguishable from one
    that delivered. The reciprocal question was asked nowhere: `git merge-tree`
    is clean against a base the worker forked from 27 commits ago, because
    textual mergeability is unaffected by a stale base.

    None -- never 0 -- when git cannot answer (an unborn base, a branch that is
    gone). A count of zero is a finding; "unknown" must never be reported as one,
    and must never veto.
    """
    fork = _git(root, "merge-base", base, branch)
    if fork.returncode != 0:
        return None
    fork_sha = (fork.stdout or "").strip()
    if not fork_sha:
        return None
    ahead = _git(root, "rev-list", "--count", "%s..%s" % (base, branch))
    behind = _git(root, "rev-list", "--count", "%s..%s" % (fork_sha, base))
    if ahead.returncode != 0 or behind.returncode != 0:
        return None
    try:
        n_ahead = int((ahead.stdout or "").strip())
        n_behind = int((behind.stdout or "").strip())
    except ValueError:
        return None
    files = []
    if n_behind:
        # --no-renames and -z for the reasons the protected-path diff spells out:
        # rename detection hides a move's source, and a non-ASCII path comes back
        # \-escaped and quoted without -z, matching no prefix it actually lives under.
        out = _git(root, "diff", "--no-renames", "--name-only", "-z",
                   "%s..%s" % (fork_sha, base), "--")
        if out.returncode == 0:
            files = [p for p in (out.stdout or "").split("\0") if p.strip()]
    return {"ahead": n_ahead, "behind": n_behind, "fork": fork_sha, "base_files": files}


def _worktree_status(path):
    """(dirty lines, HEAD sha, error) for a lane's worktree; error is None on success.

    A tip pins what was COMMITTED and says nothing about what sits uncommitted
    beside it. Measured: a lane's verifier was briefed at a pinned SHA over a
    worktree carrying 112 uncommitted lines across two files, and the "verified
    facts" it was handed -- 46 tests green, `verify` exit 0 -- had all been
    measured against those bytes. Every other check in cmd_check_branch reads the
    COMMIT, which is precisely where the uncommitted bytes are not, so nothing in
    the collection sequence could have revealed it.

    Untracked directories stay COLLAPSED -- the one place this deliberately
    diverges from _swarm_dirty's -uall. That flag is right for a small STATE_DIR;
    a provisioned lane worktree holds a .venv or node_modules, and where the
    repo's .gitignore omits one (Phase 4's ignore warning, and the live run where
    nine dependency files rode onto a branch) -uall prints tens of thousands of
    paths in place of the single `?? .venv/` an operator can act on.

    Unknown does NOT keep a tree here, and the inversion from every other "git
    could not answer" in this file is the point: those are questions about a repo
    nobody named, while this path was TYPED. The caller vetoes on error, because
    a gate reporting clean about a tree it never read is the mistyped --ticket in
    another costume.
    """
    def _why(proc, fallback):
        return ((proc.stderr or "").strip().splitlines() or [fallback])[0]

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return [], "", "it is not a directory"
    top = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        return [], "", _why(top, "it is not inside a git worktree")
    top_path = (top.stdout or "").strip()
    if os.path.realpath(top_path) != os.path.realpath(path):
        return [], "", ("it is a subdirectory of the worktree rooted at %s — name that "
                        "root, so the status this gate reads is the whole lane's tree"
                        % top_path)
    st = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                        capture_output=True, text=True)
    if st.returncode != 0:
        return [], "", _why(st, "git status exited %d" % st.returncode)
    head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        return [], "", _why(head, "git rev-parse HEAD exited %d" % head.returncode)
    return ([ln for ln in (st.stdout or "").splitlines() if ln.strip()],
            (head.stdout or "").strip(), None)


def cmd_check_branch(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    base = args.base
    try:
        # --no-renames is load-bearing: git's DEFAULT rename detection prints
        # ONLY a rename's destination, so `git mv .swarm-loop/acceptance/x.py
        # src/x.py` moves a protected file out from under the veto and the
        # protected-path match never sees the source. Spelled --no-renames
        # rather than -M0 so a repo-level diff.renames=true cannot re-enable it.
        # -z + the "--" terminator also close a quotePath hole: without them a
        # non-ASCII path comes back \-escaped and quoted, and no longer matches
        # the protected prefix it actually lives under.
        out = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z",
             f"{base}...{args.branch}", "--"],
            capture_output=True, text=True, check=True, cwd=root,
        )
    except subprocess.CalledProcessError as e:
        die(f"git diff failed: {e.stderr.strip()}")
    changed = [p for p in out.stdout.split("\0") if p.strip()]
    # Same artifact filter freeze/verify use. task-packets.md tells the worker to
    # RUN the frozen acceptance tests "as often as you like"; doing so writes
    # .swarm-loop/acceptance/__pycache__/*.pyc, and classifying those as a
    # harness tamper vetoed a worker for obeying its own packet. verify never
    # flagged them (iter_files skips caches) — the two halves of one protection
    # model must not disagree.
    artifacts = [p for p in changed if is_artifact_path(p)]
    changed = [p for p in changed if not is_artifact_path(p)]
    protected_hits = []
    swarm_hits = []
    for path in changed:
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in state["protected_paths"]):
            protected_hits.append(path)
        elif path == STATE_DIR or path.startswith(STATE_DIR + "/"):
            swarm_hits.append(path)

    config_hits = _metric_config_hits(root, changed)
    lane = _lane_commits(root, base, args.branch)

    print(f"branch {args.branch} vs {base}: {len(changed)} changed file(s)"
          + ("" if lane is None else
             f", {lane['ahead']} commit(s) ahead / {lane['behind']} behind")
          + (f" ({len(artifacts)} tool-cache/bytecode path(s) ignored)" if artifacts else ""))
    for path in changed:
        tag = "  "
        if path in protected_hits:
            tag = "!!"
        elif path in swarm_hits:
            tag = " !"
        elif path in config_hits:
            tag = " ?"
        print(f" {tag} {path}")
    # The COUNT, not the ancestry. Checked before the protected-path veto because
    # it is the more fundamental statement: an empty branch cannot touch a
    # protected path, and reporting it as OK is how a lane that delivered nothing
    # got integrated as delivered work. `is None` is tested, never falsiness --
    # a git that could not answer must not veto.
    if lane is not None and lane["ahead"] == 0:
        print(f"VETO: {args.branch} has NO COMMITS of its own vs {base} — "
              f"`git rev-list --count {base}..{args.branch}` is 0.")
        print(f"  Ancestry cannot see this: a branch with no commits IS an ancestor of "
              f"{base}, merges clean, and reads MERGED everywhere else the harness looks.")
        print("  Either the worker never committed — a failed or hung task: reclaim the "
              "ticket to the backlog and re-dispatch it — or this branch already landed "
              "and check-branch is running after the merge, in which case there is nothing "
              "left to review. Both are refusals: merging it delivers nothing.")
        print(f"  Tell them apart with:  git reflog {args.branch}   and   "
              f"{STATE_DIR}/{DISPATCH_FILE}")
        sys.exit(1)
    if protected_hits:
        print("VETO: branch modifies the frozen goal harness — do not merge; return task to backlog")
        sys.exit(1)
    if swarm_hits:
        print(f"VETO: branch writes orchestrator-owned state under {STATE_DIR}/ — do not merge")
        sys.exit(1)
    # Optional, like --ticket below, and for the same reason: the three vetoes
    # above are statements about the DIFF and hold whatever tree the lane left
    # behind, while this one has to be TOLD which tree to read. It is NOT
    # discovered from `git worktree list`, deliberately — the integration
    # branch's own worktree is legitimately dirty mid-merge and the primary
    # checkout is dirty for most of a run (every epoch writes under STATE_DIR),
    # so a gate that found its own tree would refuse the protocol's own
    # integration checks. Naming the path is the operator asserting WHICH tree
    # produced this branch's numbers.
    if args.worktree:
        dirty, wt_head, wt_err = _worktree_status(args.worktree)
        if wt_err:
            print(f"VETO: --worktree {args.worktree} could not be read as a worktree "
                  f"root: {wt_err}.")
            print("  A path this gate cannot query proves NOTHING about the bytes that "
                  "produced this branch's reported greens, and accepting it silently is "
                  "a typo DISARMING the check — the same disarm a mistyped --ticket "
                  "used to be. Give the lane's worktree root, or omit the flag and "
                  "establish provenance by hand before briefing anyone at this tip.")
            sys.exit(1)
        # Identity before cleanliness: a listing of dirt in some OTHER tree sends
        # the operator to the wrong lane to ask for a commit.
        tip = _git(root, "rev-parse", "--verify", f"{args.branch}^{{commit}}")
        tip_sha = (tip.stdout or "").strip() if tip.returncode == 0 else ""
        if tip_sha and wt_head != tip_sha:
            moved = bool(wt_head) and _git(root, "merge-base", "--is-ancestor",
                                           tip_sha, wt_head).returncode == 0
            if moved:
                print(f"VETO: {args.worktree} IS this branch's tree, and it has MOVED "
                      f"past the ref you are checking — the tree is at {wt_head[:12]}, "
                      f"you asked about {tip_sha[:12]}.")
                print("  This is step zero's own failure, caught mechanically for the "
                      "first time: three tips moved mid-collection in one run — one of "
                      "the new commits was the fix for the branch's worst blocker — and "
                      "every one was caught by eyeballing a commit count. The collection "
                      "is VOID: re-pin the tip and redo every check against the new SHA.")
            else:
                print(f"VETO: {args.worktree} is a worktree at {wt_head[:12]}, which is "
                      f"neither {tip_sha[:12]} nor a descendant of it — that tree did "
                      "not build this branch.")
                print("  A clean status about the wrong tree establishes provenance for a "
                      "branch nobody is merging. Point --worktree at the lane that built "
                      f"{args.branch}.")
            sys.exit(1)
        if dirty:
            print(f"VETO: the worktree at {args.worktree} is DIRTY — {len(dirty)} "
                  "uncommitted path(s) sitting beside the commit you are merging:")
            for ln in dirty[:40]:
                print(f"    {ln}")
            if len(dirty) > 40:
                print(f"    ... and {len(dirty) - 40} more")
            print("  A tip pins what was COMMITTED and says nothing about what sits "
                  "uncommitted beside it, and every other check here reads the commit — "
                  "which is precisely where these bytes are not. Measured: a verifier "
                  "briefed at a pinned SHA over a tree carrying 112 uncommitted lines "
                  "was handed 46 green tests and a `verify` exit 0 as FACTS, and every "
                  "one of them had been measured against bytes that SHA does not "
                  "contain.")
            print("  Ask the LANE for the commit, re-pin, and re-measure. Do not commit "
                  "it yourself: a tip you wrote is not the lane's delivery, and it "
                  "merges the one diff no worker's report covers.")
            print("  A `??` line naming a provisioned directory (.venv/, node_modules/) "
                  "is the Phase 4 ignore check failing HERE instead of at the worker's "
                  "next commit — fix .gitignore, then re-run this.")
            sys.exit(1)
        print(f"worktree {args.worktree}: CLEAN at {wt_head[:12]} — this branch's "
              "reported numbers can be attributed to the commit being merged")
    # Optional and last of the vetoes: the three DIFF vetoes above are
    # statements about the diff and hold whatever the ticket is, while this one
    # needs to be told which gate it is judging. Placed after them so a branch
    # that tampers with the frozen harness is still refused for that, not for
    # its paperwork.
    if args.ticket:
        # THE ID IS VALIDATED AGAINST THE GRAPH FIRST. It was not, and cmd_check_branch
        # did not load the graph at all — so an unknown or mistyped id had no stamps
        # and no dispatch record, `when` came back None, and the gate took its
        # "predates the ledger" advisory branch and exited 0. A one-character typo
        # (`T-l` for `T-1`) silently disarmed the merge gate while printing a
        # reassuring line, and nothing in the output told that apart from a genuine
        # resumed-run advisory. An id the graph has never heard of is not a ticket
        # this ledger predates; it is a question about nothing.
        # SystemExit is caught on purpose. _load_graph `die`s on a graph that is
        # not readable JSON, which is right for red-check (the graph IS its
        # input) and wrong here: check-branch's three real vetoes are statements
        # about the DIFF and hold whatever the graph says, so a broken
        # tickets.json must not take down a merge gate that was working before
        # this validation existed. Degrade to the same advisory as "no graph".
        try:
            gpath, gtickets = _load_graph(root, state, getattr(args, "graph", None),
                                          required=False)
        except SystemExit:
            gpath, gtickets = None, None
            print("ADVISORY — the ticket graph could not be read (see the error above), so "
                  f"--ticket {args.ticket} could not be validated against it.")
        if gtickets is None:
            print(f"ADVISORY (not a veto) — --ticket {args.ticket} could NOT be validated: "
                  "no ticket graph was found, so this run cannot tell a real id from a typo. "
                  "The red-check gate below is only as good as the id it was given.")
        else:
            known = {str(t.get("id")).strip() for t in gtickets
                     if isinstance(t, dict) and t.get("id")}
            if args.ticket not in known:
                print(f"VETO: --ticket {args.ticket!r} is not a ticket in "
                      f"{os.path.relpath(gpath, root)}.")
                print("  A ticket id nobody has heard of has no stamps and no dispatch "
                      "record, which reads to the gate below exactly like a ticket that "
                      "predates the ledger — an advisory and exit 0. That is a typo "
                      "DISARMING the merge gate, so it is refused here instead.")
                near = sorted(k for k in known
                              if k.lower() == args.ticket.lower()
                              or k.replace("-", "") == args.ticket.replace("-", ""))
                if near:
                    print("  Did you mean: " + ", ".join(near[:5]))
                sys.exit(1)
        if _redcheck_gate(root, d, args.ticket, base, args.branch,
                          (lane or {}).get("fork")):
            sys.exit(1)
    if config_hits:
        print("ADVISORY (not a veto) — these files can steer a frozen metric without "
              "appearing in any test diff:")
        for path in config_hits:
            print(f"    ? {path}")
        print("    Read each diff: a narrowed test path, an added ignore/omit/exclude, a "
              "changed runner target, or a wiped addopts moves the metric without moving "
              "the code. Legitimate config work is normal — record the justification in the "
              "collection notes, do not reflex-veto.")
    # The reciprocal count. A branch is not safe to integrate merely because it
    # merges: `git merge-tree` is clean against a base the branch forked from 27
    # commits ago, and every test the worker ran to prove itself green ran against
    # that vanished tree. Warn-only, with the number and the intersecting files,
    # because a stale base is normal in a long wave and only sometimes matters.
    if lane is not None and lane["behind"]:
        overlap = sorted(set(lane["base_files"]) & set(changed))
        print(f"ADVISORY (not a veto) — STALE BASE: {args.branch} forked at "
              f"{lane['fork'][:12]}; {base} has moved {lane['behind']} commit(s) over "
              f"{len(lane['base_files'])} file(s) since.")
        if overlap:
            print(f"    {len(overlap)} of those file(s) are ALSO changed on this branch:")
            for path in overlap[:20]:
                print(f"    ~ {path}")
            if len(overlap) > 20:
                print(f"    ... and {len(overlap) - 20} more")
            print("    A clean merge does NOT clear this — textual mergeability says nothing "
                  "about a stale base. The branch was built and self-verified against a tree "
                  f"that no longer exists. Rebase onto {base} and re-run its verify before "
                  "integrating, or read each overlapping file's diff on both sides.")
        else:
            print("    None of them intersect this branch's own changes — a statement about "
                  "TEXT, and NOT a clearance. What an empty intersection cannot see is this "
                  "lane's tests depending on semantics a sibling changed (a validation rule "
                  "tightened, a default flipped, an error type swapped, a serialization "
                  "narrowed) in files the lane never touches: the merge stays clean, and the "
                  f"branch fails at runtime or passes a weaker assertion than {base} now "
                  f"requires. Its verify ran against a {base} that is {lane['behind']} "
                  "commit(s) old either way.")
            print("    Grep this lane's own directory for REFERENCES to the modules that "
                  "moved on the base — imports, error types, constants, fixture "
                  "factories. An empty intersection is a statement about text; the "
                  "lane's tests encode expectations about behaviour.")
            print(f"    Read {base}'s diff for BEHAVIOURAL change, not for moved paths, before "
                  "clearing the lane. If you send the lane back, hand it the ancestry FACT — "
                  f"forked at {lane['fork'][:12]}, {base} moved {len(lane['base_files'])} "
                  "file(s) since — and let the LANE measure the impact: an inference about "
                  "impact, shipped as if it were measured, is its own defect, and the lane is "
                  "the one standing in the tree where the impact can be measured.")
    print("check-branch: OK (review the file list above against the task's ownership map)")


# ----------------------------------------------------------- measure/record

def _append_history(root, rows):
    """Append rows, writing the header first if the file is missing or empty.

    Self-healing because it has to be: this used to trust that cmd_init had
    written the header. If history.csv ever went missing, an append recreated it
    HEADERLESS, csv.DictReader then adopted the first DATA row as the field
    names, the first measurement of the run vanished into no_data, and every
    later analyze/resume died with a bare KeyError several cycles downstream."""
    path = os.path.join(sd(root), HISTORY_FILE)
    need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    # P-14: the rows are serialized first and appended in ONE write. Writing them
    # one at a time through the handle let two concurrent 'measure' runs
    # interleave rows mid-file; a single write to an O_APPEND handle is not
    # interleaved for a buffer this size.
    buf = io.StringIO()
    w = csv.writer(buf)
    if need_header:
        w.writerow(HISTORY_HEADER)
    for r in rows:
        w.writerow(r)
    with open(path, "a", newline="") as f:
        f.write(buf.getvalue())


def _history_reader(f, path):
    """DictReader over history.csv with the header VALIDATED, not assumed.

    Old files carry the first five columns only (no 'source'); those are fine
    and read as source=measure. Anything else is a truncated or recreated file,
    and failing here with the reason beats a KeyError three commands later."""
    r = csv.DictReader(f)
    got = list(r.fieldnames or [])
    if got[:5] != HISTORY_HEADER[:5]:
        die(f"{path} has no usable header row (found {got!r}; expected {HISTORY_HEADER!r}). "
            "The file was truncated or recreated without its header — restore it from git "
            "before scoring anything against it.")
    return r


def _row_source(row):
    """'measure' | 'record'. Rows written before the column existed are measures."""
    return (row.get("source") or "measure").strip() or "measure"


def _row_int(row, key):
    """An optional integer column, or None when it is absent/blank/unparseable.

    None is the load-bearing answer, and it is NOT 0: a row written before the
    selection columns existed, a hand-entered `record`, and a non-pytest metric
    all say "this row carries no selection fact". Reading any of them as 0 would
    make the first pytest row that deselects anything look like a regression from
    a baseline that was never measured."""
    raw = row.get(key)
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _error(value, target, direction):
    if direction == "maximize":
        return max(0.0, target - value)
    return max(0.0, value - target)


def _target(m):
    """The metric's target as a float, at every point of USE.

    P-10/D6: freeze's validator checked that `target` PARSES as a finite number
    and then three print paths applied `:g` to the RAW JSON value, so a
    hand-edited `"target": "120"` froze cleanly and died with
    `ValueError: Unknown format code 'g' for object of type 'str'` — in `measure`
    AFTER the metric command had run and BEFORE _append_history, discarding the
    whole cycle's rows, and in `analyze` AFTER cycle-N.json was already written.

    Coercing HERE rather than rewriting goals.json is deliberate: goals.json is
    inside the frozen manifest, so normalizing its bytes would either invalidate
    an existing freeze or need an amendment nobody asked for — and a run already
    frozen with a string target would stay broken. This fixes it for those runs
    too, and changes no protected byte."""
    return float(m["target"])


def _tol(m):
    """Tolerance as a float. `or 0` because a "tolerance": null in an
    ALREADY-frozen goals.json is a TypeError here, and crashing analyze on every
    retry is an infinite loop (resume's advice for a measured-but-unanalyzed
    cycle is "run analyze --cycle N"). freeze rejects it outright now."""
    return float(m.get("tolerance") or 0)


def _pending_remeasure(state):
    """Cycles whose measurements an amendment invalidated. Optional key: absence
    means nothing is pending, so state.json written by any earlier version — the
    paused run's included — reads as clean."""
    v = state.get("pending_remeasure") or []
    out = []
    for c in v:
        try:
            out.append(int(c))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def cmd_measure(args):
    root = repo_root()
    d = sd(root)
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    if not goals.get("frozen"):
        die("goals are not frozen — freeze before measuring so scores are against locked targets")
    state = load_json(os.path.join(d, STATE_FILE), "state")
    # A3/P-4: the numbers this command produces are the whole scoreboard. It must
    # not produce one against a harness it has not checked.
    _integrity_gate(root, d, state, goals, "measure")
    _dupe_metric_ids(goals, warn_only=True)
    rows, failures = [], []
    print(f"measuring cycle {args.cycle}:")
    for m in goals["metrics"]:
        tmo = _metric_timeout(m, args.timeout)
        try:
            proc = _run_metric_command(m["command"], root, tmo)
        except subprocess.TimeoutExpired:
            failures.append(m["id"])
            print(f"  {m['id']:<28} FAILED: timeout after {tmo}s")
            continue
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        try:
            value = float(lines[-1])
            # nan/inf parse fine but poison every downstream comparison: _error's
            # max(0.0, ...) turns nan into 0.0, which scores as at_target, and an
            # inf error projects to 0 and reads as "converging_on_track".
            if not math.isfinite(value):
                raise ValueError("non-finite")
        except (IndexError, ValueError):
            failures.append(m["id"])
            bad = repr(lines[-1]) if lines else "<no output>"
            print(f"  {m['id']:<28} FAILED to produce a finite number "
                  f"(exit {proc.returncode}); last line: {bad}")
            if proc.stderr.strip():
                print("      stderr: " + proc.stderr.strip().splitlines()[-1][:200])
            continue
        err = _error(value, _target(m), m["direction"])
        # SKILL.md's rule ("record PASSED / SKIPPED / DESELECTED beside every
        # binary gate reading — a gate's number is uninterpretable without
        # knowing what it did not run"), enforced instead of merely written.
        # WARN-ONLY here, deliberately and permanently: measure is on the live
        # path, a run must never be stopped mid-flight by a selection change, and
        # the exit code below still belongs to "did the command produce a
        # number". The teeth are at freeze, where a false positive costs a flag —
        # and now also at analyze, which can see across cycles what a single
        # measurement cannot: that the selection CHANGED after the freeze.
        ms_sel, ms_desel, ms_pytest = _selection_facts(
            (proc.stdout or "") + (proc.stderr or ""))
        ms_note = _selection_note(ms_sel, ms_desel, ms_pytest)
        # Stamped HERE, per metric, not once above the loop. Every row of a
        # cycle used to carry the sweep's START time, so history.csv could not
        # show per-metric duration — and the timestamp is not decoration: it is
        # what analyze compares against `last_amended_at` to decide a row is
        # pre-amendment and therefore stale.
        #
        # The selection columns are EMPTY — not 0 — for a command whose output is
        # not pytest-shaped at all. "this metric is `wc -l` and there was nothing
        # to parse" and "pytest selected zero tests" are different facts, and
        # collapsing them to 0 would manufacture a selection regression on every
        # non-pytest metric. But when the output IS pytest-shaped, a missing
        # `N deselected` token means ZERO deselected and is recorded as 0: that
        # is the whole comparison the incident needs, because the honest baseline
        # (`2 failed in 0.03s`) prints no deselection count at all and would
        # otherwise be unknown — leaving nothing for cycle 1's `2 deselected` to
        # be a regression FROM.
        rows.append([args.cycle, m["id"], value, err,
                     time.strftime("%Y-%m-%dT%H:%M:%S"), "measure",
                     (ms_sel or 0) if ms_pytest else "",
                     (ms_desel or 0) if ms_pytest else ""])
        mark = "OK " if err == 0 else "   "
        print(f"  {m['id']:<28} value={value:<12g} target={_target(m):<10g} "
              f"error={err:<12g} {mark}" + (f" [{ms_note}]" if ms_note else ""))
        if ms_desel:
            print(f"      WARNING: {m['id']} DESELECTED {ms_desel} test(s) this cycle — "
                  "this number is partly a statement about a filter, not about the code. "
                  "A filter added to pytest.ini / pyproject.toml / conftest.py moves it "
                  "without touching a frozen byte or appearing in any test diff.")
        elif ms_pytest and not ms_sel:
            print(f"      WARNING: {m['id']} ran pytest and pytest SELECTED NO TESTS — "
                  "whatever this value is, it was not observed.")
    _append_history(root, rows)
    if failures:
        print(f"measure: {len(failures)} metric command(s) failed: {', '.join(failures)}")
        print("a failing measurement command is itself a defect — fix the command or the code it measures")
        sys.exit(1)
    # D2: a complete measurement of this cycle is what clears the re-measure
    # `freeze --amend` demanded. Only a COMPLETE one — a partial measure leaves
    # the amended targets unmeasured for some metric, which is the state the flag
    # exists to keep visible.
    pending = _pending_remeasure(state)
    if args.cycle in pending:
        with _state_lock(d):
            spath = os.path.join(d, STATE_FILE)
            live = load_json(spath, "state")
            left = [c for c in _pending_remeasure(live) if c != args.cycle]
            if left:
                live["pending_remeasure"] = left
            else:
                live.pop("pending_remeasure", None)
            save_json(spath, live)
        print(f"measure: cycle {args.cycle} re-measured after the amendment that moved a "
              "target — pending_remeasure cleared"
              + (" (still pending: %s)" % ", ".join(str(c) for c in left) if left else ""))
    print(f"measure: recorded {len(rows)} metric(s) for cycle {args.cycle}")


def cmd_record(args):
    """Hand-enter one metric value. The documented escape hatch for a metric that
    genuinely cannot be measured — and, unguarded, a way to overwrite the whole
    scoreboard, because analyze keeps the LAST row per cycle in file order: a
    record issued after measure for the same cycle silently outranks the real
    measurement, and verify stays green because history.csv is not in the
    manifest. Hence the same gates measure has, plus the 'record' provenance mark
    that makes analyze say so."""
    root = repo_root()
    d = sd(root)
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    if not goals.get("frozen"):
        die("goals are not frozen — freeze before recording so values are against locked targets")
    state = load_json(os.path.join(d, STATE_FILE), "state")
    # The manifest-missing→VIOLATED promotion this command used to carry for
    # itself now lives inside _verify_quiet, so measure/analyze/resume behave
    # identically (A4/P-5).
    _integrity_gate(root, d, state, goals, "write to the scoreboard")
    metric = next((m for m in goals["metrics"] if m["id"] == args.metric), None)
    if metric is None:
        die(f"unknown metric id '{args.metric}'")
    if not math.isfinite(args.value):
        die(f"refusing to record non-finite value {args.value!r} for '{args.metric}'")
    err = _error(args.value, _target(metric), metric["direction"])
    # Selection columns left EMPTY on purpose: a hand-entered value observed no
    # test run, so it has no selection facts. Writing 0 would claim it did.
    _append_history(root, [[args.cycle, args.metric, args.value, err,
                            time.strftime("%Y-%m-%dT%H:%M:%S"), "record", "", ""]])
    print(f"recorded {args.metric}={args.value} (error={err}) for cycle {args.cycle}")
    pending = _pending_remeasure(state)
    if pending:
        print("NOTE: cycle(s) %s are still awaiting the re-measure that "
              "'freeze --amend' demanded when it moved a target — a hand-entered row does "
              "NOT satisfy it, and analyze withholds all-targets-met until 'measure --cycle "
              "<n>' has run." % ", ".join(str(c) for c in pending))
    print("marked source=record: analyze will flag this metric as hand-entered and will NOT "
          "declare all targets met off it without --trust-manual-records. Justify it in the "
          "cycle report.")


# ----------------------------------------------------------------- analyze

def _ols(points):
    """Least squares over (x, y) points. Returns slope, intercept, r2."""
    n = len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    if syy == 0:
        r2 = 1.0
    else:
        ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
        r2 = 1.0 - ss_res / syy
    return slope, intercept, r2


# stale/no_data sort ABOVE regressing: "we do not know" outranks every verdict
# derived from numbers, because it is the one state where the numbers shown are
# not about the cycle being analyzed at all.
SEVERITY = {"stale": -1, "no_data": -1,
            "regressing": 0, "stalled": 1, "converging_off_track": 2,
            "converging_on_track": 3, "insufficient_data": 4, "at_target": 5}


def _verdict(err_points, tolerance, max_cycles):
    latest_err = err_points[-1][1]
    n = len(err_points)
    if latest_err <= tolerance:
        return "at_target", None, None, None
    if n < 2:
        return "insufficient_data", None, None, None
    slope, intercept, r2 = _ols(err_points)
    projected = max(0.0, intercept + slope * max_cycles)
    if slope > SLOPE_EPS:
        return "regressing", slope, projected, r2
    if abs(slope) <= SLOPE_EPS:
        return "stalled", slope, projected, r2
    if projected <= tolerance:
        return "converging_on_track", slope, projected, r2
    return "converging_off_track", slope, projected, r2


def cmd_analyze(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    if not goals.get("frozen"):
        die("goals are not frozen — a verdict against unlocked targets means nothing; freeze first")
    # A3/H-2: analyze can print ALL TARGETS MET and exit 2 — the terminal
    # build→audit signal. It may not do that without stating what it verified.
    integrity, integrity_notes = _integrity_gate(root, d, state, goals, "score a cycle")
    _dupe_metric_ids(goals, warn_only=True)
    max_cycles = state["max_cycles"]
    amended_at = str(goals.get("last_amended_at") or "")

    history = {}
    hpath = os.path.join(d, HISTORY_FILE)
    if not os.path.exists(hpath) or os.path.getsize(hpath) == 0:
        die(f"{STATE_DIR}/{HISTORY_FILE} is missing or empty — nothing has been measured, so "
            f"there is no cycle {args.cycle} to analyze. Run 'measure --cycle {args.cycle}' first.")
    with open(hpath) as f:
        for row in _history_reader(f, hpath):
            c = int(row["cycle"])
            # Analyze the run AS OF --cycle: later rows (a re-measure of a newer
            # cycle) must not leak into a re-analysis of an earlier one.
            if c > args.cycle:
                continue
            v, e = float(row["value"]), float(row["error"])
            if not (math.isfinite(v) and math.isfinite(e)):
                die(f"history.csv cycle {c} metric '{row['metric_id']}' holds a non-finite "
                    f"value/error ({row['value']}/{row['error']}) — a poisoned row, not a "
                    "measurement; fix the metric command and re-measure that cycle")
            history.setdefault(row["metric_id"], []).append(
                (c, v, e, _row_source(row), str(row.get("timestamp") or ""),
                 _row_int(row, "selected"), _row_int(row, "deselected")))

    results = []
    pre_amendment_metrics, recomputed, selection_regressions = [], [], []
    for m in goals["metrics"]:
        # Keep the LAST measurement per cycle in FILE order -- history.csv is
        # append-only, so file order is recency. (Sorting the tuples first, as
        # this once did, keeps the largest value instead, so a stale optimistic
        # reading silently survives a corrective re-measure.) Sort by cycle only
        # after the dedupe.
        # D1/P-1/H-3: the `error` column is recomputed from `value` against the
        # target IN FORCE NOW, never consumed as stored. Error was baked in at
        # measure time, so any `freeze --amend` that moved a target silently
        # invalidated every earlier row while analyze went on eating them. The
        # live near-miss: amendment 1 raised acceptance_collected 103→120 and the
        # two cycle-0 rows read `value 103, error 0.0` — zero because 103 HAD
        # been the target. `analyze --cycle 0` before the re-measure would have
        # read latest_err = 0.0 <= tol(0) and rendered a metric 17 short of
        # target as at_target, with the stale guard silent because the row WAS
        # for cycle 0. Worse over time: _ols was fitting one line through error
        # points measured against two different targets, manufacturing a
        # converging_on_track out of a lowered target and a regressing out of a
        # raised one.
        tgt = _target(m)
        by_cycle = {}
        for c, v, e_stored, src, ts, n_sel, n_desel in history.get(m["id"], []):
            e = _error(v, tgt, m["direction"])
            if abs(e - e_stored) > 1e-9:
                recomputed.append(m["id"])
            by_cycle[c] = (v, e, src, ts, n_sel, n_desel)
        pts = [(c, v, e, src, ts, n_sel, n_desel)
               for c, (v, e, src, ts, n_sel, n_desel) in sorted(by_cycle.items())]
        if not pts:
            results.append({"id": m["id"], "verdict": "no_data",
                            "latest_cycle": None, "latest_source": None})
            continue
        err_points = [(c, e) for c, _v, e, _s, _t, _ns, _nd in pts]
        tol = _tol(m)
        verdict, slope, projected, r2 = _verdict(err_points, tol, max_cycles)
        # A verdict is only about THIS cycle if THIS cycle produced a row. When
        # 'measure --cycle N' fails (wholly or for one metric) it appends nothing
        # and exits 1; without this check the newest surviving row — possibly
        # several cycles old — is reported under the cycle-N heading, and can even
        # declare ALL TARGETS MET off a measurement that never ran.
        if pts[-1][0] != args.cycle:
            verdict, slope, projected, r2 = "stale", None, None, None
        # D1's second half. Recomputing error fixes the arithmetic; it cannot fix
        # a VALUE measured against a suite that no longer exists. A newest row
        # older than the last amendment is a reading of the previous harness, so
        # it is marked stale — which withholds all_targets_met and exits 1 with
        # "re-measure", exactly what H-09 says the operator did by hand and
        # nothing required. Strictly `<`: a re-measure inside the amendment's own
        # second is a re-measure.
        pre_amend = bool(amended_at and str(pts[-1][4]) < amended_at)
        if pre_amend:
            pre_amendment_metrics.append(m["id"])
            verdict, slope, projected, r2 = "stale", None, None, None
        # THE SELECTION REGRESSION, and it is the only part of the deselection
        # story a single measurement cannot see. freeze's teeth are INTAKE-ONLY
        # by construction — the measured attack adds the filter to pytest.ini
        # AFTER the freeze, so the gate never sees it — and measure's warning
        # went to stdout and nowhere else. Across cycles the change IS visible:
        # a metric that deselected NOTHING when it was frozen and deselects
        # something now had its selection edited after the freeze, from a file
        # no protected path covers and no test diff shows.
        #
        # Compared only between rows that BOTH carry the column (None = written
        # by an older version, or by a non-pytest command, and never compared as
        # if it were 0). A run frozen with `--allow-deselection` deselects from
        # cycle 0 onward at a constant count and is therefore NOT a regression —
        # which is what keeps this from wedging the house-rule case freeze
        # already accepted out loud.
        desel_now = pts[-1][6]
        base_desel = next((nd for _c, _v, _e, _s, _t, _ns, nd in pts if nd is not None),
                          None)
        selection_regressed = (desel_now is not None and base_desel is not None
                               and desel_now > base_desel)
        if selection_regressed:
            selection_regressions.append({
                "metric": m["id"], "baseline_deselected": base_desel,
                "latest_deselected": desel_now, "latest_selected": pts[-1][5],
                "latest_cycle": pts[-1][0],
            })
        results.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "direction": m["direction"],
            "target": tgt,
            "latest_cycle": pts[-1][0],
            "latest_value": pts[-1][1],
            "latest_error": pts[-1][2],
            "latest_source": pts[-1][3],
            "latest_timestamp": pts[-1][4],
            "latest_selected": pts[-1][5],
            "latest_deselected": desel_now,
            "selection_regressed": selection_regressed,
            "pre_amendment": pre_amend,
            "n_points": len(pts),
            "error_slope_per_cycle": slope,
            "r2": r2,
            "projected_error_at_final_cycle": projected,
            "verdict": verdict,
            "history": [{"cycle": c, "value": v, "error": e, "source": s, "timestamp": t,
                         "selected": ns, "deselected": nd}
                        for c, v, e, s, t, ns, nd in pts],
        })

    results.sort(key=lambda r: (SEVERITY.get(r["verdict"], 9), -float(r.get("latest_error") or 0)))
    n_at_target = sum(1 for r in results if r["verdict"] == "at_target")
    stale_metrics = [r["id"] for r in results if r["verdict"] in ("stale", "no_data")]
    # A hand-entered row outranks the real measurement for its cycle (analyze
    # keeps the last row per cycle). Legitimate as a documented escape hatch,
    # never as the basis of a terminal ALL-TARGETS-MET signal nobody approved.
    manual_metrics = [r["id"] for r in results if r.get("latest_source") == "record"]
    # D2/H-09: an amendment that moved a target invalidates its own baseline, and
    # nothing required the re-measure — this run got the right numbers only
    # because the operator re-ran measure 23 seconds later, by hand. The pending
    # list is written by `freeze --amend` and cleared by a complete `measure`; it
    # cannot be cleared by a hand-entered `record`.
    pending = _pending_remeasure(state)
    # `not selection_regressions` is the teeth the incident never had. Replayed
    # on this branch before it existed: honest freeze, cycle 0 value=2/error=2,
    # then `printf '[pytest]\naddopts = -m "not slow"\n' > pytest.ini` with src/
    # UNTOUCHED — verify said "OK — frozen harness intact", cycle 1 recorded
    # value=0/error=0, and `analyze --cycle 1` printed "Targets met: 1/1" and
    # "**ALL TARGETS MET**". Nothing in history.csv, cycle-1.json or cycle-1.md
    # contained the string "desel". The number was real, the harness was intact,
    # and the metric had stopped measuring the code.
    all_done = (n_at_target == len(results) and results and not stale_metrics
                and not pending and not selection_regressions
                and (not manual_metrics or args.trust_manual_records))

    # An open escalation against a metric says the orchestrator believes THAT
    # metric's goal (or the test behind it) is wrong and has asked the user to
    # change it. It is a FLAG, never a score change: the verdicts and all_done
    # above are computed exactly as before, deliberately, because an agent that
    # could move its own verdict by filing a complaint is an agent that can move
    # goalposts. All this does is stop a verdict on a disputed goal from being
    # read as gospel — by the next cycle's steering, or by the final report.
    esc_by_subject = {}
    for r in _open_escalations(d):
        esc_by_subject.setdefault((r.get("subject") or "").strip(), []).append(r)
    escalated_metrics = []
    for r in results:
        hits = esc_by_subject.get(r["id"], [])
        if not hits:
            continue
        r["escalations"] = [h["id"] for h in hits]
        for h in hits:
            escalated_metrics.append({
                "metric": r["id"], "escalation": h["id"], "kind": h.get("kind"),
                "blocking": bool(h.get("blocking")), "proposed": h.get("proposed"),
            })

    out = {
        "cycle": args.cycle,
        "max_cycles": max_cycles,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "all_targets_met": bool(all_done),
        # New optional keys; every reader uses .get, and an analysis written by
        # an earlier version stays readable (resume only reads all_targets_met +
        # stale_metrics).
        "harness_integrity": integrity,
        "harness_notes": integrity_notes,
        "pending_remeasure": pending,
        "pre_amendment_metrics": sorted(set(pre_amendment_metrics)),
        "errors_recomputed_for": sorted(set(recomputed)),
        "selection_regressions": selection_regressions,
        "stale_metrics": stale_metrics,
        "manual_records": manual_metrics,
        "manual_records_trusted": bool(args.trust_manual_records),
        "escalated_metrics": escalated_metrics,
        "goal_amendments": int(goals.get("amendments", 0)),
        "last_amended_at": goals.get("last_amended_at"),
        "last_amend_reason": goals.get("last_amend_reason"),
        "metrics": results,
        "note": ("Linear fit over per-cycle error. With <=10 points this is trend "
                 "detection, not inference; treat slope/projection as a steering "
                 "signal, not a statistical claim."),
    }
    # git cannot carry an empty directory, so a fresh clone resuming from a
    # committed .swarm-loop/ has no analysis/ at all — and a FileNotFoundError
    # here reads as exit 1, which the docs say means "re-measure": a loop.
    os.makedirs(os.path.join(d, ANALYSIS_DIR), exist_ok=True)
    jpath = os.path.join(d, ANALYSIS_DIR, f"cycle-{args.cycle}.json")
    save_json(jpath, out)

    lines = [f"# Cycle {args.cycle}/{max_cycles} analysis", ""]
    lines.append(f"Targets met: {n_at_target}/{len(results)}")
    # The integrity statement is a LINE OF THE REPORT, not a side effect of a
    # command somebody may or may not have run first: ALL TARGETS MET below can
    # now never be read without it (A3/H-2).
    lines.append("")
    lines.append("Harness integrity at analysis time: **%s** — every frozen file re-hashed "
                 "against `%s/%s`, which is itself reconciled against the append-only "
                 "`%s/%s`." % (integrity, STATE_DIR, MANIFEST_FILE, STATE_DIR, FREEZE_LOG_FILE))
    for n in integrity_notes:
        lines.append("- note: " + n)
    # Printed next to the integrity line, and BEFORE the verdict table, because
    # the two answer different questions and the integrity line was being read as
    # if it answered both: "frozen harness intact" is true of a run whose metric
    # has stopped measuring the code, because the filter that stopped it lives in
    # pytest.ini, which is not a frozen file.
    if selection_regressions:
        lines.append("")
        lines.append("> **TEST SELECTION CHANGED AFTER THE FREEZE — these numbers are partly "
                     "about a filter, not about the code.** ALL TARGETS MET is withheld while "
                     "this stands.")
        for sr in selection_regressions:
            lines.append("> - `%s`: deselected %s at its first measured cycle, %s at cycle %s "
                         "(selected %s). Nothing frozen changed: a `-m` / `-k` / `--ignore` / "
                         "`addopts` filter in pytest.ini, pyproject.toml or conftest.py moves "
                         "this without touching a protected path or appearing in any test diff."
                         % (sr["metric"], sr["baseline_deselected"], sr["latest_deselected"],
                            sr["latest_cycle"], sr["latest_selected"]))
        lines.append("> Re-measure after removing the filter, or — if the deselection is a "
                     "deliberate house rule — say so at the checkpoint and re-freeze the "
                     "metric with `freeze --amend --allow-deselection`, which records the "
                     "decision in the append-only freeze log where a reader can find it.")
    if int(goals.get("amendments", 0)):
        lines.append("")
        lines.append(f"> **GOALPOSTS MOVED {goals['amendments']}x — this trend is not against a "
                     f"single fixed target.** Last amendment {goals.get('last_amended_at')}: "
                     f"{goals.get('last_amend_reason')!r}. Full record: "
                     f"`{STATE_DIR}/{FREEZE_LOG_FILE}`.")
    lines.append("")
    lines.append("| metric | verdict | as of | value | target | error | slope/cycle | proj. final error |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        # An escalation rides in the verdict cell, it does not replace the
        # verdict: the number is still whatever the numbers say, carrying a
        # visible note that the goal it is scored against is under dispute.
        vflag = (" [ESCALATED " + ",".join(r["escalations"]) + "]"
                 if r.get("escalations") else "")
        if r["verdict"] == "no_data":
            lines.append(f"| {r['id']} | no_data{vflag} | never | – | – | – | – | – |")
            continue
        fmt = lambda x: "–" if x is None else f"{x:g}"
        asof = f"cycle {r['latest_cycle']}"
        if r["latest_cycle"] != args.cycle:
            asof += " (STALE)"
        if r.get("latest_source") == "record":
            asof += " (MANUAL)"
        if r.get("pre_amendment"):
            asof += " (PRE-AMENDMENT)"
        lines.append(
            f"| {r['id']} | {r['verdict']}{vflag} | {asof} | {fmt(r['latest_value'])} | {fmt(r['target'])} "
            f"| {fmt(r['latest_error'])} | {fmt(r['error_slope_per_cycle'])} "
            f"| {fmt(r['projected_error_at_final_cycle'])} |")
    lines.append("")
    if stale_metrics:
        lines.append(f"**NO MEASUREMENT AT CYCLE {args.cycle} for: " + ", ".join(stale_metrics) +
                     ". This analysis is NOT a verdict on cycle "
                     f"{args.cycle} — the values shown for those metrics are older rows. "
                     "'measure' failed (it exits 1 and records nothing for a metric whose "
                     "command fails). Fix the metric command or the code it measures and "
                     "re-run measure + analyze; still run 'checkpoint --cycle "
                     f"{args.cycle}' so the stall counter and head history stay intact. "
                     "If the metric is genuinely unmeasurable (goal-setting.md: a frozen test "
                     "that is itself wrong), enter the value by hand with 'record --cycle "
                     f"{args.cycle} --metric <id> --value <n>' and say so in the cycle report — "
                     "never let the loop keep scoring an unmeasured metric.**")
        lines.append("")
    if pre_amendment_metrics:
        lines.append("**MEASURED BEFORE THE LAST AMENDMENT — " + ", ".join(sorted(set(pre_amendment_metrics)))
                     + f". The newest row for these metrics is older than the amendment at "
                     f"{amended_at}, so it was produced by a harness that no longer exists: a "
                     "different target, a different frozen suite, or both. Their errors above "
                     "are recomputed against the CURRENT target, but the values themselves are "
                     f"not this harness's. Re-run 'measure --cycle {args.cycle}' and re-analyze "
                     "before steering on them or concluding anything.**")
        lines.append("")
    if pending:
        lines.append("**RE-MEASURE OWED — cycle(s) "
                     + ", ".join(str(c) for c in pending)
                     + ". A `freeze --amend` moved at least one target and the baseline it "
                     "moved has not been re-measured since. All-targets-met is WITHHELD until "
                     "'measure --cycle <n>' has run for each: an amendment invalidates its own "
                     "baseline, and a hand-entered 'record' does not satisfy it.**")
        lines.append("")
    if recomputed:
        lines.append("_Note: the stored `error` column disagreed with the current target for "
                     + ", ".join(sorted(set(recomputed))) + "; every error above is recomputed "
                     "from `value` against the target in force now, never read from history._")
        lines.append("")
    if manual_metrics:
        lines.append("**HAND-ENTERED VALUES for: " + ", ".join(manual_metrics) +
                     ". The newest row for these metrics came from 'record', not 'measure', so "
                     "it OUTRANKS any real measurement of the same cycle. Name each one and the "
                     "reason in the cycle report" +
                     (", and note that --trust-manual-records was passed."
                      if args.trust_manual_records else
                      ". All-targets-met is withheld while any of them stands; pass "
                      "--trust-manual-records only with the user's approval.") + "**")
        lines.append("")
    if escalated_metrics:
        lines.append("**DISPUTED GOALS — an escalation is OPEN against: "
                     + ", ".join(sorted({e["metric"] + " (" + e["escalation"] + ")"
                                         for e in escalated_metrics}))
                     + ". The orchestrator has told the user it believes the goal or the test "
                     "behind these metrics is wrong and has asked for a specific change. NOTHING "
                     "was changed: the verdicts above are computed exactly as usual and this "
                     "analysis moved no goalpost. But a verdict on a disputed metric is not "
                     "settled — do not steer the next cycle on it, and do not report it as a "
                     f"clean result, without saying so. See {STATE_DIR}/{ESCALATIONS_MD}.**")
        lines.append("")
    if all_done:
        lines.append("**ALL TARGETS MET — act-dependent: during the build this is the "
                     "build→audit transition; during the audit act it is the expected "
                     "steady state (see SKILL.md Phase 7).**")
    else:
        worst = [r for r in results if r["verdict"] in
                 ("stale", "no_data", "regressing", "stalled", "converging_off_track")]
        if worst:
            lines.append("Priority for next cycle (worst first): " + ", ".join(r["id"] for r in worst))
    mpath = os.path.join(d, ANALYSIS_DIR, f"cycle-{args.cycle}.md")
    with open(mpath, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines))
    print(f"\nwrote {os.path.relpath(jpath, root)} and {os.path.relpath(mpath, root)}")
    if stale_metrics:
        # Exit 1 (error), never 2: a stale analysis is a defect to fix, and exit 2
        # is reserved for terminal signals. Never 0 — a silent pass here is what
        # let a failed measure be reported as a met target.
        sys.exit(1)
    if all_done:
        sys.exit(2)  # signal: loop can stop early


# ------------------------------------------------------------------ resume

def _verify_quiet(root, d, state, goals=None):
    """(status, bad, notes) — status is 'intact' | 'VIOLATED' | 'not frozen'.

    THE one integrity oracle. cmd_verify, measure, record, analyze and resume all
    come through here, deliberately: P-4 was two implementations of the same
    check drifting apart, with the weaker one on the path the loop used every
    cycle.

    A4/P-5: an ABSENT manifest over goals that say frozen is now a VIOLATION for
    everyone. It used to return "not frozen", which cmd_record patched for itself
    ("Left as-is this gate failed OPEN") while cmd_resume — the command for when
    everything is broken — consumed the same helper unpatched and printed
    `goals : frozen (12 metrics) · harness not frozen`, a self-contradiction on
    one line, before recommending that an unattended orchestrator carry on
    dispatching. The promotion belongs in here, not in one caller. Goals that
    were never frozen still read "not frozen": that is Phase 2 incomplete, not a
    tamper, and resume must keep saying so."""
    mpath = os.path.join(d, MANIFEST_FILE)
    if goals is None:
        try:
            with open(os.path.join(d, GOALS_FILE)) as f:
                goals = json.load(f)
        except (OSError, ValueError):
            goals = {}
    if not os.path.exists(mpath):
        if goals.get("frozen"):
            return "VIOLATED", [
                f"[MISSING] {STATE_DIR}/{MANIFEST_FILE} — {STATE_DIR}/{GOALS_FILE} says this "
                "run IS frozen, so the frozen manifest itself is gone; that is an integrity "
                "violation, not an absence of one. Restore it from the commit that holds it "
                f"(`git show <commit>:{STATE_DIR}/{MANIFEST_FILE} > {STATE_DIR}/"
                f"{MANIFEST_FILE}`)."], []
        return "not frozen", [], []
    manifest = load_json(mpath, "manifest")
    bad, disk_ok = [], []
    for rel, digest in manifest["files"].items():
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            bad.append(f"[MISSING] {rel}")
        elif sha256_file(path) != digest:
            bad.append(f"[MODIFIED] {rel}")
        else:
            disk_ok.append(rel)
    try:
        for relbase in state["protected_paths"]:
            base = os.path.join(root, relbase)
            if not os.path.isdir(base):
                continue
            for path in iter_files(base):
                rel = os.path.relpath(path, root)
                if rel not in manifest["files"]:
                    bad.append(f"[ADDED] {rel}")
    except HarnessSymlink as e:
        bad.append("[SYMLINKED] " + _symlink_msg(root, e))
    # The manifest is only worth comparing against if the manifest itself is
    # authentic. This is the second half of the same question, never a separate
    # command somebody might not run.
    forged, notes = _authenticate_manifest(root, d, manifest, disk_ok)
    bad += forged
    return ("VIOLATED", bad, notes) if bad else ("intact", [], notes)


def _integrity_gate(root, d, state, goals, what):
    """Refuse to write to (or score) the scoreboard against a modified harness.

    P-4/H-2: `measure` and `analyze` produce every number the run is steered by
    and neither asserted the harness was intact — so a weakened acceptance test
    yielded inflated values at exit 0, and analyze could print ALL TARGETS MET
    and exit 2, the terminal build→audit signal, with no integrity statement
    anywhere in its output. `record` refused on the same tree. Returns the notes
    so the caller can STATE the verification it just made; a green that does not
    say what was checked is the thing being fixed here."""
    status, bad, notes = _verify_quiet(root, d, state, goals)
    if status == "VIOLATED":
        print("INTEGRITY VIOLATION — the frozen goal harness was altered:")
        for b in bad:
            print(f"  {b}")
        die(f"refusing to {what} against a modified harness — run 'restore-harness', "
            "then 'verify', then re-run this command")
    return status, notes


def _cycle_num(name, suffix):
    """Cycle number from an exact 'cycle-<int><suffix>' filename, else None.

    analysis/ and reports/ are ordinary directories that a human or an agent
    drops extra files into — 'cycle-1-audit.md', 'cycle-latest.json'. int() on
    those raised ValueError and took the RECOVERY tool down with it, which is the
    one command that must never crash."""
    prefix = "cycle-"
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    mid = name[len(prefix):len(name) - len(suffix)]
    if not mid.isdigit():
        return None
    try:
        return int(mid)
    except ValueError:
        return None


def cmd_audit_loop_done(args):
    """Record one completed audit-fix loop; the act's machine-readable progress."""
    root = repo_root()
    d = sd(root)
    # P-14: read-modify-write of state.json under a lock. checkpoint does the
    # same thing to the same file, and two of them running at once silently lost
    # one update — both exiting 0.
    with _state_lock(d):
        state = load_json(os.path.join(d, STATE_FILE), "state")
        state["audit_loops_completed"] = state.get("audit_loops_completed", 0) + 1
        if args.clean:
            state["clean_audit"] = True
        save_json(os.path.join(d, STATE_FILE), state)
    done = state["audit_loops_completed"]
    budget = state.get("audit_loops", 0)
    if args.clean:
        print(f"audit loop {done}/{budget} recorded as CLEAN — the audit minted zero tickets.")
        print("run Phase 9 (prune), then go to Termination. This signal is terminal.")
        sys.exit(2)
    print(f"audit loop {done}/{budget} completed")
    if budget and done >= budget:
        print("audit budget exhausted — go to Termination and write the final report")
        sys.exit(2)


def cmd_resume(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state (no run to resume here)")
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")

    # --- gather artifacts per cycle
    # A cycle counts as MEASURED only when every frozen metric has a row for it.
    # 'measure' records nothing for a metric whose command fails, and "some rows
    # exist" is exactly the state that let analyze report a stale verdict as if
    # it were this cycle's. seen_cycles (any row at all) stays separate: it is
    # what the artifact grid and the resume recommendation walk, and narrowing
    # THAT to fully-measured cycles would hide the partial ones entirely.
    rows_by_cycle = {}
    history_warning = None
    hpath = os.path.join(d, HISTORY_FILE)
    # P-14: an EXISTS check alone was not enough. A zero-byte history.csv gives
    # DictReader fieldnames=None, _history_reader dies on it, and the one command
    # that has to work when everything else is broken died with it.
    if os.path.exists(hpath) and os.path.getsize(hpath) == 0:
        history_warning = (f"{STATE_DIR}/{HISTORY_FILE} exists but is EMPTY (0 bytes) — every "
                           "measurement this run recorded is gone. Recover it from git "
                           f"(`git show <commit>:{STATE_DIR}/{HISTORY_FILE}`) before scoring "
                           "anything; measure re-creates the header but not the rows.")
    elif os.path.exists(hpath):
        with open(hpath) as f:
            for row in _history_reader(f, hpath):
                rows_by_cycle.setdefault(int(row["cycle"]), set()).add(row["metric_id"])
    metric_ids = {m["id"] for m in goals.get("metrics", [])}
    measured, partial = set(), set()
    for c, ids in rows_by_cycle.items():
        (measured if metric_ids <= ids else partial).add(c)
    seen_cycles = measured | partial
    analyzed, all_met = set(), False
    newer_unmet = None
    adir = os.path.join(d, ANALYSIS_DIR)
    if os.path.isdir(adir):
        for name in os.listdir(adir):
            c = _cycle_num(name, ".json")
            if c is not None:
                analyzed.add(c)
    # Analyzed cycles with not one recorded row: every metric command failed, so
    # 'measure' wrote nothing and the cycle is in neither `measured` nor
    # `partial`. It must be as loud as a partial measurement, not silent.
    unmeasured = analyzed - seen_cycles
    if analyzed:
        # "Latest analysis" means the latest analysis of a cycle that was actually
        # measured. An ad-hoc 'analyze --cycle 99' otherwise outranks every real
        # one purely because its filename number is bigger.
        real = analyzed & seen_cycles
        latest_c = max(real) if real else max(analyzed)
        latest_analysis = load_json(os.path.join(adir, f"cycle-{latest_c}.json"), "analysis")
        all_met = (latest_analysis.get("all_targets_met", False)
                   and not latest_analysis.get("stale_metrics"))
        # A wholly-unmeasured cycle drops out of `real`, which used to promote the
        # PREVIOUS green analysis to "latest" and recommend Termination off a
        # measurement that never ran. Any NEWER analysis that is itself stale or
        # red vetoes all-met: the newest thing the run knows is that it does not
        # know, and that outranks an older green.
        if all_met:
            for c in sorted(x for x in analyzed if x > latest_c):
                a = load_json(os.path.join(adir, f"cycle-{c}.json"), "analysis")
                if a.get("stale_metrics") or not a.get("all_targets_met", False):
                    all_met, newer_unmet = False, c
                    break
    checkpointed = {h["cycle"] for h in state["head_history"]}
    reported = set()
    rdir = os.path.join(d, "reports")
    if os.path.isdir(rdir):
        for name in os.listdir(rdir):
            c = _cycle_num(name, ".md")
            if c is not None:
                reported.add(c)
    final_report = os.path.exists(os.path.join(rdir, "final-report.md"))

    # --- git situation
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, cwd=root).stdout

    # Location-agnostic on purpose. Phase 4 moved task worktrees out of
    # .swarm-loop/worktrees/ into a sibling directory, and matching only one of
    # those spellings makes resume report ZERO live worktrees for every run using
    # the other — a silent failure in the exact scenario this tool exists for. So
    # accept the run's RECORDED root, the legacy in-tree path, AND any worktree
    # sitting on a task/ branch, and let the union decide. Nothing in flight is
    # allowed to be invisible.
    wt_root_abs = os.path.normpath(os.path.join(
        root, state.get("worktree_root") or f"{STATE_DIR}/worktrees"))
    worktrees = []
    for rec in git("worktree", "list", "--porcelain").split("\n\n"):
        path = branch = None
        for ln in rec.splitlines():
            if ln.startswith("worktree "):
                path = ln.split(" ", 1)[1]
            elif ln.startswith("branch "):
                branch = ln.split(" ", 1)[1]
        if not path or os.path.normpath(path) == os.path.normpath(root):
            continue
        under_root = os.path.normpath(path).startswith(wt_root_abs + os.sep)
        legacy = f"{STATE_DIR}/worktrees" in path
        on_task_branch = bool(branch and branch.startswith("refs/heads/task/"))
        if under_root or legacy or on_task_branch:
            label = branch[len("refs/heads/"):] if branch else "DETACHED"
            worktrees.append(f"{path}  [{label}]")
    merged = set(b.strip().lstrip("*+ ") for b in git("branch", "--merged", "main", "--list",
                                                     "task/*", "integration/*").splitlines() if b.strip())
    allb = set(b.strip().lstrip("*+ ") for b in git("branch", "--list",
                                                   "task/*", "integration/*").splitlines() if b.strip())
    unmerged = sorted(allb - merged)
    dirty = bool(git("status", "--porcelain").strip())
    integrity, bad, integrity_notes = _verify_quiet(root, d, state, goals)
    open_esc = _open_escalations(d)      # [] when the run predates escalations
    esc_blocking = [r for r in open_esc if r.get("blocking")]

    # --- report
    print(f"=== swarm-loop resume report: {state['project']} ===")
    print(f"budget          : cycle {state['current_cycle']}/{state['max_cycles']} checkpointed"
          f" · stall {state['stall_count']}/{state['stall_limit']} codebase"
          f" · {int(state.get('goal_stall_count') or 0)}/{_goal_stall_limit(state)} goal-progress"
          f" · autonomy {state['autonomy']} · push {state['push_remote']}")
    amendments = int(goals.get("amendments", 0))
    manifest_exists = os.path.exists(os.path.join(d, MANIFEST_FILE))
    print(f"goals           : {'frozen' if goals.get('frozen') else 'NOT FROZEN'} ({len(goals.get('metrics', []))} metrics)"
          f" · harness {integrity}"
          + (f" · AMENDED {amendments}x at {goals.get('last_amended_at')}" if amendments else ""))
    if amendments:
        print(f"                  goalposts moved {amendments}x — last {goals.get('last_amended_at')}: "
              f"{goals.get('last_amend_reason')!r} (see {STATE_DIR}/{FREEZE_LOG_FILE});"
              " the trend spans more than one target — say so in the final report")
    print("escalations     : " + (f"{len(open_esc)} OPEN"
                                    + (f" ({len(esc_blocking)} BLOCKING)" if esc_blocking else "")
                                    + " — awaiting a USER decision"
                                    if open_esc else "none open"))
    if open_esc:
        # Loud on purpose. This is the one part of the report the loop cannot act
        # on by itself, and the failure it replaces is an escalation that went
        # into a cycle report nobody read until morning.
        bar = "!" * 74
        print(bar)
        for r in open_esc:
            print(f"!!! {r['id']}{'  [BLOCKING]' if r.get('blocking') else ''}  "
                  f"{r.get('kind')} · {r.get('subject')}")
            print(f"!!!    wrong    : {_oneline(r.get('reason'), 150)}")
            print(f"!!!    proposed : {_oneline(r.get('proposed'), 150)}")
        print(f"!!! Full text + the exact command that grants each: {STATE_DIR}/{ESCALATIONS_MD}")
        print("!!! The loop changed NOTHING about these — escalating is asking, it is never")
        print("!!! granting. Only the user can grant one; close each with")
        print("!!! 'escalate --resolve <ID> --note \"...\"' once decided.")
        print(bar)
    print(f"audit loops     : {state.get('audit_loops_completed', 0)}/{state.get('audit_loops', 0)}"
          + (" CLEAN" if state.get("clean_audit") else ""))
    for b in bad:
        print(f"                  {b}")
    for n in integrity_notes:
        print(f"                  note: {n}")
    pending = _pending_remeasure(state)
    if pending:
        print("re-measure owed  : cycle(s) " + ", ".join(str(c) for c in pending)
              + " — a 'freeze --amend' moved a target and the baseline it moved has not been")
        print("                  re-measured; analyze withholds all-targets-met until "
              "'measure --cycle <n>' runs")
    if history_warning:
        print("history.csv     : " + history_warning)
    dispatches = _open_dispatches(d)
    if dispatches:
        # H-17: a ticket worker survives compaction because its worktree and
        # task/ branch do; every agent doing orchestrator-owned work with NO
        # worktree — the backlog rewriter, the public-surface regenerator, recon
        # agents, wave verifiers — used to leave no trace at all, so resume said
        # "nothing in flight" and a resumed orchestrator re-dispatched it. Two
        # agents then write the same orchestrator-owned file, which is the one
        # case worktree isolation does not cover.
        print(f"open dispatches : {len(dispatches)} recorded in {STATE_DIR}/{DISPATCH_FILE} "
              "and never closed")
        for r in dispatches:
            print("                  %s  %s · %s  (since %s)%s"
                  % (r.get("agent_ref"), r.get("kind"), r.get("subject"),
                     r.get("dispatched_at"),
                     "  files: " + ", ".join(r.get("target_files") or [])
                     if r.get("target_files") else ""))
        print("                  CHECK EACH before re-dispatching: an agent still running "
              "and a crashed one look identical from here")
    for i, line in enumerate(_graph_reconcile_lines(root, d, state)):
        print(("ticket graph    : " if i == 0 else "                  ") + line)
    for i, line in enumerate(_learning_lines(d)):
        print(("self-improvement: " if i == 0 else "                  ") + line)
    hi = max(seen_cycles | analyzed | checkpointed | reported | {0})
    print("cycle artifacts : (m)easured (a)nalyzed (c)heckpointed (r)eported"
          + ("  ·  ! in the m column = PARTIAL measurement (a metric command failed)"
             if partial else "")
          + ("  ·  x in the m column = NO measurement at all (every metric command failed)"
             if unmeasured else ""))
    for c in range(0, hi + 1):
        flags = "".join(x if c in s else "-" for x, s in
                        [("m", measured), ("a", analyzed), ("c", checkpointed), ("r", reported)])
        if c in partial:
            flags = "!" + flags[1:]
        elif c in unmeasured:
            flags = "x" + flags[1:]
        print(f"                  cycle {c}: {flags}")
    if newer_unmet is not None:
        print(f"stale analysis  : analysis/cycle-{newer_unmet}.json is NEWER than the newest "
              f"MEASURED cycle and is not all-green")
        print(f"                  — all-targets-met is WITHHELD; cycle {newer_unmet} recorded no "
              "usable measurement, so the last green analysis is not this run's verdict")
    print(f"working tree    : {'DIRTY — inspect git status first' if dirty else 'clean'}")
    print(f"live worktrees  : {len(worktrees)}" + ("".join(f"\n                  {w}" for w in worktrees)))
    # Recorded, never enforced — and the unset case is a real answer rather than
    # a blank line: it says this run never wrote down whether each lane got its
    # own directory or every agent shared one, which is exactly the ambiguity
    # that let two lanes collide on a probe script of the same name.
    sr = state.get("scratch_root")
    sr_abs = _scratch_root_abs(root, state)
    if sr:
        print("lane scratch    : %s%s" % (sr_abs, "" if sr_abs == sr else "  (recorded as %s)" % sr))
        print("                  one subdirectory per task id: <root>/<task-id>/ — RECORDED, "
              "never enforced")
        print("                  (nothing here observes the inside of a lane's turn)")
    else:
        print("lane scratch    : UNRECORDED — this run named no --scratch-root, so whether each")
        print("                  lane had its own <root>/<task-id>/ or every agent shared one")
        print("                  directory is not knowable from here")
    print(f"unmerged branches: {len(unmerged)}" + ("".join(f"\n                  {b}" for b in unmerged)))

    # --- recommendation (heuristic — confirm against the latest cycle report)
    #
    # Order matters, and it used to be wrong twice. The TERMINAL conditions —
    # kill switch, cycle ceiling, clean audit, exhausted audit budget — are
    # terminal regardless of metric colour (SKILL.md), so every one of them is
    # tested BEFORE all_met. Testing them after it produced two live
    # contradictions of checkpoint/audit-loop-done's own exit 2: "keep building"
    # after the kill switch had fired, and "re-enter the build" with red metrics
    # and an exhausted audit budget.
    budget = state.get("audit_loops", 0)
    done = state.get("audit_loops_completed", 0)
    rec, terminal = None, False
    if final_report:
        rec, terminal = "final report already written — this run is closed; start a new run with init --force", True
    elif not goals.get("frozen") and manifest_exists:
        # freeze writes the manifest first and flips the flag last, so this
        # combination means a freeze died between the two writes. It is tested
        # BEFORE the integrity check on purpose: verify necessarily reports
        # goals.json MODIFIED in this state (the manifest holds the hash of the
        # frozen bytes that never landed), and 'restore-harness' cannot help —
        # those bytes were never committed. Re-freezing is the actual repair.
        rec = ("goals are NOT frozen but a manifest exists — a freeze died between writing the "
               "manifest and flipping the frozen flag (a leftover "
               f"{STATE_DIR}/{GOALS_FILE}.tmp confirms it). 'verify' will report goals.json "
               "MODIFIED and 'restore-harness' cannot fix it: re-run 'freeze', which rewrites "
               "both. Do not measure until it succeeds")
    elif integrity == "VIOLATED":
        rec = ("restore the frozen harness before anything else — run 'restore-harness' "
               "(git checkout/restore are git-guard-blocked); scores are void until 'verify' "
               "passes. Do NOT 'freeze' to clear it: that re-locks the tampered bytes")
    elif not goals.get("frozen"):
        rec = "Phase 2 incomplete: finish goals + acceptance tests, freeze, then baseline (measure/checkpoint --cycle 0)"
    elif esc_blocking:
        # Terminal, and ABOVE the audit/kill-switch/ceiling branches on purpose:
        # every recommendation below this one ends in "write the final report",
        # and a final report written while the loop believes the harness it was
        # scored against is broken is exactly the silent outcome escalation
        # exists to prevent. It is above all_met for the same reason.
        rec, terminal = (
            "BLOCKING ESCALATION open (" + ", ".join(r["id"] for r in esc_blocking) + ") — the "
            "loop asked for a decision it is not allowed to make and must not work around. Read "
            f"{STATE_DIR}/{ESCALATIONS_MD}: it carries the full text and the exact command that "
            "grants each request. Grant it or decline it, then close it with 'escalate --resolve "
            "<ID> --note \"...\"'. Do not resume the loop, and do not write a final report, "
            "until it is resolved"), True
    elif state.get("clean_audit"):
        rec, terminal = ("clean audit loop recorded — run Phase 9 (prune) if not yet done, "
                         "then go to Termination and write the final report"), True
    elif budget > 0 and done >= budget:
        rec, terminal = (f"audit budget exhausted ({done}/{budget}) — go to Termination and "
                         "write the final report. Metrics still red? Report them as-shipped; "
                         "the budget, not the colour, ends the act"), True
    elif state["stall_count"] >= state["stall_limit"]:
        rec, terminal = ("kill switch already fired (codebase: the product tree stopped "
                         "changing) — go to Termination and write the final report"), True
    elif int(state.get("goal_stall_count") or 0) >= _goal_stall_limit(state):
        rec, terminal = ("kill switch already fired (goal progress: no graded metric improved "
                         f"for {int(state.get('goal_stall_count') or 0)} consecutive measured "
                         f"cycles, limit {_goal_stall_limit(state)}) — the codebase kept "
                         "changing and the goal did not, which is the stall the fingerprint "
                         "check structurally cannot see. Go to Termination and write the final "
                         "report, naming the pinned metric(s) and their last value"), True
    elif state["current_cycle"] >= state["max_cycles"]:
        rec, terminal = (f"cycle budget exhausted ({state['current_cycle']}/{state['max_cycles']}) "
                         "— go to Termination and write the final report"), True
    elif all_met:
        # All-green is act-dependent: during the build it is the build->audit
        # transition, and mid-audit-act it is the expected steady state. Both of
        # the terminal audit states are already handled above, so what is left
        # here is genuinely non-terminal.
        if budget > 0:
            rec = (f"all targets met ({done}/{budget} audit loops done) — NOT terminal: "
                   "if the ticket ledger is empty this is the build→audit transition / next "
                   "audit loop; if build tickets remain open, keep building")
        else:
            rec, terminal = "all targets met per latest analysis — go to Termination and write the final report", True
    else:
        L = max(seen_cycles | analyzed | {state["current_cycle"]})
        if L in checkpointed and L in reported:
            if L >= state["max_cycles"]:
                rec, terminal = "cycle budget exhausted — go to Termination and write the final report", True
            else:
                rec = f"cycle {L} fully closed — resume at Phase 1/3 planning cycle {L + 1}"
        elif L in checkpointed:
            if L == 0:
                rec = "cycle-0 baseline complete — commit .swarm-loop/ if not yet committed, then resume at Phase 3 (schedule the frontier and dispatch)"
            else:
                rec = f"cycle {L} checkpointed but not reported — resume at Phase 8 (adapt + cycle report) for cycle {L}"
        elif L in analyzed:
            rec = f"cycle {L} analyzed but not checkpointed — run 'checkpoint --cycle {L}', then Phase 8"
            if L in partial:
                rec += (f"; NOTE cycle {L} is only PARTIALLY measured, so that analysis carries a "
                        f"stale verdict — after checkpoint, fix the failing metric command and "
                        f"re-run 'measure --cycle {L}' + 'analyze --cycle {L}' before steering on it")
            elif L in unmeasured:
                rec += (f"; NOTE cycle {L} is NOT MEASURED AT ALL — every metric command failed, so "
                        f"'measure --cycle {L}' recorded zero rows and every verdict in that "
                        f"analysis is a stale value carried over from an earlier cycle. After "
                        f"checkpoint, fix the metric command(s) and re-run 'measure --cycle {L}' + "
                        f"'analyze --cycle {L}' before steering on it or concluding anything")
        elif L in partial:
            rec = (f"cycle {L} only PARTIALLY measured — a metric command failed and recorded "
                   f"nothing. Fix the command (or the code it measures), re-run 'measure --cycle {L}', "
                   f"then 'analyze --cycle {L}'; analyzing now yields a stale verdict")
        elif L in measured:
            rec = f"cycle {L} measured but not analyzed — run 'analyze --cycle {L}'"
        else:
            rec = (f"cycle {L + 1} was in Phases 3–6 (plan/dispatch/collect/integrate) — "
                   f"reconstruct from the branch and worktree state below, then continue")
    if (worktrees or unmerged) and not terminal:
        rec += ("\n                  CAUTION: live worktrees/unmerged task branches exist — run "
                "'check-branch' + collection checks on each before merging or reclaiming; "
                "branches with no commits are failed-hung tasks (reclaim to backlog)")
    print(f"recommendation  : {rec}")
    print("read before acting: the latest reports/cycle-*.md, learnings.md, backlog.md, codebase-map.md")


# --------------------------------------------------------------- checkpoint

def _goal_stall_limit(state):
    """The goal-progress stall limit, always >= 1.

    Falls back to `stall_limit` for every run initialized before this key
    existed — the live one included — so an in-flight run picks the switch up
    without a state migration. Floored at 1 because a limit of 0 would make a
    fresh run (count 0) terminal on its first checkpoint."""
    try:
        v = int(state.get("goal_stall_limit") or 0)
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        try:
            v = int(state.get("stall_limit") or 0)
        except (TypeError, ValueError):
            v = 0
    return max(1, v)


def _graded_progress(d, goals, cycle):
    """Did ANY graded metric's error fall at `cycle`? -> (verdict, one-line detail)

    verdict is one of:
      "moved"    something improved, or every graded metric is already at target
      "flat"     this cycle measured metrics and not one of them improved
      "unknown"  this cycle measured nothing comparable — a failed `measure`, a
                 run with no frozen goals, an unreadable history

    THE defect this exists for. The stall kill switch asked
    _codebase_fingerprint "did the code change", never "did the goal move".
    Those are different questions and only the second is what the run is for, so
    any loop that writes a line of product code every cycle reset the counter
    forever: a metric pinned at one value for seven consecutive cycles sat at
    stall 0/3 with 95 cycles of budget left. The diagnosis already existed —
    _verdict has returned "stalled" the whole time — and was wired to a sort key
    and a "Priority for next cycle" line, nothing that could stop anything.

    Conservative in the direction of NOT killing a live run, on purpose:
      * ANY metric improving resets the counter. A net-zero cycle where one
        metric gained what another lost is not a stall.
      * A metric's FIRST-EVER reading counts as movement: the run just learned
        something it did not know.
      * Every graded metric inside tolerance resets it. That is convergence,
        which analyze reports as ALL TARGETS MET; it must never read as a stall.
      * A cycle that measured nothing leaves the counter EXACTLY where it was —
        neither bumped nor cleared. A broken metric command is a defect to fix
        (analyze already exits 1 saying so); it may not also terminate the run.

    What it does NOT catch, stated rather than papered over: a metric creeping
    toward target by a hair every cycle registers as movement forever. That case
    belongs to _verdict's converging_off_track projection, a different signal.

    Errors are RECOMPUTED from `value` against the target in force now, never
    read from history.csv's stored `error` column — the same rule cmd_analyze
    follows, for the same reason (a `freeze --amend` that moved a target
    invalidated every error written before it).

    Never raises and never calls die(): a progress counter may not be the thing
    that crashes a checkpoint. Every failure path returns "unknown".
    """
    metrics = (goals or {}).get("metrics") or []
    if not metrics:
        return "unknown", "no graded metrics — nothing to make progress against"
    hpath = os.path.join(d, HISTORY_FILE)
    if not os.path.exists(hpath) or os.path.getsize(hpath) == 0:
        return "unknown", f"{STATE_DIR}/{HISTORY_FILE} is empty — nothing measured yet"
    values = {}
    try:
        with open(hpath) as f:
            reader = csv.DictReader(f)
            if list(reader.fieldnames or [])[:5] != HISTORY_HEADER[:5]:
                return "unknown", f"{STATE_DIR}/{HISTORY_FILE} has no usable header row"
            for row in reader:
                try:
                    c = int(row["cycle"])
                    v = float(row["value"])
                except (TypeError, ValueError):
                    continue
                # Score the run AS OF `cycle`: a re-checkpoint of an earlier
                # cycle must not read rows from a later one. Last row per cycle
                # in FILE order wins, matching cmd_analyze's dedupe.
                if c > cycle or not math.isfinite(v):
                    continue
                values.setdefault(row["metric_id"], {})[c] = v
    except (OSError, ValueError) as e:
        return "unknown", f"{STATE_DIR}/{HISTORY_FILE} unreadable: {e}"

    improved, flat, first, at_target = [], [], [], []
    for m in metrics:
        try:
            tgt, tol, direction = _target(m), _tol(m), m["direction"]
            mid = m["id"]
        except (KeyError, TypeError, ValueError):
            continue                       # a malformed metric is freeze's problem
        by_cycle = values.get(mid) or {}
        if cycle not in by_cycle:
            continue                       # not measured THIS cycle
        cur = _error(by_cycle[cycle], tgt, direction)
        if cur <= tol:
            at_target.append(mid)
        earlier = [c for c in by_cycle if c < cycle]
        if not earlier:
            first.append(mid)
            continue
        prev = _error(by_cycle[max(earlier)], tgt, direction)
        if prev - cur > PROGRESS_EPS * max(1.0, abs(prev)):
            improved.append(mid)
        else:
            flat.append(mid)

    compared = improved + flat + first
    if not compared:
        return "unknown", f"no graded metric has a cycle-{cycle} measurement"
    if improved:
        return "moved", "error fell for " + ", ".join(sorted(improved))
    if first:
        return "moved", "first-ever reading for " + ", ".join(sorted(first))
    if len(at_target) == len(compared) == len(metrics):
        return "moved", "every graded metric is at target"
    return "flat", "no improvement in " + ", ".join(sorted(flat))


def _codebase_fingerprint(root):
    """sha256 over HEAD's tree with the loop's OWN state excluded.

    The stall kill switch used to compare HEAD SHAs, a thing it can never
    meaningfully do: every cycle the loop is REQUIRED to commit its own
    bookkeeping — reports/cycle-N.md, history.csv, analysis/cycle-N.json,
    state.json — all of it tracked, so HEAD moves and the stall counter resets
    to 0 in a cycle where not one line of product code changed. Hashing the tree
    minus .swarm-loop/ measures what the switch has always claimed to measure.
    Returns None if git cannot answer (empty repo), which the caller reads as
    'fall back to the HEAD comparison'."""
    out = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD"],
                         capture_output=True, text=True, cwd=root)
    if out.returncode != 0:
        return None
    h = hashlib.sha256()
    for rec in out.stdout.split("\0"):
        if not rec:
            continue
        path = rec.split("\t", 1)[-1]
        if path == STATE_DIR or path.startswith(STATE_DIR + "/"):
            continue
        h.update(rec.encode() + b"\0")
    return h.hexdigest()


def cmd_checkpoint(args):
    root = repo_root()
    d = sd(root)
    spath = os.path.join(d, STATE_FILE)
    state = load_json(spath, "state")
    # An unborn HEAD is a clean, common state (a repo initialized but never
    # committed), not a crash: check=True used to surface it as a raw
    # CalledProcessError traceback with no hint of what to do about it.
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True, cwd=root).stdout.strip()
        tracked_files = len(subprocess.run(
            ["git", "ls-tree", "-r", "HEAD", "--name-only"],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout.splitlines())
    except subprocess.CalledProcessError:
        die("this repository has no commits yet — HEAD does not resolve, so there is no "
            "codebase state to checkpoint. Commit the baseline (including "
            f"{STATE_DIR}/) first, then re-run 'checkpoint --cycle {args.cycle}'")
    fingerprint = _codebase_fingerprint(root)
    # Tolerant read: checkpoint must still work on a run whose Phase 2 has not
    # frozen any goals (the cycle-0 baseline), and the goal-stall counter is
    # never allowed to be the thing that crashes a checkpoint.
    try:
        with open(os.path.join(d, GOALS_FILE)) as _gf:
            goals = json.load(_gf)
    except (OSError, ValueError):
        goals = {}

    # Compare against the last entry for a STRICTLY EARLIER cycle, not simply the
    # last entry: re-running 'checkpoint --cycle N' is explicitly sanctioned (a
    # retried Phase 7 block, a re-measure after a failed one), and comparing a
    # replay against its own prior entry made every replay look like "no change"
    # and bumped the stall counter — three replays fired the terminal kill switch
    # on a run that was committing real work every cycle.
    # P-14: everything from here to save_json is a read-modify-write of
    # state.json, and audit-loop-done does the same to the same file. Re-read
    # inside the lock so a concurrent update is not silently overwritten.
    lock = _state_lock(d)
    lock.__enter__()
    state = load_json(spath, "state")
    hist = state["head_history"]
    prev_entry = None
    for h_ in hist:
        if h_["cycle"] < args.cycle:
            prev_entry = h_
    replay = any(h_["cycle"] == args.cycle for h_ in hist)
    # The OTHER stall, and the one this loop is actually for: the fingerprint
    # above answers "did the code change", this answers "did the goal move".
    goal_verdict, goal_detail = _graded_progress(d, goals, args.cycle)

    prev_fp = prev_entry.get("codebase") if prev_entry else None
    prev = prev_entry["head"] if prev_entry else None
    if prev_entry is None:
        changed = True  # first checkpoint of the run
    elif prev_fp and fingerprint:
        changed = fingerprint != prev_fp
    else:
        changed = head != prev  # pre-fingerprint history, or a git that can't answer

    entry = {"cycle": args.cycle, "head": head, "changed": changed,
             "tracked_files": tracked_files, "codebase": fingerprint}
    if replay:
        # Overwrite this cycle's entry in place and leave stall_count exactly as
        # the first checkpoint of this cycle left it: a replay is not a new cycle.
        state["head_history"] = [h_ for h_ in hist if h_["cycle"] != args.cycle] + [entry]
        state["head_history"].sort(key=lambda h_: h_["cycle"])
        # Same rule the code stall follows: a replay is not a new cycle. The
        # assignment is still made so the key exists in a state.json written
        # before this counter did.
        state["goal_stall_count"] = int(state.get("goal_stall_count") or 0)
    else:
        hist.append(entry)
        state["stall_count"] = 0 if changed else state["stall_count"] + 1
        prior = int(state.get("goal_stall_count") or 0)
        # "unknown" holds the count where it is — neither bumped nor cleared. A
        # metric command that failed is a defect analyze already exits 1 over;
        # it may not silently spend the run's stall budget, and it may not
        # silently refund it either.
        state["goal_stall_count"] = (0 if goal_verdict == "moved"
                                     else prior + 1 if goal_verdict == "flat"
                                     else prior)
    # A replay of an EARLIER cycle must not rewind the budget the loop is on.
    state["current_cycle"] = max(args.cycle, int(state.get("current_cycle", 0)))
    save_json(spath, state)
    lock.__exit__(None, None, None)

    print(f"checkpoint cycle {args.cycle}: HEAD {head[:12]} "
          f"(codebase {'changed' if changed else 'UNCHANGED'} vs cycle "
          f"{prev_entry['cycle'] if prev_entry else '—'}, excluding {STATE_DIR}/)")
    if replay:
        print(f"cycle {args.cycle} was already checkpointed — entry overwritten, "
              "stall count deliberately left untouched (a replay is not a new cycle)")
    print(f"tracked files at HEAD: {tracked_files}")
    print(f"stall count: {state['stall_count']}/{state['stall_limit']} (codebase)")
    print(f"goal-progress stall: {state['goal_stall_count']}/{_goal_stall_limit(state)} "
          f"({goal_verdict} — {goal_detail})")
    # The epoch boundary IS where SKILL.md Phase 8 owes the process-loop append,
    # so this is where not having made it should be visible. Advisory; a bare
    # except because a reporting line may never move checkpoint's exit code.
    try:
        for line in _learning_lines(d):
            print("self-improvement: " + line)
    except Exception as _e:      # noqa: BLE001 - a status line is never the crash
        print(f"(self-improvement check skipped: {_e})")

    # Tripwire: a sharp drop in tracked-file count is the one signal that sees
    # an index-wipe (an emptied index turns add+commit into a full repo delete
    # that stays 100% green). Older entries lack the key — skip silently then.
    # Warning only: this must never change checkpoint's exit code.
    #
    # KNOWN AND ACCEPTED (P-14): the comparison is against the previous
    # CHECKPOINT, not the previous COMMIT, so a gradual wipe — under 10% and
    # under 25 files per cycle — never trips it. Deliberately not "fixed" by
    # tightening the threshold: this is an advisory tripwire for the catastrophic
    # case, and a per-commit version of it would fire on every legitimate large
    # deletion in a build that is supposed to be refactoring. merge-protocol.md's
    # manual half is where a gradual drift is meant to be caught.
    if prev_entry is not None and "tracked_files" in prev_entry:
        prev_count = prev_entry["tracked_files"]
        drop = prev_count - tracked_files
        if drop > max(25, 0.10 * prev_count):
            bar = "!" * 74
            print(bar)
            print("!!! WARNING: TRACKED FILE COUNT DROPPED SHARPLY AT HEAD")
            print(f"!!!   previous checkpoint : {prev_count} tracked files")
            print(f"!!!   this checkpoint     : {tracked_files} tracked files  (drop: {drop})")
            print("!!! This is the signature of an index-wipe: an emptied git index turns")
            print("!!! add+commit into a mass deletion that stays green on every check.")
            print(f"!!! BEFORE any push, run:  git diff --stat {prev[:12]}..HEAD")
            print("!!! and:                   git status")
            print("!!! Untracked '??' files that should be tracked are the tell.")
            print(bar)
    # Worktree teardown tripwire. The teardown guidance existed and was simply
    # never executed — thirteen trees, ~14.7 GB, six of them finished work — and
    # skipping it left no trace anywhere the run could see. checkpoint runs at
    # every cycle boundary, so this is where that becomes visible. STRICTLY
    # ADVISORY: it prints, and the bare except is deliberate — a disk-hygiene
    # notice may never be the thing that crashes a checkpoint or moves its exit
    # code. Prints nothing at all when nothing is stale.
    try:
        _print_stale_worktree_warning(root, d, state)
    except Exception as _e:      # noqa: BLE001 - a tripwire is never allowed to be the crash
        print(f"(worktree teardown check skipped: {_e})")

    if state["stall_count"] >= state["stall_limit"]:
        print(f"KILL SWITCH: no codebase changes for {state['stall_count']} consecutive cycles — terminate the loop and report")
        sys.exit(2)
    _gsl = _goal_stall_limit(state)
    if state["goal_stall_count"] >= _gsl:
        print(f"KILL SWITCH: no graded metric has improved for {state['goal_stall_count']} "
              f"consecutive measured cycles (limit {_gsl}) — terminate the loop and report.")
        print(f"  latest reading: {goal_detail}")
        print("  This is the OTHER stall. The codebase check above asks whether the code "
              "changed; a loop that writes product code every cycle resets it forever. This "
              "one asks whether the run is getting anywhere, which is the question the run "
              "exists to answer.")
        print(f"  Read {STATE_DIR}/{ANALYSIS_DIR}/cycle-{args.cycle}.md before reporting: the "
              "per-metric verdicts and the 'Priority for next cycle' line say WHICH goal is "
              "pinned and by how much.")
        sys.exit(2)
    if args.cycle >= state["max_cycles"]:
        print("cycle budget exhausted — this was the final cycle; terminate the loop and report")
        sys.exit(2)


# ----------------------------------------------- ticket graph / scheduler
#
# H-08, the structural finding: SKILL.md sells "a readiness scheduler — a ticket
# dispatches the moment its dependencies are close", waves "emergent, not
# planned", scheduling by "dependency readiness and blast radius". None of it was
# code. Every subcommand above this line is goal/integrity/state; not one touched
# the ticket graph, and the single most safety-critical property of the design —
# TWO CONCURRENT WORKERS MUST NEVER OWN THE SAME FILE — was re-derived by hand,
# once per wave, from a file the harness could not read. The live run's
# orchestrator wrote the same ad-hoc Python heredoc twice to do it.
#
# The graph file is NOT hard-coded to tickets.json: state.json's optional
# "ticket_graph" wins, then the conventional locations. A project that names its
# graph something else is a configuration, not a fork.

GRAPH_CANDIDATES = ("tickets.json", f"{STATE_DIR}/tickets.json", "backlog.json")
DISPATCH_FILE = "dispatch.jsonl"
BACKLOG_MD = "backlog.md"


def _load_graph(root, state, explicit=None, required=True):
    """(abs_path, [tickets]) or (None, None) when there is no graph and it is optional."""
    tried = []
    for cand in ([explicit] if explicit else
                 ([state.get("ticket_graph")] if state.get("ticket_graph") else [])
                 + list(GRAPH_CANDIDATES)):
        if not cand:
            continue
        p = cand if os.path.isabs(cand) else os.path.join(root, cand)
        tried.append(os.path.relpath(p, root))
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            die(f"{os.path.relpath(p, root)} is not readable JSON: {e}")
        tickets = data.get("tickets") if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            die(f"{os.path.relpath(p, root)} has no 'tickets' list")
        return p, tickets
    if not required:
        return None, None
    die("no ticket graph found (looked for: %s). Pass --graph <path>, or record it once with "
        "'init --ticket-graph <path>'." % ", ".join(tried), 3)


def _graph_strs(t, *keys):
    """The first present key of a ticket, coerced to a list of strings.

    The graph is a PROJECT file whose schema this harness does not own and which
    varies between runs, so every read of it goes through here. A `scope` given
    as a bare string used to be iterated CHARACTER BY CHARACTER — silently
    producing 6 one-letter ownership patterns instead of one path glob, which is
    worse than a crash because check-wave would then compare nonsense and say OK."""
    for key in keys:
        if key not in t:
            continue
        v = t[key]
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v
                    if isinstance(x, (str, int, float)) and str(x).strip()]
        return []
    return []


def _graph_index(tickets):
    """{id: ticket}, plus the ids that appear more than once.

    Non-dict entries are skipped rather than fatal: a hand-edited tickets.json
    with a stray string or a trailing null in the list used to take out
    `frontier`, `check-wave`, `reads` AND — through the B4 reconcile — `resume`
    and `freeze`, with an AttributeError traceback naming a harness internal
    instead of the file the operator has to fix."""
    idx, dupes = {}, []
    for t in tickets:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        if tid in idx:
            dupes.append(tid)
        idx[tid] = t
    return idx, dupes


def _graph_unusable(tickets):
    """[error strings] for entries _graph_index had to skip.

    Skipping keeps the tools alive on a hand-edited file; SAYING SO is what keeps
    a dropped ticket from being mistaken for a closed one. A ticket the frontier
    silently never lists is indistinguishable from a ticket that does not exist,
    and that is precisely the H-01 failure — two graphs disagreeing with nothing
    to notice."""
    bad_shape = sum(1 for t in tickets if not isinstance(t, dict))
    no_id = sum(1 for t in tickets
                if isinstance(t, dict) and not str(t.get("id") or "").strip())
    out = []
    if bad_shape:
        out.append("%d entr%s in the graph's 'tickets' list %s not an object and %s been "
                   "SKIPPED — fix the file, or the frontier is computed over fewer tickets "
                   "than it has" % (bad_shape, "y" if bad_shape == 1 else "ies",
                                    "is" if bad_shape == 1 else "are",
                                    "has" if bad_shape == 1 else "have"))
    if no_id:
        out.append("%d ticket(s) in the graph have no 'id' and have been SKIPPED — an "
                   "unnamed ticket can never be dispatched, closed, or depended on" % no_id)
    return out


def _graph_refuse_if_invalid(root, path, tickets, idx, dupes, what):
    """Refuse to reason about a graph that does not parse as one.

    `frontier` has always validated and exited 1; `check-wave` and `reads`
    unpacked `dupes` and threw it away, so both ran clean over a graph carrying
    a duplicate ticket id. That is not cosmetic for check-wave in particular:
    _graph_index keeps the LAST entry for a repeated id, so a wave was checked
    against whichever definition happened to come second, and the command still
    printed "no two members can own the same file" — the one safety property it
    exists to guarantee, answered from an ambiguous ownership map. Measured: a
    three-ticket graph with T-001 defined twice made frontier exit 1 and
    check-wave exit 0, reporting the second T-001's scope as if it were the only
    one."""
    # The two halves are NOT the same severity, and conflating them was a false
    # block on a graph this harness deliberately survives.
    #
    # _graph_unusable reports entries _graph_index SKIPPED — a stray string or a
    # trailing null from a hand edit. Skipping keeps the tools alive and saying so
    # keeps a dropped ticket from passing as a closed one; `frontier` refuses on it
    # because its whole output is the ticket set, but check-wave and reads are
    # asked about NAMED tickets and answer them correctly regardless. Advisory here.
    #
    # _graph_validate reports a graph that contradicts itself — a duplicate id, a
    # dangling dep, a cycle. A duplicate id is the one that matters most here:
    # _graph_index keeps the LAST entry, so the ownership map check-wave vetoes on
    # is read from whichever definition happened to come second, and it still
    # printed "no two members can own the same file". That is a refusal.
    for note in _graph_unusable(tickets):
        print(f"  advisory: {note}")
    errors = _graph_validate(idx, dupes)
    if not errors:
        return
    print(f"REFUSED: {os.path.relpath(path, root)} contradicts itself, so "
          f"{what} would be computed over an ambiguous graph:")
    for e in errors:
        print(f"  {e}")
    die("fix the graph and re-run — 'frontier' refuses the same graph for the same reason")


def _graph_validate(idx, dupes):
    """[error strings] — duplicate ids, dangling depends_on, dependency cycles.

    A dangling dep is not pedantry: the frontier is computed as "every dep
    closed", so a dep that does not exist can never close and its ticket is
    unreachable for the whole run, silently."""
    errors = []
    for tid in sorted(set(dupes)):
        errors.append(f"duplicate ticket id: {tid}")
    for tid in sorted(idx):
        for dep in _graph_strs(idx[tid], "depends_on"):
            if str(dep) not in idx:
                errors.append(f"{tid} depends on {dep}, which is not in the graph — {tid} can "
                              "never become ready")
    # iterative DFS: a 47-node graph will not recurse deep, but a hand-edited one
    # with a cycle must report the cycle rather than a RecursionError.
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(idx, WHITE)
    for start in sorted(idx):
        if color[start] != WHITE:
            continue
        stack = [(start, iter(_graph_strs(idx[start], "depends_on")))]
        color[start] = GREY
        path = [start]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if nxt not in idx:
                continue
            if color[nxt] == GREY:
                cyc = path[path.index(nxt):] if nxt in path else [nxt]
                errors.append("dependency CYCLE: " + " -> ".join(cyc + [nxt]))
                continue
            if color[nxt] == BLACK:
                continue
            color[nxt] = GREY
            path.append(nxt)
            stack.append((nxt, iter(_graph_strs(idx[nxt], "depends_on"))))
    return errors


def _graph_depth(idx):
    """{id: longest dependency chain below it}. Cycle-safe (a cycle reports 0)."""
    # Iterative for the same reason _graph_validate's cycle DFS is: a
    # hand-edited graph is a PROJECT file, and a deep or pathological chain must
    # report a number, not a RecursionError out of a harness internal. That
    # rationale was written once and applied to only one of the three walks.
    depth, busy = {}, set()
    for root_tid in idx:
        if root_tid in depth:
            continue
        stack = [(root_tid, None)]
        while stack:
            tid, deps_it = stack[-1]
            if deps_it is None:
                if tid in depth:
                    stack.pop()
                    continue
                if tid in busy:            # a cycle: contributes 0, as before
                    depth[tid] = 0
                    stack.pop()
                    continue
                busy.add(tid)
                deps_it = iter([x for x in _graph_strs(idx.get(tid, {}), "depends_on")
                                if x in idx])
                stack[-1] = (tid, deps_it)
            advanced = False
            for nxt in deps_it:
                if nxt not in depth:
                    stack.append((nxt, None))
                    advanced = True
                    break
            if advanced:
                continue
            deps = [x for x in _graph_strs(idx.get(tid, {}), "depends_on") if x in idx]
            depth[tid] = 1 + max([depth.get(x, 0) for x in deps] or [-1])
            busy.discard(tid)
            stack.pop()
    return depth


def _graph_unblocks(idx):
    """{id: how many tickets it transitively unblocks} — the blast-radius half of
    'dependency readiness and blast radius'. Ranks the frontier: the ticket that
    frees eleven others outranks the one that frees none."""
    rev = {tid: set() for tid in idx}
    for tid, t in idx.items():
        for dep in _graph_strs(t, "depends_on"):
            if dep in rev:
                rev[dep].add(tid)
    out = {}
    for tid in idx:
        seen, stack = set(), list(rev[tid])
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(rev.get(x, ()))
        out[tid] = len(seen)
    return out


def _norm_pat(p):
    return str(p or "").strip().replace("\\", "/").lstrip("./")


def _has_glob(p):
    # `{` is here because brace expansion IS a glob: without it `src/{a,b}/**`
    # had the literal prefix `src/{a,b}`, which shares no prefix with `src/a/**`,
    # so check-wave reported no overlap and cleared two workers to write the same
    # files. _expand_braces below handles it precisely; this is the backstop that
    # keeps a brace this function cannot expand on the conservative side.
    return any(ch in p for ch in "*?[{")


BRACE_RE = re.compile(r"\{([^{}]*)\}")
MAX_BRACE_ALTS = 64


def _expand_braces(p):
    """'src/{a,b}/**' -> ['src/a/**', 'src/b/**']. Always returns >= 1 pattern.

    Bounded at MAX_BRACE_ALTS: a pathological `{a,b}{c,d}{e,f}…` must not turn an
    ownership check into an exponential one. On overflow the ORIGINAL pattern is
    returned unexpanded, where _has_glob's `{` sends it down the conservative
    prefix path — a false conflict, which is the safe direction."""
    out = [p]
    while True:
        grew = []
        for pat in out:
            m = BRACE_RE.search(pat)
            if not m:
                grew.append(pat)
                continue
            head, tail = pat[:m.start()], pat[m.end():]
            for alt in m.group(1).split(","):
                grew.append(head + alt.strip() + tail)
        if len(grew) > MAX_BRACE_ALTS:
            return [p]
        if grew == out:
            return out
        out = grew


def _pattern_prefix(p):
    """The literal path prefix of a glob — 'packages/llm/**' -> 'packages/llm'."""
    out = []
    for seg in p.split("/"):
        if _has_glob(seg):
            break
        out.append(seg)
    return "/".join(out)


def _patterns_overlap(a, b):
    """Could two ownership patterns ever match the same file?

    Deliberately CONSERVATIVE — it answers "could", not "does". A false conflict
    costs one serialized ticket; a missed one costs two corrupted branches, and
    that asymmetry is the whole reason this function exists."""
    a, b = _norm_pat(a), _norm_pat(b)
    if not a or not b:
        return True
    # Case is folded on BOTH sides, always and on every platform. `SRC/**` and
    # `src/**` are the same directory on darwin and on a default-configured
    # Windows checkout, and this function's whole asymmetry — stated above — is
    # that a false conflict costs one serialized ticket while a missed one costs
    # two corrupted branches. Folding unconditionally also keeps the answer from
    # depending on which machine the orchestrator happens to be running on.
    for pa_ in _expand_braces(a):
        for pb_ in _expand_braces(b):
            if _overlap_one(pa_.lower(), pb_.lower()):
                return True
    return False


def _overlap_one(a, b):
    """_patterns_overlap for a single brace-free, case-folded pair."""
    if a == "**" or b == "**":
        return True
    if a == b:
        return True
    if not _has_glob(a) and fnmatch.fnmatch(a, b):
        return True
    if not _has_glob(b) and fnmatch.fnmatch(b, a):
        return True
    pa, pb = _pattern_prefix(a), _pattern_prefix(b)
    if pa == "" or pb == "":
        return True          # a glob rooted at the repo root can hit anything
    return (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/"))


def _ticket_owns(t):
    """(patterns, source). A narrowed PER-FILE map wins over the scope globs.

    'Ownership is PER FILE, not per glob' (the live run's backlog.md:14, after
    intake-report.md:45 demoted parallel_safe); `owns` is where that map lives
    when a project has built one, and `scope` is the fallback. parallel_safe is
    read nowhere in here — see cmd_check_wave for why."""
    owns = _graph_strs(t, "owns", "owned_files")
    if owns:
        return [_norm_pat(x) for x in owns], "owns"
    return [_norm_pat(x) for x in _graph_strs(t, "scope")], "scope"


def _open_dispatches(d):
    """Unclosed dispatch records, oldest first. Missing file = none (H-17)."""
    path = os.path.join(d, DISPATCH_FILE)
    if not os.path.exists(path):
        return []
    recs, closed = {}, set()
    order = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("agent_ref"):
                    continue
                ref = rec["agent_ref"]
                if rec.get("action") == "close" or rec.get("closed_at"):
                    closed.add(ref)
                else:
                    if ref not in recs:
                        order.append(ref)
                    recs[ref] = rec
    except OSError:
        return []
    return [recs[r] for r in order if r not in closed]


def cmd_dispatch(args):
    """Record a dispatched agent, or close one. Append-only; changes nothing else.

    H-17: `resume` reconstructs in-flight work exclusively from `git worktree
    list` + `git branch`, so a TICKET worker is recoverable and every agent doing
    orchestrator-owned work WITHOUT a worktree is not. Observed live: a compacted
    orchestrator found a running subagent it could not identify and had to
    interrogate it. Worse than the confusion is the silence — resume reports
    "nothing in flight" for that whole class, so a resumed orchestrator
    re-dispatches it and two agents write the same orchestrator-owned file."""
    root = repo_root()
    d = sd(root)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, DISPATCH_FILE)
    if (args.ticket or args.verdict) and not args.close:
        die("--ticket/--verdict record a COLLECTION verdict and only mean something with "
            "--close <agent-ref>: an open dispatch has not been judged yet", 3)
    if args.do_list:
        open_recs = _open_dispatches(d)
        if not open_recs:
            print("dispatch: nothing open — no recorded agent is still in flight")
            return
        print(f"{len(open_recs)} OPEN dispatch(es), oldest first:")
        for r in open_recs:
            print("  %-24s %-9s %s" % (r.get("agent_ref"), r.get("kind"), r.get("subject")))
            print("      dispatched %s%s" % (r.get("dispatched_at"),
                  "  ·  files: " + ", ".join(r.get("target_files") or [])
                  if r.get("target_files") else ""))
        return
    if args.close:
        open_recs = _open_dispatches(d)
        open_refs = {r["agent_ref"] for r in open_recs}
        if args.close not in open_refs:
            print(f"dispatch: {args.close} is not open (open: "
                  + (", ".join(sorted(open_refs)) or "none") + ") — recording the close anyway")
        # NO VERDICT IS BOUND TO THE SHA IT JUDGED. merge-protocol.md pins the tip
        # at collection and then says "redo it against the new tip" for the
        # fix->re-verify loop, and nothing re-pinned or recorded the second
        # verdict's SHA — so a green from a verifier could be, and was, a green
        # about a tree that no longer existed. Re-resolve the branch here and say
        # so out loud when it moved under the agent.
        prior = next((r for r in open_recs if r["agent_ref"] == args.close), None)
        branch = args.branch or (prior or {}).get("branch")
        tip = None
        if branch:
            out = _git(root, "rev-parse", "--verify", "--quiet", branch + "^{commit}")
            tip = (out.stdout or "").strip() or None
        # THE CLOSE RECORD CARRIED NO TICKET ID AND NO MACHINE-READABLE VERDICT.
        # agent_ref/action/closed_at/note/branch/sha: a REJECTED branch closed
        # byte-identically to an accepted one, with only free-text `note` between
        # them, and nothing tied the verdict to the ticket it was about. Both keys
        # are OPTIONAL and additive — a close without them is exactly what it was.
        rec = {"agent_ref": args.close, "action": "close",
               "closed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "note": args.note}
        tickets = _split_ids(args.ticket)
        if tickets:
            rec["tickets"] = tickets
        if args.verdict:
            rec["verdict"] = args.verdict
        if branch:
            rec["branch"] = branch
            rec["sha"] = tip
        with open(path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"dispatch: {args.close} closed")
        if tickets and args.verdict:
            print("  ledger: %s -> %s.  This record outlives the branch: `retire` deletes "
                  "task/* once it lands, and `frontier --closed-from-ledger` still reads "
                  "this." % (", ".join(tickets), args.verdict))
        elif tickets:
            print("  !! --ticket %s recorded with NO --verdict, so it closes NOTHING — "
                  "--closed-from-ledger reads accepted verdicts ONLY. Re-close with "
                  "--verdict accepted|rejected." % ", ".join(tickets))
        elif args.verdict:
            print("  !! --verdict %s names no ticket (--ticket T-nnn), so no ticket's "
                  "closure is recorded and --closed-from-ledger cannot see this."
                  % args.verdict)
        if branch:
            was = (prior or {}).get("sha")
            print(f"  verdict is bound to {branch}@{(tip or 'UNRESOLVABLE')[:12]}")
            if was and tip and was != tip:
                print(f"  !! {branch} MOVED since dispatch ({was[:12]} -> {tip[:12]}). Whatever "
                      "this agent reported was measured against the OLD tip. Re-verify against "
                      "the tip above before merging, and record THAT verdict — a green about a "
                      "tree that no longer exists is not a green.")
            elif was and tip and was == tip:
                print("  branch is unmoved since dispatch — the verdict is about this tip")
            elif branch and not tip:
                print(f"  !! {branch} does not resolve — it was deleted or never created, so "
                      "there is no tree this verdict can be about")
        return
    missing = [n for n, v in (("--agent", args.agent), ("--kind", args.kind),
                              ("--subject", args.subject)) if not str(v or "").strip()]
    if missing:
        die("dispatch needs " + " ".join(missing)
            + " (or use --list / --close <agent-ref>)", 3)
    files = [x.strip() for x in (args.files or "").split(",") if x.strip()]
    # `head` is the ORCHESTRATOR's HEAD and always was; it says nothing about the
    # branch being judged. --branch pins the tip this dispatch is actually about.
    tip = None
    if args.branch:
        out = _git(root, "rev-parse", "--verify", "--quiet", args.branch + "^{commit}")
        tip = (out.stdout or "").strip() or None
    rec = {
        "agent_ref": args.agent, "action": "open", "kind": args.kind,
        "subject": args.subject, "target_files": files,
        "dispatched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "closed_at": None,
        "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
    }
    if args.branch:
        rec["branch"] = args.branch
        rec["sha"] = tip
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"dispatch: recorded {args.agent} ({args.kind}) on {args.subject}")
    if args.branch:
        print(f"  judging {args.branch}@{(tip or 'UNRESOLVABLE')[:12]}"
              + ("" if tip else "  !! that branch does not resolve"))
    else:
        print("  no --branch given: this dispatch's verdict is bound to no tree, so a "
              "re-verify after a fix cannot be told apart from the original")
    print(f"  close it at collection:  swarmloop.py dispatch --close {args.agent}")


def _split_ids(chunks):
    """['T-1,T-2', ' T-3'] -> ['T-1', 'T-2', 'T-3']. Order preserved, deduped."""
    out = []
    for chunk in (chunks or []):
        for tid in re.split(r"[,\s]+", chunk):
            tid = tid.strip()
            if tid and tid not in out:
                out.append(tid)
    return out


def _leading_ticket_id(suffix):
    """'T-010' -> 'T-010'.  'T-010-supersedes-T-011' -> 'T-010'.  junk -> None.

    The id is anchored at the START of a task branch's suffix and must be followed
    by a separator or nothing. What this replaces was `re.findall(r"\bT-\d+\b")`
    over the whole `git branch` LINE, which closed BOTH ids named by
    `task/T-010-supersedes-T-011` — and the second one was never built."""
    m = re.match(r"^(T-\d+)(?:[-_./].*)?$", suffix or "")
    return m.group(1) if m else None


def _first_parent_shas(root, base):
    """The commits on `base`'s FIRST-PARENT chain, or None if git could not say.

    The invariant this rests on: provisioning a lane is `git branch task/T-003
    <base>`, so a provisioned lane's tip is a commit that WAS base's tip — and a
    base that only ever moves FORWARD (a commit on it, a merge into it, a
    fast-forward) keeps every commit it ever pointed at on its first-parent
    chain. Therefore a merged task/* branch whose tip is NOT on that chain cannot
    be a lane provisioned from base: base reached that tip through the SECOND
    parent of a merge, which is what landing a built lane does.

    None (git failed) is deliberately not folded into the empty set — an empty
    set would read as "nothing is on the chain" and close every branch."""
    out = _git(root, "rev-list", "--first-parent", base)
    if out.returncode != 0:
        return None
    return set((out.stdout or "").split())


def _rev_count(root, a, b):
    """`git rev-list --count a..b`, or 0 when git could not answer.

    0-on-failure is the safe direction HERE and only here: every caller reads a
    POSITIVE count as evidence that work exists, so a failure reads as
    "no evidence", never as "no work"."""
    out = _git(root, "rev-list", "--count", "%s..%s" % (a, b))
    if out.returncode != 0:
        return 0
    try:
        return int((out.stdout or "").strip())
    except ValueError:
        return 0


def _dispatch_branch_shas(d):
    """{branch: [sha, ...]} — every branch tip a dispatch record ever PINNED.

    Both record kinds carry it: `open` stores the tip at dispatch (before the
    lane has committed anything), `close` stores the tip the verdict was measured
    against. A tip that has moved PAST one of these is positive proof that the
    lane committed — the one thing git alone cannot establish about a merged
    branch. Absent-is-fine: the live run keeps no dispatch.jsonl at all."""
    out = {}
    path = os.path.join(d, DISPATCH_FILE)
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                br, sha = rec.get("branch"), rec.get("sha")
                if not (isinstance(br, str) and br.strip()):
                    continue
                if not (isinstance(sha, str) and sha.strip()):
                    continue
                seen = out.setdefault(br.strip(), [])
                if sha.strip() not in seen:
                    seen.append(sha.strip())
    except OSError:
        return out
    return out


def _closed_from_merged(root, base, d=None):
    """(ids, notes) — tickets whose task/* branch is merged into `base` AND for
    which something POSITIVELY establishes that the lane carried work.

    `git branch --merged` is not that something. "Merged" means exactly
    "contributes nothing base lacks", which is equally true of a lane that landed
    and of `git branch task/T-003 main` — a provisioned-and-not-yet-dispatched
    lane, which Phase 3 calls "the normal state". Two predicates that LOOK like
    they discriminate do not:

      * `rev-list --count base..branch == 0` is true of BOTH, by definition.
      * `tip == base_sha` is true of a provisioned lane AND of a branch merged
        fast-forward — git's default when base has not moved. It is also FALSE
        for a provisioned lane the moment base takes any further commit, which in
        a swarm is continuously, so as a guard it essentially never fired.

    So closure here is driven by POSITIVE evidence only, in authority order:

      1. the dispatch ledger — a recorded tip for this branch that the branch has
         since moved past. Proof the lane committed.
      2. the first-parent chain — see _first_parent_shas. Proof base reached this
         tip through a merge's second parent.

    With neither, the lane is UNDETERMINED and stays OPEN, and the note says what
    could not be determined instead of asserting a reason. That direction is the
    whole point: a false-CLOSED silently drops real work out of the frontier, a
    false-OPEN merely burns a lane on something already done.

    ONE thing this still cannot see, and the notes say so when it closes
    anything: `--merged` is REACHABILITY, not PRESENCE. `git revert -m 1` of a
    merge takes the work back out of base and leaves the branch listed as merged
    forever, so a reverted ticket still reads CLOSED here. Only a REJECTED ledger
    verdict reopens it.

    Every non-closure is REPORTED. A branch that looks landed and is being
    ignored is exactly the thing an operator has to see; a silent drop is the
    same defect wearing the other sign."""
    ids, notes = set(), []
    d = d if d is not None else sd(root)
    out = _git(root, "branch", "--merged", base, "--list", "task/*")
    if out.returncode != 0:
        msg = " ".join(((out.stderr or "") + " " + (out.stdout or "")).split())
        return ids, ["--closed-from-merged: git could not list branches merged into %s "
                     "(%s) — NOTHING was derived from git" % (base, msg or "no output")]
    names = []
    for line in (out.stdout or "").splitlines():
        name = line[2:].strip() if line[:2] in ("  ", "* ", "+ ") else line.strip()
        if name:
            names.append(name)
    if not names:
        return ids, notes
    fp = _first_parent_shas(root, base)
    if fp is None:
        notes.append("--closed-from-merged: git could not walk %s's first-parent history, "
                     "so the one PROOF that a lane landed rather than sat provisioned is "
                     "unavailable — only the dispatch ledger is being consulted" % base)
    pinned = _dispatch_branch_shas(d)
    by_first_parent = []
    for name in names:
        suffix = _ticket_of_branch(name)
        tid = _leading_ticket_id(suffix)
        if not tid:
            notes.append("--closed-from-merged: SKIPPED %s — no T-nnn ticket id at the "
                         "front of the branch name, so it closes nothing" % name)
            continue
        tip = (_git(root, "rev-parse", "--verify", "--quiet",
                    name + "^{commit}").stdout or "").strip() or None
        if not tip:
            notes.append("--closed-from-merged: UNDETERMINED %s (%s) — NOT closed. git "
                         "lists it as merged but will not resolve it to a commit, so there "
                         "is no tip to judge" % (name, tid))
            continue
        why = None
        for sha in pinned.get(name, []):
            if sha != tip and _rev_count(root, sha, tip) > 0:
                why = ("the dispatch ledger pinned %s@%s and its tip has since moved to %s"
                       % (name, sha[:12], tip[:12]))
                break
        if why is not None:
            # Rare and load-bearing: the ONLY evidence that rescues a lane merged
            # fast-forward, whose tip git cannot tell from a provisioned one.
            notes.append("--closed-from-merged: %s closes %s — %s" % (name, tid, why))
        elif fp is not None and tip not in fp:
            why = "first-parent"
            by_first_parent.append(tid)
        if why is None:
            notes.append(
                "--closed-from-merged: UNDETERMINED %s (%s) — NOT closed. Its tip %s IS on "
                "%s's first-parent history, which is equally true of a lane provisioned "
                "from %s and never built and of a lane merged FAST-FORWARD, and no dispatch "
                "record pins an earlier tip for it. Nothing here can tell those apart, so "
                "it stays OPEN. If it did land, record it: dispatch --close <ref> --ticket "
                "%s --verdict accepted."
                % (name, tid, tip[:12], base, base, tid))
            continue
        if suffix != tid:
            notes.append("--closed-from-merged: %s closes %s ONLY — the rest of that branch "
                         "name is not a second ticket" % (name, tid))
        ids.add(tid)
    if by_first_parent:
        # One line for the ordinary case rather than one per branch: a live run
        # lands fifty of these and the frontier already prints a screenful.
        shown = sorted(by_first_parent)
        notes.append("--closed-from-merged: %d ticket(s) closed on first-parent evidence — "
                     "their branch tips are NOT on %s's first-parent history, so %s reached "
                     "them through the second parent of a merge: %s%s"
                     % (len(shown), base, base, ", ".join(shown[:12]),
                        ", …" if len(shown) > 12 else ""))
    if ids:
        notes.append("--closed-from-merged: this is REACHABILITY, not presence — a merge "
                     "that was later `git revert`-ed leaves its branch listed by `git "
                     "branch --merged` forever, so a reverted ticket still reads CLOSED "
                     "here. Only a REJECTED ledger verdict reopens one.")
    return ids, notes


def _closed_from_ledger(d):
    """(ids, notes) — tickets closed by an explicit `dispatch --close --ticket …
    --verdict accepted` record.

    Why this exists at all: closure derived from git is NON-MONOTONIC, because
    the harness erases its own signal. `retire` deletes every landed task branch
    (_landed_branches), so `--closed-from-merged` FORGETS a ticket the moment its
    worktree is reclaimed — the work is still in `main`, the evidence is not.
    This record is append-only and outlives the branch it judged.

    LAST verdict per ticket wins: a ticket accepted in one cycle and rejected by
    a later audit is open again, and the ordering of an append-only log is the
    only thing that can say so. A record with no `verdict` key closes nothing —
    the free-text `note` is not a verdict and never was, which is why a REJECTED
    branch used to close exactly like an accepted one."""
    path = os.path.join(d, DISPATCH_FILE)
    if not os.path.exists(path):
        return set(), ["--closed-from-ledger: no %s/%s — nothing has been closed with an "
                       "explicit --ticket/--verdict yet" % (STATE_DIR, DISPATCH_FILE)]
    verdict, bad = {}, 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(rec, dict) or rec.get("action") != "close":
                    continue
                if rec.get("verdict") not in ("accepted", "rejected"):
                    continue
                for tid in (rec.get("tickets") or []):
                    if isinstance(tid, str) and tid.strip():
                        verdict[tid.strip()] = rec["verdict"]
    except OSError as e:
        return set(), ["--closed-from-ledger: cannot read %s (%s)" % (path, e)]
    accepted = sorted(t for t, v in verdict.items() if v == "accepted")
    rejected = sorted(t for t, v in verdict.items() if v == "rejected")
    notes = []
    if accepted:
        # THE LEDGER IS A TRUST BOUNDARY WITH NO READ-SIDE CHECK, and the read
        # side is the place to say so. `dispatch --close` warns once, at write
        # time, when the agent-ref was never open — and then records it anyway,
        # which is correct for an append-only log. Nothing here re-derives
        # whether the branch existed, whether the verdict was earned, or whether
        # a human appended the line by hand; all three are honoured identically.
        notes.append("--closed-from-ledger: %d ticket(s) closed on an OPERATOR-RECORDED "
                     "verdict, not on anything re-verified here — %s is append-only and a "
                     "record naming a branch that never existed closes its ticket exactly "
                     "like a real one" % (len(accepted), DISPATCH_FILE))
    if rejected:
        notes.append("--closed-from-ledger: %d ticket(s) carry a REJECTED verdict and stay "
                     "OPEN: %s" % (len(rejected), ", ".join(rejected)))
    if bad:
        notes.append("--closed-from-ledger: %d unparseable line(s) in %s were skipped"
                     % (bad, DISPATCH_FILE))
    return set(accepted), notes


def _closure_banner(root, d, base, sources, n_closed):
    """The lines `frontier` prints ABOUT its own `closed` count.

    `0 closed` is a well-formed, confident number that on the default path cannot
    be anything else. No ticket field anywhere in this file is read for status —
    closure enters ONLY through the flags below — so with none of them supplied
    every ticket reads OPEN, the READY set is the graph's ROOTS, and work that
    already shipped sorts to the TOP of it. Measured on a live run: 108 tickets ·
    0 closed · 27 READY, ranked `T-000 depth 0 unblocks 81 "Monorepo skeleton
    with green empty verify pipeline"` — a ticket that had shipped long before —
    while the five tickets actually worth dispatching read `blocked` behind it.
    Handing the same graph the 50 ids that had landed turned it into 110 · 50
    closed · 36 READY with the real work on top.

    A number with no path to being wrong is this project's dominant defect class,
    so the no-source case now says so in the loudest terms the command has."""
    if sources:
        if n_closed:
            return []
        return ["  NOTE: a closure source WAS supplied (%s) and it found NOTHING closed."
                % ", ".join(sources),
                "  On a first wave that is correct. Later it means the source has gone "
                "blind: --closed-from-merged stops seeing landed work the moment `retire` "
                "deletes the landed branches. --closed-from-ledger does not."]
    lines = [
        "  !! 0 closed because NO CLOSURE SOURCE WAS SUPPLIED — it is NOT a measurement.",
        "     Nothing here reads a ticket status field (the graph carries none), so every "
        "ticket reads OPEN, the READY set below is the graph's ROOTS rather than a work "
        "queue, and anything already built sorts to the TOP of it.",
        "     Supply one:  --closed T-001,T-002  ·  --closed-from-merged  ·  "
        "--closed-from-ledger",
    ]
    # Evidence, not heuristic: a landed task branch or an accepted ledger verdict
    # is direct proof that this run is past its first wave. Its ABSENCE proves
    # nothing (retire deletes landed branches), so this can only under-report —
    # which is why the quiet case says "consistent with" and not "therefore".
    ev, blind = [], []
    landed, landed_notes = _closed_from_merged(root, base, d)
    # A git call that FAILED must not read as "nothing has landed" — that is the
    # same silent-wrong this whole banner exists to kill, one level down.
    blind += [n for n in landed_notes if "git could not" in n]
    # And neither must a lane nothing could CLASSIFY. A branch merged
    # fast-forward is indistinguishable from a provisioned one, so "no landed
    # branch found" over a pile of undetermined lanes is not a first wave — it is
    # a question that was not answered.
    undetermined = [n for n in landed_notes if "UNDETERMINED" in n]
    if landed:
        shown = sorted(landed)
        ev.append("%d ticket(s) have a task/* branch LANDED on %s (%s%s)"
                  % (len(shown), base, ", ".join(shown[:6]),
                     ", …" if len(shown) > 6 else ""))
    accepted, _ = _closed_from_ledger(d)
    if accepted:
        ev.append("%d ticket(s) carry an ACCEPTED verdict in %s" % (len(accepted),
                                                                    DISPATCH_FILE))
    if blind:
        lines.append("     COULD NOT LOOK: " + " / ".join(blind)
                     + ". Absence of evidence below is absence of a working query, not "
                       "absence of landed work — check --base.")
    if ev:
        lines.append("     EVIDENCE that this run is NOT at its first wave: " + "; ".join(ev)
                     + ". Every one of those is being ranked below as if it were unbuilt.")
    if undetermined:
        lines.append("     UNDETERMINED: %d task/* branch(es) are merged into %s and nothing "
                     "here can say whether they carried work — a lane provisioned from %s "
                     "and never built and a lane merged FAST-FORWARD have identical git "
                     "shape. Run `frontier --closed-from-merged` for the per-branch reason. "
                     "This run may well be past its first wave." % (len(undetermined), base,
                                                                    base))
    if not ev and not blind and not undetermined:
        lines.append("     (No landed task/* branch and no accepted ledger verdict found — "
                     "CONSISTENT with a first wave, and proof of nothing: `retire` deletes "
                     "landed branches, so this evidence goes quiet on a run that is far "
                     "along.)")
    return lines


def _closed_from_args(root, args):
    """(closed ids, {"sources": [...], "notes": [...]}).

    `sources` is the load-bearing half: it is the difference between "nothing is
    done" and "nobody said what is done", which the bare count cannot express."""
    closed, notes, sources = set(), [], []
    d = sd(root)
    closed.update(_split_ids(args.closed))
    if args.closed:
        sources.append("--closed")
    if getattr(args, "closed_from_merged", False):
        sources.append("--closed-from-merged")
        ids, n = _closed_from_merged(root, args.base, d)
        closed |= ids
        notes += n
    if getattr(args, "closed_from_ledger", False):
        sources.append("--closed-from-ledger")
        ids, n = _closed_from_ledger(d)
        closed |= ids
        notes += n
    # INVARIANT the `unknown ids` note in cmd_frontier depends on: `closed` is
    # non-empty only if some source ran, and every source appends to `sources`
    # BEFORE it contributes. So `sources` is non-empty wherever that note can
    # print — which is why the `or "--closed"` fallback it used to carry had no
    # path to running, and is gone. No assert guards this: every other error in
    # this file exits with a message rather than a traceback, and a guard whose
    # only reachable behaviour is crashing is not an improvement on dead code.
    return closed, {"sources": sources, "notes": notes}


def cmd_frontier(args):
    """The ready set, with depth and transitive-unblocks. Replaces the ad-hoc
    Python heredoc the live orchestrator hand-wrote twice in one evening."""
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    path, tickets = _load_graph(root, state, args.graph)
    idx, dupes = _graph_index(tickets)
    errors = _graph_unusable(tickets) + _graph_validate(idx, dupes)
    closed, closure = _closed_from_args(root, args)
    closure_banner = _closure_banner(root, d, args.base, closure["sources"], len(closed))
    unknown_closed = sorted(c for c in closed if c not in idx)
    depth, unblocks = _graph_depth(idx), _graph_unblocks(idx)

    ready, blocked = [], []
    for tid in sorted(idx):
        if tid in closed:
            continue
        deps = _graph_strs(idx[tid], "depends_on")
        open_deps = [x for x in deps if x not in closed]
        (blocked if open_deps else ready).append((tid, open_deps))
    ready.sort(key=lambda r: (-unblocks.get(r[0], 0), depth.get(r[0], 0), r[0]))

    if args.json:
        payload = {
            "graph": os.path.relpath(path, root),
            "tickets": len(idx), "closed": sorted(closed),
            "unknown_closed_ids": unknown_closed,
            "errors": errors,
            "ready": [{"id": t, "depth": depth.get(t, 0), "unblocks": unblocks.get(t, 0),
                       "verify": idx[t].get("verify"),
                       "owns": _ticket_owns(idx[t])[0]} for t, _ in ready],
            "blocked": [{"id": t, "waiting_on": od} for t, od in blocked],
        }
        # Additive, and only when there is something to say: a reader written
        # against the old shape sees an unchanged document on the path where
        # closure was supplied and behaved.
        if closure_banner or closure["notes"]:
            payload["closure"] = {"sources": closure["sources"],
                                  "notes": closure["notes"],
                                  "warning": closure_banner}
        print(json.dumps(payload, indent=2))
    else:
        print(f"graph: {os.path.relpath(path, root)}  ·  {len(idx)} ticket(s)  ·  "
              f"{len(closed)} closed  ·  {len(ready)} READY  ·  {len(blocked)} blocked")
        for line in closure_banner:
            print(line)
        for line in closure["notes"]:
            print("  " + line)
        if unknown_closed:
            print("  NOTE: closure (%s) named ids that are not in the graph: %s"
                  % (", ".join(closure["sources"]), ", ".join(unknown_closed)))
        for e in errors:
            print(f"  GRAPH ERROR: {e}")
        print("")
        print("  READY (dispatchable now — ordered by what each one unblocks):")
        for tid, _ in ready:
            own, src = _ticket_owns(idx[tid])
            print("    %-9s depth %-3d unblocks %-3d  %s" %
                  (tid, depth.get(tid, 0), unblocks.get(tid, 0),
                   (idx[tid].get("title") or "")[:52]))
            print("              owns(%s): %s" % (src, ", ".join(own) or "(none declared)"))
        if not ready:
            print("    (none — every open ticket is waiting on a dependency)")
        print("")
        print(f"  BLOCKED ({len(blocked)}):")
        for tid, od in blocked[:40]:
            print("    %-9s waiting on %s" % (tid, ", ".join(od)))
        if len(blocked) > 40:
            print(f"    ... and {len(blocked) - 40} more")
        print("")
        print("  A ready set is NOT a wave: run 'check-wave <ids>' before dispatching, and")
        print("  'red-check' before trusting any of their verify commands.")
    if errors:
        sys.exit(1)


def cmd_check_wave(args):
    """Refuse a wave whose members could own the same file.

    parallel_safe is read NOWHERE here, deliberately. Three documents gave three
    answers — EXECUTION.md "parallelize only tickets marked parallel_safe",
    intake-report.md:45 "unusable as the dispatch predicate in both directions",
    backlog.md:14 "ownership is PER FILE, not per glob" — and no code. Ownership
    overlap is the property that actually matters, it is computable, and this
    computes it. The flag is reported as advisory context and never vetoes."""
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    path, tickets = _load_graph(root, state, args.graph)
    idx, dupes = _graph_index(tickets)
    _graph_refuse_if_invalid(root, path, tickets, idx, dupes,
                             "the ownership overlap this command vetoes on")
    wave = []
    for raw in args.tickets:
        for tid in re.split(r"[,\s]+", raw):
            if tid.strip():
                wave.append(tid.strip())
    unknown = [t for t in wave if t not in idx]
    if unknown:
        die("not in %s: %s" % (os.path.relpath(path, root), ", ".join(unknown)))
    if len(set(wave)) != len(wave):
        die("the same ticket is listed twice in the wave")

    owns = {t: _ticket_owns(idx[t]) for t in wave}
    # A member that declares nothing owns EVERYTHING until it says otherwise —
    # the rule this command already PRINTS, and used to contradict one line
    # later. An empty ownership list made the overlap loops treat the member as
    # owning nothing, so a mixed wave (one ticket declaring `scope`, one with
    # `scope` absent / [] / null) passed clean; the refusal below only ever
    # fired when EVERY member declared nothing. `owns` stays the DECLARED map
    # (the display and that all-undeclared refusal both read it); `eff` is what
    # the conflict machinery is allowed to reason with.
    eff = {t: (p or ["**"]) for t, (p, _) in owns.items()}
    conflicts = []
    for i, a in enumerate(wave):
        for b in wave[i + 1:]:
            for pa in eff[a]:
                for pb in eff[b]:
                    if _patterns_overlap(pa, pb):
                        conflicts.append((a, b, pa, pb))
                        break
                else:
                    continue
                break
    # Also against work already in flight that owns no worktree (H-17): the only
    # place check-wave can see it is the dispatch log.
    inflight = []
    for rec in _open_dispatches(d):
        for f_ in rec.get("target_files") or []:
            for t in wave:
                for pa in eff[t]:
                    if _patterns_overlap(pa, f_):
                        inflight.append((t, rec.get("agent_ref"), pa, f_))
                        break

    print(f"wave of {len(wave)}: {', '.join(wave)}   (graph {os.path.relpath(path, root)})")
    for t in wave:
        pats, src = owns[t]
        print("  %-9s owns(%s): %s" % (t, src, ", ".join(pats) or "(NOTHING DECLARED)"))
        if not pats:
            print("            a ticket that declares no ownership cannot be checked for "
                  "overlap — treat it as owning everything until it says otherwise")
    unsafe = [t for t in wave if idx[t].get("parallel_safe") is False]
    if unsafe:
        print("  advisory: parallel_safe is false for " + ", ".join(unsafe)
              + " — recorded, NOT a veto (the flag is unusable as a dispatch predicate in "
                "both directions; ownership overlap is the property that matters)")
    if not owns or all(not p for p, _ in owns.values()):
        print("check-wave: REFUSED — no ticket in this wave declares any ownership")
        sys.exit(1)
    if conflicts:
        print("VETO: ticket ownership sets intersect — these must not run concurrently:")
        for a, b, pa, pb in conflicts:
            print(f"  {a} <-> {b}   via  {pa}  ∩  {pb}")
        print("Split the wave (dispatch one, then the other) or narrow the ownership map.")
        sys.exit(1)
    if inflight:
        print("VETO: a wave member overlaps work already IN FLIGHT (from "
              f"{STATE_DIR}/{DISPATCH_FILE}):")
        for t, ref, pa, f_ in inflight:
            print(f"  {t} <-> {ref}   via  {pa}  ∩  {f_}")
        print("Close the dispatch first ('dispatch --close <ref>') or hold the ticket back.")
        sys.exit(1)
    # B5/H-10, the other half: disjoint WRITE sets are not the whole safety
    # property. If A's frozen tests import a module B is building in the same
    # wave, A is graded against a moving target and its red/green says nothing
    # about A. Ownership is still disjoint, so this is NOT a veto — it is the
    # thing the orchestrator has to know before it reads either branch's result.
    try:
        res = _scan_read_edges(root, state, idx)
    except Exception:
        res = None                       # never let a diagnostic take the veto down
    crossing = []
    if res:
        for a in wave:
            for b, mods in (res["reads"].get(a) or {}).items():
                if b in wave:
                    crossing.append((a, b, mods))
    if crossing:
        print("  advisory (read edges, NOT a veto — the write sets are disjoint):")
        for a, b, mods in crossing:
            print("    %s's frozen tests import %s — which %s is building in this same wave. "
                  "%s cannot be graded until %s merges." % (a, ", ".join(mods), b, a, b))
    # Commit counts, asked BEFORE dispatch rather than after. Nothing here
    # vetoes, deliberately: pre-dispatch, a branch with zero commits is a
    # freshly provisioned lane, which is the normal state, and refusing it would
    # block the loop's own provisioning step. What IS actionable here is the
    # reciprocal question nobody asked live -- seven lanes were dispatched onto
    # worktrees 26-27 commits behind base, and every one of them merged clean.
    base = args.base
    head = _git(root, "rev-parse", "--verify", "--quiet", base + "^{commit}")
    if head.returncode != 0:
        print(f"  fork accounting: SKIPPED — cannot resolve base ref {base!r}")
    else:
        fresh, existing = [], []
        for t in wave:
            br = "task/" + t
            if _git(root, "rev-parse", "--verify", "--quiet",
                    br + "^{commit}").returncode != 0:
                fresh.append(t)
            else:
                existing.append((t, br, _lane_commits(root, base, br)))
        print("  base %s @ %s" % (base, head.stdout.strip()[:12])
              + ("  ·  %d member(s) have no branch yet and will fork from it: %s"
                 % (len(fresh), ", ".join(fresh)) if fresh else ""))
        for t, br, lane in existing:
            if lane is None:
                print(f"  {br}: git could not count commits against {base}")
                continue
            print("  %s: %d commit(s) ahead / %d behind %s"
                  % (br, lane["ahead"], lane["behind"], base))
            if lane["ahead"] == 0:
                print("      no commits yet — an EMPTY lane. Expected for a branch that was "
                      "provisioned and not yet dispatched; a FAILED task if it was already "
                      "dispatched and collected. check-branch refuses it at integration, "
                      "which is the gate that matters; this is the early warning.")
            if lane["behind"]:
                drift = [f for f in lane["base_files"]
                         if any(_scope_match(p, f) for p in eff[t])]
                print("      STALE BASE: forked at %s; %s has moved %d commit(s) over %d "
                      "file(s) since%s" % (lane["fork"][:12], base, lane["behind"],
                                           len(lane["base_files"]),
                                           ", %d of them inside this ticket's own ownership:"
                                           % len(drift) if drift
                                           else " — none inside this ticket's ownership."))
                for f_ in drift[:10]:
                    print(f"      ~ {f_}")
                if len(drift) > 10:
                    print(f"      ... and {len(drift) - 10} more")
                if drift:
                    print("      Re-create this worktree from current %s before dispatching. "
                          "Otherwise the worker builds and self-verifies against a tree that "
                          "no longer exists, and a clean merge at collection will not reveal "
                          "it — textual mergeability is unaffected by a stale base." % base)
    print("check-wave: OK — no two members can own the same file, and nothing in flight "
          "overlaps them")


# ------------------------------------------------- findings -> tickets

FINDINGS_FILE = "findings.jsonl"

# Ranked so a threshold comparison is an integer comparison and never a string
# one. UNKNOWN is deliberately absent: an unrecognised severity is an error, not
# a silent skip, because a severity nobody recognises is exactly how a HIGH gets
# filtered out of its own report.
SEVERITY_RANK = {"BLOCKER": 5, "CRITICAL": 5, "HIGH": 4,
                 "MEDIUM": 3, "LOW": 2, "INFO": 1}


def _finding_digest(rec):
    """Stable identity for a finding: severity + location + defect, verbatim."""
    payload = json.dumps({k: rec.get(k) for k in ("severity", "location", "defect")},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _finding_scope(location):
    """The FILE a location names, with a trailing line range removed and NOTHING else.

    The live defect this exists to make impossible: the in-session formatter did
    `str(x.get('location')).split('/')[-1][:60]` — keep the basename, throw the
    directory away — so `fixtures/generator/__init__.py:175` was recorded as
    `__init__.py:175`, and four of eight minted tickets pointed at a file that
    does not contain the defect. A ticket naming the wrong file is worse than no
    ticket: it sends a worker to read code that is fine, and the real defect
    keeps its cover. There is no '/' split in this function and there must never
    be one.
    """
    loc = str(location or "").strip()
    if not loc:
        return None
    m = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", loc)
    return ((m.group(1) if m else loc).strip()) or None


def _read_findings(path):
    """[records] from a JSON array, a {"findings": [...]} object, or JSONL.

    Accepts all three because the producer is a checker subagent whose output
    shape the harness does not own, and refusing the shape is how a report ends
    up pasted into a commit message instead."""
    try:
        raw = sys.stdin.read() if path == "-" else open(path).read()
    except OSError as e:
        die(f"cannot read findings from {path!r}: {e}")
    raw = raw.strip()
    if not raw:
        die(f"no findings in {path!r} — an empty report is not a clean report; if the "
            "checkers genuinely returned nothing, say so in the cycle report rather than "
            "recording an empty file")
    try:
        data = json.loads(raw)
    except ValueError:
        data = []
        for i, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except ValueError as e:
                die(f"{path}: line {i} is neither part of a JSON document nor a JSONL "
                    f"record: {e}")
    if isinstance(data, dict):
        data = data.get("findings") or data.get("results") or [data]
    if not isinstance(data, list):
        die(f"{path}: expected a list of finding records, or an object with a "
            f"'findings' list — got {type(data).__name__}")
    out = []
    for i, rec in enumerate(data):
        if not isinstance(rec, dict):
            die(f"{path}: entry {i} is a {type(rec).__name__}, not a finding object")
        out.append(rec)
    return out


def cmd_findings(args):
    """Record checker findings durably, THEN mint tickets from them.

    Two live defects, one command, and the order is the whole point.

    (1) Five checkers returned six HIGH defects as structured records. The
        MEDIUMs were persisted as tickets; the HIGHs were written to no repo
        artifact at all — not escalations, not the backlog, not a report — and
        survived only in a subagent journal and two commit messages. The
        severity filter was, in effect, inverted: the band that most needed a
        ticket got nothing. So EVERY record is appended verbatim to
        findings.jsonl BEFORE any threshold is applied, and the reconciliation
        between what was recorded and what was minted is printed every run and
        asserted at the end. A finding can be judged not worth a ticket; it can
        no longer be judged out of existence.

    (2) The formatter that minted the tickets basename-split `location` and
        never read `reproduction`, so half the tickets pointed at the wrong
        file. Both fields are now carried verbatim — see _finding_scope.

    The harness offered nothing for this step, so every run re-invented it and
    re-invented the bugs. That is why this is a subcommand and not a paragraph
    in references/: the last three things that were only a paragraph were the
    worktree teardown that never ran, the LEARNED.md append that never happened,
    and this."""
    root = repo_root()
    d = sd(root)
    os.makedirs(d, exist_ok=True)
    records = _read_findings(args.file)

    # --- validate BEFORE writing anything: a half-recorded batch is worse than
    #     a refused one, because the operator cannot tell which half landed.
    threshold = SEVERITY_RANK[args.min_severity.strip().upper()]
    bad = []
    for i, rec in enumerate(records):
        sev = str(rec.get("severity") or "").strip().upper()
        if sev not in SEVERITY_RANK:
            bad.append(f"  entry {i}: severity {rec.get('severity')!r} is not one of "
                       + "/".join(sorted(SEVERITY_RANK, key=lambda k: -SEVERITY_RANK[k])))
        if not str(rec.get("defect") or "").strip():
            bad.append(f"  entry {i}: no 'defect' text — a finding with no statement of "
                       "the defect cannot become a ticket anyone can act on")
    if bad:
        print("REFUSED: the finding report is malformed, and nothing has been written:")
        for b in bad:
            print(b)
        die("fix the report and re-run; an unrecognised severity is how a HIGH gets "
            "silently dropped, which is the defect this command exists to prevent")

    # --- 1. record EVERY finding, verbatim, at every severity, first.
    fpath = os.path.join(d, FINDINGS_FILE)
    seen = set()
    if os.path.exists(fpath):
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        seen.add(json.loads(line).get("finding_id"))
                    except ValueError:
                        continue
        except OSError:
            seen = set()

    stamped, fresh = [], 0
    for rec in records:
        fid = _finding_digest(rec)
        out = dict(rec)                     # the producer's fields, untouched
        out["finding_id"] = fid
        out["severity"] = str(rec.get("severity")).strip().upper()
        out.setdefault("recorded_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        if args.cycle is not None:
            out.setdefault("cycle", args.cycle)
        stamped.append(out)
        if fid not in seen:
            fresh += 1
    if not args.dry_run:
        with open(fpath, "a") as f:
            for out in stamped:
                if out["finding_id"] not in seen:
                    f.write(json.dumps(out, sort_keys=True, ensure_ascii=False) + "\n")

    by_sev = {}
    for out in stamped:
        by_sev.setdefault(out["severity"], []).append(out)
    order = sorted(by_sev, key=lambda s: -SEVERITY_RANK[s])
    print(f"findings: {len(stamped)} record(s) read, {fresh} new"
          + ("  [DRY RUN — nothing written]" if args.dry_run else
             f" appended to {STATE_DIR}/{FINDINGS_FILE}"))
    for sev in order:
        print(f"  {sev:<8} {len(by_sev[sev])}")

    if args.no_tickets:
        print(f"--no-tickets: recorded only. Every severity is on disk; nothing was minted.")
        return

    # --- 2. mint tickets, carrying location and reproduction VERBATIM.
    try:
        with open(os.path.join(d, STATE_FILE)) as _sf:
            _state = json.load(_sf)
    except (OSError, ValueError):
        _state = {}                      # a graph passed by --graph needs no state

    # --- 1b. PARTITION ON WHO MAY FIX IT, before a ticket exists that nobody
    #     can close. Two adversarial swarms returned in one session — one on the
    #     product, one on the frozen acceptance harness that grades it — and the
    #     orchestrator piped both reports into this command in a single gesture,
    #     minutes after stating the correct distinction in prose. The split was
    #     perfectly clean and machine-detectable: every finding from the second
    #     swarm named a protected path. Roughly thirty tickets would have been
    #     minted at workers whom `check-branch` vetoes from that path EVERY
    #     time; only an unrelated --dry-run stopped it. A distinction an
    #     orchestrator can state correctly and act against in the next tool call
    #     is not a prose problem, so the recorded location decides the
    #     destination here and memory never gets a vote.
    #
    #     Before the threshold test, deliberately: lowering --min-severity must
    #     not turn a harness defect into a ticket, and reporting one as
    #     "ticketable later" would be a lie. Before the graph load too — a
    #     report that is ALL harness needs no graph, writes nothing, and must
    #     not re-seal anything.
    _protected = [str(p).strip() for p in
                  (_state.get("protected_paths") or MANDATORY_PROTECTED)
                  if str(p).strip()]
    harness, mintable = [], []
    for out in stamped:
        _sc = _finding_scope(out.get("location"))
        # Normalised, because a location is FREE TEXT from a checker subagent while
        # check-branch reads `git diff --name-only`, already canonical. A leading
        # "./" was enough to smuggle a harness finding into the ticket ledger.
        # (normpath alone drops "./" and collapses "//" and ".."; never lstrip("./"),
        # which takes a CHARACTER SET and silently disarms the whole partition.)
        if _sc:
            _sc = os.path.normpath(_sc)
        # BOTH of check-branch's vetoes, or this asks a NARROWER question than the
        # surface it protects: protected_paths ("modifies the frozen goal harness")
        # AND all of STATE_DIR/ ("orchestrator-owned state"). A finding at
        # .swarm-loop/worker-preamble.md is exactly as unclosable as one in the
        # acceptance suite.
        if _sc and (any(_sc == p or _sc.startswith(p.rstrip("/") + "/") for p in _protected)
                    or _sc == STATE_DIR or _sc.startswith(STATE_DIR + "/")):
            harness.append(out)
        else:
            mintable.append(out)
    if harness:
        _rec = "recorded" if not args.dry_run else "NOT recorded — this is a --dry-run"
        print(f"\n{len(harness)} finding(s) name a path NO WORKER MAY TOUCH — {_rec}, "
              "and REFUSED a ticket at every severity:")
        for out in harness:
            print(f"  {out['severity']:<8} {str(out.get('location') or '')[:70]}")
        print("  No worker may fix these — `check-branch` vetoes every branch that touches a\n"
              "  protected path or orchestrator-owned state, so each would be a ticket\n"
              "  dispatched to somebody forbidden to close it. They belong to the\n"
              "  ORCHESTRATOR, and only the user can authorise the change. File each:")
        print("    swarmloop.py escalate --kind harness --subject <path> "
              "--reason \"<the finding and its evidence>\" --proposed \"<the exact change>\"")
        print("  and act on the ruling with `freeze --amend`. Never mint these by hand either: "
              "a\n  worker cannot close what the branch veto rejects.")
    if not mintable:
        print(f"\nEVERY severity band is accounted for: 0 minted + 0 below threshold + 0 "
              f"already ticketed + {len(harness)} harness-owned = {len(stamped)} recorded.")
        return

    gpath, tickets = _load_graph(root, _state, args.graph, required=True)
    idx, _dupes = _graph_index(tickets)
    already = {str(t.get("finding_id") or "") for t in tickets if isinstance(t, dict)}

    # --- G-1, the interesting half: freeze now hashes the ticket graph, and
    #     THIS command writes to it on every audit loop. Two ways to lose, and
    #     both were live options:
    #
    #       * refuse the mint on a frozen run — the audit loop stops turning
    #         findings into tickets, which is the whole of act two; or
    #       * write and say nothing — 'verify' then reports a tamper for work
    #         the harness itself did, every loop, until nobody reads it.
    #
    #     Neither. A mint is a SANCTIONED write to one protected file, so it is
    #     recorded the way an amendment is: re-hash exactly that path into the
    #     manifest and append a delta to the append-only freeze log naming the
    #     tickets minted (_reseal_protected).
    #
    #     The precondition is the whole safety property. Re-sealing a graph whose
    #     bytes ALREADY disagree with the manifest would make this command the
    #     laundry — a worker adds `bootstrap: true` to its own ticket, the next
    #     audit loop's `findings` re-seals it, and `verify` goes green over the
    #     exemption forever. So a graph that does not match is refused here, and
    #     the findings are already on disk when it happens.
    #
    #     Scoped to the graph ALONE, deliberately: an unrelated harness tamper is
    #     `verify`'s to report and must not be blessed here, and equally must not
    #     stop the audit loop from recording what it found.
    grel_graph = os.path.relpath(gpath, root).replace(os.sep, "/")
    _mpath = os.path.join(d, MANIFEST_FILE)
    _manifest = {}
    if os.path.exists(_mpath):
        try:
            with open(_mpath) as _mf:
                _manifest = json.load(_mf) or {}
        except (OSError, ValueError):
            _manifest = {}
    frozen_graph_digest = (_manifest.get("files") or {}).get(grel_graph)
    if frozen_graph_digest is not None:
        on_disk = sha256_file(gpath)
        if on_disk != frozen_graph_digest:
            print("INTEGRITY VIOLATION — the frozen ticket graph was altered:")
            print(f"  [MODIFIED] {grel_graph}")
            print(f"    frozen {frozen_graph_digest[:16]}… / on disk {on_disk[:16]}…")
            die("refusing to mint into a ticket graph that no longer matches %s/%s. The "
                "findings are safe (%s) — but minting would re-hash whatever ELSE was "
                "changed in the same file into the manifest, and a hand-added "
                "`bootstrap: true` on a worker's own ticket is exactly what that would "
                "launder. Run 'verify', then 'restore-harness', then re-run this command; "
                "if the change was a legitimate amendment, record it with "
                "'freeze --amend \"<reason>\"' first."
                % (STATE_DIR, MANIFEST_FILE,
                   "this is a --dry-run: nothing was written" if args.dry_run
                   else f"recorded in {STATE_DIR}/{FINDINGS_FILE} above"))

    nums = []
    for tid in idx:
        m = re.match(r"^([A-Za-z]+-)(\d+)$", tid)
        if m and m.group(1) == args.prefix:
            nums.append(int(m.group(2)))
    nxt = (max(nums) + 1) if nums else 1
    width = max(3, max((len(str(n)) for n in nums), default=3))

    minted, below, dup = [], [], []
    resealed, reseal_seq = None, None
    for out in mintable:
        if out["finding_id"] in already:
            dup.append(out)
            continue
        if SEVERITY_RANK[out["severity"]] < threshold:
            below.append(out)
            continue
        defect = str(out.get("defect")).strip()
        title = defect.splitlines()[0].strip()
        loc = str(out.get("location") or "").strip()
        scope = _finding_scope(loc)
        tid = f"{args.prefix}{nxt:0{width}d}"
        nxt += 1
        t = {
            "id": tid,
            "title": title,
            # A placeholder that FAILS red-check on purpose. `true` would pass
            # vacuously and let an ungated ticket dispatch; this exits non-zero
            # with zero tests selected, which red-check scores WEAK and refuses.
            # The ticket is not dispatchable until someone writes a real gate.
            "verify": "false  # NO GATE YET — write one that fails on this finding first",
            "scope": [scope] if scope else [],
            "depends_on": [],
            "source": "finding",
            "severity": out["severity"],
            # Verbatim. Not basenamed, not truncated.
            "location": loc,
            "defect": defect,
            "finding_id": out["finding_id"],
        }
        repro = out.get("reproduction")
        if repro not in (None, ""):
            t["reproduction"] = repro      # the field the old formatter never read
        if not scope and loc:
            t["scope_note"] = ("location did not name a file — declare ownership by hand "
                               "before this ticket joins a wave")
        minted.append(t)

    # --- 3. the reconciliation. Every recorded finding is minted, deliberately
    #     below the threshold, or already present. Anything else is a bug in
    #     this command, and it dies rather than reporting a clean run.
    accounted = len(minted) + len(below) + len(dup) + len(harness)
    if accounted != len(stamped):
        die(f"INTERNAL: {len(stamped)} findings recorded but only {accounted} accounted for "
            f"({len(minted)} minted, {len(below)} below threshold, {len(dup)} already "
            "ticketed). Refusing to report a clean run over an unexplained gap — this is "
            "exactly the shape of the defect this command exists to prevent")

    if args.dry_run:
        print(f"\n[DRY RUN] would mint {len(minted)} ticket(s) into "
              f"{os.path.relpath(gpath, root)}:")
    elif minted:
        # Re-read and write back through the SAME shape the project uses: the
        # graph is a project file whose schema this harness does not own, so a
        # bare list stays a bare list and {"tickets": [...]} keeps its siblings
        # (version, project) rather than being flattened out from under them.
        #
        # `elif minted` and not `else`: this used to rewrite the file even when
        # it had nothing to add, and re-serialising is a BYTE CHANGE (indent,
        # key order, trailing newline). Harmless while the graph was an ordinary
        # project file; now that freeze hashes it, the second run of an
        # unchanged report would have moved the hash and turned `verify` red
        # over a mint of zero tickets.
        with open(gpath) as _gf:
            full = json.load(_gf)
        if isinstance(full, list):
            save_json(gpath, full + minted)
        else:
            full["tickets"] = list(full.get("tickets") or []) + minted
            save_json(gpath, full)
        print(f"\nminted {len(minted)} ticket(s) into {os.path.relpath(gpath, root)}:")
        if frozen_graph_digest is not None:
            resealed = sha256_file(gpath)
            reseal_seq = _reseal_protected(
                root, d, grel_graph, frozen_graph_digest, resealed,
                "graph-mint",
                "findings minted %d ticket(s): %s"
                % (len(minted), ", ".join(t["id"] for t in minted)),
                extra={"tickets_minted": [t["id"] for t in minted]})
    else:
        print(f"\nminted 0 ticket(s) into {os.path.relpath(gpath, root)}:")
    for t in minted:
        print(f"  {t['id']:<9} {t['severity']:<8} {t['location'] or '(no location)'}")
        print(f"            {t['title'][:96]}")
    if reseal_seq is not None:
        print(f"\nthe ticket graph is a PROTECTED path, so this mint was RE-SEALED and recorded "
              "— it is a sanctioned write, not a silent one:")
        print(f"  {grel_graph}  {frozen_graph_digest[:16]}… -> {resealed[:16]}…")
        print(f"  {STATE_DIR}/{FREEZE_LOG_FILE} record #{reseal_seq}, action `graph-mint` "
              "(append-only). 'verify' is green on exactly these bytes and no others.")
    if below:
        print(f"\n{len(below)} finding(s) BELOW --min-severity {args.min_severity} — recorded, "
              "not ticketed. They are on disk and can be ticketed later by lowering the "
              "threshold; they were not discarded:")
        for out in below:
            print(f"  {out['severity']:<8} {str(out.get('location') or '')[:70]}")
    if dup:
        print(f"\n{len(dup)} finding(s) already carry a ticket — skipped.")
    print(f"\nEVERY severity band is accounted for: {len(minted)} minted + {len(below)} "
          f"below threshold + {len(dup)} already ticketed"
          + (f" + {len(harness)} harness-owned" if harness else "")
          + f" = {len(stamped)} recorded.")
    if minted:
        print("Each minted ticket's `verify` is a PLACEHOLDER that red-check refuses. Write a "
              "gate that fails on the finding before dispatching it.")


def _collected_count(text):
    """How many tests pytest actually SELECTED to run. None when it says none.

    It used to read `collected N items` — the number pytest COLLECTED — and
    pytest prints the collected count and the deselected count on the SAME
    line. So `pytest -m ticket_T010`, written before that marker exists (the
    single most likely shape for a declared per-ticket `verify`), prints
    `collected 120 items / 120 deselected / 0 selected`, exits non-zero, and
    red-check scored it `red (120 test(s) collected) — a real gate`. Zero tests
    ran. Selection is what "was anything observed?" actually means, so an
    explicit `N selected` wins, and `collected - deselected` is the fallback
    for the pytest versions that do not print it.

    The old fallback `(\\d+) (?:passed|failed|error)` was unanchored and matched
    the bare substring `error` inside `errors`, so any tool that says "errors"
    handed red-check a fabricated test count off a run that collected nothing:
    tsc's `Found 3 errors in 2 files.` -> 3, ruff's `Found 12 errors.` -> 12,
    eslint's `7 problems (7 errors, 0 warnings)` -> 7 — each one scored "a real
    gate". The fallback is now anchored on both sides and reads only pytest's
    own `N passed` / `N failed` summary. A compiler's error count is not a test
    count, and a gate that produced no test count is WEAK, which is the honest
    answer."""
    m = re.search(r"\b(\d+) selected\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\bcollected (\d+) item", text)
    if m:
        n = int(m.group(1))
        de = re.search(r"\b(\d+) deselected\b", text)
        return max(0, n - int(de.group(1))) if de else n
    m = re.search(r"\b(\d+) (?:passed|failed)\b", text)
    return int(m.group(1)) if m else None


# The other half of the count above, and the one SKILL.md has demanded beside
# every binary gate reading since the deselection incident while nothing
# computed it. `N deselected` is pytest's own word, printed in -q and verbose
# alike, and it is the ONLY token that separates the two worlds below.
DESELECTED_RE = re.compile(r"\b(\d+) deselected\b")
# Evidence that the text came from a pytest run at all. Without this test,
# "selected zero" is indistinguishable from "this metric is `wc -l` and there
# was nothing to parse", and refusing the second is a false refusal on a
# perfectly good metric. `error`/`errors` are deliberately NOT in here: tsc's
# `Found 3 errors in 2 files.` and ruff's `Found 12 errors.` are the exact
# strings _collected_count was hardened against reading as a test count.
PYTEST_SHAPE_RE = re.compile(
    r"\bcollected \d+ item|\b\d+ (?:selected|deselected|passed|failed)\b|\bno tests ran\b")


def _selection_facts(text):
    """(selected, deselected, looks_like_pytest) over one command's WHOLE output.

    Measured end to end against the live shape: an honest red baseline
    (`2 failed in 0.03s`), then a worker adds a marker filter to pytest.ini — a
    path nothing protects — and with src/ still EMPTY the same metric prints
    `2 deselected in 0.00s`. freeze said ok, verify said the harness was intact,
    measure recorded value=0 error=0, analyze printed ALL TARGETS MET. Every
    number in that chain is well-formed and every one of them is a lie. The
    whole scoring apparatus was driven to target by DESELECTION, and the single
    token that tells the two worlds apart was read by no code in this file.

    `selected` is None — never 0 — when the output carries no test count at all;
    the distinction is what keeps a compiler's error count from being read as a
    test count, and what keeps `looks_like_pytest` load-bearing rather than
    decorative.
    """
    de = DESELECTED_RE.search(text)
    return (_collected_count(text),
            int(de.group(1)) if de else None,
            bool(PYTEST_SHAPE_RE.search(text)))


def _selection_note(sel, desel, is_pytest):
    """The human rendering of _selection_facts. '' when there is nothing to say.

    The `N test(s) selected` wording is pinned by the P2 regression case and is
    not free to drift: it is the phrasing that replaced `N test(s) collected`
    when collected-vs-selected turned out to be the whole defect.
    """
    if desel:
        return "%s test(s) selected, %d DESELECTED" % ("?" if sel is None else sel, desel)
    if is_pytest and not sel:
        return "ZERO tests selected"
    if sel:
        return "%d test(s) selected" % sel
    return ""


def _forced_exit(text, returncode):
    """True when a non-zero exit cannot have come from the tests that ran.

    The FORCED shape: `pytest tests/test_scaffold.py; exit 1` — three passing
    scaffold tests and a red manufactured by the wrapper, scored `red (exit 1,
    3 test(s) selected) — a real gate`. What IS cheaply visible is the
    contradiction: pytest summarised only passes, so whatever produced the
    non-zero exit was not the test run.

    ADVISORY ONLY, and it must stay that way. A wrapper that runs a green pytest
    and then a linter that legitimately fails prints exactly this shape, and its
    red is real — it just comes from the linter. The residual limit is stated
    where the advisory is printed rather than guessed at here.
    """
    if not returncode:
        return False
    if not re.search(r"\b\d+ passed\b", text):
        return False
    if re.search(r"\b\d+ (?:failed|error|errors)\b", text) or "no tests ran" in text:
        return False
    return True


REDCHECK_FILE = "redcheck.jsonl"


def _ticket_test_nodes(root, state, tid):
    """(nodes, unparsed) — pytest node ids of the FROZEN tests carrying
    @pytest.mark.ticket("<tid>"), plus the frozen files that could not be PARSED.

    Same file set and the same marker rules the read-edge scanner uses — the
    protected paths, because the acceptance suite is protected by construction —
    reduced to the one thing red-check needs and that scanner does not keep:
    WHICH tests grade this ticket, not how many.

    `unparsed` is returned, not swallowed. It used to be a bare `continue`, and
    the caller then printed "this ticket carries no frozen test at all — freeze's
    acceptance-coverage gate owns that finding" — a claim the code could not
    support twice over: the ticket's ONLY grading test may live in exactly the
    file that failed to parse, and the coverage gate counts marks with THIS SAME
    scanner, so it will not own the finding either. An except that swallows a
    condition and a sentence that asserts its absence are the same defect this
    change set exists to remove; the parse still must not be fatal, so the
    condition is carried out instead of being either raised or hidden.
    """
    nodes, unparsed = [], []
    for path in _read_test_files(root, state, None):
        relf = os.path.relpath(path, root)
        try:
            with open(path) as f:
                tree = ast.parse(f.read(), filename=relf)
        except (OSError, SyntaxError, ValueError) as e:
            # Never fatal, for the same reason the read-edge scan is not: an
            # unparseable frozen suite is measure's finding to report, not a
            # reason a diagnostic takes red-check down with a traceback. But it
            # IS reported — see the docstring.
            unparsed.append("%s (%s)" % (relf, type(e).__name__))
            continue

        def visit(body, prefix, inherited):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    visit(n.body, prefix + [n.name], _ticket_marks(n) or inherited)
                    continue
                if isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                                  ast.For, ast.AsyncFor, ast.While)):
                    for field in ("body", "orelse", "finalbody"):
                        sub = getattr(n, field, None)
                        if isinstance(sub, list):
                            visit(sub, prefix, inherited)
                    for h in getattr(n, "handlers", []) or []:
                        visit(getattr(h, "body", []) or [], prefix, inherited)
                    continue
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not n.name.startswith("test"):
                    continue
                if tid in (_ticket_marks(n) or inherited):
                    nodes.append("::".join([relf] + prefix + [n.name]))

        visit(tree.body, [], _pytestmark_tickets(tree))
    return sorted(set(nodes)), sorted(set(unparsed))


# Lines `pytest --collect-only -q` emits as ERROR REPORTING, which QUOTE THE NODE
# ID BACK. This list is the fix for the defect that defeated the whole
# discriminator: `seen()` accepted any line ending in "/<file>::<test>", and
# every one of these shapes ends in exactly that. Measured on pytest 6.2.4,
# `--collect-only -q`, rc 4:
#   ERROR: not found: /abs/path/.swarm-loop/acceptance/test_x.py::test_one
#   (no name '/abs/path/.../test_x.py::test_one' in any of [<Module ...>])
#   ERROR: file or directory not found: .swarm-loop/acceptance/test_x.py::test_one
# so a node id that DOES NOT EXIST scored hits == len(nodes), desel None, and the
# probe answered ('ok', 'all N frozen test(s) survive collection here') — an
# affirmative sentence, printed by red-check as "a real gate", stamped `red`, and
# accepted by check-branch. The error message quoting the question back was being
# counted as the answer.
_PROBE_NOISE_RE = re.compile(
    r"^(?:ERROR\b|E\s|\(no name |!!!|=+$|_+$|-+$|In:|\s*Hint:)")


def _probe_noise(ln):
    """True for a collect-only line that is pytest talking ABOUT node ids rather
    than listing collected ones. See _PROBE_NOISE_RE for the measured shapes."""
    return bool(_PROBE_NOISE_RE.match(ln)) or " not found: " in ln


def _probe_id_match(ln, ident):
    """True when `ln` is the collected-item line for node id `ident`.

    Equality is not enough and never was: pytest EXPANDS a parametrized test's
    node id, so `file.py::test_one` is listed as `file.py::test_one[1]` and
    `file.py::test_one[2]`. The old matcher required equality or a "/"-suffix,
    so every parametrized grading test scored hits == 0 — the DESELECTED
    discriminator was silently inert on one of the commonest test shapes while
    reporting "a rootdir/path-shape difference, not a finding", which was wrong
    about both the fact and the cause. Measured: pytest 6.2.4 lists
    `.swarm-loop/acceptance/test_p.py::test_two[1]` for `::test_two`.
    """
    return ln == ident or ln.startswith(ident + "[")


def _selection_probe(wt, nodes, timeout, unparsed=()):
    """(verdict, detail) — did this project's OWN pytest config keep the ticket's
    frozen tests, or throw them away?

    THE DISCRIMINATOR FOR THE DESELECTION CLASS, and the only one that closes it.
    Measured: a ticket's `verify` was `./scripts/verify.sh check`, which is
    `pytest -q -m 'not slow' tests`; the ticket's own grading test was
    slow-marked, so it never ran once. red-check's `1 test(s) selected` came from
    an UNRELATED broken test, and when a later ticket fixed that test the gate
    went GREEN with the defect fully live. The ticket's own test sat in
    "2 deselected" on every single run. Counting what a gate selected cannot see
    this; only asking whether THESE tests survived can.

    Naming the node ids explicitly does not bypass a marker filter — `-m` is
    applied after collection however the items got there — so an `addopts` or a
    wrapper-inherited filter shows up here as a deselection of the ticket's own
    tests, which is a precise finding rather than a guess from a filename.

    THE RETURN CODE IS READ, and `ok` requires it to be 0. It was available and
    ignored, and that is the other half of the echo defect above: pytest exits 4
    when a named node id cannot be resolved, so "every node id appeared in the
    output AND pytest refused the run" was a contradiction the probe resolved in
    favour of the happy answer. Measured rc taxonomy, pytest 6.2.4,
    `--collect-only -q` on an explicit `<file>::<test>`:
        rc 0  collected — the item lines are real
        rc 4  not found / file or directory not found (INCLUDING the case where
              the module raises ImportError: naming an unresolvable node id is a
              usage error, not a collection interrupt)
        rc 5  pytest ran and collected nothing (a marker filter -> `N deselected`,
              or a conftest `pytest_collection_modifyitems` that empties items)

    Uncertainty is still never a finding — `unknown` covers no pytest on this
    interpreter, a usage error we cannot classify, a rootdir whose node-id shapes
    do not match — and `uncollectable` (rc 4 WITH a collection error) is
    advisory-only on purpose: the throwaway worktree has no venv, so a missing
    third-party import is the DEFAULT for any project with dependencies and a
    refusal there would refuse every ticket in such a project. The false-positive
    cost of the refusing verdicts is a refused merge, so the tie goes to the
    branch — but "I could not look" must never again print as "I looked and it
    was fine".
    """
    unparsed = list(unparsed or ())
    if not nodes:
        if unparsed:
            # NOT "no frozen test at all": the ticket's only grading test may be
            # in exactly the file that would not parse, and freeze's coverage
            # gate reads marks with this same scanner, so it cannot own the
            # finding either.
            return "unknown", ("%d frozen acceptance file(s) could not be PARSED, so "
                               "whether this ticket has a grading test is unknown, not "
                               "answered: %s" % (len(unparsed), ", ".join(unparsed[:3])))
        return "unknown", ("this ticket carries no frozen test at all — freeze's "
                           "acceptance-coverage gate owns that finding, not this one")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"] + nodes,
            capture_output=True, text=True, cwd=wt, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown", "a collect-only probe could not be run in this worktree"
    out = (proc.stdout or "") + (proc.stderr or "")
    rc = proc.returncode
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    item_lines = [ln for ln in lines if not _probe_noise(ln)]
    tail = ("; %d frozen acceptance file(s) also failed to PARSE (%s)"
            % (len(unparsed), ", ".join(unparsed[:3]))) if unparsed else ""

    def seen(node):
        # Suffix match, not equality: pytest prints node ids relative to ITS
        # rootdir, which is not always the repo root. Equality would report a
        # rootdir difference as a deselection — a false refusal on a branch that
        # did nothing wrong. The suffix is matched only against lines that are
        # not pytest's own error reporting (_probe_noise), and only through
        # _probe_id_match, which also accepts pytest's parametrized expansion.
        base = node.split("/")[-1]
        for ln in item_lines:
            if _probe_id_match(ln, node) or _probe_id_match(ln, base):
                return True
            i = ln.find("/" + base)
            if i >= 0 and _probe_id_match(ln[i + 1:], base):
                return True
        return False

    hits = sum(1 for n in nodes if seen(n))
    _sel, desel, _is_pytest = _selection_facts(out)
    # A collection ERROR (the module raised on import) is environment-shaped:
    # this throwaway worktree has no venv, so it is the default for a project
    # with third-party dependencies. Detected separately from "not found" so it
    # can be advisory while "not found" refuses.
    collect_error = ("ERROR collecting" in out or "Interrupted:" in out
                     or bool(re.search(r"\b\d+ errors? in \b", out)))
    not_found = any(" not found: " in ln for ln in lines)

    if rc == 0 and hits == len(nodes) and not desel:
        return "ok", ("all %d frozen test(s) survive collection here" % len(nodes)) + tail
    if hits == len(nodes) and not collect_error and not desel:
        # Every id listed AND a non-zero exit. Not `ok` (the runner refused the
        # run it was asked to do) and emphatically not `partial`, which used to
        # catch this and render it as the nonsense "only N of N frozen test(s)
        # survive collection here". This is also the case that keeps the two
        # echo guards independent: the line filter sees nothing wrong here, and
        # only the return code can.
        return "unconfirmed", (
            "pytest LISTED all %d of this ticket's frozen test(s) and then exited %d — the "
            "collect-only run it was asked to do did not complete cleanly, so this is not a "
            "positive observation. ADVISORY: read the collect output by hand%s"
            % (len(nodes), rc, tail))
    if hits and not collect_error:
        return "partial", ("only %d of %d frozen test(s) survive collection here%s"
                           % (hits, len(nodes),
                              " (%d deselected)" % desel if desel else "") + tail)
    if collect_error:
        return "uncollectable", (
            "the probe could NOT collect this ticket's %d frozen test(s) here — pytest "
            "reported a COLLECTION ERROR (exit %d), which in a throwaway worktree with no "
            "venv is usually a missing dependency rather than a finding about the gate. "
            "ADVISORY: this red is simply unverified, not disproved%s"
            % (len(nodes), rc, tail))
    if desel:
        return "deselected", ("all %d of this ticket's frozen test(s) were DESELECTED by "
                              "this project's own pytest configuration (%d deselected)"
                              % (len(nodes), desel) + tail)
    if not_found or rc == 4:
        return "missing", (
            "pytest could not resolve ANY of this ticket's %d frozen node id(s) at this "
            "base (exit %d, `not found`) — the grading test does not exist in the tree the "
            "gate was run against, so whatever went red, it was not this ticket's test%s"
            % (len(nodes), rc, tail))
    if rc == 5:
        return "empty", ("the probe collected NONE of this ticket's %d frozen test(s): "
                         "pytest ran and selected nothing (exit 5) with no deselection "
                         "count to explain it%s" % (len(nodes), tail))
    if any("::" in ln for ln in item_lines):
        return "unknown", ("the probe collected OTHER tests but none matching this "
                           "ticket's node ids, and pytest exited %d — read the ids by hand "
                           "before treating this as a finding%s" % (rc, tail))
    return "unknown", ("no pytest collect-only probe was possible: "
                       + (lines[-1][:120] if lines else "<no output>") + tail)


def _append_redcheck(d, recs):
    """Append-only ledger of red-check verdicts. Same shape as dispatch.jsonl,
    and harness-owned for the same reason.

    red-check is mandatory in three places in SKILL.md and was invoked by
    NOTHING — `grep -c cmd_red_check` returned 2, the def and the set_defaults.
    From the live run's own findings.jsonl:120, three tickets were reported
    CLOSED TWICE with their defects live; red-check would have caught all three
    (VACUOUS for two, WEAK for one) and nobody ran it until cycle 10. A remembered
    command is not a gate. A stamp is a thing check-branch can ask for.
    """
    if not recs:
        return
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, REDCHECK_FILE), "a") as f:
        for rec in recs:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def _redcheck_stamps(d):
    """({ticket: [record, ...]} in file order, ledger_state).

    ledger_state is one of "absent" | "ok" | "empty" | "unreadable", and it
    exists because the gate below was FAIL-OPEN on a distinction it could not
    make. `: > .swarm-loop/redcheck.jsonl` used to turn a live VETO into exit 0
    for an entire run: an empty file parsed to {}, the gate took its
    "no ledger yet" branch, and printed `no .swarm-loop/redcheck.jsonl exists` —
    a statement that was false about the file it had just read, over a file whose
    truncation is the single cheapest way to disarm the merge gate. This ledger
    is not in git-guard's LEDGER_NAMES either, so nothing blocks the redirect
    (see the NEEDS note in SKILL.md Phase 5) — which is exactly why it must not
    ALSO fail open here.

    Only _append_redcheck ever writes this file, and it returns before creating
    anything when it has no records, so "exists but yields zero usable records"
    cannot be produced by a normal run.
    """
    path = os.path.join(d, REDCHECK_FILE)
    out = {}
    if not os.path.exists(path):
        return out, "absent"
    try:
        size = os.path.getsize(path)
    except OSError:
        return out, "unreadable"
    if size == 0:
        return out, "empty"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("ticket"):
                    out.setdefault(str(rec["ticket"]), []).append(rec)
    except OSError:
        return {}, "unreadable"
    return out, ("ok" if out else "unreadable")


# Values a ticket graph may spell `bootstrap` with. Read STRICTLY, and never as
# plain truthiness: `"bootstrap": "false"` is a non-empty string, so `bool()`
# would read the graph's own word for "no" as yes and exempt the ticket from the
# must-go-red check. An unrecognised value is a malformed graph, not a default.
_BOOTSTRAP_TRUE = {"true", "yes", "1"}
_BOOTSTRAP_FALSE = {"false", "no", "0", ""}


def _ticket_bootstrap(t):
    """True when the ticket graph says this ticket is THE bootstrap ticket.

    DATA ONLY. There is deliberately no title heuristic here and there must never
    be one: the live run's root ticket happens to be titled "Monorepo skeleton
    with green empty verify pipeline", and keying an exemption on a title string
    is how a check ends up answering a question nobody asked — every later ticket
    whose title happens to say "skeleton" would inherit it. Absent field = not a
    bootstrap ticket, full stop.

    Raises ValueError for a value that is neither, so a typo cannot silently
    become an exemption.
    """
    if "bootstrap" not in (t or {}):
        return False
    v = t.get("bootstrap")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(int(v))
    s = str(v).strip().lower()
    if s in _BOOTSTRAP_TRUE:
        return True
    if s in _BOOTSTRAP_FALSE:
        return False
    raise ValueError(repr(t.get("bootstrap")))


def cmd_red_check(args):
    """Run each ticket's declared `verify` in a CLEAN worktree; fail any that
    already passes.

    H-05, and it was all five of wave 1, not one: T-010/T-013/T-014 exited 0 on
    nothing but T-000's scaffold smoke tests (`2 passed`), T-011/T-012 reported
    `no tests ran`. Not one of the five declared gates carried any signal, and a
    worker that runs its own verify and sees green has learned nothing. The
    orchestrator caught it by hand and wrote the warning into all five packets.

    H-7's correction is folded in: exit 4/5 is the CORRECT pre-build shape, not
    an environment failure — so the classification is by what was SELECTED, not
    by the exit code alone. A gate that selects zero tests is weak for the same
    reason a green one is: nothing about the ticket is being observed.

    THE EXIT CODE IS THE CONTRACT, and it covers every non-red bucket. It used
    to be `if vacuous: sys.exit(1)` — so WEAK, NO GATE and SKIPPED all exited 0
    and the caller read that as "these gates are fine". T-011/T-012 above are
    WEAK, not VACUOUS: two of the five tickets that motivated this command would
    have passed its own gate. Exit 0 now means every selected ticket has a gate
    that was CHECKED and found genuinely red."""
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    path, tickets = _load_graph(root, state, args.graph)
    idx, _dupes = _graph_index(tickets)
    sel = []
    for raw in (args.ticket or []):
        for tid in re.split(r"[,\s]+", raw):
            if tid.strip():
                sel.append(tid.strip())
    if not sel:
        sel = sorted(idx)
    unknown = [t for t in sel if t not in idx]
    if unknown:
        die("not in %s: %s" % (os.path.relpath(path, root), ", ".join(unknown)))

    # `bootstrap` is read over the WHOLE graph, not just the selection: it is a
    # statement about the graph's shape, and "true on T-0 only" is not checkable
    # from a subset. Read before anything runs so a malformed graph is refused
    # before it costs a worktree.
    bootstrap_ids, bad_bootstrap = [], []
    for tk in tickets:
        if not isinstance(tk, dict):
            continue
        try:
            if _ticket_bootstrap(tk):
                bootstrap_ids.append(str(tk.get("id")))
        except ValueError as e:
            bad_bootstrap.append("%s: bootstrap=%s" % (tk.get("id"), e))
    if bad_bootstrap:
        die("ticket graph has an unreadable `bootstrap` value — it must be a JSON boolean "
            "(true/false), and an unrecognised value is refused rather than defaulted, "
            "because defaulting it either way is a silent exemption or a silent refusal on "
            "the graph's root ticket: " + "; ".join(bad_bootstrap), 3)
    if len(bootstrap_ids) > 1:
        die("ticket graph declares `bootstrap: true` on %d tickets (%s). The contract is "
            "`true on T-0 only` — the graph has ONE root whose legitimate outcome is a green "
            "empty pipeline. A second one is either a mistake or someone widening the "
            "must-go-red exemption to get a wave moving, and widening it silently is exactly "
            "how a ticket ships on a gate that cannot fail. Fix the graph."
            % (len(bootstrap_ids), ", ".join(sorted(bootstrap_ids))), 3)

    base = args.base
    rev = subprocess.run(["git", "rev-parse", "--verify", base],
                         capture_output=True, text=True, cwd=root)
    if rev.returncode != 0:
        die(f"base ref '{base}' does not resolve — pass --base <ref>")
    # The SHA, not the ref name: a stamp check-branch can test for ancestry
    # against must survive `main` moving on after the check was run.
    base_sha = (rev.stdout or "").strip() or base
    vacuous, weak, red, nogate, skipped, bootstrap = [], [], [], [], [], []
    # HUNG is its own bucket, never a shade of WEAK. A gate that DEADLOCKS on
    # broken code is worse than one that passes on it: in CI it burns to the job
    # timeout and prints no red at all, so the defect reads as ABSENT. Filed
    # under `weak` it was byte-identical in the ledger to "exited non-zero,
    # selected nothing" — and the ledger, not this terminal, is what
    # check-branch reads after the fact.
    hung = []
    # The two close-time buckets. They are REFUSALS only under --close and
    # advisory otherwise, because a false positive on the pre-dispatch path stops
    # a ticket that has nothing wrong with it while a false positive at merge
    # costs one re-run. The stamp records the honest classification either way.
    swallowed, deselected_t = [], []
    # Tickets the PRE-DISPATCH path scored `red` but STAMPED non-red. Empty
    # under --close by construction. It exists because the two disagreed
    # silently: the summary said "N real" while the ledger check-branch reads
    # said `swallowed`, and the operator learned the difference at the merge
    # gate, after the work was built.
    pre_dispatch_stamped = []
    stamps = []
    tmpbase = tempfile.mkdtemp(prefix="swarmloop-redcheck-")
    print(f"red-check: {len(sel)} ticket(s) against a clean worktree of "
          f"{base} ({base_sha[:12]})"
          + ("   [--close: retro-check, the selection findings REFUSE]" if args.close else ""))
    try:
        for tid in sel:
            cmd = (idx[tid].get("verify") or "").strip()
            if not cmd:
                nogate.append(tid)
                stamps.append((tid, "no-gate", cmd, None))
                print(f"  {tid:<9} NO GATE — the ticket declares no 'verify' command")
                continue
            wt = os.path.join(tmpbase, tid)
            # subprocess git, not a Bash tool call, so the skill's own PreToolUse
            # git-guard is not in the path here — this is the harness itself, and
            # it only ever creates and removes its OWN throwaway worktree.
            add = _git(root, "worktree", "add", "--detach", "-q", wt, base)
            if add.returncode != 0:
                # SKIPPED used to land in NO bucket at all: it was not counted
                # in the summary and it could not affect the exit code, so a
                # provisioning failure read exactly like a ticket that had been
                # checked and passed. An unchecked gate is not a checked one.
                skipped.append(tid)
                stamps.append((tid, "skipped", cmd, None))
                print(f"  {tid:<9} SKIPPED — could not create a worktree, so this "
                      "ticket's gate was NEVER CHECKED: "
                      + (add.stderr or "").strip()[:160])
                continue
            try:
                proc = _run_metric_command(cmd, wt, args.timeout)
                out = (proc.stdout or "") + (proc.stderr or "")
                n, n_desel, n_pytest = _selection_facts(out)
                note = _selection_note(n, n_desel, n_pytest)
                # THE BOOTSTRAP EXEMPTION, and it is exactly two shapes wide.
                # T-0 is the graph's sole root — repo skeleton, harness, lint/CI
                # config, marker registration — and every other ticket depends on
                # it. Its declared outcome IS "pipeline green on an empty suite",
                # so in a clean worktree with nothing implemented its gate exits 0
                # (VACUOUS) or non-zero with nothing selected (WEAK). Both put
                # red-check at exit 1 on THE ONE TICKET THAT MUST DISPATCH FIRST,
                # which is a false refusal on the loop's own dispatch step.
                #
                # It exempts a ticket from MUST-GO-RED and from nothing else: the
                # NO GATE check above still fires (a bootstrap ticket with no
                # `verify` is still refused), SKIPPED still fires, and a timeout
                # is still HUNG — none of those is "a green empty pipeline". The
                # producer's other half is what keeps this from being a hole:
                # build-docs still requires T-0 to ship one attributed smoke test
                # (`test_harness_runs`, marked ticket("T-0")), so freeze's
                # acceptance-coverage gate still grades it.
                is_bootstrap = tid in bootstrap_ids
                if proc.returncode == 0:
                    if is_bootstrap:
                        bootstrap.append(tid)
                        stamps.append((tid, "bootstrap", cmd, None))
                        print(f"  {tid:<9} BOOTSTRAP — `{cmd}` exits 0 on an empty tree, "
                              "and for this ticket that IS the outcome: a green pipeline "
                              "over an empty suite" + (f" ({note})" if note else ""))
                        print("            Exempt from must-go-red because the graph says "
                              "`bootstrap: true`, and from NOTHING else. Its one attributed "
                              "smoke test is still graded by freeze's coverage gate.")
                        continue
                    vacuous.append(tid)
                    stamps.append((tid, "vacuous", cmd, None))
                    print(f"  {tid:<9} VACUOUS — `{cmd}` EXITS 0 with nothing implemented"
                          + (f" ({note})" if note else ""))
                    continue
                if not n:
                    if is_bootstrap:
                        bootstrap.append(tid)
                        stamps.append((tid, "bootstrap", cmd, None))
                        print(f"  {tid:<9} BOOTSTRAP — exit {proc.returncode} with no tests "
                              "SELECTED, which is the pre-build shape of a pipeline that has "
                              "nothing to run yet; the graph says `bootstrap: true`")
                        continue
                    weak.append(tid)
                    stamps.append((tid, "weak", cmd, None))
                    print(f"  {tid:<9} WEAK — exit {proc.returncode}, but NO tests were "
                          f"SELECTED: `{cmd}` proves nothing about this ticket either way")
                    continue
                # From here the gate is red on the exit code and selected
                # something. That was the whole test until three shapes were
                # measured slipping through it and being scored "a real gate".
                verdict, extra = "red", []
                # SWALLOWED. `pytest -q tests > /dev/null 2>&1 || echo '1 failed'; exit 1`
                # — _collected_count reads the WRAPPER'S OWN echo, not any test
                # run, and scored it red. DEVNULL_RE has guarded metric commands
                # since the four-false-zeros incident; ticket verify strings are
                # the same string in the same danger and were never checked.
                #
                # ANCHORED, not searched over the whole string: see
                # _swallowing_redirect. Matching DEVNULL_RE against the whole
                # verify command scored `command -v jq >/dev/null 2>&1 || exit 3;
                # pytest -q tests/test_t1.py` as SWALLOWED — a false refusal at
                # merge whose printed remedy re-runs the same string and stamps
                # `swallowed` again.
                dn = _swallowing_redirect(cmd)
                if dn:
                    verdict = "swallowed"
                    extra.append("SWALLOWED — `%s` discards its own output (%r), so the count "
                                 "above may come from the wrapper's echo rather than from any "
                                 "test run. Redirect to a file instead: "
                                 "> %s/reports/%s-verify.log 2>&1"
                                 % (dn[1], dn[0], STATE_DIR, tid))
                # DESELECTION — the whole ballgame. Ask whether THIS ticket's own
                # frozen tests survive the project's own pytest config, rather
                # than whether something somewhere went red.
                _nodes, _unparsed = _ticket_test_nodes(root, state, tid)
                pv, pdetail = _selection_probe(wt, _nodes, min(args.timeout, 300), _unparsed)
                if pv in ("deselected", "empty", "missing"):
                    if verdict == "red":
                        verdict = "deselected"
                    extra.append("%s — %s. Whatever went red above, it was not this "
                                 "ticket's grading tests."
                                 % ("DESELECTED" if pv == "deselected" else pv.upper(),
                                    pdetail))
                elif pv == "partial":
                    extra.append("partial selection — %s" % pdetail)
                elif pv in ("uncollectable", "unconfirmed"):
                    # NEVER a refusal, and the reason is measured: the throwaway
                    # worktree has no venv, so an ImportError on a third-party
                    # dependency is the DEFAULT for any project that has any.
                    # Refusing here would refuse every ticket in such a project.
                    # `unconfirmed` rides along for the same reason — an exit
                    # code nobody can explain is not evidence against a branch.
                    extra.append("UNVERIFIED — %s" % pdetail)
                elif pv == "unknown" and _unparsed:
                    extra.append("UNVERIFIED — %s" % pdetail)
                if _forced_exit(out, proc.returncode):
                    extra.append("FORCED? — the pytest summary reports only passes, so this "
                                 "non-zero exit did not come from the test run "
                                 "(`... ; exit 1`). ADVISORY ONLY and it stays that way: a "
                                 "wrapper whose red legitimately comes from a linter after a "
                                 "green pytest prints exactly this. Read the command.")
                if verdict == "red":
                    red.append(tid)
                    print(f"  {tid:<9} red (exit {proc.returncode}, {note}) — a real gate"
                          + (f", {pdetail}" if pv == "ok" else ""))
                else:
                    # THE PRE-DISPATCH CONTRADICTION, closed. This used to print
                    # `red`, count the ticket in the `red` bucket and exit 0
                    # while STAMPING `swallowed`/`deselected` — and check-branch
                    # --ticket reads the stamp, so the operator's next signal was
                    # a VETO at merge, after the work was built, from a run that
                    # had told them the gate was fine. The stamp stays honest
                    # (that is deliberate); what changes is that the pre-dispatch
                    # path now says out loud what it recorded and what it will do.
                    label = "SWALLOWED" if verdict == "swallowed" else "DESELECTED"
                    if args.close:
                        (swallowed if verdict == "swallowed" else deselected_t).append(tid)
                    else:
                        red.append(tid)
                    print(f"  {tid:<9} {label if args.close else 'red'} "
                          f"(exit {proc.returncode}, {note})"
                          + ("" if args.close else
                             f" — but STAMPED `{verdict}`, which "
                             f"`check-branch --ticket {tid}` will REFUSE at merge"))
                    if not args.close:
                        pre_dispatch_stamped.append((tid, verdict))
                # The PROBE'S OWN ANSWER travels with the verdict. Without it a
                # `red` stamped over a gate whose grading tests could not be
                # collected at all is byte-identical, in the ledger, to a `red`
                # whose grading tests were observed running — and the merge gate
                # reads the ledger, not this terminal.
                stamps.append((tid, verdict, cmd, pv))
                for line in extra:
                    print("            " + line)
            except subprocess.TimeoutExpired:
                hung.append(tid)
                stamps.append((tid, "hung", cmd, None))
                print(f"  {tid:<9} HUNG — `{cmd}` produced NO VERDICT in "
                      f"{args.timeout}s and was killed by process group")
            finally:
                _git(root, "worktree", "remove", "--force", wt)
    finally:
        _git(root, "worktree", "prune")
        try:
            os.rmdir(tmpbase)
        except OSError:
            pass

    # Written whatever the verdicts were and BEFORE the exit below: a red-check
    # that refused is exactly the run whose finding must outlive the terminal it
    # was printed in.
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    _append_redcheck(d, [{"ticket": t, "verdict": v, "base": base, "base_sha": base_sha,
                          "cmd": c, "mode": "close" if args.close else "pre-dispatch",
                          "selection": sv, "timestamp": now} for t, v, c, sv in stamps]) 

    print("")
    print(f"red-check: {len(red)} real · {len(vacuous)} VACUOUS · {len(weak)} weak · "
          f"{len(nogate)} with no gate · {len(skipped)} SKIPPED"
          + (f" · {len(hung)} HUNG" if hung else "")
          + (f" · {len(bootstrap)} BOOTSTRAP" if bootstrap else "")
          + (f" · {len(swallowed)} SWALLOWED · {len(deselected_t)} DESELECTED"
             if args.close else ""))
    if bootstrap:
        print("  %s carries `bootstrap: true` in the graph, so a green empty pipeline is its "
              "declared outcome and does NOT refuse here. It is exempt from must-go-red and "
              "from nothing else — no gate at all is still NO GATE." % ", ".join(bootstrap))
    if stamps:
        print(f"  {len(stamps)} verdict(s) stamped into {STATE_DIR}/{REDCHECK_FILE} "
              f"against {base_sha[:12]} — `check-branch --ticket <id>` reads them")
    if pre_dispatch_stamped:
        # The exit code stays 0 here on purpose — a selection finding must never
        # stop a ticket that has not been built yet — but silence was the defect:
        # the run reported "N real" and the ledger it wrote disagreed.
        print(f"  !! {len(pre_dispatch_stamped)} of those verdict(s) are NOT `red`, and this "
              "run counted them as red anyway (the pre-dispatch path never refuses):")
        for t, v in pre_dispatch_stamped:
            print(f"       {t:<9} stamped `{v}` — `check-branch --ticket {t}` will VETO this "
                  "branch at merge unless the gate is fixed and re-checked first")
        print("     Fix the verify command NOW, while nothing has been built against it. "
              "Re-running `red-check --close` over an unchanged command re-stamps the same "
              "verdict, so the veto clears only when the CAUSE named above is gone.")
    if vacuous or weak or nogate or skipped:
        print("A ticket's verify command must FAIL before the ticket is built. A gate that")
        print("passes on an empty tree measures the scaffold, not the work; a gate that")
        print("collects nothing measures nothing at all. Rewrite these before dispatching")
        print("them, and say in each task packet which grader is the real one.")
    if hung:
        print("A gate that HANGS is not a gate that failed — it is the one outcome that")
        print("looks like ABSENCE: in CI it runs to the job timeout and prints no red at")
        print("all, so the defect it should have caught reads as not there. Fix the hang as")
        print("its own defect before judging the gate, then audit what the kill left behind:")
        print("a backend does not notice its client is gone, and a queued destructive")
        print("statement can commit after it, wrecking shared state for every later run.")
    if swallowed or deselected_t:
        print("A gate that went red for a reason unrelated to this ticket is not this")
        print("ticket's gate. Measured: a verify of `pytest -q -m 'not slow' tests` scored a")
        print("red that came from an UNRELATED broken test while the ticket's own grading")
        print("test sat in `2 deselected` on every run — and when a later ticket fixed that")
        print("unrelated test the gate went GREEN with the defect fully live.")
    # The exit code IS the contract, and it used to be `if vacuous` alone —
    # WEAK, NO GATE and SKIPPED all exited 0. The incident this command was
    # built for is in its own docstring: T-011/T-012 "reported `no tests ran`".
    # That is pytest exit 5 with `collected 0 items`, which classifies WEAK —
    # so two of the five tickets that motivated red-check would have walked
    # straight through its gate. A ticket whose gate proves nothing is not
    # dispatchable, and a ticket that was never checked is not either.
    #
    # swallowed/deselected are appended to this list only under --close, and are
    # empty otherwise BY CONSTRUCTION (the loop files those tickets under `red`
    # on the pre-dispatch path). That is the whole false-positive story: the
    # selection probe's answer can be wrong on a project with a custom runner or
    # a non-root pytest rootdir, and a wrong answer must never be able to refuse
    # a ticket that has not been built yet.
    #
    # `bootstrap` is deliberately absent from this list and `nogate`/`skipped`
    # are deliberately still in it. The exemption removes a ticket from
    # vacuous/weak at the point of classification, so it cannot silently widen:
    # a bootstrap ticket with no `verify` never reaches the exemption at all.
    # `hung` is in the list for the same reason as `skipped`: a gate that produced
    # no verdict was not checked, and an unchecked gate is not a red one.
    if vacuous or weak or nogate or skipped or hung or swallowed or deselected_t:
        sys.exit(1)


# The skill's cross-run memory lives NEXT TO THIS FILE, never in the run's repo:
# one LEARNED.md is shared by every run on this machine. The reporter below and
# the writer in `distil --to-skill` both resolve it here, so the count an
# operator reads and the file an append lands in can never be two files.
LEARNED_ENTRIES_MARKER = "## Entries"
LEARNED_CAP = 40          # the ceiling LEARNED.md states for itself
LEARNINGS_SOFT_CAP = 30   # the ceiling SKILL.md Phase 8 states for the run file


def _learned_path():
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         os.pardir, LEARNED_MD))


def _learned_entries(text):
    """-> (entry_blocks, marked). One block per entry, continuations folded in.

    An entry OPENS with a bullet or a heading and runs to the next blank line;
    entries in this file wrap at ~94 columns with two-space continuations, so a
    reader that kept only opening lines would see a third of each entry — which
    is how a duplicate check over the first line alone passes a lesson whose text
    simply wrapped differently. Counting is unaffected: one block per entry.

    `marked` is False when the '## Entries' header is gone, in which case the
    WHOLE file is read — preamble rule bullets included, which is why the count
    is only ever advisory in that state and why the writer refuses outright
    rather than appending into a document whose shape it cannot find.

    '## ' is an opener as well as '- ' and '### ', and that is not cosmetic: the
    first entries this file ever held were written by hand as `## Heading` blocks
    with prose bodies, and a reader that only knew bullets counted EIGHT real
    entries as zero — reporting "EMPTY (0 entries) — Termination is supposed to
    distil" over a file that had just been filled. A count that is wrong about
    the file it exists to measure is the defect this whole command is about, so
    every entry shape the file has actually carried is counted.
    """
    marked = LEARNED_ENTRIES_MARKER in text
    body = text.split(LEARNED_ENTRIES_MARKER, 1)[-1] if marked else text
    blocks, cur = [], None
    for ln in body.splitlines():
        if ln.startswith("- ") or ln.startswith("### ") or ln.startswith("## "):
            if cur is not None:
                blocks.append(cur)
            cur = ln
        elif cur is not None:
            if not ln.strip():
                blocks.append(cur)
                cur = None
            else:
                cur += "\n" + ln
    if cur is not None:
        blocks.append(cur)
    return blocks, marked


def _learning_lines(d):
    """Two lines on the self-improvement loop: the per-run file, and the skill's.

    P1 / OPEN §3: SKILL.md tells the loop to append to `.swarm-loop/learnings.md`
    at every epoch boundary and to distil that into the skill's `LEARNED.md` at
    Termination — and NOTHING has ever checked. `grep -rn LEARNED` over the whole
    harness returned no hits, the capacity gate was prose, and the result is a
    `LEARNED.md` with zero entries across every run to date. This does not
    enforce the append (see below); it makes the omission visible in the status
    block the orchestrator already reads every cycle, which is the difference
    between a rule nobody kept and a rule nobody kept IN FRONT OF THEM.

    This function stays read-only. The append is `distil`, a separate command an
    operator or Termination invokes deliberately, because LEARNED.md is shared by
    every run on the machine and by any concurrent run — a status line that
    silently edited it would put a machine-wide write inside `resume`.

    Never raises: a missing skill directory (the script invoked from a copy, a
    relocated skill) degrades to 'cannot see it', never to a crash in `resume`,
    which is the command for when everything else is already broken.
    """
    out = []
    lpath = os.path.join(d, LEARNINGS_MD)
    try:
        n = sum(1 for ln in open(lpath) if ln.strip()) if os.path.exists(lpath) else None
    except OSError:
        n = None
    if n is None:
        out.append(f"{STATE_DIR}/{LEARNINGS_MD}: ABSENT — Phase 8 appends the process-loop lesson "
                   "for every epoch to it; a run with none has recorded nothing about its own "
                   "orchestration (`distil --lesson ... --because ...` writes one)")
    else:
        out.append(f"{STATE_DIR}/{LEARNINGS_MD}: {n} non-empty line(s)")

    # The skill's own memory, located from THIS file rather than from the repo.
    learned = _learned_path()
    try:
        if not os.path.exists(learned):
            out.append(f"{LEARNED_MD}: not found at {learned} — cannot report the skill's memory")
            return out
        with open(learned) as f:
            text = f.read()
    except OSError as e:
        out.append(f"{LEARNED_MD}: unreadable ({e})")
        return out
    ents, marked = _learned_entries(text)
    entries = len(ents)
    if not marked:
        # The writer refuses in this state; the reporter says why the number
        # below is not to be trusted rather than quietly counting rule bullets.
        out.append(f"{LEARNED_MD}: no '{LEARNED_ENTRIES_MARKER}' marker at {learned} — the whole "
                   f"file is being counted ({entries} bullet(s)) and `distil --to-skill` REFUSES "
                   "until the marker is restored")
    elif entries == 0:
        out.append(f"{LEARNED_MD}: EMPTY (0 entries) — Termination is supposed to distil "
                   f"{STATE_DIR}/{LEARNINGS_MD} into it with `distil --to-skill`. Every run so "
                   "far has ended without doing so, which is why it is empty; do not let this "
                   "one be another")
    else:
        out.append(f"{LEARNED_MD}: {entries} entr(ies)"
                   + (f"  ·  AT CAPACITY (>={LEARNED_CAP}) — SKILL.md's capacity gate applies: "
                      "stop and tell the user before further runs" if entries >= LEARNED_CAP
                      else ""))
    return out


def _graph_reconcile_lines(root, d, state):
    """[lines] — where the ticket graph and backlog.md disagree. [] when they
    agree, or when the project has neither.

    H-01: tickets.json (47) and backlog.md (45) had drifted — amendment 1 added
    T-036 and T-087 to the graph and the ledger never followed, while backlog.md
    still stated "45 tickets, 87 edges" in three places. The harness could not
    catch it by construction: it never read the graph at all, so `resume` printed
    `harness intact` over a ledger two tickets and three frontier counts stale.
    The graph is ground truth; backlog.md is the human view of it."""
    path, tickets = _load_graph(root, state, None, required=False)
    if path is None:
        return []
    bpath = os.path.join(d, BACKLOG_MD)
    idx, dupes = _graph_index(tickets)
    lines = []
    for e in _graph_unusable(tickets) + _graph_validate(idx, dupes):
        lines.append("GRAPH ERROR: " + e)
    if not os.path.exists(bpath):
        return lines
    try:
        with open(bpath) as f:
            text = f.read()
    except OSError:
        return lines
    in_backlog = set(re.findall(r"\bT-\d+\b", text))
    json_only = sorted(set(idx) - in_backlog)
    backlog_only = sorted(in_backlog - set(idx))
    edges = sum(len(_graph_strs(t, "depends_on")) for t in idx.values())
    # The rest of the structure the brief asks resume/freeze/amend to re-derive:
    # roots, depth and the initial frontier. Derived HERE rather than trusted from
    # the ledger, because the ledger is the thing being checked.
    roots = sorted(t for t in idx if not _graph_strs(idx[t], "depends_on"))
    depth = _graph_depth(idx)
    maxdepth = max(depth.values()) if depth else 0
    deepest = sorted(t for t in idx if depth.get(t) == maxdepth)
    frontier0 = sorted(t for t in idx if t not in roots
                       and all(x in roots for x in _graph_strs(idx[t], "depends_on")))
    if json_only or backlog_only:
        lines.append("DRIFT between %s (%d tickets, %d edges) and %s/%s"
                     % (os.path.relpath(path, root), len(idx), edges, STATE_DIR, BACKLOG_MD))
        if json_only:
            lines.append("  in the graph, missing from the ledger: " + ", ".join(json_only))
        if backlog_only:
            lines.append("  in the ledger, missing from the graph: " + ", ".join(backlog_only))
        # Printed only inside an existing drift block, never on its own: these are
        # the numbers needed to REGENERATE backlog.md, and on a graph that agrees
        # with its ledger they would just be noise on every resume.
        lines.append("  the graph as it actually stands: root(s) %s · max depth %d "
                     "(deepest: %s) · initial frontier %s"
                     % (", ".join(roots) or "NONE", maxdepth, ", ".join(deepest) or "-",
                        ", ".join(frontier0) or "NONE"))
    # A graph the whole design assumes is a single tree from one scaffold ticket.
    # Zero roots means a cycle swallowed the entry point; several means several
    # independent trees, and the "wave 1" every document talks about is then not
    # one thing. Both are structural, so they are reported whatever the ledger says.
    if len(roots) != 1:
        lines.append("%s has %d root ticket(s) (no depends_on): %s — the scheduler, the "
                     "ledger and every 'wave 1' in the docs assume exactly one scaffold "
                     "ticket that everything descends from"
                     % (os.path.relpath(path, root), len(roots), ", ".join(roots) or "NONE"))
    # Stated counts: reported only when NOT ONE statement in the file matches the
    # graph. backlog.md is deliberately a NARRATIVE — the live ledger records
    # "44 tickets, 80 edges", then "45, 87", then "47, 94" as the graph grew — so
    # flagging every stale number fires on the file's own history, and "four
    # depth-3 tickets" matches a naive `(\d+) tickets` outright (it did, on the
    # first run of this check). What H-01 actually caught is a ledger in which
    # the CURRENT count appears nowhere at all.
    # 'max depth' is spelled out rather than a bare `depth (\d+)`: the live ledger
    # says "depth 3 behind T-050" and "T-036 sits at depth 2" in prose, and a naive
    # pattern matches both and reports drift on a ledger that is correct.
    for label, want, pat in (("ticket", len(idx), r"\b(\d+)\s+tickets?\b"),
                             ("edge", edges, r"\b(\d+)\s+edges\b"),
                             ("max-depth", maxdepth, r"\bmax(?:imum)?\s+depth\s+(\d+)\b")):
        stated = [int(x) for x in re.findall(pat, text)]
        if stated and want not in stated:
            lines.append("  %s/%s never states the graph's actual %s count (%d) anywhere; "
                         "it says %s"
                         % (STATE_DIR, BACKLOG_MD, label, want,
                            ", ".join(str(x) for x in sorted(set(stated)))))
    if lines:
        lines.append("  the GRAPH is ground truth and the ledger is derived — regenerate "
                     "backlog.md from it (H-25: an amendment must regenerate every artifact "
                     "it invalidates) before dispatching off either")
    return lines


# --------------------------------------------------- read edges (B5 / H-10)
#
# `scope` is a set of write-ownership globs, and the harness — and every human
# reading it — used that ONE field to answer TWO different questions:
#
#   1. what may this worker WRITE?  (merge conflict-avoidance, check-branch)
#   2. what does this ticket's acceptance need to EXIST?
#
# Only (1) was encoded anywhere. So a ticket's obligation to ship a port that
# satisfies OTHER tickets' frozen tests was invisible in the data and survived
# only as hand-written prose in public-surface.md. The live orchestrator named
# the failure mode exactly: "an unstated cross-ticket obligation is exactly how
# T-030 ships a port that satisfies only itself" — T-030 builds to pass its own
# two tests, T-032 and T-033 then fail on a surface that exists but is shaped
# wrong, and the diagnosis lands two waves later.
#
# The naive check — "a test's imports must be inside the marked ticket's scope" —
# is WRONG and was withdrawn during the live run: it fires on every legitimate
# cross-ticket read (T-032 and T-033 both import T-030's eligibility module, and
# the chain T-032 -> T-033 -> T-030 already guarantees it exists). The rule that
# actually holds is the closure rule below.

# conftest.py is IN: its imports break collection for every test in the
# directory, which is the widest read edge a file can carry, and it was the one
# test-adjacent filename this pattern excluded.
READ_TEST_RE = re.compile(r"^(test_.+|.+_test|conftest)\.py$")


def _is_catchall(pat):
    """A scope that owns the repo. T-000's scope is literally ["**"], and if a
    catch-all counted as ownership then EVERY module would map to the scaffold
    ticket, every import would resolve to a closed ticket, and the check would
    report a clean graph forever. Excluded from module attribution only — it is
    still real ownership for check-wave, which is why _patterns_overlap treats
    "**" as intersecting everything."""
    p = _norm_pat(pat)
    return p in ("", "**", "*", ".", "./**", "**/*", "/**")


def _scope_match(pat, path):
    """Does an ownership glob cover this repo-relative path?

    fnmatch alone is not enough: `packages/llm/**` must also match the package
    module itself (`packages/llm.py`, `packages/llm/__init__.py`) and the
    directory `packages/llm`, none of which carry the trailing separator the
    pattern demands."""
    pat, path = _norm_pat(pat).lower(), _norm_pat(path).lower()
    if not pat or not path:
        return False
    for one in _expand_braces(pat):     # same brace rule the overlap check uses
        if one.endswith("/**"):
            pre = one[:-3]
            if path == pre or path.startswith(pre + "/"):
                return True
        if fnmatch.fnmatch(path, one):
            return True
    return False


def _module_candidates(mod):
    """Repo paths a dotted import could name, most literal first.

    The dash alias is not cosmetic. Measured on the live graph: SIX read edges
    were invisible because the ticket scopes say `packages/store-agent/src/...`
    while the import says `packages.store_agent.src...` — a Python package
    directory cannot contain a hyphen, so one of the two is simply wrong. The
    alias is applied so the edge is seen, and the mismatch is reported so
    somebody fixes the scope map rather than living on the alias."""
    p = _norm_pat(mod).replace(".", "/")
    if not p:
        return [], []
    literal = [p, p + ".py", p + "/__init__.py"]
    q = p.replace("_", "-")
    alias = [q, q + ".py", q + "/__init__.py"] if q != p else []
    return literal, alias


def _scope_owner_index(idx):
    """({tid: [attributable patterns]}, {top-level repo dirs any scope names}).

    The root set is how an unowned import is told from a stdlib one without
    hard-coding a stdlib list: `apps.trust.src.scoring` shares a first segment
    with a declared scope, `inspect` does not."""
    scopes, roots = {}, set()
    for tid, t in idx.items():
        pats = []
        for raw in _graph_strs(t, "scope"):
            p = _norm_pat(raw)
            if not p:
                continue
            roots.add(p.split("/")[0])
            if not _is_catchall(p):
                pats.append(p)
        scopes[tid] = pats
    roots.discard("**")
    roots.discard("*")
    return scopes, roots


def _module_owners(mod, scopes):
    """(owning ticket ids, True when only the dash alias matched)."""
    literal, alias = _module_candidates(mod)
    owners = {tid for tid, pats in scopes.items()
              if any(_scope_match(p, c) for p in pats for c in literal)}
    if owners:
        return owners, False
    owners = {tid for tid, pats in scopes.items()
              if any(_scope_match(p, c) for p in pats for c in alias)}
    return owners, bool(owners)


def _dep_closure(idx):
    """{id: every ticket transitively below it}. Cycle-safe; _graph_validate
    reports the cycle separately, this must not hang on one."""
    # Iterative, same rationale as _graph_depth and _graph_validate.
    out = {}
    for tid in idx:
        seen, stack = set(), [tid]
        while stack:
            cur = stack.pop()
            for dep in _graph_strs(idx.get(cur, {}), "depends_on"):
                if dep in idx and dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        out[tid] = seen
    return out


def _ticket_marks(node):
    """Ticket ids named by @pytest.mark.ticket("T-0xx") on a def/class."""
    out = []
    for dec in getattr(node, "decorator_list", []) or []:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Attribute) and func.attr == "ticket":
            for a in getattr(dec, "args", []) or []:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.append(a.value.strip())
    return out


def _dotted_imports(node):
    """Absolute dotted module names imported anywhere inside a node.

    Relative imports (level > 0) are skipped: they resolve inside the acceptance
    package itself, never to a product module, so they can carry no read edge."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for al in n.names:
                out.add(al.name)
        elif isinstance(n, ast.ImportFrom):
            if not n.level and n.module:
                out.add(n.module)
    return out


def _module_level_imports(tree):
    """Imports reachable at MODULE scope, including guarded ones.

    Read off bare `tree.body`, the two commonest shapes for an import that
    breaks collection were both invisible: `try: import x / except ImportError`
    and `if TYPE_CHECKING: import x`. Descends control flow; never descends into
    a def or class, because a function body's imports are attributed per-test."""
    out, stack = set(), list(tree.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= _dotted_imports(n)
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            v = getattr(n, field, None)
            if isinstance(v, list):
                stack.extend(v)
    return out


def _pytestmark_tickets(tree):
    """Ticket ids from a module-level `pytestmark = pytest.mark.ticket("T-1")`.

    pytestmark marks EVERY test in the file and was handled nowhere, so a file
    that used it instead of per-function decorators read as entirely unmarked —
    and an unmarked test is invisible to the read-edge gate that `freeze`
    depends on. Accepts the bare form and the list/tuple form."""
    out = []
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets):
            continue
        vals = (n.value.elts if isinstance(n.value, (ast.List, ast.Tuple)) else [n.value])
        for v in vals:
            func = v.func if isinstance(v, ast.Call) else v
            if isinstance(func, ast.Attribute) and func.attr == "ticket":
                for a in getattr(v, "args", []) or []:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        out.append(a.value.strip())
    return out


def _read_test_files(root, state, explicit):
    """The frozen test files to AST-parse.

    Default is the PROTECTED PATHS — the acceptance suite is protected by
    construction, so "the frozen tests" needs no second configuration key and
    cannot drift away from what freeze actually locked."""
    bases = list(explicit or state.get("protected_paths") or [])
    files = []
    for rel in bases:
        base = rel if os.path.isabs(rel) else os.path.join(root, rel)
        if os.path.isfile(base):
            if base.endswith(".py"):
                files.append(base)
            continue
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".git")]
            for fn in sorted(filenames):
                if READ_TEST_RE.match(fn):
                    files.append(os.path.join(dirpath, fn))
    return sorted(set(files))


def _scan_read_edges(root, state, idx, explicit_tests=None, closed=()):
    """Derive the read edges the graph does not declare, and check the closure rule.

    THE RULE, and it is not the obvious one: a cross-ticket import is legal iff
    the module's OWNING ticket lies in the importing ticket's transitive
    dependency closure. Self-ownership counts, and so does anything the caller
    declares closed.

    Measured against the live run: AST-parsing all 120 frozen tests produced
    EXACTLY ONE violation and zero false positives against the cross-ticket
    reads public-surface.md documents by hand — so it is precise enough to gate
    a freeze on. The one hit is real and unfixable by adding an edge:
    test_e6_trust.py:1463 is marked T-065 and imports apps.trust.src.scoring,
    which T-062 owns, while T-062.depends_on already contains T-065 — so T-065
    is graded before the module exists, 1 of its 6 frozen tests can never pass,
    and the "missing" edge would close a cycle. That is precisely why this runs
    at intake instead of at dispatch."""
    scopes, roots = _scope_owner_index(idx)
    closure = _dep_closure(idx)
    closed = set(closed)
    files = _read_test_files(root, state, explicit_tests)
    res = {
        "test_files": len(files), "tests_scanned": 0, "unparsed": [],
        "reads": {}, "violations": [], "unmapped": [], "dash_mismatch": [],
        "unmarked_tests": 0, "shared_scope": [],
        # {ticket_id: how many frozen tests carry its marker}. goal-setting.md
        # has always said "Zero frozen tests is not a row in a table; it stops
        # the freeze" and nothing computed it, so a ticket with no acceptance
        # coverage at all froze silently and was graded exclusively by the suite
        # its own author wrote.
        "marked_tests": {},
    }
    reads = {}

    def record(tid, mod, owners, alias, relf, lineno):
        if tid in owners:
            # The ticket writes this module itself, so there is NO read edge and
            # nothing to check — self-ownership satisfies the rule by definition.
            # It matters that this returns before the loop below: two live
            # tickets co-declare `services/ingest/src/adapters/**`, and without
            # this the shared scope surfaced as T-020 and T-023 "reading" each
            # other's module in both directions. Shared WRITE ownership is a real
            # defect, but it is check-wave's to report, not a read edge.
            if len(owners) > 1:
                res["shared_scope"].append((mod, tuple(sorted(owners))))
            return
        for owner in sorted(owners):
            reads.setdefault(tid, {}).setdefault(owner, set()).add(mod)
            if alias:
                res["dash_mismatch"].append((tid, owner, mod))
        # Legal if ANY owner is reachable. A module inside two overlapping scopes
        # is an ownership defect in its own right; treating it as illegal here as
        # well would double-report one fault as a read violation the operator
        # cannot act on.
        if owners & (closure.get(tid, set()) | closed):
            return
        res["violations"].append({
            "ticket": tid, "module": mod, "owned_by": sorted(owners),
            "file": relf, "line": lineno,
        })

    for path in files:
        relf = os.path.relpath(path, root)
        try:
            with open(path) as f:
                tree = ast.parse(f.read(), filename=relf)
        except (OSError, SyntaxError, ValueError) as e:
            # Never fatal: a frozen suite that does not parse is a real problem,
            # but it is measure's problem to report, not a reason this read-edge
            # pass takes the whole freeze down with a traceback.
            res["unparsed"].append((relf, str(e)[:120]))
            continue

        def attribute(marks, mods, lineno):
            for mod in sorted(mods):
                owners, alias = _module_owners(mod, scopes)
                if not owners:
                    # Only product modules are interesting. A module whose first
                    # segment is not a declared scope root is stdlib or a
                    # third-party package, and listing those would bury the real
                    # findings under a hundred lines of `json, os, re`.
                    if mod.split(".")[0] in roots:
                        res["unmapped"].append((sorted(marks)[0], mod, relf, lineno))
                    continue
                for tid in marks:
                    if tid in idx:
                        record(tid, mod, owners, alias, relf, lineno)

        # Module-level imports are attributed to EVERY ticket marked in the file.
        # Not pedantry: a top-level `from packages.contracts import X` that does
        # not resolve fails COLLECTION, so it takes down every test in the file
        # and not only the ones that use it — a wider blast radius than the
        # function-level import the live finding was about, not a narrower one.
        file_marks = []
        mod_level = _module_level_imports(tree)
        file_level_marks = _pytestmark_tickets(tree)

        def visit(body, inherited):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    visit(n.body, _ticket_marks(n) or inherited)
                    continue
                # Descend control flow. The old walk skipped anything that was
                # not a def or a class, so a test guarded by `if
                # sys.version_info >= …:` or defined inside a `try:` was neither
                # scanned nor counted as unmarked — invisible in both columns.
                if isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                                  ast.For, ast.AsyncFor, ast.While)):
                    for field in ("body", "orelse", "finalbody"):
                        sub = getattr(n, field, None)
                        if isinstance(sub, list):
                            visit(sub, inherited)
                    for h in getattr(n, "handlers", []) or []:
                        visit(getattr(h, "body", []) or [], inherited)
                    continue
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not n.name.startswith("test"):
                    continue
                marks = _ticket_marks(n) or inherited
                res["tests_scanned"] += 1
                if not marks:
                    res["unmarked_tests"] += 1
                    continue
                file_marks.extend(marks)
                for _tid in marks:
                    res["marked_tests"][_tid] = res["marked_tests"].get(_tid, 0) + 1
                attribute(marks, _dotted_imports(n), n.lineno)

        visit(tree.body, file_level_marks)
        if mod_level and file_marks:
            attribute(sorted(set(file_marks)), mod_level, 1)

    res["reads"] = {t: {o: sorted(m) for o, m in sorted(v.items())}
                    for t, v in sorted(reads.items())}
    res["violations"].sort(key=lambda v: (v["ticket"], v["module"]))
    res["unmapped"] = sorted(set(res["unmapped"]))
    res["dash_mismatch"] = sorted(set(res["dash_mismatch"]))
    res["shared_scope"] = sorted(set(res["shared_scope"]))
    return res


def _read_edge_report(res, idx):
    """[lines] — the human rendering, shared by `reads` and by freeze."""
    lines = []
    if res["unparsed"]:
        for relf, err in res["unparsed"]:
            lines.append(f"  COULD NOT PARSE {relf}: {err}")
    for v in res["violations"]:
        lines.append(
            "  ILLEGAL READ: %s imports %s at %s:%d, but %s owns it and is NOT in %s's "
            "dependency closure" % (v["ticket"], v["module"], v["file"], v["line"],
                                    "/".join(v["owned_by"]), v["ticket"]))
        for owner in v["owned_by"]:
            if v["ticket"] in _graph_strs(idx.get(owner, {}), "depends_on"):
                lines.append(
                    "      and the edge that would fix it CANNOT be added: %s already "
                    "depends on %s, so %s -> %s would close a CYCLE. The fix is to re-mark "
                    "the test or split the ticket — an amendment, not an edge."
                    % (owner, v["ticket"], v["ticket"], owner))
    if res["dash_mismatch"]:
        lines.append("  SCOPE/IMPORT NAMING MISMATCH (%d) — matched only after treating '_' "
                     "as '-'; a Python package directory cannot contain a hyphen, so one "
                     "side is wrong:" % len(res["dash_mismatch"]))
        for tid, owner, mod in res["dash_mismatch"][:10]:
            lines.append(f"      {tid} reads {mod}, scope-owned by {owner}")
        if len(res["dash_mismatch"]) > 10:
            lines.append(f"      ... and {len(res['dash_mismatch']) - 10} more")
    if res["shared_scope"]:
        # Found for free by the read pass, and it is the check-wave hazard seen
        # from the other side: two tickets that declare the same write glob can
        # never be dispatched concurrently, and nothing in the graph says so.
        lines.append("  SHARED WRITE OWNERSHIP (%d) — a frozen test's module falls inside "
                     "MORE THAN ONE ticket's scope; those tickets can never run in the same "
                     "wave:" % len(res["shared_scope"]))
        for mod, owners in res["shared_scope"][:10]:
            lines.append("      %s  claimed by %s" % (mod, ", ".join(owners)))
        if len(res["shared_scope"]) > 10:
            lines.append(f"      ... and {len(res['shared_scope']) - 10} more")
    if res["unmapped"]:
        lines.append("  UNOWNED PRODUCT MODULES (%d) — a frozen test imports these and NO "
                     "ticket's scope claims them, so nobody is obliged to build them:"
                     % len(res["unmapped"]))
        for tid, mod, relf, lineno in res["unmapped"][:10]:
            lines.append(f"      {mod}   (read by {tid}, {relf}:{lineno})")
        if len(res["unmapped"]) > 10:
            lines.append(f"      ... and {len(res['unmapped']) - 10} more")
    return lines


def cmd_reads(args):
    """Derive the read edges `scope` cannot express, and enforce the closure rule."""
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    path, tickets = _load_graph(root, state, args.graph)
    idx, dupes = _graph_index(tickets)
    _graph_refuse_if_invalid(root, path, tickets, idx, dupes,
                             "the read edges this command gates 'freeze' on")
    reads_closed, _closure = _closed_from_args(root, args)
    res = _scan_read_edges(root, state, idx, args.tests, reads_closed)

    if args.json:
        out = dict(res)
        out["graph"] = os.path.relpath(path, root)
        out["dash_mismatch"] = [{"ticket": a, "owner": b, "module": c}
                                for a, b, c in res["dash_mismatch"]]
        out["unmapped"] = [{"ticket": a, "module": b, "file": c, "line": dd}
                           for a, b, c, dd in res["unmapped"]]
        out["shared_scope"] = [{"module": a, "owners": list(b)}
                               for a, b in res["shared_scope"]]
        out["unparsed"] = [{"file": a, "error": b} for a, b in res["unparsed"]]
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print("reads: %d frozen test file(s), %d test(s) scanned, %d marked ticket(s) with "
              "read edges  (graph %s)"
              % (res["test_files"], res["tests_scanned"], len(res["reads"]),
                 os.path.relpath(path, root)))
        if not res["test_files"]:
            print("  no frozen test files under the protected paths — pass --tests <dir> if "
                  "the acceptance suite lives somewhere else")
        if res["unmarked_tests"]:
            print("  %d test(s) carry no @pytest.mark.ticket marker — they grade nothing in "
                  "particular and contribute no read edge" % res["unmarked_tests"])
        print("")
        print("  INBOUND (what each ticket's frozen tests import from other tickets):")
        for tid in sorted(res["reads"]):
            for owner, mods in sorted(res["reads"][tid].items()):
                print("    %-9s reads %-9s %s" % (tid, owner, ", ".join(mods)))
        if not res["reads"]:
            print("    (none — every frozen test imports only what its own ticket owns)")
        print("")
        # The half of H-10 that packets actually need. The inbound view tells a
        # worker what to import; only the OUTBOUND view tells T-030 that it is
        # shipping a port for T-032 and T-033 and not only for itself.
        outbound = {}
        for tid, owners in res["reads"].items():
            for owner, mods in owners.items():
                outbound.setdefault(owner, {})[tid] = mods
        print("  OUTBOUND OBLIGATIONS (state these in the owner's task packet — 'you are")
        print("  shipping this port for these tickets' tests, not only your own'):")
        for owner in sorted(outbound):
            for tid, mods in sorted(outbound[owner].items()):
                print("    %-9s must satisfy %-9s via %s" % (owner, tid, ", ".join(mods)))
        if not outbound:
            print("    (none)")
        for line in _read_edge_report(res, idx):
            print(line)
        print("")
        print("  The rule: a cross-ticket import is legal iff the OWNING ticket is in the")
        print("  importing ticket's transitive dependency closure. 'imports must be inside")
        print("  scope' is NOT the rule — it fires on every legitimate cross-ticket read.")

    if args.write:
        _reads_write(root, path, res)
    if res["violations"] and not args.no_fail:
        sys.exit(1)


def _reads_write(root, path, res):
    """Add an optional `reads` key to each ticket in the graph file.

    Additive only, and it never touches a key it did not write: the graph is a
    PROJECT file whose schema this harness does not own, and a paused run must
    still load it with whatever reader it already has."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        die(f"cannot rewrite {os.path.relpath(path, root)}: {e}")
    tickets = data.get("tickets") if isinstance(data, dict) else data
    n = 0
    for t in tickets:
        if not isinstance(t, dict):
            continue
        want = sorted(res["reads"].get(str(t.get("id") or ""), {}))
        if want != (t.get("reads") or []):
            if want:
                t["reads"] = want
            elif "reads" in t:
                del t["reads"]
            n += 1
    save_json(path, data)
    print(f"  wrote 'reads' on {n} ticket(s) in {os.path.relpath(path, root)} "
          "(additive optional key — every existing reader still loads it)")


def _freeze_read_gate(root, d, state, allow, already_frozen=False):
    """The INTAKE gate. Silent no-op for a project with no ticket graph.

    D4 — it blocks at intake and only at intake. A first freeze is the moment
    the answer is cheap: nothing is locked, the goals are still malleable, and
    the remedy (re-mark the test, split the ticket, add the edge) costs a text
    edit. Once the harness is frozen, every one of those remedies IS an
    amendment — so refusing `freeze --amend` refuses the only door out of the
    state it is complaining about, and the operator is left with a run that can
    never be amended again.

    It was also a straight backward-compatibility break. A run frozen by an
    earlier version has data this gate never inspected; the live paused
    ProxyShop run's next `freeze --amend` hit `exit 1` on state that was legal
    when it was written, over an escape hatch that appeared in no document. A
    gate that refuses an operation the user could always perform, on bytes an
    older version wrote, is a regression whatever it is checking for.

    So on an already-frozen project the finding is reported loudly, by name, and
    the freeze proceeds. `--allow-read-violations` remains the explicit override
    for the first freeze, where blocking is still the right call.
    """
    _, tickets = _load_graph(root, state, None, required=False)
    if tickets is None:
        return
    idx, _dupes = _graph_index(tickets)
    if not idx:
        return
    res = _scan_read_edges(root, state, idx)

    # goal-setting.md's hard stop, finally implemented. Same intake-only shape as
    # the read gate below and for the same D4 reason: at a FIRST freeze the
    # remedy (write the test, or split the ticket) is a text edit against goals
    # that are still malleable; once frozen, every remedy is itself an amendment,
    # so refusing `freeze --amend` would refuse the only door out.
    uncovered = sorted(t for t in idx if not res["marked_tests"].get(t))
    if uncovered:
        print("acceptance coverage — %d frozen test(s) across %d ticket(s):"
              % (res["tests_scanned"], len(idx) - len(uncovered)))
        print("  %d TICKET(S) CARRY NO FROZEN TEST AT ALL:" % len(uncovered))
        for t in uncovered:
            title = str((idx[t] or {}).get("title") or "").strip()
            print("    %-9s %s" % (t, title[:70]))
        if not allow and not already_frozen:
            die("freeze REFUSED: %d ticket(s) in the graph have ZERO frozen tests carrying "
                "their marker, so nothing outside their own author's suite will ever grade "
                "them. Measured on a real run: the ticket with zero frozen tests was the "
                "foundation five others built on, and an adversarial review of the finished "
                "branch found two critical defects squarely where its own tests could not "
                "reach. Write an acceptance test marked "
                "@pytest.mark.ticket(\"<id>\") for each, or drop the ticket. Freeze anyway "
                "with --allow-read-violations once you have decided it is acceptable."
                % len(uncovered))
        print("  ADVISORY only (%s) — these tickets are graded by nobody but themselves."
              % ("--allow-read-violations passed" if allow else "already frozen"))

    lines = _read_edge_report(res, idx)
    if not lines:
        return
    print("read edges (B5/H-10) — derived from %d frozen test(s):" % res["tests_scanned"])
    for line in lines:
        print(line)
    if not res["violations"]:
        return
    if not allow and not already_frozen:
        die("freeze REFUSED: %d frozen test(s) import a module owned by a ticket that is not "
            "in their own dependency closure. Those tests can never pass — the module will "
            "not exist when they are graded. Re-mark the test, split the ticket, or add the "
            "edge if it does not close a cycle; 'swarmloop.py reads' shows the full map. "
            "Freeze anyway with --allow-read-violations once you have decided it is "
            "acceptable." % len(res["violations"]))
    if allow:
        print("  --allow-read-violations was passed: freezing over %d illegal read(s)."
              % len(res["violations"]))
        return
    bar = "!" * 74
    print(bar)
    print("!!! %d ILLEGAL CROSS-TICKET READ(S) IN THE FROZEN SUITE — ADVISORY, NOT A REFUSAL"
          % len(res["violations"]))
    for v in res["violations"]:
        print("!!!   %s:%s  %s imports %s (owned by %s — outside %s's closure)"
              % (v.get("file"), v.get("line"), v.get("ticket"), v.get("module"),
                 ", ".join(v.get("owned_by") or []) or "?", v.get("ticket")))
    print("!!! Those tests cannot pass as marked: the module will not exist when they are")
    print("!!! graded. This harness is ALREADY FROZEN, so the fix is itself an amendment —")
    print("!!! refusing this one would leave no way to make it. The freeze proceeds; run")
    print("!!! 'swarmloop.py reads' for the full map and fix them in THIS amendment.")
    print(bar)


# ------------------------------------------------------------------ status

# ------------------------------------------------- worktree teardown (retire)
#
# The disk finding. A provisioned worktree costs about a GIGABYTE (.venv ~312M
# + node_modules ~636M), so the scaling law of fan-out is DISK, not context.
# On the live run wave 1 merged, landed on main and was pushed — and its five
# task/T-0xx worktrees plus the integration worktree were all still on disk when
# wave 2 added six more: thirteen trees, ~14.7 GB measured, six of them finished
# work. SKILL.md's merge protocol has said to retire them since step 10 was
# written. The advice was never the defect. The defect was that NOTHING
# EXECUTED IT and nothing noticed that nothing had, which is why this is a
# subcommand with a `checkpoint` tripwire behind it and not another paragraph.
#
# The rules below are not negotiable, and they are the whole reason teardown is
# safe to mechanise:
#
#   * plain `git worktree remove <path>` ONLY. It is the one teardown that
#     validates the target really is a worktree and REFUSES one holding
#     uncommitted work. That refusal IS the dirty-tree check — retire
#     deliberately does not pre-check cleanliness, because a `git status`
#     pre-flight races the agent that is still writing in the tree.
#   * NEVER --force, NEVER `worktree move`, NEVER `rm -rf` of a worktree path.
#     All three are blocked by hooks/git-guard.py, and --force discards exactly
#     the uncommitted work the plain form exists to protect.
#   * a refusal is a FINDING, not an obstacle: report it, leave the tree
#     standing, and escalate. Same for a TIMEOUT — a 16 GB teardown once died at
#     two minutes (exit 143), and the answer to that is a second `retire`, never
#     a bigger hammer.
#   * ANCESTOR OF main, never merged-into-integration. On the live run fix
#     agents re-entered a ticket's worktree AFTER adjudication and
#     re-verification ran at the new tips; a tree retired at integration-merge
#     would have taken the fix with it.

WORKTREE_IDLE_DEFAULT = 900     # seconds of quiet before a tree counts unattended
WORKTREE_REMOVE_TIMEOUT = 120   # per `git worktree remove` (a 16 GB one hit 143 at 120s)
WORKTREE_DU_TIMEOUT = 60        # `retire` sizing bound; 14.7 GB measured at ~38s cold
WORKTREE_DU_CHECKPOINT = 3      # `checkpoint` sizing bound — it runs every cycle boundary


def _worktree_root_abs(root, state):
    """The run's recorded worktree root, absolute. Same fallback `resume` uses."""
    return os.path.normpath(os.path.join(
        root, state.get("worktree_root") or f"{STATE_DIR}/worktrees"))


def _scratch_root_abs(root, state):
    """The run's recorded per-lane scratch root, absolute — or None if unrecorded.

    Deliberately WITHOUT the default `_worktree_root_abs` carries. An unrecorded
    scratch root means nobody wrote down whether the lanes were subdivided at
    all, and inventing a plausible path here would answer that question with a
    guess. Callers render the None case as UNRECORDED instead.
    """
    sr = state.get("scratch_root")
    return os.path.normpath(os.path.join(root, sr)) if sr else None


def _base_branch(state, explicit=None):
    return explicit or state.get("base_branch") or "main"


def _wt_records(root):
    """`git worktree list --porcelain` -> [{"path","branch","flags"}], PRIMARY FIRST.

    Parsed line-by-line off the `worktree ` sentinel rather than by splitting on
    blank lines: a `locked <reason>` line carries arbitrary operator text, and
    the record separator is not something to bet a deletion on. [] when git
    cannot answer, which every caller reads as 'retire nothing'.
    """
    out = _git(root, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        return []
    recs, cur = [], None
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            if cur:
                recs.append(cur)
            cur = {"path": line[len("worktree "):].strip(), "branch": None, "flags": set()}
        elif cur is None or not line.strip():
            continue
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip()
        else:
            cur["flags"].add(line.split(" ", 1)[0].strip())
    if cur:
        recs.append(cur)
    return recs


def _short_branch(branch):
    if not branch:
        return None
    return branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch


def _ticket_of_branch(branch):
    """'refs/heads/task/T-014' -> 'T-014'; None for anything not a task branch."""
    short = _short_branch(branch)
    if short and short.startswith("task/"):
        return short[len("task/"):] or None
    return None


def _is_ancestor(root, ref, base):
    """True / False / None. None means git could not answer (a ref that is gone,
    an unborn base) and is deliberately NOT folded into False — 'unknown' must
    keep a tree, and False must be reportable as 'this work did not land'."""
    out = _git(root, "merge-base", "--is-ancestor", ref, base)
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None


def _wt_admin_dir(path):
    """A linked worktree's admin dir ($GIT_COMMON_DIR/worktrees/<name>), read out
    of its `.git` FILE. One open() instead of a `git rev-parse` subprocess,
    because this is called per tree from `checkpoint`."""
    p = os.path.join(path, ".git")
    try:
        if os.path.isdir(p):
            return p                       # the primary checkout
        with open(p) as f:
            line = f.read().strip()
    except OSError:
        return None
    if line.startswith("gitdir:"):
        return os.path.normpath(os.path.join(path, line.split(":", 1)[1].strip()))
    return None


def _wt_last_activity(path):
    """Newest mtime among cheap agent-driven signals in a worktree; None = unknown.

    Deliberately NOT a walk of the tree. A provisioned worktree carries a .venv
    and node_modules — `du -sk` of the live run's thirteen took 38 seconds — and
    this runs from `checkpoint`. These few stat() calls cost nothing and move for
    exactly the events that matter: a commit (logs/HEAD, COMMIT_EDITMSG), a
    checkout (HEAD, ORIG_HEAD), an index refresh (index), and any file created or
    deleted at the top of the tree (the directory itself).
    """
    newest = None
    cands = [path]
    admin = _wt_admin_dir(path)
    if admin:
        cands += [os.path.join(admin, n) for n in
                  ("index", "HEAD", "ORIG_HEAD", "logs/HEAD", "COMMIT_EDITMSG")]
    for c in cands:
        try:
            m = os.lstat(c).st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def _dispatch_holder(open_recs, path, branch):
    """An OPEN dispatch record that plausibly still owns this worktree, or None.

    The ledger is OPTIONAL: the live run has no dispatch.jsonl at all, so
    _open_dispatches returns [] and this contributes nothing rather than
    exploding. When the ledger IS kept it is the best live-agent signal there is,
    because it says so in words instead of being inferred from a timestamp.
    """
    npath = os.path.normpath(path)
    tid = _ticket_of_branch(branch)
    short = _short_branch(branch)
    for r in open_recs:
        rp = str(r.get("worktree") or "").strip()
        if rp and os.path.normpath(rp) == npath:
            return r
        subj = str(r.get("subject") or "")
        if short and short in subj:
            return r
        if tid and re.search(r"(?<![A-Za-z0-9_])" + re.escape(tid) + r"(?![A-Za-z0-9_])", subj):
            return r
    return None


def _retire_scan(root, state, base, idle_seconds, now=None):
    """Classify this run's worktrees. -> (worktree_root, candidates, kept)

    candidates  retirable: the branch has LANDED on `base` and nothing is holding
                the tree. Cleanliness is NOT tested here on purpose — see the
                section header.
    kept        [(rec, reason)] for everything this run owns that is not
                retirable. A reason is always a sentence an operator can act on.

    Worktrees this run does not own are not reported at all. Ownership uses the
    SAME union `resume` uses — the recorded root, the legacy in-tree path, or any
    tree sitting on a refs/heads/task/ branch — because Phase 4 moved worktrees
    to a sibling directory and matching a single spelling made every run on the
    other layout invisible. (resume still carries that rule inline; fold the two
    together when there is no live build invoking this file by path.)
    """
    now = time.time() if now is None else now
    wt_root = _worktree_root_abs(root, state)
    recs = _wt_records(root)
    open_recs = _open_dispatches(sd(root))
    base_ok = _git(root, "rev-parse", "--verify", "--quiet",
                   base + "^{commit}").returncode == 0
    nroot = os.path.normpath(root)
    cands, kept = [], []
    for i, rec in enumerate(recs):
        path = rec["path"]
        npath = os.path.normpath(path)
        # The primary checkout is excluded STRUCTURALLY (git always lists it
        # first) and again by path. It is where the frozen metric commands run,
        # and it is the one tree whose loss would end the run.
        if i == 0 or npath == nroot or "bare" in rec["flags"]:
            continue
        owned = (npath.startswith(wt_root + os.sep)
                 or f"{STATE_DIR}/worktrees" in path
                 or bool(rec["branch"] and rec["branch"].startswith("refs/heads/task/")))
        if not owned:
            continue
        short = _short_branch(rec["branch"])
        if not base_ok:
            kept.append((rec, "cannot resolve base ref '%s' — nothing here is provably "
                              "landed, so nothing is retirable" % base))
            continue
        if "prunable" in rec["flags"]:
            kept.append((rec, "git reports this worktree prunable (its directory is gone) — "
                              "`git worktree prune` clears the stale record; retire does not "
                              "guess at half-deleted trees"))
            continue
        if "locked" in rec["flags"]:
            kept.append((rec, "LOCKED (`git worktree lock`) — someone claimed this tree "
                              "deliberately; unlock it yourself if that claim is stale"))
            continue
        if not rec["branch"]:
            kept.append((rec, "detached HEAD — there is no branch to test against %s, so "
                              "nothing proves this work landed" % base))
            continue
        anc = _is_ancestor(root, rec["branch"], base)
        if anc is None:
            kept.append((rec, "git could not compare %s to %s" % (short, base)))
            continue
        if not anc:
            kept.append((rec, "%s has NOT landed on %s — merged into an integration branch "
                              "is NOT landed. Rejected or in-flight work keeps its diff in "
                              "this tree for the retry packet" % (short, base)))
            continue
        holder = _dispatch_holder(open_recs, path, rec["branch"])
        if holder:
            kept.append((rec, "an OPEN dispatch still owns this tree: %s (%s) — "
                              "`dispatch --close %s` when that agent is collected"
                              % (holder.get("agent_ref"), holder.get("subject"),
                                 holder.get("agent_ref"))))
            continue
        if idle_seconds > 0:
            last = _wt_last_activity(path)
            if last is not None and (now - last) < idle_seconds:
                kept.append((rec, "touched %ds ago (idle guard: %ds) — a fix agent re-entering "
                                  "an adjudicated ticket's tree is exactly how landed work gets "
                                  "retired too early. Re-run with --idle-seconds 0 once every "
                                  "agent for this tree is collected"
                                  % (int(now - last), idle_seconds)))
                continue
        cands.append(rec)
    return wt_root, cands, kept


def _worktree_remove(root, path, timeout):
    """Plain `git worktree remove`. -> (True, "") or (False, reason).

    There is no --force path in this function and there must never be one: the
    plain form's refusal of a dirty tree is the only thing standing between an
    agent's uncommitted work and a mechanical loop that runs every cycle.
    """
    try:
        out = subprocess.run(["git", "worktree", "remove", path],
                             capture_output=True, text=True, cwd=root, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, ("`git worktree remove` did not finish within %ds — treated as a "
                       "REFUSAL. A 16 GB teardown timed out at two minutes once already; "
                       "removal is resumable, so re-run retire (with a larger --timeout if "
                       "the tree really is that big). NEVER escalate a timeout to --force"
                       % timeout)
    except OSError as e:
        return False, "could not run git: %s" % e
    if out.returncode == 0:
        return True, ""
    msg = " ".join(((out.stderr or "") + " " + (out.stdout or "")).split())
    return False, msg or ("git worktree remove exited %d" % out.returncode)


def _landed_branches(root, base, assume_removed=()):
    """task/* and integration/* branches that are ancestors of `base` and are
    checked out NOWHERE. Ancestor-of-base is what makes `git branch -D` lossless:
    every commit on the branch is already reachable from base. A branch that is
    NOT an ancestor is a rejected or in-flight ticket and keeps its diff."""
    out = _git(root, "for-each-ref", "--format=%(refname)",
               "refs/heads/task", "refs/heads/integration")
    if out.returncode != 0:
        return []
    gone = set(assume_removed)
    live = set(r["branch"] for r in _wt_records(root) if r["branch"]) - gone
    landed = []
    for ref in (out.stdout or "").splitlines():
        ref = ref.strip()
        if not ref or ref in live:
            continue
        if _is_ancestor(root, ref, base) is True:
            landed.append(_short_branch(ref))
    return sorted(landed)


def _du_kb(path, timeout):
    """(kilobytes, note). Always bounded: `du -sk` of the live run's 14.7 GB
    worktree root took 38 seconds cold, which is why no caller ever runs it
    unbounded and `checkpoint` gives it three seconds and a cached fallback."""
    if timeout <= 0:
        return None, "sizing skipped"
    if not os.path.isdir(path):
        return None, "no worktree root on disk"
    try:
        out = subprocess.run(["du", "-sk", path], capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "du exceeded %ds" % timeout
    except OSError as e:
        return None, "du unavailable (%s)" % e
    if out.returncode != 0:
        return None, "du failed"
    try:
        return int((out.stdout or "").split()[0]), ""
    except (ValueError, IndexError):
        return None, "du output not understood"


def _human_kb(kb):
    if kb is None:
        return "?"
    v = float(kb)
    for unit in ("K", "M", "G", "T"):
        if v < 1024.0 or unit == "T":
            return "%.1f%s" % (v, unit)
        v /= 1024.0


def _print_stale_worktree_warning(root, d, state):
    """checkpoint's teardown tripwire. Prints NOTHING when nothing is stale.

    This is the half of the change that matters. Retiring worktrees was already
    documented and already correct; what was missing was any way for the run to
    notice it had been skipped. checkpoint runs at every cycle boundary, so this
    is where an accumulating worktree root becomes visible in the run's OWN
    OUTPUT instead of only on the disk. Advisory by construction: it prints and
    returns, and it must never gain the power to end a run — a full disk is a
    cost, not a corrupted result.

    Scanned with idle_seconds=0 ON PURPOSE, which is the one place this differs
    from `retire`. The idle guard protects a REMOVAL — it is what stops a tree a
    fix agent re-entered from being deleted. A printed line cannot hurt a busy
    tree, and running the warning under the guard made it go silent for fifteen
    minutes after every wave landed: precisely the moment a batch of finished
    trees hits the disk, and precisely the thing this exists to make visible.
    Each line instead says whether `retire` would currently hold that tree back.
    """
    base = _base_branch(state)
    wt_root, cands, _kept = _retire_scan(root, state, base, 0)
    if not cands:
        return
    now = time.time()
    open_recs = _open_dispatches(d)
    holds = []
    for rec in cands:
        holder = _dispatch_holder(open_recs, rec["path"], rec["branch"])
        if holder:
            holds.append("  (open dispatch %s — retire holds this one)" % holder.get("agent_ref"))
            continue
        last = _wt_last_activity(rec["path"])
        if last is not None and (now - last) < WORKTREE_IDLE_DEFAULT:
            holds.append("  (touched %ds ago — retire holds this one)" % int(now - last))
        else:
            holds.append("")
    size_kb, note = _du_kb(wt_root, WORKTREE_DU_CHECKPOINT)
    if size_kb is not None:
        disk = _human_kb(size_kb)
    else:
        cached = state.get("worktree_root_size_kb")
        if cached:
            disk = ("%s as of %s (cached by the last retire; this checkpoint's own du: %s)"
                    % (_human_kb(cached), state.get("worktree_root_size_at") or "?", note))
        else:
            disk = "not measured (%s) — `retire --dry-run` reports it" % note
    bar = "-" * 74
    print(bar)
    print("WORKTREE TEARDOWN OVERDUE: %d worktree(s) have LANDED on %s and are still on disk"
          % (len(cands), base))
    for rec, hold in zip(cands, holds):
        print("  %-26s %s%s" % (_short_branch(rec["branch"]) or "DETACHED", rec["path"], hold))
    print("  worktree root : %s" % wt_root)
    print("  disk          : %s" % disk)
    print("  A provisioned worktree costs about a GIGABYTE (.venv + node_modules), so the")
    print("  scaling law of fan-out is DISK, not context. Reclaim them now:")
    print("      swarmloop.py retire")
    if all(h for h in holds):
        print("  (every one is held back right now for the reason noted beside it — once")
        print("   those agents are collected, `retire --idle-seconds 0` reclaims them)")
    print("  (advisory only — this never changes checkpoint's exit code)")
    print(bar)


def cmd_retire(args):
    """Reclaim every worktree of this run whose work has LANDED on the base branch."""
    root = repo_root()
    d = sd(root)
    spath = os.path.join(d, STATE_FILE)
    state = load_json(spath, "state")
    base = _base_branch(state, args.base)
    idle = max(0, args.idle_seconds)
    timeout = max(1, args.timeout)
    wt_root, cands, kept = _retire_scan(root, state, base, idle)

    retired, refused = [], []
    if not args.dry_run:
        for rec in cands:
            ok, why = _worktree_remove(root, rec["path"], timeout)
            (retired if ok else refused).append((rec, why))

    # Branch cleanup. In --dry-run the candidate trees have NOT been removed, so
    # their branches still read as checked out; assume_removed keeps the dry run
    # honest about what the real run would delete.
    branches_deleted, branches_planned, branches_failed = [], [], []
    if not args.keep_branches:
        assume = [r["branch"] for r in (cands if args.dry_run else [x[0] for x in retired])
                  if r["branch"]]
        landed = _landed_branches(root, base, assume)
        if args.dry_run:
            branches_planned = landed
        else:
            for b in landed:
                if _git(root, "branch", "-D", b).returncode == 0:
                    branches_deleted.append(b)
                else:
                    branches_failed.append(b)

    size_kb, size_note = _du_kb(wt_root, max(0, args.du_seconds))
    after = _wt_records(root)
    remaining = max(0, len(after) - 1)      # the primary checkout is not "remaining"

    # State is bookkeeping, never the point of the command: --dry-run writes
    # nothing at all, and a failed write is a note rather than a failure.
    if not args.dry_run:
        try:
            with _state_lock(d):
                st = load_json(spath, "state")
                st["last_retire_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if size_kb is not None:
                    st["worktree_root_size_kb"] = size_kb
                    st["worktree_root_size_at"] = st["last_retire_at"]
                save_json(spath, st)
        except Exception as e:              # noqa: BLE001
            if not args.as_json:
                print("retire: note: could not record the size in %s/%s (%s)"
                      % (STATE_DIR, STATE_FILE, e))

    if args.as_json:
        def _rows(pairs):
            return [{"path": r["path"], "branch": _short_branch(r["branch"]),
                     "ticket": _ticket_of_branch(r["branch"]), "reason": why}
                    for r, why in pairs]
        print(json.dumps({
            "worktree_root": wt_root,
            "base": base,
            "dry_run": bool(args.dry_run),
            "idle_seconds": idle,
            "retirable": [{"path": r["path"], "branch": _short_branch(r["branch"]),
                           "ticket": _ticket_of_branch(r["branch"])} for r in cands],
            "retired": _rows(retired),
            "refused": _rows(refused),
            "kept": _rows(kept),
            "branches_deleted": branches_deleted,
            "branches_would_delete": branches_planned,
            "branches_delete_failed": branches_failed,
            "worktrees_remaining": remaining,
            "root_size_kb": size_kb,
            "root_size": _human_kb(size_kb) if size_kb is not None else None,
            "root_size_note": size_note,
        }, indent=2, sort_keys=True))
        sys.exit(1 if refused else 0)

    print("retire: base %s · worktree root %s%s"
          % (base, wt_root, "  [DRY RUN — nothing will be touched]" if args.dry_run else ""))
    if not cands and not kept:
        print("  nothing to retire — this run owns no worktrees")
    for rec in cands if args.dry_run else []:
        print("  WOULD RETIRE  %-24s %s"
              % (_short_branch(rec["branch"]) or "DETACHED", rec["path"]))
    for rec, _why in retired:
        print("  RETIRED       %-24s %s"
              % (_short_branch(rec["branch"]) or "DETACHED", rec["path"]))
    for rec, why in refused:
        print("  REFUSED       %-24s %s"
              % (_short_branch(rec["branch"]) or "DETACHED", rec["path"]))
        print("                %s" % why)
    for rec, why in kept:
        print("  kept          %-24s %s"
              % (_short_branch(rec["branch"]) or "DETACHED", rec["path"]))
        print("                %s" % why)
    if branches_planned:
        print("  branches that WOULD be deleted (landed on %s): %s"
              % (base, ", ".join(branches_planned)))
    if branches_deleted:
        print("  branches deleted (landed on %s): %s" % (base, ", ".join(branches_deleted)))
    if branches_failed:
        print("  branches git refused to delete: %s" % ", ".join(branches_failed))
    print("  summary: %d retired · %d REFUSED · %d kept · %d worktree(s) still on disk"
          % (len(retired), len(refused), len(kept), remaining))
    print("  worktree root size: %s%s"
          % (_human_kb(size_kb) if size_kb is not None else "unmeasured",
             "" if size_kb is not None else " (%s)" % size_note))
    if refused:
        print("  A REFUSAL IS A FINDING, NOT AN OBSTACLE. The tree is still standing and its")
        print("  uncommitted work is intact. Do NOT reach for --force or rm -rf (both are")
        print("  blocked by git-guard, and --force discards exactly what the refusal saved).")
        print("  Record each one so a sleeping operator sees it:")
        for rec, why in refused:
            print('      swarmloop.py escalate --kind harness --subject "%s" \\' % rec["path"])
            print('          --reason "%s"' % _oneline(why, 160))
        sys.exit(1)
    sys.exit(0)


# ------------------------------------------------------- push gate / conflicts
#
# D-1, reproduced on a fresh fixture before a line of this was written. SKILL.md's
# Phase 7 push gate required that "everything .swarm-loop/ currently holds is
# committed" — while Phase 7's OWN four commands write into .swarm-loop/:
# `measure` appends history.csv, `analyze` writes analysis/cycle-N.json,
# `checkpoint` rewrites state.json. After the sequence the tree reads
# `M .swarm-loop/history.csv`, `M .swarm-loop/state.json`, `?? .swarm-loop/
# analysis/`, so the condition was false the instant it was evaluated — in every
# epoch, of every run, forever — and the failure was SILENT, because an
# orchestrator that never pushes prints nothing about not pushing. The one and
# only offsite copy of the run's evidence was withheld by a gate nobody could
# satisfy. The prose now fires the push at the END of Phase 8, after the ledger
# commit, which is the only ordering under which that bullet's own rationale is
# true; this is the mechanical half, because a prose ordering forces nothing and
# prose invariants in this skill have a measured 100% failure rate.
#
# It is deliberately INCOMPLETE and says so on every run. SKILL.md's six bullets
# are seven conditions once the rung-2 bullet is split into its mechanical half
# (has the verifier returned) and its judgement half (does its finding invalidate
# a merged branch); this command scores five and cannot score two. A gate that
# quietly answers five of seven while LOOKING like it answered seven is the exact
# defect class this file keeps producing, so the other two are printed as an
# operator checklist — on the pass path as loudly as on the fail path.

# The two conditions no command can answer, verbatim in the gate's own terms.
PUSH_GATE_OPERATOR = (
    ("integrations-green",
     "this epoch's integrations landed green on `main`",
     "The suite that proves it ran in the primary checkout at merge time "
     "(merge-protocol step 4), and left no artifact this command can read. "
     "Re-reading a green from a log is not the same claim."),
    ("rung2-findings",
     "no rung-2 verifier finding invalidates a branch merged into this epoch's "
     "`main`",
     "Whether a finding invalidates a merged branch is a judgement about the "
     "finding's content. The command can only see THAT a verifier is still "
     "outstanding, which is the half above."),
)


def _swarm_dirty(root):
    """Uncommitted paths under .swarm-loop/, or None if git could not answer.

    -uall on purpose: git collapses an untracked DIRECTORY to a single entry,
    and `?? .swarm-loop/analysis/` was the exact shape of the D-1 reproduction —
    the directory tells an operator less than the cycle file inside it. Ignored
    paths (worktrees/, archive/, tool caches) are absent by construction, which
    is why this is a usable gate condition at all.
    """
    out = _git(root, "status", "--porcelain", "-uall", "--", STATE_DIR)
    if out.returncode != 0:
        return None
    return [ln for ln in (out.stdout or "").splitlines() if ln.strip()]


def _push_gate_analysis(d, cycle):
    """(status, detail) for the 'analyze did not exit 1' rung."""
    jpath = os.path.join(d, ANALYSIS_DIR, "cycle-%d.json" % cycle)
    rel = "%s/%s/cycle-%d.json" % (STATE_DIR, ANALYSIS_DIR, cycle)
    if not os.path.exists(jpath):
        return "FAIL", ("%s does not exist — cycle %d was never analyzed, so this "
                        "epoch has no values to have read before pushing. Run "
                        "'analyze --cycle %d'." % (rel, cycle, cycle))
    try:
        with open(jpath) as f:
            a = json.load(f)
    except (OSError, ValueError) as e:
        return "FAIL", "%s will not parse (%s) — treat it as no analysis at all" % (rel, e)
    if a.get("cycle") is not None and int(a["cycle"]) != cycle:
        return "FAIL", ("%s records cycle %s, not %d — the file was hand-edited or "
                        "copied; re-run 'analyze --cycle %d'"
                        % (rel, a.get("cycle"), cycle, cycle))
    stale = list(a.get("stale_metrics") or [])
    if stale:
        return "FAIL", ("no measurement at cycle %d for: %s. 'analyze' exits 1 on this, "
                        "and the table it printed is not a verdict on this cycle — "
                        "re-measure before pushing" % (cycle, ", ".join(stale)))
    # Read POSITIVELY: the rung passes only on the one value that means the
    # numbers were written against a verified-intact harness.
    #
    # This was `== "VIOLATED"`, a refusal that could not fire: `analyze` is the
    # only writer of this file and it routes through `_integrity_gate`, which
    # `die()`s on VIOLATED before `save_json` is ever reached — so no file on
    # disk can carry that string, and a violated-at-analyze-time epoch has no
    # cycle-N.json at all (caught above by 'does not exist'). A rung that reads
    # as a live guard and cannot fire is exactly what this command was added to
    # remove. What CAN happen is what the cycle-mismatch guard above already
    # guards for — a hand-edited or copied file — plus a file written by another
    # tool or a future schema; any value that is not "intact" means nothing here
    # has established that the harness was sound when these numbers were taken.
    integ = a.get("harness_integrity")
    if integ is None:
        # An analysis written before this key existed. Not a pass and not a
        # failure: the question cannot be answered from the file. UNANSWERABLE
        # blocks the push exactly like FAIL, so this degrades safely.
        return "UNANSWERABLE", ("%s carries no harness_integrity — it predates the key "
                                "(a run resumed across a skill upgrade), so whether the "
                                "harness was intact when these numbers were written is "
                                "not recorded. Re-run 'verify' and 'analyze --cycle %d', "
                                "or read the epoch report by hand." % (rel, cycle))
    if integ != "intact":
        return "FAIL", ("%s records harness_integrity=%r, not 'intact' — nothing here "
                        "establishes that the frozen harness was sound when those numbers "
                        "were taken, and no later 'verify' makes them retroactively sound. "
                        "Re-run 'analyze --cycle %d'." % (rel, integ, cycle))
    return "PASS", ("%s exists, records cycle %d, records harness_integrity=intact, and "
                    "no metric is stale" % (rel, cycle))


def _push_gate_tracked(state, cycle):
    """(status, detail) for the tracked-file-drop rung, using checkpoint's own
    arithmetic — drop > max(25, 10% of previous) — so the two sites cannot
    disagree about what 'sharp' means."""
    hist = state.get("head_history") or []
    entry = prev = None
    for h in hist:
        try:
            c = int(h.get("cycle"))
        except (TypeError, ValueError):
            continue
        if c == cycle:
            entry = h
        elif c < cycle:
            if prev is None or c > int(prev.get("cycle", -1)):
                prev = h
    if entry is None:
        return "FAIL", ("cycle %d has no entry in state.json head_history — "
                        "'checkpoint --cycle %d' has not run, so the tracked-file "
                        "number this rung is sourced from does not exist. A condition "
                        "nobody sources a number for passes vacuously every epoch."
                        % (cycle, cycle))
    if prev is None:
        return "PASS", ("cycle %d is the first checkpoint of this run — %s tracked "
                        "files, nothing earlier to drop from"
                        % (cycle, entry.get("tracked_files", "?")))
    if "tracked_files" not in entry or "tracked_files" not in prev:
        return "UNANSWERABLE", ("state.json predates the tracked_files key on cycle %d "
                                "or %s — the count cannot be compared. Read "
                                "`git ls-tree -r HEAD --name-only | wc -l` against the "
                                "epoch report by hand." % (cycle, prev.get("cycle")))
    now, was = entry["tracked_files"], prev["tracked_files"]
    drop = was - now
    if drop > max(25, 0.10 * was):
        return "FAIL", ("tracked files fell %d (cycle %s: %d -> cycle %d: %d), past "
                        "checkpoint's own max(25, 10%%) line. That is the index-wipe "
                        "signature; explain the drop in the epoch report before the "
                        "push, or do not push."
                        % (drop, prev.get("cycle"), was, cycle, now))
    return "PASS", ("tracked files %d -> %d since cycle %s (no sharp drop)"
                    % (was, now, prev.get("cycle")))


def cmd_push_gate(args):
    """Score the mechanically checkable rungs of SKILL.md's Phase 7 push gate.

    Runs at the END of Phase 8, after the ledger commit — see the D-1 note above
    for why nowhere earlier can satisfy it. A false refusal here is CHEAP by
    construction: the prescribed response to any unmet condition is already
    "name it in the epoch report and carry it to the next epoch's gate", and this
    command sits nowhere near dispatch, where a false refusal would wedge the run.
    A false PASS is the expensive direction, so every rung that cannot be answered
    is printed as unanswered rather than counted.

    Exit 0 = every mechanical rung was ANSWERED and passed (the two operator
    rungs are still yours). Exit 1 = do not push — which covers a rung that came
    back UNANSWERABLE just as much as one that FAILED, because a gate that could
    not read one of its own conditions has not cleared it. Never exit 2: this is
    not a terminal signal.
    """
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    cycle = args.cycle
    mode = state.get("push_remote", "when-clean")
    conds = []

    def add(cid, ref, status, detail):
        conds.append({"id": cid, "condition": ref, "status": status, "detail": detail})

    if mode == "never":
        # The honest answer to "may I push?" is no, so this is exit 1 — but it is
        # a configuration, not a failed gate, and the text has to say which.
        out = {"cycle": cycle, "verdict": "no-remote", "push_remote": mode,
               "conditions": [], "operator_checklist": []}
        if args.as_json:
            print(json.dumps(out, indent=2))
        else:
            print("push-gate cycle %d: NOT APPLICABLE — this run was initialized "
                  "--push-remote never." % cycle)
            print("  The loop never touches a remote. Skip the push step entirely; there is "
                  "no condition here to satisfy and nothing to carry to the next epoch.")
            print("  Exit 1 because the answer to 'may I push?' is no — not because "
                  "anything failed.")
        sys.exit(1)

    # (1) verify — the live integrity oracle, not a remembered one.
    status, bad, notes = _verify_quiet(root, d, state)
    if status == "intact":
        add("verify", "`verify` passed this epoch", "PASS",
            "frozen manifest re-hashed and authenticated: intact")
    elif status == "not frozen":
        add("verify", "`verify` passed this epoch", "FAIL",
            "goals were never frozen — there are no locked targets for a measurement "
            "to have been against, so there is no number this push would be carrying")
    else:
        add("verify", "`verify` passed this epoch", "FAIL",
            "harness VIOLATED (%d finding(s), first: %s) — a violated harness voids the "
            "scores, so there is no number to have read"
            % (len(bad), bad[0] if bad else "?"))
    for n in notes:
        add("verify-note", "`verify` passed this epoch", "NOTE", n)

    # (2) analyze produced values for THIS cycle.
    st, detail = _push_gate_analysis(d, cycle)
    add("analysis", "`analyze` did not exit 1 — no metric is stale", st, detail)

    # (3) tracked-file count, sourced from checkpoint's own recorded number.
    st, detail = _push_gate_tracked(state, cycle)
    add("tracked-files", "this epoch's tracked-file count is not an unexplained drop",
        st, detail)

    # (4) rung-2 verifiers returned. ONLY dispatches of kind 'verifier' block.
    # Blocking on every open dispatch would make the gate unsatisfiable in the
    # healthy case the skill is built around — dispatch is rolling, so one
    # epoch's merges and the next epoch's builds overlap by design, and an open
    # TICKET dispatch is the normal state of a wide swarm, not a push blocker.
    # That would have re-created D-1 in a new place.
    open_recs = _open_dispatches(d)
    verifiers = [r for r in open_recs if (r.get("kind") or "") == "verifier"]
    others = [r for r in open_recs if (r.get("kind") or "") != "verifier"]
    if verifiers:
        add("rung2-returned", "every rung-2 verifier dispatched for this epoch has returned",
            "FAIL",
            "%d verifier dispatch(es) still open in %s/%s: %s. A verdict still outstanding "
            "is an unmet condition, not a pass — wait for it, or carry the push to the next "
            "epoch's gate."
            % (len(verifiers), STATE_DIR, DISPATCH_FILE,
               ", ".join(str(r.get("agent_ref")) for r in verifiers)))
    else:
        add("rung2-returned", "every rung-2 verifier dispatched for this epoch has returned",
            "PASS",
            "no open dispatch of kind 'verifier' in %s/%s%s"
            % (STATE_DIR, DISPATCH_FILE,
               "" if not others else
               " (%d other dispatch(es) open — rolling dispatch, not a push blocker: %s)"
               % (len(others), ", ".join(str(r.get("agent_ref")) for r in others))))

    # (5) .swarm-loop/ committed — the rung D-1 was about.
    dirty = _swarm_dirty(root)
    if dirty is None:
        add("swarm-committed", "everything `.swarm-loop/` holds is committed", "UNANSWERABLE",
            "git status failed — check the repository by hand before pushing")
    elif dirty:
        add("swarm-committed", "everything `.swarm-loop/` holds is committed", "FAIL",
            "%d uncommitted path(s) under %s/: %s. Phase 8's ledger commit "
            "(`git add %s && git commit`) has not run — a push of a main carrying none of "
            "the epoch's evidence is what makes 'resume' on a fresh clone useless, and "
            "this push is the run's only offsite copy of it."
            % (len(dirty), STATE_DIR, "; ".join(x.strip() for x in dirty[:8])
               + (" ..." if len(dirty) > 8 else ""), STATE_DIR))
    else:
        add("swarm-committed", "everything `.swarm-loop/` holds is committed", "PASS",
            "%s/ is clean at HEAD" % STATE_DIR)

    failed = [c for c in conds if c["status"] == "FAIL"]
    unans = [c for c in conds if c["status"] == "UNANSWERABLE"]
    scored = [c for c in conds if c["status"] in ("PASS", "FAIL")]
    # D-1b: an UNANSWERABLE rung blocks exactly like a FAIL. It used not to, and
    # the command then printed "1 mechanical condition(s) this command could NOT
    # answer ... they are not passes." and "the mechanical conditions are MET"
    # one line apart, at exit 0 — so `push-gate --cycle N && git push` spent the
    # unanswered rung as a pass. Two real producers reach it: a state.json that
    # predates the tracked_files key (a run resumed across a skill upgrade) and
    # `git status` itself failing, which is the D-1 rung. The sibling `conflicts`
    # draws the same line for the same reason, and this command's own docstring
    # says which direction is the expensive one: a false PASS.
    blocking = failed + unans

    if args.as_json:
        print(json.dumps({
            "cycle": cycle,
            # Three verdicts, not two: "clear" must never be the word for a gate
            # that could not read one of its own rungs.
            "verdict": ("blocked" if failed else ("unanswerable" if unans else "clear")),
            "push_remote": mode,
            # The count of mechanical rungs is a property of the GATE and does
            # not shrink when one of them cannot be read — it used to be
            # len(scored), so an unanswered rung silently turned "5 of 7" into
            # "4 of 7" and the subset looked complete.
            "mechanical_conditions": len(scored) + len(unans),
            "mechanical_answered": len(scored),
            "unanswered": [c["id"] for c in unans],
            "gate_conditions_total": len(scored) + len(unans) + len(PUSH_GATE_OPERATOR),
            "conditions": conds,
            "operator_checklist": [{"id": i, "condition": c, "why_unmechanizable": w}
                                   for i, c, w in PUSH_GATE_OPERATOR],
        }, indent=2))
        sys.exit(1 if blocking else 0)

    print("push-gate cycle %d (push-remote %s): %d of the gate's conditions are mechanical%s"
          % (cycle, mode, len(scored) + len(unans),
             " and were scored here" if not unans else
             " — %d answered here, %d UNANSWERABLE (an unanswered rung is not a pass)"
             % (len(scored), len(unans))))
    for c in conds:
        if c["status"] == "NOTE":
            print("      note: %s" % _oneline(c["detail"], 200))
            continue
        print("  %-13s %s" % (c["status"], c["condition"]))
        print("      %s" % _oneline(c["detail"], 400))
    # Printed on the PASS path too, and it is the whole point of printing it: a
    # gate that answers a strict subset while looking complete is how a green
    # gets manufactured.
    print("  --- NOT ANSWERED HERE — %d condition(s) only you can score:"
          % len(PUSH_GATE_OPERATOR))
    for _i, cond, why in PUSH_GATE_OPERATOR:
        print("  OPERATOR      %s" % cond)
        print("      %s" % why)
    if unans:
        print("  --- %d mechanical condition(s) this command could NOT answer (above, "
              "UNANSWERABLE) — they are not passes." % len(unans))
    if blocking:
        if failed:
            print("push-gate: DO NOT PUSH — %d condition(s) unmet: %s"
                  % (len(failed), ", ".join(c["id"] for c in failed)))
        if unans:
            print("push-gate: DO NOT PUSH — %d condition(s) UNANSWERED: %s. This gate has "
                  "not cleared: an unanswered rung is not a met one. Each line above says "
                  "how to answer it by hand."
                  % (len(unans), ", ".join(c["id"] for c in unans)))
        print("Name each in the epoch report with why, COMMIT that amendment (`git add %s "
              "&& git commit`) — the report was already committed before this gate ran, so "
              "an uncommitted note re-dirties %s and this gate's last rung fails on the "
              "re-run for a reason you created by obeying this line — then carry the push "
              "to the next epoch's gate. Do not push a subset and do not push anyway."
              % (STATE_DIR, STATE_DIR))
        sys.exit(1)
    print("push-gate: the mechanical conditions are MET. This is not permission to push — "
          "score the operator conditions above first, then push and verify each remote's "
          "SHA (the verify-the-deploy skill), never the command's exit code.")
    sys.exit(0)


OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _merge_tree_pair(root, a, b):
    """(verdict, [detail]) — 'clean' | 'conflict' | 'unknown'. Needs git 2.38+.

    Touches no working tree and needs no worktree, which is why this can be asked
    of a whole batch at once.

    MEASURED, because the documented mapping is wrong in a way that manufactures
    a confident false VETO: `git merge-tree` exits 1 both for a real conflict AND
    for a ref it cannot resolve ("merge-tree: nosuch - not something we can
    merge", stdout EMPTY). Read as the docs describe it, a typo'd branch name
    comes back as a conflict naming zero files. The tell is stdout: a real
    conflict writes the merged tree's OID on line 0 and the conflicted paths
    after it. No OID, no answer. Unrelated histories exit 128 and a git too old
    for --write-tree exits 129; both are 'unknown', never 'clean'.
    """
    r = _git(root, "merge-tree", "--write-tree", "--name-only", "--no-messages", a, b)
    lines = [x for x in (r.stdout or "").splitlines()]
    if r.returncode == 0 and lines and OID_RE.match(lines[0].strip()):
        return "clean", []
    if r.returncode == 1 and lines and OID_RE.match(lines[0].strip()):
        return "conflict", [x.strip() for x in lines[1:] if x.strip()]
    msg = ((r.stderr or "").strip() or (r.stdout or "").strip()
           or "exit %d with no output" % r.returncode)
    return "unknown", ["git merge-tree could not answer (exit %d): %s"
                       % (r.returncode, msg.splitlines()[0])]


def _resolve_branch(root, ref):
    """(resolved_ref, sha) or (None, None). A bare ticket id also tries task/<id>."""
    cands = [ref] if ref.startswith("task/") else [ref, "task/" + ref]
    for cand in cands:
        r = _git(root, "rev-parse", "--verify", "--quiet", cand + "^{commit}")
        sha = (r.stdout or "").strip()
        if r.returncode == 0 and sha:
            return cand, sha
    return None, None


def _branch_files(root, base, ref):
    """Files `ref` changed since it forked from `base`, or None if git failed.

    --no-renames -z -- for the reasons check-branch spells out: default rename
    detection prints only a rename's destination, and a non-ASCII path comes back
    \\-escaped and quoted without -z.
    """
    r = _git(root, "diff", "--no-renames", "--name-only", "-z",
             "%s...%s" % (base, ref), "--")
    if r.returncode != 0:
        return None
    return set(p for p in (r.stdout or "").split("\0") if p.strip())


def cmd_conflicts(args):
    """Pairwise TEXTUAL conflict between task branches, answered by git.

    D-2: `merge-tree` appeared four times in this skill and all four were prose.
    check-wave's ownership veto reasons about DECLARED globs and can only ever be
    as right as the declarations; nothing anywhere compared two branches' actual
    content to each other.

    NOT a pre-dispatch check, and it must never be moved into check-wave. Phase 4
    makes check-wave a precondition of SPAWNING, so at check-wave time every lane
    holds zero commits — measured at exactly that moment this command reports
    "0 comparable branches", and a variant that printed a tidy pairwise verdict
    over empty branches would be read as a green about work that does not exist
    yet. This belongs at Phase 5 / merge-protocol step 3, where every branch
    carries its final commits.

    The ADVISORY verdict is the higher-value half, and it is worth stating
    exactly what it is and is not. It IS `files[a] & files[b]` — branch A's
    touched files intersected with branch B's. It is NOT a comparison against
    check-wave's declared globs: nothing here loads the ticket graph or reads a
    ticket's `owns`/`scope`, and no ticket id appears in the output. This
    docstring and merge-protocol.md both used to claim the glob comparison, and
    the code never did it — the claim was the load-bearing justification for the
    verdict, so it is corrected rather than softened. What ADVISORY actually
    buys, which is real: it is the first check anywhere that compares two
    branches' ACTUAL content to each other rather than to their declarations, so
    a wrong ownership map becomes visible as two branches editing one file. The
    operator does the second half — read both diffs against what those tickets
    declared. Textual mergeability is not agreement.
    """
    root = repo_root()
    base = args.base
    if _resolve_branch(root, base)[0] is None:
        die("base ref %r does not resolve — nothing to compare branches against" % base)

    wanted = []
    for raw in args.branches:
        for b in re.split(r"[,\s]+", raw):
            if b.strip() and b.strip() not in wanted:
                wanted.append(b.strip())

    # ZERO branches is the shortest possible version of this command's whole
    # failure mode. argparse's nargs="+" only guarantees a non-empty ARGV, not a
    # non-empty branch list: `conflicts ""`, `conflicts ,` and `conflicts "  "`
    # all split to nothing, which is exactly what an unset shell variable
    # expands to in `conflicts "$IDS" --base main && git merge ...`. That used to
    # print "0 comparable branch(es)" and exit 0 — a green handed back for
    # comparing nothing at all.
    if not wanted:
        die("the argument list resolved to ZERO branch names (%s) — nothing was compared, "
            "and nothing compared is not a clean batch. An unset or empty shell variable "
            "expanded into this command line is the usual cause. Name the branches."
            % ", ".join(repr(x) for x in args.branches))

    live, skipped, files = [], [], {}
    # Dedupe on the RESOLVED ref, not on the argument spelling. This command
    # advertises that a bare ticket id also resolves task/<id>, so `conflicts
    # T-103 task/T-103` used to put ONE branch into the live set twice and then
    # compare it to itself — printing an ADVISORY that a branch disagrees with
    # itself over the files it touched, a verdict that cannot reflect reality, at
    # exactly the moment the operator is choosing merge order.
    seen = set()
    for b in wanted:
        ref, _sha = _resolve_branch(root, b)
        if ref is None:
            skipped.append((b, "no such branch (nor task/%s) — nothing to compare" % b))
            continue
        if ref in seen:
            continue
        seen.add(ref)
        lane = _lane_commits(root, base, ref)
        if lane is None:
            skipped.append((ref, "git could not count commits against %s" % base))
            continue
        if lane["ahead"] == 0:
            skipped.append((ref, "0 commits ahead of %s — an EMPTY lane. At merge time "
                                 "that is a task that delivered nothing, and a clean "
                                 "answer about it is an answer about nothing" % base))
            continue
        touched = _branch_files(root, base, ref)
        if touched is None:
            # Never substitute an empty set here. Empty makes every pair look
            # like disjoint file sets and prints CLEAN — a confident wrong
            # verdict manufactured out of a failed subprocess.
            skipped.append((ref, "git could not list the files it changed since %s, so "
                                 "whether its file set is disjoint from anything cannot "
                                 "be checked" % base))
            continue
        live.append((ref, lane))
        files[ref] = touched

    pairs = []
    for i, (a, _la) in enumerate(live):
        for b, _lb in live[i + 1:]:
            verdict, detail = _merge_tree_pair(root, a, b)
            shared = sorted(files[a] & files[b])
            if verdict == "conflict":
                pairs.append({"a": a, "b": b, "verdict": "VETO", "files": detail,
                              "shared": shared})
            elif verdict == "unknown":
                pairs.append({"a": a, "b": b, "verdict": "UNKNOWN", "files": [],
                              "shared": shared, "why": detail[0]})
            elif shared:
                pairs.append({"a": a, "b": b, "verdict": "ADVISORY", "files": [],
                              "shared": shared})
            else:
                pairs.append({"a": a, "b": b, "verdict": "CLEAN", "files": [],
                              "shared": []})

    stale = [{"branch": ref, "behind": lane["behind"], "fork": lane["fork"]}
             for ref, lane in live if lane["behind"]]
    veto = [p for p in pairs if p["verdict"] == "VETO"]
    unknown = [p for p in pairs if p["verdict"] == "UNKNOWN"]
    advisory = [p for p in pairs if p["verdict"] == "ADVISORY"]
    clean = [p for p in pairs if p["verdict"] == "CLEAN"]
    # A shorter list read as a clean list is this command's failure mode, so an
    # unanswerable BRANCH is exit 1 exactly like an unanswerable PAIR. At the one
    # place this is meant to run — ordering a collected batch's merges — a
    # missing ref is a typo and an empty lane is a failed task, and neither is a
    # reason to hand back exit 0. Nothing here is on the dispatch path, so a
    # false refusal costs a reread, not a wedged run.
    bad = bool(veto or unknown or skipped)

    if args.as_json:
        print(json.dumps({
            "base": base,
            "comparable": [ref for ref, _l in live],
            "unanswerable": [{"branch": b, "why": w} for b, w in skipped],
            "pairs": pairs,
            "stale_base": stale,
            "verdict": "veto" if veto else ("unknown" if unknown else
                                            ("advisory" if advisory else "clean")),
            "blind_spot": "merge-tree says nothing about base staleness: a branch forked "
                          "27 commits ago merges clean pairwise and still dies at "
                          "`merge --ff-only`. See stale_base.",
        }, indent=2))
        sys.exit(1 if bad else 0)

    print("conflicts: %d comparable branch(es) against base %s" % (len(live), base))
    for b, why in skipped:
        print("  UNANSWERABLE  %s: %s" % (b, why))
    if len(live) < 2:
        print("conflicts: nothing to compare — fewer than two branches carry commits.")
        print("  This is NOT a clean verdict about anything: no pair was formed, so no "
              "pair was judged.")
        sys.exit(1 if bad else 0)

    if veto:
        print("VETO: these branch PAIRS conflict textually — they cannot both land unattended:")
        for p in veto:
            print("  %s <-> %s" % (p["a"], p["b"]))
            for f_ in p["files"]:
                print("      ! %s" % f_)
        print("  Merge them one at a time in dependency order and resolve on the integration")
        print("  branch (merge-protocol step 5), or return the later one to the backlog.")
    for p in unknown:
        print("  UNKNOWN   %s <-> %s: %s" % (p["a"], p["b"], p.get("why")))
        print("      Not a clean answer. Do not read it as one.")
    if advisory:
        print("ADVISORY (merges clean) — the same file changed by BOTH sides:")
        for p in advisory:
            print("  %s <-> %s   via  %s" % (p["a"], p["b"], ", ".join(p["shared"])))
        print("  Textual mergeability is not agreement: two clean hunks in one file are how")
        print("  the ownership map gets contradicted without anything failing. These files")
        print("  were TOUCHED; check-wave only ever saw what the tickets DECLARED.")
    for p in clean:
        print("  CLEAN     %s <-> %s   (disjoint file sets, merges clean)" % (p["a"], p["b"]))

    # The blind spot, printed every run including the all-clean one. A clean
    # pairwise answer is not permission to land: merge-tree is unaffected by a
    # stale base, and both stale branches in the fixture merged clean (exit 0)
    # while `git merge --ff-only` died with exit 128.
    print("  BLIND SPOT: this command compares branches to EACH OTHER, never to the base.")
    print("  A branch forked long ago merges clean pairwise and still fails "
          "`merge --ff-only`;")
    print("  merge-protocol step 9 rebases the batch onto %s first for exactly that reason."
          % base)
    if stale:
        for s in stale:
            print("      STALE BASE  %s: %d commit(s) behind %s (forked at %s)"
                  % (s["branch"], s["behind"], base, s["fork"][:12]))
    else:
        print("      (no branch here is behind %s right now — that can change before you "
              "merge)" % base)
    sys.exit(1 if bad else 0)


def cmd_status(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    # Same summary the resume header prints. Kept inside the JSON object rather
    # than as a banner line after it, so 'status' output stays parseable.
    open_esc = _open_escalations(d)      # [] when the run predates escalations
    esc_blocking = [r["id"] for r in open_esc if r.get("blocking")]
    print(json.dumps({
        "project": state["project"],
        "escalations": (f"{len(open_esc)} OPEN"
                        + (f" ({len(esc_blocking)} BLOCKING) — AWAITING USER DECISION"
                           if esc_blocking else " — awaiting a user decision")
                        + f", see {STATE_DIR}/{ESCALATIONS_MD}"
                        if open_esc else "none open"),
        "escalations_open": [r["id"] for r in open_esc],
        "escalations_blocking": esc_blocking,
        "cycle": f"{state['current_cycle']}/{state['max_cycles']}",
        "audit_loops": f"{state.get('audit_loops_completed', 0)}/{state.get('audit_loops', 0)}"
                       + (" CLEAN" if state.get("clean_audit") else ""),
        "autonomy": state["autonomy"],
        "push_remote": state["push_remote"],
        # "stall" is kept under its original name: a live run may already be
        # reading it. The goal-progress counter is an ADDITION, never a rename.
        "stall": f"{state['stall_count']}/{state['stall_limit']}",
        "stall_codebase": f"{state['stall_count']}/{state['stall_limit']}",
        "stall_goal_progress": (f"{int(state.get('goal_stall_count') or 0)}"
                                f"/{_goal_stall_limit(state)}"),
        # Optional, and ABSENT from every state.json written before the flag
        # existed — hence .get(). Two keys on purpose: the raw value stays
        # machine-readable, and the prose says what a null MEANS, because a bare
        # null reads as "no scratch directories were used" when what it actually
        # means is "nobody recorded whether the lanes shared one". Recorded,
        # never enforced: nothing here sees the inside of a lane's turn.
        "scratch_root": state.get("scratch_root"),
        "lane_scratch": ("%s — one subdirectory per task id (<root>/<task-id>/); RECORDED, "
                         "never enforced: nothing here observes the inside of a lane's turn"
                         % state["scratch_root"] if state.get("scratch_root") else
                         "UNRECORDED — this run named no --scratch-root, so whether each lane "
                         "had its own subdirectory or every agent shared one path is not "
                         "knowable from here"),
        "goals_frozen": goals.get("frozen", False),
        "goal_amendments": int(goals.get("amendments", 0)),
        "last_amended_at": goals.get("last_amended_at"),
        "n_metrics": len(goals.get("metrics", [])),
    }, indent=2))


# ------------------------------------------------------------------ distil
#
# The self-improvement loop's write half. SKILL.md has declared this loop since
# the first version and NOTHING has ever closed it: LEARNED.md carried zero
# entries across every run to date, because the append was a paragraph and
# paragraphs in this skill have a measured 100% failure rate at being followed.
# `_learning_lines` made the omission visible in `resume`/`checkpoint`; this
# makes it mechanical.
#
# TWO TIERS, and the difference between them is blast radius, not ceremony:
#
#   default          -> .swarm-loop/learnings.md   ONE repo, this run. Routine.
#   --to-skill       -> <skill>/LEARNED.md         EVERY future run on this
#                                                  machine. Validated, capped,
#                                                  compare-and-swapped, locked.
#
# The safe tier is the DEFAULT on purpose: a mistyped or forgotten flag writes
# the entry into the run's own file, which is recoverable, instead of into
# machine-wide memory, which is not. Each invocation writes exactly ONE file —
# there is no both-tiers mode, so there is no partial-success state to reason
# about when the validator refuses.
#
# The validation IS the feature. A writer that accepts any string is the defect
# class this whole skill is a catalogue of: a mechanism that emits a confident
# result and has no red path. LEARNED.md's own header says an overgrown,
# project-specific memory file "degrades every future run more than it helps" —
# so every rule that header states is enforced below, with a refusal, and the
# refusal names the offending token rather than editorializing.

# Project-specificity, by the signals that are unambiguous rather than by taste.
# Every one of these is a thing that cannot be true of a durable orchestration
# lesson: a lesson about wave sizing does not need a home directory in it.
_DL_PATHY = re.compile(r"(?:/Users/|/home/|/opt/|/etc/|/var/|~/|\b[A-Za-z]:\\)")
# `sh` is deliberately NOT in this alternation. It is a real TLD and it is also
# the extension of every shell script in this repository, so including it made
# "run the gate with tests/run.sh" refuse as "a URL, host or remote: 'run.sh'" —
# a refusal whose stated reason is false, which sends the author to fix a thing
# that is not there. A matcher that names the wrong rule is worse than a missing
# rule, because the author cannot tell it is wrong from the message.
_DL_URLY = re.compile(r"(?:https?://|git@[\w.-]+:"
                      r"|\b[\w-]+\.(?:com|net|org|io|ai|dev|app|co|cloud|local)\b)", re.I)
# An interior capital is what product and service names look like: ClearView,
# ProxyShop, DynamoDB, GitHub. The fix is always to name the ROLE ("the shared
# database", "the remote") instead of the brand, and that rewrite makes the entry
# MORE durable, which is the whole point of the file.
_DL_PROPER = re.compile(r"\b[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")
# ...with one exception class: the language, runtime or platform the orchestrator
# itself runs on is technical vocabulary, not a project fact, and "name the ROLE
# it played" is not advice anyone can act on for an operating system. These are
# spelled with an interior capital by convention and by nobody's choice.
_DL_PROPER_OK = ("macos", "javascript", "typescript", "openssh", "openssl")
# Ticket / defect / cycle identifiers: the canonical project trivia.
_DL_TICKET = re.compile(r"\b([A-Z]{1,4})-\d{1,5}\b")
# ...but the same shape spells every standard an orchestration lesson may
# legitimately name. "Decode every captured log as UTF-8" was refused as "a
# ticket or defect identifier: 'UTF-8'"; so were SHA-256, RFC-2119, HTTP-2 and
# GPT-4. A project key ("T-036", "PROJ-14") is still refused — that is the rule
# — but an encoding, a digest, a wire protocol or a model generation is not one.
_DL_TICKET_OK = ("utf", "sha", "aes", "rsa", "rfc", "http", "iso", "ieee",
                 "gpt", "tls", "ssl", "sse", "pep", "ecma", "ipv", "utc",
                 "usb", "sql", "crc", "arm", "cve")

# "Imperative" enforced the only way a regex can — and enforced to mean what the
# refusal SAYS it means. The original list refused "Never …" and "Always …",
# which are the two most common shapes a real rule takes, with the message "that
# is a report about what happened". Measured against ground truth it refused 13
# of the 20 entries in this skill's own hand-curated archive and 6 of the 8 the
# first real run ever wrote. A gate that refuses the correct form does not teach
# the form; it teaches writing around the gate, which is worse than no gate. So:
#
#   * an article or pronoun opener is a report ONLY IF the sentence also
#     NARRATES — "the merge queue WAS slower than expected" reports; "a shared
#     scratchpad path is not lane isolation" states a standing rule.
#   * "Never …"/"Always …" open an instruction, full stop.
#   * "When …"/"If …"/"Every …" open a clause, and are judged by what the clause
#     ARRIVES at: an instruction after the comma, or a deontic ("must", "should").
_DL_NOT_IMPERATIVE = ("the", "a", "an", "this", "that", "it", "we", "i", "they",
                      "there", "our", "its", "his", "her", "because",
                      "never", "always")
# Past-tense narration: the actual signal the refusal message names. Kept
# deliberately small and high-precision — a missed report costs one weak entry,
# a false hit costs a correct lesson its author was told to mangle.
_DL_NARRATES = re.compile(r"\b(?:was|were|wasn't|weren't|had|did|didn't|got|"
                          r"became|went|took|spent|used\s+to|turned\s+out|"
                          r"ended\s+up|has\s+been|have\s+been)\b", re.I)
# Adverbs that open a canonical imperative. "Never merge an unrebased branch" IS
# the verb a future run should obey. Exactly two members, and both are earning
# their place: they are the only openers that appear on the report list above AND
# open an instruction, so this is what stops "Never dispatch a lane whose base
# WAS rewritten" — an instruction that happens to name a past failure inside
# itself — from being read as narration. Anything else imperative ("Prefer …",
# "Stop …") is not on any list and needs no entry here; adding one would be an
# allowlist member no input can reach.
_DL_IMPERATIVE_ADVERB = ("never", "always")
# Subordinators and quantifiers. Not imperative on their own, not reports either:
# judged by the main clause below.
_DL_CLAUSE_OPENER = ("when", "if", "after", "before", "during", "once", "unless",
                     "while", "whenever", "wherever", "every", "each", "any",
                     "all", "no", "most", "some", "one", "two", "three")
_DL_DEONTIC = re.compile(r"\b(?:must|should|shall|cannot|can't|may\s+not|"
                         r"has\s+to|have\s+to|needs?\s+to|is\s+not|are\s+not|"
                         r"never|always)\b", re.I)
# Openers whose -ed/-ing spelling is NOT a participle. Two classes, both of them
# false refusals the -ed/-ing test produces on its own: imperative verbs spelled
# that way (Feed, Seed, Embed, Bring, String) and pronouns and adverbs spelled
# that way — "Nothing calls this is a defect only when a frozen test passes
# because of it" is a real entry in this file, refused as "a participle".
# ("read" and "spread" were in this list and were dead: both end in "ad", so the
# participle test never reached them.)
_DL_IMPERATIVE_OK = ("feed", "seed", "need", "embed", "shed", "shred",
                     "proceed", "exceed", "succeed", "speed", "breed",
                     "bleed", "bring", "ring", "sing", "string", "cling", "fling",
                     "swing", "wring",
                     "nothing", "something", "anything", "everything",
                     "instead", "indeed", "ahead")

_DL_LESSON_MIN, _DL_LESSON_MAX = 20, 400
_DL_BECAUSE_MIN, _DL_BECAUSE_MAX = 30, 900


def _dl_norm(s):
    """One line, no control characters, no leading bullet. Entries are counted by
    a line prefix, so an entry containing a newline would be counted twice and
    would break every future count including its own capacity gate.

    Vertical tab and form feed are turned into a space like every other control
    character, not dropped: the filter used to strip them BEFORE the map could
    replace them, which silently welded the words on either side together
    ('cap\\x0bwave' -> 'capwave') while the docstring's promise still held."""
    s = "".join(" " if ch in "\t\r\n\x0b\x0c" else ch
                for ch in (s or "") if ch >= " " or ch in "\t\r\n\x0b\x0c")
    s = " ".join(s.split())
    while s[:2] in ("- ", "* ") or s[:1] == "#":
        s = s.lstrip("-*# ").strip()
    return s


def _dl_wrap(text, width=94, indent="  "):
    """Wrap to the shape the archived LEARNED.md used: one leading '- ' line,
    continuations indented two spaces so only the first line is ever counted."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if len(cand) > width and cur:
            lines.append(cur)
            cur = indent + w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _dl_project_hits(field, value):
    """[(rule, offending token)] — every project-specific signal in one field.

    Every hit must be TRUE of the token it names. A refusal that reports "a
    ticket or defect identifier: 'UTF-8'" is not a strict gate, it is a wrong
    answer delivered with a citation, and the author it is aimed at cannot fix
    the thing it names because the thing is not there. So the two matchers whose
    shape collides with ordinary technical vocabulary carry an explicit
    exception list, and the exception is checked against the token the matcher
    actually captured rather than against the sentence.
    """
    hits = []
    for rule, rx in (("a machine or repo path", _DL_PATHY),
                     ("a URL, host or remote", _DL_URLY),
                     ("a product / service / API name (interior capital)", _DL_PROPER),
                     ("a ticket or defect identifier", _DL_TICKET)):
        for m in rx.finditer(value):
            tok = m.group(0)
            if rx is _DL_TICKET and m.group(1).lower() in _DL_TICKET_OK:
                continue                       # UTF-8, SHA-256, RFC-2119, GPT-4
            if rx is _DL_PROPER and tok.lower() in _DL_PROPER_OK:
                continue                       # macOS, JavaScript
            hits.append((field, rule, tok))
            break
    return hits


def _dl_opener(s):
    """The first word of `s`, stripped of markdown punctuation, lowercased."""
    w = (s.strip().split(" ", 1)[0] if s and s.strip() else "").strip("*_`\"'(),.:;—-").lower()
    return w if w.isalpha() else ""


def _dl_is_participle(w):
    """True when `w` is an -ed/-ing form that is not an imperative or a pronoun."""
    return bool(w) and w not in _DL_IMPERATIVE_OK and (w.endswith("ing") or w.endswith("ed"))


def _dl_imperative_fault(lesson):
    """None when --lesson reads as a rule, else the one reason it does not.

    The rule LEARNED.md states about itself is "write entries imperative and
    specific"; the thing this refusal exists to stop is an entry that NARRATES —
    "the merge queue was slower than expected all cycle" — because a future run
    cannot obey a story. Four openings, four different questions:

      "Never …" / "Always …"   an instruction. Accepted outright; refusing these
                               was the original defect, and it refused them with
                               the message "that is a report about what happened",
                               which is false of every one of them.
      "When …" / "Every …"     a clause. Accepted if it ARRIVES somewhere — an
                               instruction after the comma ("When a worker goes
                               quiet, probe it"), or a deontic ("Every ticket
                               must carry a machine-checkable goal").
      "The …" / "A …"          a subject. Refused only when the sentence also
                               narrates; a standing claim ("A shared scratchpad
                               path is not lane isolation") is a rule, and four
                               of the eight entries the first real run wrote open
                               exactly this way.
      "Capping …"              a participle. Still refused — it is the one
                               opening that is reliably a description of a
                               practice rather than an instruction to follow it.
    """
    first = _dl_opener(lesson)
    if not first:
        return None
    if first in _DL_IMPERATIVE_ADVERB:
        return None
    if first in _DL_NOT_IMPERATIVE:
        if _DL_NARRATES.search(lesson):
            return ("--lesson opens with %r and narrates in the past tense — that is a report "
                    "about what happened. Open with the verb a future run should obey" % first)
        return None
    if first in _DL_CLAUSE_OPENER:
        main = lesson.partition(",")[2]
        m = _dl_opener(main)
        # The main clause is judged by the same two questions as a top-level
        # opening, so "When X, the persisted copies become stale" (a standing
        # claim) passes and "When the queue was slow, the wave stalled" (a story)
        # does not.
        if m and not _dl_is_participle(m) and not (m in _DL_NOT_IMPERATIVE
                                                   and _DL_NARRATES.search(lesson)):
            return None
        if _DL_DEONTIC.search(lesson):
            return None
        return ("--lesson opens with %r and never reaches an instruction — a clause is a rule "
                "only once it says what to DO ('When a worker goes quiet, probe it with a "
                "counter') or what MUST hold ('Every ticket must carry a frozen goal'). As "
                "written it is a report" % first)
    if _dl_is_participle(first):
        return ("--lesson opens with %r — a participle, not an imperative. Open with "
                "the verb a future run should obey" % first)
    return None


def _dl_validate(lesson, because, existing):
    """[] when the entry may enter machine-wide memory, else the reasons it may not.

    Enforces exactly the rules LEARNED.md's own header states, in its own order:
    durable and project-agnostic; imperative and specific; carrying the failure
    that motivated it; not a duplicate of something already there.
    """
    bad = []
    for field, value, lo, hi in (("--lesson", lesson, _DL_LESSON_MIN, _DL_LESSON_MAX),
                                 ("--because", because, _DL_BECAUSE_MIN, _DL_BECAUSE_MAX)):
        if len(value) < lo:
            bad.append("%s is %d characters — under %d it is a stub, not a lesson a future "
                       "run could judge the applicability of" % (field, len(value), lo))
        elif len(value) > hi:
            bad.append("%s is %d characters — over %d belongs in the final report, not in a "
                       "file every future run reads in full" % (field, len(value), hi))
        bad.extend("%s contains %s: %r — LEARNED.md holds orchestration lessons, not project "
                   "facts. Name the ROLE it played, not the name it had"
                   % (f, rule, tok) for f, rule, tok in _dl_project_hits(field, value))

    fault = _dl_imperative_fault(lesson)
    if fault:
        bad.append(fault)

    key = re.sub(r"[^a-z0-9]+", "", lesson.lower())
    if key:
        for ln in existing:
            if key in re.sub(r"[^a-z0-9]+", "", ln.lower()):
                bad.append("this lesson is ALREADY in the file — LEARNED.md's curation rule is "
                           "to merge duplicates, not to add one: %s" % _oneline(ln, 120))
                break
    return bad


_DL_STOP = frozenset("""
    a an and are as at be been but by can for from had has have if in into is it
    its not of on one only or our over so than that the their them then there
    these they this those to two under until up was were what when where which
    while who why will with would you your every each any all no most some three
    do does did done doing about after before during other same such very just
    also more much many both few less least than because since until yet still
    """.split())


def _dl_provenance(run_text, lesson):
    """-> (n_present, [absent words]) for the lesson's distinctive words.

    (None, []) when the lesson carries too few distinctive words to judge, which
    is not a licence: the length floor in `_dl_validate` is what catches those.

    This is the whole content of the provenance gate. It cannot prove the run
    file's claim was TRUE — no mechanism here can — but it can prove the promoted
    sentence has some relationship to what the run wrote down, which is the one
    thing the previous line-count could not distinguish and the one thing the
    gate's own comment claimed it did.
    """
    run_words = set(re.findall(r"[a-z0-9]+", run_text.lower()))
    words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]+", lesson.lower()))
             if len(w) >= 4 and w not in _DL_STOP]
    if len(words) < 3:
        return None, []
    missed = [w for w in words if w not in run_words]
    return len(words) - len(missed), missed


class _learned_lock(object):
    """O_EXCL lockfile beside LEARNED.md, held across read-verify-append.

    Deliberately NOT `_state_lock`'s policy. That lock proceeds with a warning
    after its timeout, which is right for state.json — refusing to checkpoint
    because a SIGKILLed holder left a lockfile would be worse than the race. Here
    the opposite holds: the loser of a race is at Termination, the cost of a
    refusal is one re-run of one command, and the cost of proceeding is a lost
    update to memory shared by every future run on the machine. So it REFUSES.

    A stale lock is still broken after `stale` seconds, or a killed run would
    wedge the file permanently.
    """

    # 5s is ~500x the time a real append holds this (read 2 KB, validate, write,
    # fsync); a wait longer than that is a holder that is not coming back.
    def __init__(self, path, timeout=5.0, stale=120.0):
        self.path, self.timeout, self.stale, self.held = path + ".lock", timeout, stale, False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, ("%d\n" % os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue
                if age > self.stale:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    die("another process has held %s for %ds — it is mid-write on the skill's "
                        "shared memory and NOTHING was written here. Re-run distil once it is "
                        "done; if that lockfile is an orphan it self-clears after %ds"
                        % (self.path, int(age), int(self.stale)))
                time.sleep(0.2)
            except OSError as e:
                die("cannot lock %s (%s) — refusing to append to machine-wide memory unlocked"
                    % (self.path, e))

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def _dl_append(path, seen, payload):
    """Append `payload` iff the file is still byte-for-byte `seen`. -> None or dies.

    Compare-and-swap, not a rewrite. `seen` is what the capacity count and the
    duplicate check were computed against; if the file moved under us — a second
    run's distil, an operator editing it in a buffer, an editor's truncate-then-
    write caught mid-flight — those two answers are stale and the append would
    write past a capacity gate that no longer holds. O_APPEND is what makes the
    write itself atomic against another appender, and never truncating is what
    makes 'never rewrite or reorder existing entries' a property of the code
    rather than a promise in a docstring.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as e:
        die("cannot open %s for append (%s) — nothing was written" % (path, e))
    try:
        now = open(path, "rb").read()
        if now != seen:
            die("%s changed while this command was validating (%d -> %d bytes). NOTHING was "
                "written: the entry count and duplicate check this append relied on are stale. "
                "Re-run distil." % (path, len(seen), len(now)))
        if now and not now.endswith(b"\n"):
            payload = b"\n" + payload
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_distil(args):
    """Close the self-improvement loop: record a lesson in the run, or promote one.

    Exit 0 wrote something (or --dry-run validated it), 1 refused, 3 is usage.
    Never 2: an orchestrator reads 2 as a terminal signal, and a full or picky
    memory file is not a reason to end a run.
    """
    root = repo_root()
    d = sd(root)
    lpath = os.path.join(d, LEARNINGS_MD)
    learned = _learned_path()

    if args.do_list:
        for line in _learning_lines(d):
            print("self-improvement: " + line)
        print("run file : %s" % lpath)
        print("skill mem: %s" % learned)
        return

    missing = [n for n, v in (("--lesson", args.lesson), ("--because", args.because))
               if not str(v or "").strip()]
    if missing:
        # --because is required, not optional prose: "with the failure that
        # motivated them" is a rule LEARNED.md states, and a required argument is
        # the only spelling of that rule this repo has ever seen kept.
        die("distil needs " + " ".join(missing) + " — a lesson without the failure that "
            "motivated it cannot be judged for applicability by the run that reads it next. "
            "Say what to do, and what went wrong that makes it worth doing. (Or --list.)", 3)
    lesson, because = _dl_norm(args.lesson), _dl_norm(args.because)
    if not lesson or not because:
        # Non-empty as ARGUMENTS but empty as CONTENT — "- - -", "###". Caught
        # here rather than written as an entry with an empty bold span, because
        # tier 1 does no other validation and this is the one shape that would
        # corrupt the file's own line-prefix accounting.
        die("--lesson and --because are empty once markdown punctuation is stripped — there is "
            "no lesson here to record", 3)

    # ---------------------------------------------------------------- tier 1
    if not args.to_skill:
        state = {}
        spath = os.path.join(d, STATE_FILE)
        if os.path.exists(spath):
            try:
                with open(spath) as f:
                    state = json.load(f)
            except (ValueError, OSError):
                state = {}
        cycle = args.cycle if args.cycle is not None else int(state.get("current_cycle", 0) or 0)
        entry = _dl_wrap("- (cycle %d) **%s** — %s" % (cycle, lesson, because))
        if args.dry_run:
            print("distil --dry-run: would append to %s/%s\n%s" % (STATE_DIR, LEARNINGS_MD, entry))
            return
        os.makedirs(d, exist_ok=True)
        try:
            prior = open(lpath, "rb").read() if os.path.exists(lpath) else b""
        except OSError as e:
            die("cannot read %s/%s (%s) — nothing was written" % (STATE_DIR, LEARNINGS_MD, e))
        lead = "" if not prior else ("\n" if prior.endswith(b"\n") else "\n\n")
        with open(lpath, "a") as f:
            f.write(lead + entry + "\n")
        n = sum(1 for ln in open(lpath) if ln.startswith("- "))
        print("distil: appended to %s/%s (%d entr(ies))" % (STATE_DIR, LEARNINGS_MD, n))
        print(entry)
        if n > LEARNINGS_SOFT_CAP:
            # A warning, never a refusal, and the asymmetry with --to-skill is
            # deliberate: this file is one repo's, it is curated by hand every
            # epoch, and refusing to record a lesson mid-run loses the lesson.
            print("distil: %s/%s now holds %d entries, over SKILL.md Phase 8's ~%d — curate it "
                  "at this epoch boundary (merge duplicates, drop the least useful)."
                  % (STATE_DIR, LEARNINGS_MD, n, LEARNINGS_SOFT_CAP))
        return

    # ---------------------------------------------------------------- tier 2
    # Provenance: distillation COMPRESSES something the run recorded. The first
    # version of this gate counted non-blank lines in the run file and never
    # compared the promoted lesson to a byte of it, so a run file containing the
    # single character `x` licensed any sentence at all — vacuous at exactly the
    # thing it exists to do. It now measures the overlap it claims to.
    try:
        run_text = open(lpath).read() if os.path.exists(lpath) else ""
    except OSError:
        run_text = ""
    if not run_text.strip():
        die("%s/%s is empty or absent — there is nothing to distil FROM, so this entry "
            "would be invented rather than learned. Record it in the run first:\n"
            "    swarmloop.py distil --lesson \"%s\" --because \"...\""
            % (STATE_DIR, LEARNINGS_MD, _oneline(lesson, 60)))
    kept, missed = _dl_provenance(run_text, lesson)
    if kept is not None and kept * 2 < kept + len(missed):
        die("%s/%s does not contain this lesson — %d of its %d distinctive words appear "
            "nowhere in what the run recorded, starting with %s. Distillation compresses "
            "what a run learned; a lesson that shares no vocabulary with the run file was "
            "written at Termination, not learned during it. Record it in the run first:\n"
            "    swarmloop.py distil --lesson \"%s\" --because \"...\"\n"
            "  (This measures textual overlap, which proves the lesson EXISTED as a run "
            "artifact. It cannot prove the run artifact was true — nothing here can.)"
            % (STATE_DIR, LEARNINGS_MD, len(missed), kept + len(missed),
               ", ".join(repr(w) for w in missed[:5]), _oneline(lesson, 60)))

    if not os.path.exists(learned):
        die("%s not found at %s — this script is running from a copy outside the skill, so "
            "there is no cross-run memory to append to. Put the distilled lessons in the final "
            "report under 'proposed skill updates' instead." % (LEARNED_MD, learned))

    with _learned_lock(learned):
        try:
            seen = open(learned, "rb").read()
        except OSError as e:
            die("%s is unreadable (%s) — nothing was written" % (learned, e))
        text = seen.decode("utf-8", "replace")
        entries, marked = _learned_entries(text)
        if not marked:
            die("%s has no '%s' section — refusing to append into a file whose entry "
                "region cannot be located. Restore the marker; every count this command makes "
                "(capacity, duplicates) is measured below it."
                % (learned, LEARNED_ENTRIES_MARKER))

        # The capacity gate LEARNED.md writes for itself, enforced verbatim: at
        # the cap this REFUSES, and it does not trim to make room. A full
        # learnings file means the skill has a backlog, and silently deleting the
        # oldest entry would hide exactly the signal that says so.
        if len(entries) >= LEARNED_CAP:
            bar = "!" * 74
            print(bar)
            print("!!! %s IS FULL — %d entries (cap %d). NOTHING WAS WRITTEN."
                  % (LEARNED_MD, len(entries), LEARNED_CAP))
            print(bar)
            print("  This is not a trim-and-continue. A full learnings file means the SKILL has")
            print("  a backlog: run an improvement pass that promotes every encodable entry into")
            print("  SKILL.md, the references, or swarmloop.py itself, then curate what remains,")
            print("  before further runs. Tell the user that, in your next message to them.")
            print("  The lesson this command refused is NOT lost — put it in the final report")
            print("  under 'proposed skill updates':")
            print(_dl_wrap("- **%s** — %s" % (lesson, because)))
            sys.exit(1)

        bad = _dl_validate(lesson, because, entries)
        if bad:
            print("distil: REFUSED — this entry may not enter %s. Nothing was written."
                  % LEARNED_MD, file=sys.stderr)
            for b in bad:
                print("  - " + b, file=sys.stderr)
            print("  LEARNED.md is read in full by every future run on this machine; an entry "
                  "that is project-specific, vague or duplicated costs every one of them.",
                  file=sys.stderr)
            sys.exit(1)

        entry = _dl_wrap("- **%s** — %s · distilled %s"
                         % (lesson, because, time.strftime("%Y-%m-%d")))
        if args.dry_run:
            print("distil --dry-run: %s VALIDATED, %d/%d entries in %s, nothing written\n%s"
                  % (LEARNED_MD, len(entries), LEARNED_CAP, learned, entry))
            return
        _dl_append(learned, seen, ("\n" + entry + "\n").encode("utf-8"))
        total = len(entries) + 1
        print("distil: appended to %s (%d/%d entries)" % (learned, total, LEARNED_CAP))
        print(entry)
        if total >= LEARNED_CAP:
            # LEARNED.md aims this at "the agent whose append makes this file 40
            # entries" — this one. The append stands; the next is refused above.
            bar = "!" * 74
            print(bar)
            print("!!! THAT APPEND FILLED %s — %d/%d. The next one will be REFUSED."
                  % (LEARNED_MD, total, LEARNED_CAP))
            print(bar)
            print("  STOP AND TELL THE USER in your next message: the skill needs an improvement")
            print("  pass that promotes every encodable entry into SKILL.md, the references or")
            print("  swarmloop.py, and curates what remains, before further runs.")


# ------------------------------------------------- wiring check (reachable)
#
# The incident: two commits recovered from a killed agent, both claiming
# progress in prose. Commit A added a function with ZERO call sites, absent from
# both __all__ lists, carrying docstrings that asserted a behaviour existing
# nowhere — 0% effective, and the suite was GREEN over a fully live
# HIGH-severity defect. Commit B's function was wired at two independent points
# on the real path and its claim held exactly. The commit messages did not
# distinguish them; ten seconds of call-site grep did.
#
# So the check runs on every preserved commit REGARDLESS of what the message
# claims, IN EITHER DIRECTION. An earlier reading of the same incident — that
# self-assessed progress is biased generous — was drawn from n=1 and falsified
# by commit B in the opposite direction. The discriminator was never the prose,
# which is exactly why this can be a command instead of a habit.
#
# The question, verbatim: is the new function reachable from the real entry
# point, exported, and on the path the user's command actually takes?
#
# Where it errs it errs toward the FALSE ALARM. getattr, a registry, a decorator
# table or a template can hide a real call site; calling such a symbol unwired
# costs a human ten seconds, calling an unwired one wired costs what the
# incident cost. Every blind spot is printed WITH the verdicts (_reach_limits)
# rather than left for the reader to already know about — a clean report from
# this command is not proof of wiring and must never be quoted as one.
#
# There is a THIRD error, and the first version of this command committed it: a
# confident verdict about a hop it never took. Measured read-only against a live
# FastAPI project, 24.5% of public top-level functions came back not-wired, and
# 20 of the 31 "NO CALL SITES" — 'defined, and NOTHING references it' — were
# route handlers registered by @router.get/@router.post, i.e. the most-wired code
# in the repo. Overclaiming the completeness of its own analysis is the same
# defect this command was built to catch, committed by the command itself; at
# that rate a reader stops believing the one verdict it exists to produce. So a
# registering decorator on the definition now gets its own verdict, its own line
# naming the decorator and where it sits, and a diagnosis that says the table on
# the other side was NOT followed. Named cause beats small number.

REACH_SKIP_DIRS = frozenset((
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    "build", "dist", ".eggs",
))

# How many definitions / call sites / hints are enumerated before the list is
# elided. The COUNT on the header line is always exact; only the listing caps.
REACH_LIST_CAP = 8

# Decorators that WRAP a definition in place and register it with nothing. A
# symbol carrying only these and no callers is still the incident's shape, so it
# keeps NO CALL SITES. Everything else — @router.post, @app.task, @click.command,
# @pytest.fixture, @foo.register, and every decorator this list has never heard
# of — is treated as a POSSIBLE registration, because decorator registration is
# not an edge case in Python: it is how routes, CLI commands, event handlers,
# plugins and fixtures are wired. Matching is on the WHOLE dotted label, never
# the last component, so `@app.cache` is not mistaken for `@functools.cache`.
REACH_INERT_DECORATORS = frozenset((
    "staticmethod", "classmethod", "property", "cached_property",
    "functools.cached_property",
    "abstractmethod", "abstractproperty", "abc.abstractmethod",
    "abc.abstractproperty", "abc.abstractclassmethod", "abc.abstractstaticmethod",
    "wraps", "functools.wraps",
    "lru_cache", "functools.lru_cache", "cache", "functools.cache",
    "total_ordering", "functools.total_ordering",
    "contextmanager", "contextlib.contextmanager",
    "asynccontextmanager", "contextlib.asynccontextmanager",
    "overload", "typing.overload", "final", "typing.final",
    "no_type_check", "typing.no_type_check",
    "runtime_checkable", "typing.runtime_checkable",
    "dataclass", "dataclasses.dataclass",
))

# What a decorator this scanner cannot render down to a dotted name is called in
# the report. It counts as a POSSIBLE registration for the same reason: an
# expression we could not read is not an expression we know to be inert.
REACH_DEC_OPAQUE = "@<expression>"

# Identifier scan used to find a wanted name INSIDE a longer string literal.
REACH_WORD_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _reach_body(node):
    """A module/class/function body with its DOCSTRING dropped.

    A docstring is documentation, not code, and this scanner reads string
    literals looking for hidden call sites. Without this, every `Public
    surface::` block that shows `accept_offer(auction=...)` as a usage example
    raises a hint that a call site might be hiding in it — measured, that was two
    thirds of the hints on a live repo, which is how a hint stops being read."""
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def _reach_dec_label(node):
    """A decorator expression -> '@router.post', or REACH_DEC_OPAQUE.

    Rendered by its CALLEE only: `@router.post("/x", status_code=201)` reads
    `@router.post`, because which table it registers into is the question and
    the arguments are not."""
    n = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if not isinstance(n, ast.Name):
        return REACH_DEC_OPAQUE
    parts.append(n.id)
    return "@" + ".".join(reversed(parts))


def _reach_is_test(relf):
    """True for a file whose greens are about the test, not about the product.

    A symbol every one of whose callers is a test is the sibling of the
    zero-caller case and reads identically in a suite: it IS exercised, so
    something goes green over it, and nothing the user runs ever reaches it."""
    parts = relf.replace(os.sep, "/").split("/")
    base = parts[-1]
    if base == "conftest.py" or base.startswith("test_") or base.endswith("_test.py"):
        return True
    if parts[:2] == [STATE_DIR, "acceptance"]:
        return True
    return any(p in ("tests", "test", "testing", "__tests__", "spec", "specs")
               for p in parts[:-1])


def _reach_files(root, paths):
    """(files, unresolved specs) — the .py files to parse, sorted and deduped.

    A directory named in --path is scanned even when REACH_SKIP_DIRS would have
    skipped it during a walk: the skip list exists to keep a vendored copy of
    the world out of an unqualified repo-root scan, not to refuse an explicit
    instruction to look somewhere.

    A spec that names nothing this scan can read comes back UNRESOLVED rather
    than being dropped. Silently ignoring it produced a confident NO CALL SITES
    on genuinely wired code whenever some OTHER --path happened to find files,
    while the header line went on asserting the typo'd path had been scanned."""
    out, unresolved = [], []
    for spec in paths:
        base = spec if os.path.isabs(spec) else os.path.join(root, spec)
        base = os.path.normpath(base)
        if os.path.isfile(base):
            if base.endswith(".py"):
                out.append(base)
            else:
                unresolved.append(spec)
            continue
        if not os.path.isdir(base):
            unresolved.append(spec)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames if x not in REACH_SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
    return sorted(set(out)), unresolved


def _reach_scan(root, paths, wanted):
    """One ast pass over the tree -> the tables the wiring check reads.

    Same machinery and same failure policy as the read-edge scanner: a file that
    does not parse is RECORDED and skipped, never fatal. Unlike that scanner an
    unparsed file here is a hole in the ANSWER — its call sites are invisible —
    so it is printed next to the verdicts and not only in --json."""
    res = {
        "files": 0, "unparsed": [], "unresolved_paths": [],
        "defs": {}, "refs": {}, "imports": {}, "exports": {}, "strings": {},
        "edges": {}, "children": {}, "modules": {},
    }
    files, res["unresolved_paths"] = _reach_files(root, paths)
    res["files"] = len(files)
    wanted = set(wanted)

    # PRE-PASS: `from m import foo as _foo` binds a wanted symbol under a name
    # this scan would otherwise never look for, so `_foo()` counted as nothing.
    # That failed in the FALSE-ALARM direction and it fired for real: measured on
    # a live repo, one symbol's ONLY consumption path was that idiom — called
    # from four packages' __init__ as `_bind_submodules` — and it reported NO
    # CALL SITES, the verdict whose text is 'defined, and NOTHING references it'.
    # The map is per-FILE, because an alias binds only in the module that spells
    # it; a repo-wide map would let one file's rename over-count another's.
    alias_by_file = {}
    for path in files:
        try:
            with open(path) as f:
                src = f.read()
        except OSError:
            continue
        # An ImportFrom carrying an asname must contain both words. The guard is
        # here so the second parse below is paid only by files that could
        # possibly matter, not by every file in the tree.
        if " as " not in src or "import" not in src:
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue          # the main pass records it as unparsed
        m = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for al in n.names:
                    if al.asname and al.asname != al.name and al.name in wanted:
                        m[al.asname] = al.name
        if m:
            alias_by_file[os.path.relpath(path, root)] = m

    for path in files:
        relf = os.path.relpath(path, root)
        try:
            with open(path) as f:
                tree = ast.parse(f.read(), filename=relf)
        except (OSError, SyntaxError, ValueError) as e:
            res["unparsed"].append((relf, str(e)[:120]))
            continue
        # A dotted label, not an importable module path: --path may point
        # outside the repo, and the `..` segments of that relpath would otherwise
        # render every holder as `............opt.lib.foo`.
        mod = ".".join(p for p in relf[:-3].replace(os.sep, "/").split("/")
                       if p not in ("", ".", ".."))
        is_test = _reach_is_test(relf)
        is_init = os.path.basename(relf) == "__init__.py"
        res["modules"][mod] = relf

        def add_def(name, qual, lineno, kind, parent, decs=(), _f=relf, _t=is_test):
            res["defs"].setdefault(name, []).append(
                {"file": _f, "line": lineno, "kind": kind, "qual": qual, "test": _t,
                 "decorators": list(decs),
                 "registering": any(d["label"] == REACH_DEC_OPAQUE
                                    or d["label"][1:] not in REACH_INERT_DECORATORS
                                    for d in decs)})
            res["children"].setdefault(parent, []).append((qual, kind))

        aliases = alias_by_file.get(relf, {})

        def add_ref(name, how, lineno, holder, _f=relf, _t=is_test, _al=aliases):
            res["edges"].setdefault(holder, set()).add(name)
            real = _al.get(name)
            if real is not None:
                # The closure has to follow the alias too, or a symbol reached
                # only under its local name reads UNREACHED for the same reason
                # it used to read NO CALL SITES.
                res["edges"][holder].add(real)
            key = real if real is not None else name
            if key in wanted:
                res["refs"].setdefault(key, []).append(
                    {"file": _f, "line": lineno, "how": how, "holder": holder,
                     "test": _t, "alias": (name if real is not None else None)})

        def add_export(name, lineno, via, _f=relf):
            res["exports"].setdefault(name, []).append(
                {"file": _f, "line": lineno, "via": via})

        def add_import(n, _f=relf, _t=is_test, _i=is_init):
            # Only `from M import S` binds a SYMBOL. `import M` binds a module,
            # and the S in `M.S` arrives below as an attribute reference — so
            # recording it here too would double-count one site.
            base = ("." * (n.level or 0)) + (n.module or "")
            for al in n.names:
                if al.name == "*":
                    continue
                res["imports"].setdefault(al.name, []).append(
                    {"file": _f, "line": n.lineno, "module": base,
                     "as": al.asname or al.name, "test": _t})
                if _i:
                    # A package __init__ re-export is a public surface even with
                    # no __all__: `from pkg import S` resolves through it.
                    add_export(al.name, n.lineno, "package __init__")

        def visit(n, holder, _mod=mod):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(n, ast.ClassDef) else "function"
                qual = "%s.%s" % (holder, n.name)
                # The decorators ON the definition are recorded WITH it. The
                # scanner used to visit this list and then throw it away, so a
                # decorator-registered function — the whole HTTP surface of a
                # FastAPI app — got the most confident failure verdict this
                # command has with no hint that the wiring was sitting on the
                # line above its own def.
                add_def(n.name, qual, n.lineno, kind, holder,
                        [{"label": _reach_dec_label(d), "file": relf, "line": d.lineno}
                         for d in n.decorator_list])
                # A decorator expression also evaluates in the ENCLOSING scope,
                # so `@registry.register` is a reference held by that scope — and
                # it is one of the ways a registry hides the only call site a
                # symbol has.
                for dec in n.decorator_list:
                    visit(dec, holder)
                if isinstance(n, ast.ClassDef):
                    for b in list(n.bases) + [k.value for k in n.keywords]:
                        visit(b, holder)
                else:
                    for dflt in (list(n.args.defaults)
                                 + [x for x in n.args.kw_defaults if x is not None]):
                        visit(dflt, holder)
                    if n.returns is not None:
                        visit(n.returns, holder)
                for s in _reach_body(n):
                    visit(s, qual)
                return
            if isinstance(n, ast.ImportFrom):
                add_import(n)
                return
            # `import M` had an early return of its own here. It was provably a
            # no-op — an alias node exposes no visitable children, so the generic
            # descent below does nothing either — and a branch that cannot be
            # made to fail is a claim this suite cannot defend. The rule it was
            # standing in for is real and IS defended, in add_import above and by
            # the attribute case below.
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and holder == _mod:
                tgts = (list(n.targets) if isinstance(n, ast.Assign) else [n.target])
                names = [t.id for t in tgts if isinstance(t, ast.Name)]
                value = getattr(n, "value", None)
                if "__all__" in names and value is not None:
                    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                        for el in value.elts:
                            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                                add_export(el.value, n.lineno, "__all__")
                    # The names inside __all__ are the export record, not string
                    # literals a getattr might resolve: visiting the value here
                    # would report every exported symbol as a dynamic hint.
                    return
                for nm in names:
                    add_def(nm, "%s.%s" % (_mod, nm), n.lineno, "assignment", _mod)
                if value is not None:
                    visit(value, holder)
                for t in tgts:
                    if not isinstance(t, ast.Name):
                        visit(t, holder)
                if isinstance(n, ast.AnnAssign) and n.annotation is not None:
                    visit(n.annotation, holder)
                return
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Load):
                    add_ref(n.id, "name", n.lineno, holder)
                return
            if isinstance(n, ast.Attribute):
                if isinstance(n.ctx, ast.Load):
                    add_ref(n.attr, "attr", n.lineno, holder)
                visit(n.value, holder)
                return
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                if n.value in wanted:
                    res["strings"].setdefault(n.value, []).append(
                        {"file": relf, "line": n.lineno, "test": is_test,
                         "inside": False})
                else:
                    # The limits block promised "where the bare name appears as a
                    # STRING LITERAL it is listed above as a dynamic hint", and
                    # the implementation only ever matched a string whose WHOLE
                    # value was the name. So it missed the case it most needed:
                    # Python source embedded in a string. Measured — a symbol
                    # whose only reference in 343 files was `webhooks.
                    # webhook_sink()` inside a textwrap.dedent subprocess probe
                    # got ZERO hints under 'defined, and NOTHING references it'.
                    # Only CALL-SHAPED occurrences count (`name(` or `.name`), so
                    # a docstring that merely says the name stays quiet.
                    for m in REACH_WORD_RX.finditer(n.value):
                        w = m.group(0)
                        if w not in wanted:
                            continue
                        head = n.value[m.start() - 1] if m.start() else ""
                        tail = n.value[m.end():m.end() + 1]
                        if tail != "(" and head != ".":
                            continue
                        res["strings"].setdefault(w, []).append(
                            {"file": relf, "line": n.lineno, "test": is_test,
                             "inside": True})
                return
            for c in ast.iter_child_nodes(n):
                visit(c, holder)

        for stmt in _reach_body(tree):
            visit(stmt, mod)

    return res


def _reach_entries(res, entries, root):
    """--from specs -> (seed scope qualnames, unresolved specs).

    Three spellings, tried in this order: a file path or dotted module (its
    MODULE scope, i.e. what import time executes), `module-or-path:symbol`, and
    a bare symbol name (every definition of it). A spec that resolves to nothing
    comes back unresolved and is reported loudly — an UNREACHED verdict computed
    from an empty seed set is a statement about nothing at all."""
    quals = {}
    for name, ds in res["defs"].items():
        quals[name] = [d["qual"] for d in ds]
    by_relf = dict((v, k) for k, v in res["modules"].items())

    def as_module(spec):
        spec = spec.strip()
        if spec in res["modules"]:
            return spec
        cand = os.path.normpath(spec)
        if cand in by_relf:
            return by_relf[cand]
        if os.path.isabs(cand):
            rel = os.path.relpath(cand, root)
            if rel in by_relf:
                return by_relf[rel]
        return None

    seeds, unresolved = set(), []
    for spec in entries:
        mod = as_module(spec)
        if mod is not None:
            seeds.add(mod)
            continue
        if ":" in spec:
            left, _, sym = spec.rpartition(":")
            lmod = as_module(left)
            hit = [q for q in quals.get(sym, [])
                   if lmod is not None and (q == "%s.%s" % (lmod, sym)
                                            or q.startswith(lmod + "."))]
            if hit:
                seeds.update(hit)
                continue
            unresolved.append(spec)
            continue
        if quals.get(spec):
            seeds.update(quals[spec])
            continue
        unresolved.append(spec)
    return sorted(seeds), unresolved


def _reach_bfs(res, seeds):
    """Name-keyed forward closure from the seeded scopes.

    Edges are NAME-keyed, not import-resolved: two same-named functions in
    different modules are one node here. That over-approximates reachability —
    the false-OK direction — which is why every symbol carrying more than one
    definition is flagged AMBIGUOUS in the report rather than quietly graded.
    Reaching a scope also reaches the CLASS bodies inside it, because a class
    body executes when its enclosing scope does; a nested FUNCTION is not
    entered that way, since reaching it requires a reference to its name."""
    quals = {}
    for name, ds in res["defs"].items():
        quals[name] = [d["qual"] for d in ds]
    reached = set()
    pending = []

    def enter(q):
        if q in reached:
            return
        reached.add(q)
        pending.append(q)
        for child, kind in res["children"].get(q, ()):
            if kind == "class":
                enter(child)

    for q in seeds:
        enter(q)
    while pending:
        cur = pending.pop()
        for name in res["edges"].get(cur, ()):
            for tq in quals.get(name, ()):
                enter(tq)
    return reached


def _reach_verdict(res, sym, reached, have_entries):
    """One symbol's layers, and the verdict they add up to."""
    defs = res["defs"].get(sym, [])
    own = [d["qual"] for d in defs]

    def is_own(holder):
        return any(holder == q or holder.startswith(q + ".") for q in own)

    # A reference from inside the symbol's own body wires nothing: recursion is
    # not a caller, and neither is a helper nested inside it.
    refs = [r for r in res["refs"].get(sym, []) if not is_own(r["holder"])]
    prod = [r for r in refs if not r["test"]]
    tests = [r for r in refs if r["test"]]
    reached_here = bool(own) and any(q in reached for q in own)
    decs = []
    for d in defs:
        decs += d.get("decorators", [])
    registering = any(d.get("registering") for d in defs)

    if not defs:
        verdict = "UNDEFINED"
    elif not prod and registering:
        # A registering decorator IS a reference to this definition, made by the
        # enclosing module at import time — it is simply one this scanner cannot
        # follow into the table on the other side. Reporting it as NO CALL SITES
        # ('defined, and NOTHING references it') was the tool committing the
        # exact overclaim it exists to catch: on the repo this was measured
        # against, that verdict landed on the application's entire HTTP surface.
        verdict = "DECORATOR-REGISTERED"
    elif not refs:
        verdict = "NO CALL SITES"
    elif not prod:
        verdict = "TESTS ONLY"
    elif have_entries and not reached_here:
        verdict = "UNREACHED"
    else:
        verdict = "WIRED"
    return {
        "symbol": sym, "verdict": verdict, "definitions": defs,
        "call_sites": refs, "product_call_sites": len(prod),
        "test_call_sites": len(tests), "exported": res["exports"].get(sym, []),
        "imported_by": res["imports"].get(sym, []),
        "dynamic_hints": res["strings"].get(sym, []),
        "decorators": decs, "registering_decorator": registering,
        "ambiguous": len(defs) > 1,
        "reached_from_entry": (reached_here if have_entries else None),
    }


REACH_DIAGNOSIS = {
    "UNDEFINED":
        "no definition of this name exists in what was scanned. Either the commit does "
        "not contain what its message claims, or the scan missed it — widen --path "
        "before concluding.",
    "NO CALL SITES":
        "defined, and NOTHING references it. This is the recovered commit's exact shape: "
        "a function, a docstring asserting a behaviour, and no wiring. A suite stays "
        "GREEN over it.",
    "DECORATOR-REGISTERED":
        "no PRODUCT code references it by name, but its definition carries a decorator "
        "that can register it — a route table, a CLI group, an event handler, a plugin, "
        "a fixture. That is how such code is wired in Python, so this is NOT the "
        "recovered commit's shape. It is NOT an all-clear either: this scanner does not "
        "follow a decorator into whatever holds it, so confirm BY EYE that the table it "
        "registers into is itself imported and reached.",
    "TESTS ONLY":
        "every caller is a test. Something goes green over this symbol and nothing the "
        "user runs reaches it — the zero-caller defect wearing a passing suite.",
    "UNREACHED":
        "it has product callers, but no call path from any --from entry point reaches "
        "it. Either those callers are themselves unreached, or the only path runs "
        "through a hop this scanner cannot follow (see the limits below).",
    "WIRED":
        "defined, called from product code, and reachable from an entry point where one "
        "was named.",
}


def _reach_limits(have_entries):
    """[lines] — printed with EVERY report, an all-WIRED one included.

    A blind spot the reader has to already know about is not a stated limit, and
    this command is only worth having if its OK can be trusted. So the list of
    what it cannot see ships attached to the answer."""
    lines = [
        "  WHAT THIS CANNOT SEE — a clean report is not proof of wiring:",
        "    * Python only. A call from JS/TS, a template, YAML, a shell script or a",
        "      packaging entry_points table is invisible to an ast pass over .py files.",
        "    * getattr(), a registry dict, a decorator table or a plugin loader hides a",
        "      real call site. Where the bare name appears as a STRING LITERAL it is",
        "      listed above as a dynamic hint and the verdict is NOT upgraded on it: that",
        "      errs toward the false ALARM (ten seconds of a human's time) over the false",
        "      OK (what the incident cost).",
        "    * Python source that lives inside a STRING is text, not code, to an ast",
        "      pass — a subprocess probe built with textwrap.dedent, an exec(), a",
        "      generated module. Measured: one symbol on the live repo this was tuned",
        "      against is called ONLY from such a probe. A CALL-SHAPED occurrence",
        "      (`name(` or `.name`) is reported above as an `embedded` hint and the",
        "      verdict is not upgraded on it; a name merely MENTIONED in prose is not",
        "      reported at all, so the absence of that line proves nothing.",
        "    * references are matched by the identifier AT THE REFERENCE SITE. A",
        "      `from m import foo as _foo` rename IS followed (the alias is resolved",
        "      per file and the caller is shown `[as _foo]`), but a name rebound any",
        "      other way — `g = foo` then `g()`, a dict of callables, a partial — is",
        "      not, and reads as no caller at all.",
        "    * a package whose public surface is a PEP 562 module __getattr__ / lazy",
        "      table reads as exported NOWHERE: `exported` knows __all__ and a",
        "      package __init__'s from-imports, and nothing else.",
        "    * a file the RUNNING interpreter cannot parse contributes no call sites",
        "      at all. It is named above per file, and the modern trigger is a version",
        "      mismatch rather than broken code — measured, a `type X = ...` (PEP 695,",
        "      3.12+) statement takes a whole module out of the answer under 3.11.",
        "    * a DECORATOR on the definition is reported and NOT followed. This scanner",
        "      sees that @router.post is there; it cannot see whether that router is",
        "      itself imported and mounted. DECORATOR-REGISTERED therefore means 'the",
        "      wiring is a hop I did not take', never 'wired' — the hop is yours to check.",
        "    * a caller counts as a TEST by its PATH alone: conftest.py, test_*.py,",
        "      *_test.py, anything under tests/ test/ testing/ __tests__/ spec/ specs/,",
        "      and the frozen acceptance directory. Product code under a directory named",
        "      `spec` is read as a test here, which downgrades WIRED to TESTS ONLY —",
        "      the false-ALARM direction, and one look settles it.",
        "    * references are matched BY NAME, not resolved through imports. Two",
        "      same-named symbols in different modules are one node — it over-counts",
        "      callers, in the false-OK direction. Every symbol with more than one",
        "      definition is flagged AMBIGUOUS above.",
    ]
    if have_entries:
        lines += [
            "    * --from reachability is a NAME-KEYED forward closure over static",
            "      references. It is the weakest layer here: read UNREACHED as a question",
            "      to answer, and DEFINED-but-uncalled as the finding.",
        ]
    else:
        lines += [
            "    * NO --from WAS GIVEN, so nothing was checked for reachability from an",
            "      entry point. 'called from somewhere' is not 'on the path the user's",
            "      command takes' — a symbol called only by other dead code reads as WIRED",
            "      here. Pass --from to separate them.",
        ]
    lines.append(
        "    * only what is on disk NOW is scanned; this reads no git history.")
    return lines


def cmd_reachable(args):
    """The wiring check, as a command instead of a habit."""
    root = repo_root()
    wanted = []
    for chunk in (args.symbol or []):
        for x in chunk.split(","):
            x = x.strip()
            if x and x not in wanted:
                wanted.append(x)
    if not wanted:
        # Exit 3, not die()'s 1. task-packets.md tells the orchestrator to read
        # this command's exit code, and 1 already means "a symbol is not wired" —
        # so the tool FAILING must not be spelled the same as the tool FINDING
        # something. 3 is this file's usage-error code (see _Parser.error).
        die("--symbol needs at least one name (every value given was empty)", code=3)
    entries = []
    for chunk in (args.entries or []):
        for x in chunk.split(","):
            x = x.strip()
            if x and x not in entries:
                entries.append(x)

    paths = args.path or [root]
    res = _reach_scan(root, paths, wanted)
    seeds, unresolved = _reach_entries(res, entries, root) if entries else ([], [])
    reached = _reach_bfs(res, seeds) if seeds else set()
    rows = [_reach_verdict(res, s, reached, bool(entries)) for s in wanted]
    bad = [r for r in rows if r["verdict"] != "WIRED"]
    # What the EXIT CODE is about is narrower than what `not_wired` is about. A
    # DECORATOR-REGISTERED symbol is not proven wired and stays in not_wired, but
    # it does not fail the run: the commonest wiring idiom in Python cannot be a
    # hard failure, or the mandatory check fires on every route in the repo and
    # its readers learn to pass --no-fail.
    findings = [r for r in bad if r["verdict"] != "DECORATOR-REGISTERED"]
    shown = [os.path.relpath(p, root) if os.path.isabs(p) else p for p in paths]
    shown = [p if p != "." else "the repo root" for p in shown]

    if args.json:
        print(json.dumps({
            "scanned_files": res["files"],
            "scan_roots": shown,
            "paths_unresolved": res["unresolved_paths"],
            "unparsed": [{"file": a, "error": b} for a, b in res["unparsed"]],
            "entry_points": entries,
            "entry_scopes_resolved": seeds,
            "entry_points_unresolved": unresolved,
            "scopes_reached": len(reached),
            "symbols": rows,
            "not_wired": [r["symbol"] for r in bad],
            "findings": [r["symbol"] for r in findings],
            "limits": _reach_limits(bool(entries)),
        }, indent=2, sort_keys=True))
    else:
        print("reachable: %d symbol(s) against %d python file(s) under %s"
              % (len(wanted), res["files"], ", ".join(shown)))
        if entries:
            print("  entry points: %s  -> %d scope(s) seeded, %d scope(s) reached"
                  % (", ".join(entries), len(seeds), len(reached)))
        if res["unresolved_paths"]:
            # A --path naming nothing used to be dropped in silence while the
            # header line above went on asserting it had been scanned — the one
            # place this command was not meticulous about the failure class it
            # is meticulous about everywhere else.
            print("  --path DID NOT RESOLVE: %s — it names no directory and no .py file "
                  "that exists, so NOTHING under it was scanned. The file count above is "
                  "about the paths that DID resolve."
                  % ", ".join(res["unresolved_paths"]))
        if unresolved:
            print("  ENTRY POINT DID NOT RESOLVE: %s — it names no file, module or symbol "
                  "in the scan." % ", ".join(unresolved))
            if not seeds:
                print("      NO entry scope resolved at all, so the reachability layer is "
                      "reporting about an EMPTY graph: every UNREACHED below is an artefact "
                      "of that, not a finding.")
        for relf, err in res["unparsed"]:
            print("  COULD NOT PARSE %s: %s — its call sites are invisible to this answer"
                  % (relf, err))
        if not res["files"]:
            print("  NO PYTHON FILES WERE SCANNED. This is an ast pass over .py and can say "
                  "NOTHING about a codebase in another language; do not read the verdicts "
                  "below as evidence about one.")
        print("")
        for r in rows:
            print("  %-38s %s" % (r["symbol"], r["verdict"]))
            if r["definitions"]:
                for d in r["definitions"][:REACH_LIST_CAP]:
                    print("      defined    %s:%d  (%s%s)"
                          % (d["file"], d["line"], d["kind"],
                             ", in a TEST file" if d["test"] else ""))
                if len(r["definitions"]) > REACH_LIST_CAP:
                    print("      defined    ... and %d more"
                          % (len(r["definitions"]) - REACH_LIST_CAP))
            else:
                print("      defined    NOWHERE in the %d file(s) scanned" % res["files"])
            if r["call_sites"]:
                print("      callers    %d (%d product, %d test)"
                      % (len(r["call_sites"]), r["product_call_sites"], r["test_call_sites"]))
                for c in r["call_sites"][:REACH_LIST_CAP]:
                    print("                 %s:%d  in %s%s%s"
                          % (c["file"], c["line"], c["holder"],
                             ("  [as %s]" % c["alias"]) if c.get("alias") else "",
                             "  [TEST]" if c["test"] else ""))
                if len(r["call_sites"]) > REACH_LIST_CAP:
                    print("                 ... and %d more"
                          % (len(r["call_sites"]) - REACH_LIST_CAP))
            else:
                print("      callers    NONE — zero references outside its own body, across "
                      "%d file(s)" % res["files"])
            if r["exported"]:
                print("      exported   %s" % "; ".join(
                    "%s (%s:%d)" % (e["via"], e["file"], e["line"])
                    for e in r["exported"][:REACH_LIST_CAP]))
            else:
                print("      exported   NO — in no __all__, re-exported by no package "
                      "__init__")
            if r["imported_by"]:
                print("      imported   %s" % ", ".join(
                    "%s:%d" % (i["file"], i["line"])
                    for i in r["imported_by"][:REACH_LIST_CAP]))
            if r["decorators"]:
                print("      decorated  %s%s" % (
                    "; ".join("%s at %s:%d" % (d["label"], d["file"], d["line"])
                              for d in r["decorators"][:REACH_LIST_CAP]),
                    (" ... and %d more" % (len(r["decorators"]) - REACH_LIST_CAP)
                     if len(r["decorators"]) > REACH_LIST_CAP else "")))
                if r["registering_decorator"]:
                    print("                 a route/registry/plugin table may be its only "
                          "call site. This scanner does NOT follow a decorator into the "
                          "table on the other side.")
                else:
                    print("                 every one of these WRAPS IN PLACE and registers "
                          "nothing, so the verdict above is not softened on them.")
            if r["reached_from_entry"] is not None:
                print("      reachable  %s from %s"
                      % ("YES" if r["reached_from_entry"] else "NO", ", ".join(entries)))
            if r["ambiguous"]:
                print("      AMBIGUOUS  %d definitions share this name; references are "
                      "matched by name alone, so the layers above MERGE them and read "
                      "generously" % len(r["definitions"]))
            bare = [h for h in r["dynamic_hints"] if not h.get("inside")]
            inside = [h for h in r["dynamic_hints"] if h.get("inside")]
            if bare:
                print("      dynamic    the bare name appears as a STRING at %s — a getattr "
                      "or registry may reach it. The verdict above is NOT upgraded on this."
                      % ", ".join("%s:%d" % (h["file"], h["line"])
                                  for h in bare[:REACH_LIST_CAP]))
            if inside:
                print("      embedded   the name appears CALL-SHAPED inside a longer string "
                      "at %s — Python source in a string (a subprocess probe, an exec, a "
                      "generated module) is text to an ast pass, so a real call site can "
                      "hide there. Read the string before believing the verdict above; it "
                      "is NOT upgraded on this."
                      % ", ".join("%s:%d" % (h["file"], h["line"])
                                  for h in inside[:REACH_LIST_CAP]))
            print("      %s" % REACH_DIAGNOSIS[r["verdict"]])
        print("")
        counts = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print("  summary: " + " · ".join(
            "%d %s" % (counts.get(v, 0), v)
            for v in ("WIRED", "DECORATOR-REGISTERED", "NO CALL SITES", "TESTS ONLY",
                      "UNREACHED", "UNDEFINED")))
        for line in _reach_limits(bool(entries)):
            print(line)

    if (findings or unresolved or res["unresolved_paths"]) and not args.no_fail:
        sys.exit(1)
    sys.exit(0)


# -------------------------------------------------------------------- main

def main():
    class _Parser(argparse.ArgumentParser):
        # Usage errors must not collide with exit 2 == TERMINAL SIGNAL: a typo'd
        # `checkpoint --cycel N` exiting 2 reads as the kill switch to a
        # literal-minded orchestrator mid-overnight-run. Usage errors exit 3.
        def error(self, message):
            self.print_usage(sys.stderr)
            print(f"swarmloop: usage error: {message}", file=sys.stderr)
            sys.exit(3)

    p = _Parser(prog="swarmloop", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="initialize .swarm-loop state")
    s.add_argument("--max-cycles", type=int, required=True,
                   help="safety ceiling on measurement epochs (runaway guard, not a plan)")
    s.add_argument("--audit-loops", type=int, default=0,
                   help="user-set number of whole-codebase audit-fix loops after the build converges")
    s.add_argument("--project", default=None)
    s.add_argument("--autonomy", default="approve-cycle-1",
                   choices=["approve-cycle-1", "approve-every-cycle", "fully-autonomous"])
    s.add_argument("--push-remote", default="when-clean", choices=["when-clean", "never"])
    s.add_argument("--stall-limit", type=int, default=3,
                   help="consecutive cycles with an UNCHANGED codebase before the kill switch")
    s.add_argument("--goal-stall-limit", type=int, default=None,
                   help="consecutive MEASURED cycles in which no graded metric improved, "
                        "before the kill switch (default: --stall-limit). The two are "
                        "different questions: a loop that writes code every cycle never "
                        "trips the first one")
    s.add_argument("--worktree-root", default=None,
                   help="where task worktrees are created (default: a SIBLING dir "
                        "../<repo>-worktrees, so they stay out of every root-walking metric "
                        f"command; falls back to {STATE_DIR}/worktrees/ if the parent is not "
                        "writable). Recorded in state.json so 'resume' can find them.")
    s.add_argument("--scratch-root", default=None, metavar="PATH",
                   help="where each lane's PRIVATE scratch/temp directory lives — one per "
                        "ticket, <scratch-root>/<task-id>/, so two agents cannot write the "
                        "same throwaway filename and run each other's. Optional; recorded in "
                        "state.json so 'resume' and 'status' can still say whether the lanes "
                        "were subdivided after a compaction. Keep it OUTSIDE the repo: metric "
                        "commands run with cwd = the repo root, and their exclusions freeze at "
                        "cycle 0. Recorded, never enforced — the harness never observes the "
                        "inside of a lane's turn.")
    s.add_argument("--protect-path", action="append", default=None, metavar="PATH",
                   help="repeatable; REPLACES the protected-path list (default: "
                        f"{STATE_DIR}/acceptance + {STATE_DIR}/{GOALS_FILE}, plus whatever "
                        "a previous run in this repo had added — --force carries those "
                        "forward rather than silently unprotecting them)")
    s.add_argument("--ticket-graph", default=None, metavar="PATH",
                   help="where the ticket graph lives (default: search tickets.json, "
                        f"{STATE_DIR}/tickets.json, backlog.json). Recorded in state.json "
                        "for 'frontier' / 'check-wave' / 'red-check'.")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("freeze", help="lock goals.json + acceptance tests")
    s.add_argument("--skip-smoke", action="store_true",
                   help="skip the metric smoke-run (not recommended)")
    s.add_argument("--smoke-timeout", type=int, default=2400,
                   help="per-command smoke-run timeout in seconds (default matches measure); "
                        "a metric may override it with a per-metric \"timeout\" field")
    s.add_argument("--amend", metavar="REASON", default=None,
                   help="re-freeze an ALREADY-frozen harness. Only the user, awake, amends a "
                        "frozen goal: pass the reason they approved. Prints the old->new hashes "
                        "and diffs and appends an append-only record to "
                        f"{STATE_DIR}/{FREEZE_LOG_FILE}")
    s.add_argument("--allow-missing-protected", action="store_true",
                   help="freeze even though a path in protected_paths does not exist yet. "
                        "It will be UNPROTECTED for the whole run while check-branch still "
                        "vetoes branches touching it — say so in the cycle-0 report.")
    s.add_argument("--allow-read-violations", action="store_true",
                   help="freeze even though a frozen test imports a module owned by a ticket "
                        "outside its own dependency closure (B5/H-10). Those tests can never "
                        "pass; run 'reads' to see them. Only for a graph you have decided is "
                        "wrong in a way you will fix outside the freeze. Needed only for a "
                        "FIRST freeze: on an already-frozen run the same finding is printed "
                        "as a loud advisory and the freeze (or --amend) proceeds, because "
                        "fixing it is itself an amendment.")
    s.add_argument("--allow-deselection", action="store_true",
                   help="freeze even though a metric command's smoke run DESELECTS tests or "
                        "selects none. Only for a project whose deselection is a deliberate "
                        "convention (a `-m 'not slow'` house rule): the metric's number is then "
                        "partly a statement about the filter, and the filter lives in files no "
                        "protected path covers. Record at the cycle-0 checkpoint which tests it "
                        "will never run.")
    s.set_defaults(func=cmd_freeze)

    s = sub.add_parser("verify", help="check the frozen harness is intact")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("restore-harness",
                       help="rewrite protected files back to their frozen bytes from git")
    s.add_argument("--remove-added", action="store_true",
                   help="also delete files added under protected paths after the freeze")
    s.set_defaults(func=cmd_restore_harness)

    s = sub.add_parser("check-branch", help="veto branches touching protected/orchestrator paths")
    s.add_argument("--branch", required=True)
    s.add_argument("--base", default="main")
    s.add_argument("--ticket", default=None, metavar="ID",
                   help="OPTIONAL: also refuse this branch unless <ID> carries a red-check "
                        f"verdict of `red` in {STATE_DIR}/{REDCHECK_FILE} at an ancestor of "
                        "its merge parent. Advisory for a ticket that predates the ledger "
                        "(or that dispatch.jsonl cannot date), blocking from the first stamp "
                        "onward — a resumed run has no stamps for work already in flight and "
                        "must not be wedged by a gate it predates. Omitting the flag leaves "
                        "check-branch behaving exactly as before.")
    s.add_argument("--worktree", default=None, metavar="PATH",
                   help="OPTIONAL: also refuse this branch unless the worktree at PATH "
                        "IS this branch's tree and `git -C PATH status --porcelain` is "
                        "EMPTY, printing the uncommitted paths. A tip pins what was "
                        "committed and says nothing about what sits uncommitted beside "
                        "it: a verifier was once briefed at a pinned SHA over a tree "
                        "holding 112 uncommitted lines, and the greens it was handed as "
                        "facts had been measured against those bytes. NOT discovered "
                        "from `git worktree list` — the integration worktree and the "
                        "primary checkout are both legitimately dirty during a run. "
                        "Omitting the flag leaves check-branch behaving exactly as "
                        "before.")
    s.set_defaults(func=cmd_check_branch)

    s = sub.add_parser("measure", help="run all metric commands and record")
    s.add_argument("--cycle", type=int, required=True)
    s.add_argument("--timeout", type=int, default=2400,
                   help="per-command timeout in seconds; must strictly EXCEED any timeout "
                        "inside a metric command so the inner one fires first and the metric "
                        "still reports a number. A per-metric \"timeout\" field overrides it.")
    s.set_defaults(func=cmd_measure)

    s = sub.add_parser("record", help="record one metric value manually")
    s.add_argument("--cycle", type=int, required=True)
    s.add_argument("--metric", required=True)
    s.add_argument("--value", type=float, required=True)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("analyze", help="regression + verdict per metric")
    s.add_argument("--cycle", type=int, required=True)
    s.add_argument("--trust-manual-records", action="store_true",
                   help="allow all-targets-met even when a metric's newest row came from "
                        "'record' rather than 'measure' (needs the user's approval)")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("checkpoint", help="record HEAD; drive stall kill switch")
    s.add_argument("--cycle", type=int, required=True)
    s.set_defaults(func=cmd_checkpoint)

    s = sub.add_parser("audit-loop-done", help="record a completed audit-fix loop (exit 2 when terminal)")
    s.add_argument("--clean", action="store_true",
                   help="this loop's whole-codebase audit minted zero tickets (terminal via prune)")
    s.set_defaults(func=cmd_audit_loop_done)

    s = sub.add_parser(
        "escalate",
        help="ask the USER to change something frozen/blocked (records ONLY — changes no "
             "goal, metric, manifest or history row)")
    s.add_argument("--kind", choices=list(ESCALATION_KINDS), default=None,
                   help="what is being escalated")
    s.add_argument("--subject", default=None, metavar="ID|PATH",
                   help="the metric id, file path or ticket id the request is about "
                        "(the (kind, subject) pair is what de-duplicates re-filings)")
    s.add_argument("--path", default=None, metavar="PATH",
                   help="the FILE this request is about, as a repo-relative path. Structured "
                        "on purpose: 'freeze --amend' links an escalation to the amendment "
                        "that answers it by matching what the amend actually touched, and a "
                        "prose subject (a pytest nodeid, above all) matched nothing — ESC-002 "
                        "was answered by amendment 2 and the link never fired.")
    s.add_argument("--test", default=None, metavar="NODEID",
                   help="the specific test, e.g. test_spec_criteria.py::test_name — the "
                        "canonical way to name ONE frozen test")
    s.add_argument("--reason", default=None, metavar="TEXT",
                   help="why it is wrong, WITH the evidence that shows it")
    s.add_argument("--proposed", default=None, metavar="TEXT",
                   help="the specific change being requested")
    s.add_argument("--blocking", action="store_true",
                   help="no further progress is possible without a decision; exits 2 "
                        "(terminal condition for autonomy — same meaning as the kill switch)")
    s.add_argument("--cycle", type=int, default=None,
                   help="defaults to state.json's current_cycle")
    s.add_argument("--resolve", metavar="ID", default=None,
                   help="mark an escalation resolved because the USER decided it; --note is "
                        "REQUIRED and records what they decided")
    s.add_argument("--withdraw", metavar="ID", default=None,
                   help="the LOOP retracting a question that became moot (the route pivoted, "
                        "the ticket was dropped). --note is REQUIRED, the record says NEVER "
                        "ANSWERED forever, and a BLOCKING escalation may not be withdrawn at "
                        "all — that one stopped the run to wait for a person")
    s.add_argument("--note", default=None, metavar="TEXT",
                   help="with --resolve: what the user decided. With --withdraw: why the "
                        "question no longer needs an answer. Required by both — an ask closed "
                        "with no recorded decision is indistinguishable from a loop retiring "
                        "its own unanswered question")
    s.add_argument("--open-asks", action="store_true", dest="open_asks",
                   help="THE END-OF-TURN BLOCK: print every OPEN ask (newest first, blocking "
                        "on top) and NOTHING AT ALL when none are open, so it can end every "
                        "user-facing turn unconditionally. Exits 2 if any is blocking — the "
                        "run is waiting on a human, which is neither done nor failed")
    s.add_argument("--list", action="store_true", dest="do_list",
                   help="print OPEN escalations (the only ones anything is waiting on); "
                        "exits 2 if any of them is blocking. Prints a line when there are "
                        "none — use --open-asks for the silent, every-turn form")
    s.add_argument("--all", action="store_true", dest="show_all",
                   help="with --list: include resolved escalations too")
    s.set_defaults(func=cmd_escalate)

    s = sub.add_parser("frontier", help="ready set from the ticket graph (dependency readiness "
                                        "+ blast radius)")
    s.add_argument("--graph", default=None, metavar="PATH",
                   help="ticket graph file (default: state.json's ticket_graph, else "
                        "tickets.json)")
    s.add_argument("--closed", action="append", default=None, metavar="IDS",
                   help="repeatable, comma-separated: tickets already merged/closed")
    s.add_argument("--closed-from-merged", action="store_true",
                   help="also treat as closed a ticket whose task/* branch is merged into "
                        "--base AND is POSITIVELY shown to have carried work: a dispatch "
                        "record pinning a tip it has since moved past, or a tip off --base's "
                        "first-parent history. Being --merged proves nothing on its own — a "
                        "provisioned lane is trivially merged — so anything else is named "
                        "UNDETERMINED and stays OPEN")
    s.add_argument("--closed-from-ledger", action="store_true",
                   help="also treat as closed every ticket named by a `dispatch --close "
                        "--ticket … --verdict accepted` record (LAST verdict per ticket "
                        "wins). Unlike --closed-from-merged this survives `retire` deleting "
                        "the landed branch — and it re-verifies nothing")
    s.add_argument("--base", default="main")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_frontier)

    s = sub.add_parser("findings", help="record checker findings durably, then mint tickets "
                                       "from them (location and reproduction carried verbatim)")
    s.add_argument("--file", default="-", metavar="PATH",
                   help="JSON array, {\"findings\": [...]} object, or JSONL. '-' (default) "
                        "reads stdin. Each record: severity, location, defect, reproduction")
    s.add_argument("--graph", default=None, metavar="PATH")
    s.add_argument("--min-severity", default="MEDIUM",
                   choices=["BLOCKER", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"],
                   help="mint tickets at or above this severity. Everything BELOW it is "
                        "still recorded verbatim — the threshold decides what gets a ticket, "
                        "never what gets remembered (default: MEDIUM)")
    s.add_argument("--prefix", default="T-", help="minted ticket id prefix (default: T-)")
    s.add_argument("--cycle", type=int, default=None, help="stamp the records with this cycle")
    s.add_argument("--no-tickets", action="store_true",
                   help="record only; mint nothing")
    s.add_argument("--dry-run", action="store_true",
                   help="show what would be recorded and minted; write nothing")
    s.set_defaults(func=cmd_findings)

    s = sub.add_parser("check-wave", help="refuse a wave whose members could own the same file")
    s.add_argument("tickets", nargs="+", metavar="T-0xx")
    s.add_argument("--graph", default=None, metavar="PATH")
    s.add_argument("--base", default="main",
                   help="ref the wave's worktrees fork from — used to count how far any "
                        "EXISTING member branch has drifted behind it (report only, "
                        "never a veto)")
    s.set_defaults(func=cmd_check_wave)

    s = sub.add_parser("conflicts", help="pairwise TEXTUAL conflict between task branches, "
                                        "answered by git merge-tree rather than by declared "
                                        "globs (Phase 5 / merge-protocol step 3)")
    s.add_argument("branches", nargs="+", metavar="BRANCH",
                   help="comma- or space-separated; a bare ticket id also resolves task/<id>")
    s.add_argument("--base", default="main",
                   help="the ref every branch forked from (default: main)")
    s.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable report, same exit codes")
    s.set_defaults(func=cmd_conflicts)

    s = sub.add_parser("push-gate", help="score the mechanical rungs of the Phase 7 push gate "
                                        "(run at the END of Phase 8, after the ledger commit; "
                                        "exit 1 = do not push)")
    s.add_argument("--cycle", type=int, required=True,
                   help="the epoch being closed — the cycle measure/analyze/checkpoint just ran")
    s.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable report, same exit codes")
    s.set_defaults(func=cmd_push_gate)

    s = sub.add_parser("red-check", help="prove each ticket's verify command FAILS before it "
                                         "is built (exit 1 on VACUOUS, WEAK, HUNG, NO GATE or "
                                         "SKIPPED — exit 0 means every gate is real; a ticket "
                                         "carrying `bootstrap: true` in the graph is exempt "
                                         "from must-go-red and from nothing else)")
    s.add_argument("--ticket", action="append", default=None, metavar="IDS",
                   help="repeatable, comma-separated (default: every ticket in the graph)")
    s.add_argument("--graph", default=None, metavar="PATH")
    s.add_argument("--base", default="main",
                   help="ref to check out into the throwaway worktree (the pre-build state)")
    # Spelled out rather than --timeout: the regression suite pins the 2400s
    # defaults of measure/freeze by grepping this file for `--timeout`, and a
    # third flag of the same name inside a different subparser is a
    # cross-command collision waiting to confuse both.
    s.add_argument("--per-ticket-timeout", type=int, default=600, dest="timeout",
                   help="per-ticket timeout in seconds (default 600). Expiry is its own "
                        "verdict, HUNG — a gate that deadlocks on an unbuilt tree prints "
                        "no red at all in CI, which is worse than one that passes")
    s.add_argument("--close", action="store_true",
                   help="CLOSE-TIME retro-check: point --base at the ticket's merge parent and "
                        "ask 'was this gate red immediately before this work landed?'. Turns the "
                        "selection findings (SWALLOWED, DESELECTED, MISSING, collected-NONE) "
                        "from advisories into refusals. UNVERIFIED (a collect-only run that hit "
                        "a collection error) and FORCED stay advisory in both modes. Never pass "
                        "--close on the pre-dispatch path: those findings can be wrong on a "
                        "custom runner, and a false refusal there stops a ticket that has "
                        "nothing wrong with it. NOTE that the classification is the same either "
                        "way — without --close the ticket is COUNTED red and exits 0, but the "
                        "stamp still records the finding and check-branch --ticket still reads "
                        "it, so a selection finding at dispatch is a merge veto in waiting and "
                        "is now printed as one.")
    s.set_defaults(func=cmd_red_check)

    s = sub.add_parser("reads", help="read edges the ticket graph does not declare, and the "
                                     "cross-ticket import closure check (B5/H-10)")
    s.add_argument("--graph", default=None, metavar="PATH")
    s.add_argument("--tests", action="append", default=None, metavar="DIR",
                   help="repeatable: where the frozen tests live (default: the protected "
                        "paths, which is what freeze actually locked)")
    s.add_argument("--closed", action="append", default=None, metavar="IDS",
                   help="repeatable, comma-separated: tickets already merged — their modules "
                        "exist, so importing them is legal from anywhere")
    s.add_argument("--base", default="main", help=argparse.SUPPRESS)
    s.add_argument("--write", action="store_true",
                   help="write the derived edges back as an optional 'reads' key on each "
                        "ticket in the graph file (additive; no existing reader breaks)")
    s.add_argument("--no-fail", action="store_true",
                   help="report illegal reads without exiting non-zero")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_reads)

    s = sub.add_parser("dispatch", help="record a dispatched agent / close one (append-only)")
    s.add_argument("--agent", default=None, metavar="REF",
                   help="how the orchestrator refers to this agent")
    s.add_argument("--kind", default=None,
                   choices=["ticket", "regen", "recon", "verifier", "audit", "other"])
    s.add_argument("--subject", default=None, metavar="TEXT",
                   help="ticket id, document, or task the agent was given")
    s.add_argument("--files", default=None, metavar="A,B",
                   help="files this agent owns — what check-wave intersects against for work "
                        "that has no worktree")
    s.add_argument("--branch", default=None, metavar="REF",
                   help="the branch this dispatch JUDGES. Its tip is resolved and recorded at "
                        "open and again at close, so a verdict is bound to the SHA it was "
                        "measured against and a branch that moved under the agent is reported "
                        "instead of silently accepted")
    s.add_argument("--close", default=None, metavar="REF", help="mark a dispatch collected")
    s.add_argument("--ticket", action="append", default=None, metavar="IDS",
                   help="with --close: repeatable, comma-separated ticket ids this verdict "
                        "is about. Recorded on the close record, which outlives the branch "
                        "`retire` deletes")
    s.add_argument("--verdict", default=None, choices=["accepted", "rejected"],
                   help="with --close: the machine-readable verdict. Only 'accepted' closes "
                        "a ticket for `frontier --closed-from-ledger`; the free-text --note "
                        "never could, which is why a rejected branch used to close exactly "
                        "like an accepted one")
    s.add_argument("--note", default=None, metavar="TEXT")
    s.add_argument("--list", action="store_true", dest="do_list",
                   help="print dispatches that were never closed")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("retire", help="reclaim worktrees whose work has LANDED on the base "
                                      "branch (plain `git worktree remove`, never --force)")
    s.add_argument("--dry-run", action="store_true",
                   help="report the plan and mutate NOTHING — no removal, no branch delete, "
                        "no state write")
    s.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable report instead of the human one")
    s.add_argument("--base", default=None, metavar="REF",
                   help="the branch work must have LANDED on before its tree is retirable "
                        "(default: state's base_branch, else main). Merged into an integration "
                        "branch is NOT landed")
    s.add_argument("--idle-seconds", type=int, default=WORKTREE_IDLE_DEFAULT, metavar="N",
                   help="keep any tree whose git activity is newer than N seconds — the guard "
                        "against retiring a tree a fix agent re-entered after adjudication "
                        f"(default {WORKTREE_IDLE_DEFAULT}; 0 disables it)")
    s.add_argument("--timeout", type=int, default=WORKTREE_REMOVE_TIMEOUT, metavar="SEC",
                   help="seconds allowed for ONE `git worktree remove`; a timeout is reported "
                        "as a REFUSAL and never escalated to --force (default "
                        f"{WORKTREE_REMOVE_TIMEOUT})")
    s.add_argument("--du-seconds", type=int, default=WORKTREE_DU_TIMEOUT, metavar="SEC",
                   help="bound on sizing the worktree root; 0 skips it (default "
                        f"{WORKTREE_DU_TIMEOUT} — 14.7 GB measured at ~38s)")
    s.add_argument("--keep-branches", action="store_true",
                   help="retire the worktrees but delete no branches")
    s.set_defaults(func=cmd_retire)

    s = sub.add_parser("distil", help="record a process lesson in the run — or, with "
                                      "--to-skill, promote one into the skill's cross-run memory")
    s.add_argument("--lesson", default=None, metavar="TEXT",
                   help="the imperative instruction a future run should obey")
    s.add_argument("--because", default=None, metavar="TEXT",
                   help="the failure that motivated it — required, because a lesson whose "
                        "motivating failure is missing cannot be judged for applicability")
    s.add_argument("--to-skill", action="store_true", dest="to_skill",
                   help=f"append to the skill's {LEARNED_MD} instead of the run's file. This is "
                        "machine-wide memory read by EVERY future run: the entry is validated "
                        f"project-agnostic, capped at {LEARNED_CAP}, and appended under a lock")
    s.add_argument("--cycle", type=int, default=None, metavar="N",
                   help="cycle to tag a run-file entry with (default: state's current_cycle)")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="validate and render the entry, write nothing")
    s.add_argument("--list", action="store_true", dest="do_list",
                   help="report both tiers and where they live; write nothing")
    s.set_defaults(func=cmd_distil)

    s = sub.add_parser("resume", help="reconstruct run state after an interrupted session")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("status", help="print loop state")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("reachable",
                       help="the wiring check: is a symbol defined, called, exported and "
                            "reachable from a real entry point (exit 1 when it is not)")
    s.add_argument("--symbol", action="append", default=None, required=True, metavar="NAME",
                   help="repeatable, comma-separated: the symbol(s) a commit claims to have "
                        "added")
    s.add_argument("--from", action="append", default=None, dest="entries", metavar="ENTRY",
                   help="repeatable, comma-separated: an entry point — a file (src/cli.py), "
                        "a dotted module, `module:symbol`, or a bare symbol name. WITHOUT it "
                        "the reachability layer is skipped and only defined/called/exported "
                        "are reported")
    s.add_argument("--path", action="append", default=None, metavar="DIR",
                   help="repeatable: where to scan (default: the whole repo root, minus "
                        "vendor/cache directories — a directory named here is scanned even "
                        "if it is on that skip list)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--no-fail", action="store_true",
                   help="print the verdicts without exiting non-zero")
    s.set_defaults(func=cmd_reachable)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # The metric command's process group has already been reaped by
        # _run_metric_command on its way out (start_new_session detaches it from
        # the terminal, so Ctrl-C never reaches it directly). All that is left is
        # to say so in one line instead of a traceback, and with an exit code
        # that cannot be mistaken for 2 (terminal) or 3 (usage).
        print("\nswarmloop: interrupted (SIGINT) — nothing was recorded or concluded by this "
              "command; any metric command it started was killed. Re-run when ready.",
              file=sys.stderr)
        sys.exit(130)
