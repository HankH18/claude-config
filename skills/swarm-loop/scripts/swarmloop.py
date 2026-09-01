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
import csv
import difflib
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
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
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
IGNORED_SUFFIXES = (".pyc", ".pyo")


def is_artifact_path(rel):
    """True for tool-cache/bytecode paths, which iter_files skips.

    check-branch needs the SAME filter freeze/verify use: running the frozen
    acceptance suite (which task-packets.md explicitly invites workers to do)
    writes .swarm-loop/acceptance/__pycache__/*.pyc, and without this the two
    halves of the same protection model disagree — verify says intact, while
    check-branch vetoes the branch as a harness tamper."""
    parts = rel.replace("\\", "/").split("/")
    if any(p in IGNORED_DIRS for p in parts[:-1]):
        return True
    return parts[-1].endswith(IGNORED_SUFFIXES) or parts[-1] in IGNORED_DIRS


class HarnessSymlink(Exception):
    """A symlinked DIRECTORY under a protected path. Fatal — see iter_files."""


def _symlink_msg(root, exc):
    path = str(exc)
    try:
        dest = os.readlink(path)
    except OSError:
        dest = "?"
    return (f"symlinked directory under a protected path: {os.path.relpath(path, root)} "
            f"-> {dest!r}. os.walk does not descend a symlinked directory, so freeze "
            "records ZERO hashes for that whole subtree and verify's ADDED scan cannot "
            "see behind it either — the acceptance tests it points at could be gutted "
            "with nothing noticing. Replace the symlink with the real files.")


def iter_files(base):
    """Walk files under base, skipping tool caches/bytecode — build artifacts are
    not tampering, and including them makes every real run a false positive.

    A symlinked DIRECTORY is a hard error, not a skip: it is invisible to both
    halves of the integrity check, so silently walking past one would hand back
    a manifest that protects less than it claims to."""
    if os.path.isfile(base):
        yield base
        return
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in IGNORED_DIRS]
        for name in dirs:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                raise HarnessSymlink(p)
        for name in sorted(files):
            if not name.endswith(IGNORED_SUFFIXES):
                yield os.path.join(dirpath, name)


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
        "protected_paths": [
            f"{STATE_DIR}/acceptance",
            f"{STATE_DIR}/{GOALS_FILE}",
        ],
    }
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
    files = {}
    try:
        for rel in state["protected_paths"]:
            base = os.path.join(root, rel)
            if not os.path.exists(base):
                continue
            for path in iter_files(base):
                files[os.path.relpath(path, root)] = sha256_file(path)
    except HarnessSymlink as e:
        # Fatal, not skippable: hashing around it would produce a manifest that
        # protects less than it claims to, and nothing downstream would know.
        die(_symlink_msg(root, e))
    return files


def _manifest_delta(old, new):
    """(added, removed, changed) between two {relpath: sha256} maps."""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(r for r in (set(old) & set(new)) if old[r] != new[r])
    return added, removed, changed


def _commit_with_blob(root, rel, digest):
    """Newest commit reachable from HEAD whose blob for <rel> hashes to <digest>.

    Deliberately NOT the freeze-time HEAD: .swarm-loop/ is committed *after*
    freeze + baseline (goal-setting.md, "Baseline"), so the freeze-time commit
    does not contain the frozen files at all and recording it would be useless.
    Matching on the manifest digest finds the right commit regardless — and
    proves the bytes it holds are the frozen ones rather than assuming it."""
    p = _git(root, "rev-list", "HEAD", "--", rel)
    if p.returncode != 0:
        return None
    for commit in p.stdout.split():
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


