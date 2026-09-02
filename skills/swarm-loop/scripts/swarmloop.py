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
  red-check   Prove each ticket's verify command FAILS before it is built
  dispatch    Record a dispatched agent / close one (append-only)
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
HISTORY_FILE = "history.csv"
ANALYSIS_DIR = "analysis"

# 'source' is load-bearing, not decoration: analyze keeps the LAST row per cycle
# in file order, so a hand-entered 'record' silently outranks the real 'measure'
# for that cycle. The column is what lets analyze say so out loud. Files written
# by older versions have only the first five columns and are still readable.
HISTORY_HEADER = ["cycle", "metric_id", "value", "error", "timestamp", "source"]

SLOPE_EPS = 1e-9  # below this magnitude, treat the error trend as flat

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
    print(f"  stall limit  : {state['stall_limit']} unchanged cycles")
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


def _log_git_check(root, d, recs):
    """Was freeze-log.jsonl REWRITTEN since it was last committed?

    The log is documented as append-only in five places and nothing enforced it.
    Git does: the newest committed copy must be a PREFIX of the copy on disk.
    Compared as records rather than bytes so a trailing-newline difference is not
    an accusation.

    Returns (violation | None, note | None). A committed copy that does not even
    START this run — the previous run's log, left behind because `init --force`
    rotates the file aside — is a NOTE, not a violation: there is nothing
    append-only about two different runs."""
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

    v, n = _log_git_check(root, d, recs)
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
        notes.append("%d of %d frozen file(s) are not in git history yet — commit "
                     "%s/ so 'restore-harness' can repair a tamper (first: %s)"
                     % (len(unanchored),
                        len([r for r in manifest.get("files", {}) if r != grel]),
                        STATE_DIR, unanchored[0]))
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
        L.append("### %s \u00b7 %s \u00b7 `%s` \u2014 resolved %s"
                 % (r.get("id"), r.get("kind"), r.get("subject"), r.get("resolved_at")))
        L.append("")
        L.append("- filed %s (cycle %s)%s"
                 % (r.get("timestamp"), r.get("cycle"),
                    " \u00b7 was BLOCKING" if r.get("blocking") else ""))
        L.append("- **what was wrong**: " + str(r.get("reason", "")))
        L.append("- **proposed**: " + str(r.get("proposed", "")))
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
            print("  %s  %s \u00b7 %s \u2014 resolved %s: %s"
                  % (r.get("id"), r.get("kind"), r.get("subject"), r.get("resolved_at"),
                     _oneline(r.get("resolution_note")) or "(no note)"))
    print("")
    print("full text: %s/%s   (append-only log: %s/%s)"
          % (STATE_DIR, ESCALATIONS_MD, STATE_DIR, ESCALATIONS_FILE))
    if blocking:
        print("AWAITING USER DECISION \u2014 a BLOCKING escalation is open; the loop must not "
              "continue past it.")
        sys.exit(2)