def _freeze_logged(d, frozen_at):
    """True when freeze-log.jsonl records a COMPLETED freeze at this timestamp.

    freeze writes the manifest, renames goals.json into place, and only then
    appends its log line — so a manifest whose frozen_at never reached the log is
    the signature of a freeze that died mid-write (resume's 'a freeze died
    between the two writes' branch). Nothing was ever locked in that state, and
    re-running freeze is the documented repair, so the goalpost gate must let it
    through."""
    path = os.path.join(d, FREEZE_LOG_FILE)
    if not frozen_at or not os.path.exists(path):
        return False
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
                if isinstance(rec, dict) and rec.get("frozen_at") == frozen_at:
                    return True
    except OSError:
        return False
    return False


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

    if not goals.get("metrics"):
        die("goals.json has no metrics — define goals before freezing")
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
    crashed_freeze = (prior is not None and not frozen_before
                      and os.path.exists(os.path.join(d, GOALS_FILE) + ".tmp")
                      and not _freeze_logged(d, prior.get("frozen_at")))
    locked_before = frozen_before or (prior is not None and not crashed_freeze)
    if args.amend is not None and not args.amend.strip():
        die("--amend requires a reason: the user's own words for why this frozen goal moves", 3)
    if args.amend and not locked_before:
        print("note: goals were not frozen — --amend ignored, this is the initial freeze")
        args.amend = None
    # An amendment is recorded when a LOCKED harness is re-frozen; a manifest
    # with no delta and no --amend is just an ordinary (re)freeze.
    amending = locked_before and (frozen_before or bool(args.amend))
    pre = added = removed = changed = None
    pre_bytes = {}
    if locked_before:
        pre = _protected_hashes(root, state)
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
            err = _error(value, float(m["target"]), m["direction"])
            tol = float(m.get("tolerance", 0) or 0)
            if err <= tol:
                already_met.append(m["id"])
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {m['target']:g}, "
                      f"error {err:g})  ** ALREADY AT TARGET **")
            else:
                print(f"  {m['id']:<28} ok (parsed {value:g}, target {m['target']:g}, error {err:g})")
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

    files = _protected_hashes(root, state)
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

    save_json(os.path.join(d, MANIFEST_FILE), {
        "frozen_at": goals["frozen_at"],
        "amendments": int(goals.get("amendments", 0)),
        "files": files,
    })
    os.replace(gtmp, gpath)
    head = _git(root, "rev-parse", "HEAD").stdout.strip() or None

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
        linked = []
        for r in _open_escalations(d):
            subj = (r.get("subject") or "").strip()
            if not subj:
                continue
            if (subj in touched or subj in basenames or subj in metric_ids
                    or any(p.endswith("/" + subj) for p in touched)):
                linked.append(r)
        _append_freeze_log(d, {
            "action": "amend",
            "amendment": goals["amendments"],
            "reason": args.amend,
            "prev_frozen_at": prev_frozen_at,
            "frozen_at": goals["frozen_at"],
            "head": head,
            "escalations": [r["id"] for r in linked],
            "modified": [{"path": r, "old": old_files[r], "new": pre[r]} for r in changed],
            "added": [{"path": r, "new": pre[r]} for r in added],
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
            "reason": None,
            "prev_frozen_at": None,
            "frozen_at": goals["frozen_at"],
            "head": head,
            "modified": [], "added": [{"path": r, "new": h} for r, h in sorted(files.items())],
            "removed": [],
        })

    print(f"frozen: {len(files)} file(s) under protection")
    for rel in sorted(files):
        print(f"  {rel}")
    print("goals and acceptance tests are now locked; 'verify' will detect any change")


# ------------------------------------------------------------------ verify

def cmd_verify(args):
    root = repo_root()
    d = sd(root)
    manifest = load_json(os.path.join(d, MANIFEST_FILE), "manifest (run 'freeze' first)")
    bad = []
    for rel, digest in manifest["files"].items():
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            bad.append((rel, "MISSING"))
        elif sha256_file(path) != digest:
            bad.append((rel, "MODIFIED"))
    # new files added under protected dirs are also a violation
    state = load_json(os.path.join(d, STATE_FILE), "state")
    try:
        for relbase in state["protected_paths"]:
            base = os.path.join(root, relbase)
            if not os.path.isdir(base):
                continue
            for path in iter_files(base):
                rel = os.path.relpath(path, root)
                if rel not in manifest["files"]:
                    bad.append((rel, "ADDED"))
    except HarnessSymlink as e:
        print("INTEGRITY VIOLATION — " + _symlink_msg(root, e))
        sys.exit(1)
    if bad:
        print("INTEGRITY VIOLATION — the frozen goal harness was altered:")
        for rel, kind in bad:
            print(f"  [{kind}] {rel}")
        print("scores against a modified harness are void. Restore the frozen bytes with")
        print("  swarmloop.py restore-harness")
        print("(`git checkout -- <path>` / `git restore <path>` are blocked by this skill's own")
        print(" git-guard hook; `git show <commit>:<path> > <path>` is the allowed manual form.)")
        print("Do NOT re-run 'freeze' — that re-locks the tampered bytes; freeze refuses without --amend.")
        sys.exit(1)
    print("verify: OK — frozen harness intact")


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
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(HISTORY_HEADER)
        for r in rows:
            w.writerow(r)


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