def _escalate_resolve(args, d, root, state, project):
    rid = _norm_esc_id(args.resolve)
    if rid is None:
        die("'%s' is not an escalation id (expected e.g. ESC-003)" % args.resolve, 3)
    recs = {r["id"]: r for r in _load_escalations(d)}
    rec = recs.get(rid)
    if rec is None:
        die("unknown escalation id %s (on file: %s)"
            % (rid, ", ".join(sorted(recs)) or "none"))
    if rec.get("status") == "resolved":
        # The log is append-only and a decision is not re-taken: silently
        # "re-resolving" would let a second answer overwrite the first with no
        # trace of which one the run actually proceeded under.
        die("%s was already resolved at %s (%s). File a NEW escalation if the question is "
            "open again." % (rid, rec.get("resolved_at"),
                             _oneline(rec.get("resolution_note")) or "no note"))
    cycle = args.cycle if args.cycle is not None else int(state.get("current_cycle", 0) or 0)
    _append_escalation(d, {
        "id": rid, "action": "resolve", "status": "resolved",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "cycle": cycle,
        "note": args.note, "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
    })
    _rewrite_escalations_md(d, root, project)
    print("%s resolved \u2014 %s \u00b7 %s" % (rid, rec.get("kind"), rec.get("subject")))
    print("  decision: " + (args.note or "(no note recorded \u2014 say what was decided next time)"))
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
    if args.resolve is not None:
        return _escalate_resolve(args, d, root, state, project)

    missing = [name for name, v in (("--kind", args.kind), ("--subject", args.subject),
                                    ("--reason", args.reason), ("--proposed", args.proposed))
               if not str(v or "").strip()]
    if missing:
        die("escalate needs " + " ".join(missing) + " \u2014 an escalation the user cannot act on "
            "is worse than none. Name the thing, the evidence that it is wrong, and the exact "
            "change you are asking for. (Or use --list / --resolve <ID>.)", 3)
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
        for m in goals["metrics"]:
            tmo = _metric_timeout(m, args.smoke_timeout)
            try:
                proc = _run_metric_command(m["command"], root, tmo)
            except subprocess.TimeoutExpired:
                smoke_failures.append(m["id"])
                print(f"  {m['id']:<28} FAIL: timeout after {tmo}s")
                continue
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
            if err <= tol:
                already_met.append(m["id"])
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {_target(m):g}, "
                      f"error {err:g})  ** ALREADY AT TARGET **")
            else:
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {_target(m):g}, error {err:g})")
        if smoke_failures:
            print(f"freeze: {len(smoke_failures)} metric command(s) failed the smoke-run: {', '.join(smoke_failures)}")
            print("manifest NOT written; goals remain unfrozen — fix the command(s) and re-run freeze")
            sys.exit(1)
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

    print(f"branch {args.branch} vs {base}: {len(changed)} changed file(s)"
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
    if protected_hits:
        print("VETO: branch modifies the frozen goal harness — do not merge; return task to backlog")
        sys.exit(1)
    if swarm_hits:
        print(f"VETO: branch writes orchestrator-owned state under {STATE_DIR}/ — do not merge")
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
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
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
        rows.append([args.cycle, m["id"], value, err, ts, "measure"])
        mark = "OK " if err == 0 else "   "
        print(f"  {m['id']:<28} value={value:<12g} target={_target(m):<10g} error={err:<12g} {mark}")
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
    _append_history(root, [[args.cycle, args.metric, args.value, err,
                            time.strftime("%Y-%m-%dT%H:%M:%S"), "record"]])
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
                (c, v, e, _row_source(row), str(row.get("timestamp") or "")))

    results = []
    pre_amendment_metrics, recomputed = [], []
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
        for c, v, e_stored, src, ts in history.get(m["id"], []):
            e = _error(v, tgt, m["direction"])
            if abs(e - e_stored) > 1e-9:
                recomputed.append(m["id"])
            by_cycle[c] = (v, e, src, ts)
        pts = [(c, v, e, src, ts) for c, (v, e, src, ts) in sorted(by_cycle.items())]
        if not pts:
            results.append({"id": m["id"], "verdict": "no_data",
                            "latest_cycle": None, "latest_source": None})
            continue
        err_points = [(c, e) for c, _v, e, _s, _t in pts]
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
            "pre_amendment": pre_amend,
            "n_points": len(pts),
            "error_slope_per_cycle": slope,
            "r2": r2,
            "projected_error_at_final_cycle": projected,
            "verdict": verdict,
            "history": [{"cycle": c, "value": v, "error": e, "source": s, "timestamp": t}
                        for c, v, e, s, t in pts],
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
    all_done = (n_at_target == len(results) and results and not stale_metrics
                and not pending
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
          f" · stall {state['stall_count']}/{state['stall_limit']} · autonomy {state['autonomy']}"
          f" · push {state['push_remote']}")
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
        rec, terminal = "kill switch already fired — go to Termination and write the final report", True
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
    else:
        hist.append(entry)
        state["stall_count"] = 0 if changed else state["stall_count"] + 1
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
    print(f"stall count: {state['stall_count']}/{state['stall_limit']}")

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
    if state["stall_count"] >= state["stall_limit"]:
        print(f"KILL SWITCH: no codebase changes for {state['stall_count']} consecutive cycles — terminate the loop and report")
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
    depth, busy = {}, set()

    def walk(tid):
        if tid in depth:
            return depth[tid]
        if tid in busy:
            return 0
        busy.add(tid)
        deps = [x for x in _graph_strs(idx.get(tid, {}), "depends_on") if x in idx]
        depth[tid] = 1 + max([walk(x) for x in deps] or [-1])
        busy.discard(tid)
        return depth[tid]

    for tid in idx:
        walk(tid)
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
    return any(ch in p for ch in "*?[")


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
    if not a or not b or a == "**" or b == "**":
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
        open_refs = {r["agent_ref"] for r in _open_dispatches(d)}
        if args.close not in open_refs:
            print(f"dispatch: {args.close} is not open (open: "
                  + (", ".join(sorted(open_refs)) or "none") + ") — recording the close anyway")
        with open(path, "a") as f:
            f.write(json.dumps({"agent_ref": args.close, "action": "close",
                                "closed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "note": args.note}, sort_keys=True) + "\n")
        print(f"dispatch: {args.close} closed")
        return
    missing = [n for n, v in (("--agent", args.agent), ("--kind", args.kind),
                              ("--subject", args.subject)) if not str(v or "").strip()]
    if missing:
        die("dispatch needs " + " ".join(missing)
            + " (or use --list / --close <agent-ref>)", 3)
    files = [x.strip() for x in (args.files or "").split(",") if x.strip()]
    with open(path, "a") as f:
        f.write(json.dumps({
            "agent_ref": args.agent, "action": "open", "kind": args.kind,
            "subject": args.subject, "target_files": files,
            "dispatched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "closed_at": None,
            "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
        }, sort_keys=True) + "\n")
    print(f"dispatch: recorded {args.agent} ({args.kind}) on {args.subject}")
    print(f"  close it at collection:  swarmloop.py dispatch --close {args.agent}")


def _closed_from_args(root, args):
    closed = set()
    for chunk in (args.closed or []):
        for tid in re.split(r"[,\s]+", chunk):
            if tid.strip():
                closed.add(tid.strip())
    if getattr(args, "closed_from_merged", False):
        out = subprocess.run(["git", "branch", "--merged", args.base, "--list", "task/*"],
                             capture_output=True, text=True, cwd=root).stdout
        for line in out.splitlines():
            for tid in re.findall(r"\bT-\d+\b", line):
                closed.add(tid)
    return closed


def cmd_frontier(args):
    """The ready set, with depth and transitive-unblocks. Replaces the ad-hoc
    Python heredoc the live orchestrator hand-wrote twice in one evening."""
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    path, tickets = _load_graph(root, state, args.graph)
    idx, dupes = _graph_index(tickets)
    errors = _graph_unusable(tickets) + _graph_validate(idx, dupes)
    closed = _closed_from_args(root, args)
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
        print(json.dumps({
            "graph": os.path.relpath(path, root),
            "tickets": len(idx), "closed": sorted(closed),
            "unknown_closed_ids": unknown_closed,
            "errors": errors,
            "ready": [{"id": t, "depth": depth.get(t, 0), "unblocks": unblocks.get(t, 0),
                       "verify": idx[t].get("verify"),
                       "owns": _ticket_owns(idx[t])[0]} for t, _ in ready],
            "blocked": [{"id": t, "waiting_on": od} for t, od in blocked],
        }, indent=2))
    else:
        print(f"graph: {os.path.relpath(path, root)}  ·  {len(idx)} ticket(s)  ·  "
              f"{len(closed)} closed  ·  {len(ready)} READY  ·  {len(blocked)} blocked")
        if unknown_closed:
            print("  NOTE: --closed named ids that are not in the graph: "
                  + ", ".join(unknown_closed))
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
    conflicts = []
    for i, a in enumerate(wave):
        for b in wave[i + 1:]:
            for pa in owns[a][0]:
                for pb in owns[b][0]:
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
                for pa in owns[t][0]:
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
    print("check-wave: OK — no two members can own the same file, and nothing in flight "
          "overlaps them")


def _collected_count(text):
    """pytest's collected/selected count, when it says one. None otherwise."""
    m = re.search(r"collected (\d+) item", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+) (?:passed|failed|error)", text)
    return int(m.group(1)) if m else None


def cmd_red_check(args):
    """Run each ticket's declared `verify` in a CLEAN worktree; fail any that
    already passes.

    H-05, and it was all five of wave 1, not one: T-010/T-013/T-014 exited 0 on
    nothing but T-000's scaffold smoke tests (`2 passed`), T-011/T-012 reported
    `no tests ran`. Not one of the five declared gates carried any signal, and a
    worker that runs its own verify and sees green has learned nothing. The
    orchestrator caught it by hand and wrote the warning into all five packets.

    H-7's correction is folded in: exit 4/5 is the CORRECT pre-build shape, not
    an environment failure — so the classification is by what was COLLECTED, not
    by the exit code alone. A gate that collects zero tests is weak for the same
    reason a green one is: nothing about the ticket is being observed."""
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

    base = args.base
    if subprocess.run(["git", "rev-parse", "--verify", base],
                      capture_output=True, cwd=root).returncode != 0:
        die(f"base ref '{base}' does not resolve — pass --base <ref>")
    vacuous, weak, red, nogate = [], [], [], []
    tmpbase = tempfile.mkdtemp(prefix="swarmloop-redcheck-")
    print(f"red-check: {len(sel)} ticket(s) against a clean worktree of {base}")
    try:
        for tid in sel:
            cmd = (idx[tid].get("verify") or "").strip()
            if not cmd:
                nogate.append(tid)
                print(f"  {tid:<9} NO GATE — the ticket declares no 'verify' command")
                continue
            wt = os.path.join(tmpbase, tid)
            # subprocess git, not a Bash tool call, so the skill's own PreToolUse
            # git-guard is not in the path here — this is the harness itself, and
            # it only ever creates and removes its OWN throwaway worktree.
            add = _git(root, "worktree", "add", "--detach", "-q", wt, base)
            if add.returncode != 0:
                print(f"  {tid:<9} SKIPPED — could not create a worktree: "
                      + (add.stderr or "").strip()[:160])
                continue
            try:
                proc = _run_metric_command(cmd, wt, args.timeout)
                out = (proc.stdout or "") + (proc.stderr or "")
                n = _collected_count(out)
                if proc.returncode == 0:
                    vacuous.append(tid)
                    print(f"  {tid:<9} VACUOUS — `{cmd}` EXITS 0 with nothing implemented"
                          + (f" ({n} test(s) collected)" if n else ""))
                elif not n:
                    weak.append(tid)
                    print(f"  {tid:<9} WEAK — exit {proc.returncode}, but NO tests were "
                          f"collected: `{cmd}` proves nothing about this ticket either way")
                else:
                    red.append(tid)
                    print(f"  {tid:<9} red (exit {proc.returncode}, {n} test(s) collected) — "
                          "a real gate")
            except subprocess.TimeoutExpired:
                weak.append(tid)
                print(f"  {tid:<9} WEAK — `{cmd}` timed out after {args.timeout}s")
            finally:
                _git(root, "worktree", "remove", "--force", wt)
    finally:
        _git(root, "worktree", "prune")
        try:
            os.rmdir(tmpbase)
        except OSError:
            pass

    print("")
    print(f"red-check: {len(red)} real · {len(vacuous)} VACUOUS · {len(weak)} weak · "
          f"{len(nogate)} with no gate")
    if vacuous or weak or nogate:
        print("A ticket's verify command must FAIL before the ticket is built. A gate that")
        print("passes on an empty tree measures the scaffold, not the work; a gate that")
        print("collects nothing measures nothing at all. Rewrite these before dispatching")
        print("them, and say in each task packet which grader is the real one.")
    if vacuous:
        sys.exit(1)


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

READ_TEST_RE = re.compile(r"^(test_.+|.+_test)\.py$")


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
    pat, path = _norm_pat(pat), _norm_pat(path)
    if not pat or not path:
        return False
    if pat.endswith("/**"):
        pre = pat[:-3]
        if path == pre or path.startswith(pre + "/"):
            return True
    return fnmatch.fnmatch(path, pat)


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
    out = {}

    def walk(tid, seen):
        for dep in _graph_strs(idx.get(tid, {}), "depends_on"):
            if dep in idx and dep not in seen:
                seen.add(dep)
                walk(dep, seen)
        return seen

    for tid in idx:
        out[tid] = walk(tid, set())
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
        file_marks, mod_level = [], set()
        for n in tree.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mod_level |= _dotted_imports(n)

        def visit(body, inherited):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    visit(n.body, _ticket_marks(n) or inherited)
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
                attribute(marks, _dotted_imports(n), n.lineno)

        visit(tree.body, [])
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
    res = _scan_read_edges(root, state, idx, args.tests, _closed_from_args(root, args))

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
        "stall": f"{state['stall_count']}/{state['stall_limit']}",
        "goals_frozen": goals.get("frozen", False),
        "goal_amendments": int(goals.get("amendments", 0)),
        "last_amended_at": goals.get("last_amended_at"),
        "n_metrics": len(goals.get("metrics", [])),
    }, indent=2))


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
    s.add_argument("--stall-limit", type=int, default=3)
    s.add_argument("--worktree-root", default=None,
                   help="where task worktrees are created (default: a SIBLING dir "
                        "../<repo>-worktrees, so they stay out of every root-walking metric "
                        f"command; falls back to {STATE_DIR}/worktrees/ if the parent is not "
                        "writable). Recorded in state.json so 'resume' can find them.")
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
                   help="mark an escalation resolved (the user decided); pair with --note")
    s.add_argument("--note", default=None, metavar="TEXT",
                   help="with --resolve: what the user decided")
    s.add_argument("--list", action="store_true", dest="do_list",
                   help="print OPEN escalations (the only ones anything is waiting on); "
                        "exits 2 if any of them is blocking")
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
                   help="also treat every T-nnn named by a task/* branch merged into --base "
                        "as closed")
    s.add_argument("--base", default="main")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_frontier)

    s = sub.add_parser("check-wave", help="refuse a wave whose members could own the same file")
    s.add_argument("tickets", nargs="+", metavar="T-0xx")
    s.add_argument("--graph", default=None, metavar="PATH")
    s.set_defaults(func=cmd_check_wave)

    s = sub.add_parser("red-check", help="prove each ticket's verify command FAILS before it "
                                         "is built")
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
                   help="per-ticket timeout in seconds (default 600)")
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
    s.add_argument("--close", default=None, metavar="REF", help="mark a dispatch collected")
    s.add_argument("--note", default=None, metavar="TEXT")
    s.add_argument("--list", action="store_true", dest="do_list",
                   help="print dispatches that were never closed")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("resume", help="reconstruct run state after an interrupted session")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("status", help="print loop state")
    s.set_defaults(func=cmd_status)

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