def cmd_measure(args):
    root = repo_root()
    d = sd(root)
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    if not goals.get("frozen"):
        die("goals are not frozen — freeze before measuring so scores are against locked targets")
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
        err = _error(value, float(m["target"]), m["direction"])
        rows.append([args.cycle, m["id"], value, err, ts, "measure"])
        mark = "OK " if err == 0 else "   "
        print(f"  {m['id']:<28} value={value:<12g} target={m['target']:<10g} error={err:<12g} {mark}")
    _append_history(root, rows)
    if failures:
        print(f"measure: {len(failures)} metric command(s) failed: {', '.join(failures)}")
        print("a failing measurement command is itself a defect — fix the command or the code it measures")
        sys.exit(1)
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
    integrity, bad = _verify_quiet(root, d, state)
    # goals.json says frozen, so _verify_quiet's "not frozen" can only mean the
    # MANIFEST is gone — which freeze's own refusal text calls an integrity
    # violation, not an absence of one. Left as-is this gate failed OPEN:
    # deleting manifest.json turned the refusal into 'recorded ... EXIT=0'.
    if integrity == "not frozen":
        integrity = "VIOLATED"
        bad = [f"[MISSING] {STATE_DIR}/{MANIFEST_FILE} — the frozen manifest itself is gone; "
               f"restore it from the commit that holds it (`git show <commit>:{STATE_DIR}/"
               f"{MANIFEST_FILE} > {STATE_DIR}/{MANIFEST_FILE}`)"]
    if integrity == "VIOLATED":
        print("INTEGRITY VIOLATION — the frozen goal harness was altered:")
        for b in bad:
            print(f"  {b}")
        die("refusing to write to the scoreboard against a modified harness — "
            "run 'restore-harness', then 'verify', then re-record")
    metric = next((m for m in goals["metrics"] if m["id"] == args.metric), None)
    if metric is None:
        die(f"unknown metric id '{args.metric}'")
    if not math.isfinite(args.value):
        die(f"refusing to record non-finite value {args.value!r} for '{args.metric}'")
    err = _error(args.value, float(metric["target"]), metric["direction"])
    _append_history(root, [[args.cycle, args.metric, args.value, err,
                            time.strftime("%Y-%m-%dT%H:%M:%S"), "record"]])
    print(f"recorded {args.metric}={args.value} (error={err}) for cycle {args.cycle}")
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
    max_cycles = state["max_cycles"]

    history = {}
    hpath = os.path.join(d, HISTORY_FILE)
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
            history.setdefault(row["metric_id"], []).append((c, v, e, _row_source(row)))

    results = []
    for m in goals["metrics"]:
        # Keep the LAST measurement per cycle in FILE order -- history.csv is
        # append-only, so file order is recency. (Sorting the tuples first, as
        # this once did, keeps the largest value instead, so a stale optimistic
        # reading silently survives a corrective re-measure.) Sort by cycle only
        # after the dedupe.
        by_cycle = {}
        for c, v, e, src in history.get(m["id"], []):
            by_cycle[c] = (v, e, src)
        pts = [(c, v, e, src) for c, (v, e, src) in sorted(by_cycle.items())]
        if not pts:
            results.append({"id": m["id"], "verdict": "no_data",
                            "latest_cycle": None, "latest_source": None})
            continue
        err_points = [(c, e) for c, _v, e, _s in pts]
        # `or 0`: a "tolerance": null in goals.json is a TypeError here, and
        # crashing analyze every retry is an infinite loop (resume's advice for a
        # measured-but-unanalyzed cycle is "run analyze --cycle N"). freeze now
        # rejects it outright; this keeps an already-frozen run from wedging.
        tol = float(m.get("tolerance") or 0)
        verdict, slope, projected, r2 = _verdict(err_points, tol, max_cycles)
        # A verdict is only about THIS cycle if THIS cycle produced a row. When
        # 'measure --cycle N' fails (wholly or for one metric) it appends nothing
        # and exits 1; without this check the newest surviving row — possibly
        # several cycles old — is reported under the cycle-N heading, and can even
        # declare ALL TARGETS MET off a measurement that never ran.
        if pts[-1][0] != args.cycle:
            verdict, slope, projected, r2 = "stale", None, None, None
        results.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "direction": m["direction"],
            "target": m["target"],
            "latest_cycle": pts[-1][0],
            "latest_value": pts[-1][1],
            "latest_error": pts[-1][2],
            "latest_source": pts[-1][3],
            "n_points": len(pts),
            "error_slope_per_cycle": slope,
            "r2": r2,
            "projected_error_at_final_cycle": projected,
            "verdict": verdict,
            "history": [{"cycle": c, "value": v, "error": e, "source": s} for c, v, e, s in pts],
        })

    results.sort(key=lambda r: (SEVERITY.get(r["verdict"], 9), -float(r.get("latest_error") or 0)))
    n_at_target = sum(1 for r in results if r["verdict"] == "at_target")
    stale_metrics = [r["id"] for r in results if r["verdict"] in ("stale", "no_data")]
    # A hand-entered row outranks the real measurement for its cycle (analyze
    # keeps the last row per cycle). Legitimate as a documented escape hatch,
    # never as the basis of a terminal ALL-TARGETS-MET signal nobody approved.
    manual_metrics = [r["id"] for r in results if r.get("latest_source") == "record"]
    all_done = (n_at_target == len(results) and results and not stale_metrics
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

def _verify_quiet(root, d, state):
    mpath = os.path.join(d, MANIFEST_FILE)
    if not os.path.exists(mpath):
        return "not frozen", []
    manifest = load_json(mpath, "manifest")
    bad = []
    for rel, digest in manifest["files"].items():
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            bad.append(f"[MISSING] {rel}")
        elif sha256_file(path) != digest:
            bad.append(f"[MODIFIED] {rel}")
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
        bad.append("[SYMLINKED DIR] " + _symlink_msg(root, e))
    return ("VIOLATED", bad) if bad else ("intact", [])


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
    hpath = os.path.join(d, HISTORY_FILE)
    if os.path.exists(hpath):
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
    integrity, bad = _verify_quiet(root, d, state)
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
