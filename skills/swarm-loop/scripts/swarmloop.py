#!/usr/bin/env python3
"""swarmloop.py — deterministic harness for the swarm-loop skill.

Owns the numeric/state side of the loop so the orchestrating agent never has to
eyeball a trend or hand-compute a regression. Pure stdlib. All state lives in
.swarm-loop/ at the repo root.

Subcommands:
  init        Create .swarm-loop/ state (cycle budget, autonomy, stall limit)
  freeze      Hash goals.json + protected paths into manifest.json (goal lock)
  verify      Re-hash and compare against manifest; nonzero exit on tampering
  check-branch  Flag a git branch that touches protected paths (pre-merge veto)
  measure     Run every metric command, record values+errors for a cycle
  record      Manually record one metric value for a cycle
  analyze     Per-metric OLS over cycle history: slope, projection, verdict
  checkpoint  Record HEAD for the cycle; drive the no-change kill switch
  audit-loop-done  Record a completed audit-fix loop (exit 2 when terminal)
  resume      Reconstruct an interrupted run from artifacts alone
  status      Print loop state summary

Exit codes: 0 ok · 1 error/violation · 2 kill switch or terminal condition ·
3 usage error (bad flag/subcommand — never a terminal signal; fix and re-run).
"""

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time

STATE_DIR = ".swarm-loop"
STATE_FILE = "state.json"
GOALS_FILE = "goals.json"
MANIFEST_FILE = "manifest.json"
HISTORY_FILE = "history.csv"
ANALYSIS_DIR = "analysis"

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


def iter_files(base):
    """Walk files under base, skipping tool caches/bytecode — build artifacts are
    not tampering, and including them makes every real run a false positive."""
    if os.path.isfile(base):
        yield base
        return
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in IGNORED_DIRS]
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
        stale_history = os.path.exists(hist) and sum(1 for _ in open(hist)) > 1
        analysis_files = sorted(os.listdir(adir)) if os.path.isdir(adir) else []
        if stale_history or analysis_files:
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
            print(f"archived {n_moved} stale run file(s) (history.csv + analysis/) "
                  f"to {os.path.relpath(arch, root)}; starting fresh history")
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
            csv.writer(f).writerow(["cycle", "metric_id", "value", "error", "timestamp"])

    # keep worktrees and archived stale runs out of the tree
    gi = os.path.join(root, ".gitignore")
    existing = open(gi).read() if os.path.exists(gi) else ""
    for marker in (f"{STATE_DIR}/worktrees/", f"{STATE_DIR}/archive/"):
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

    # Smoke-run every metric command exactly as 'measure' will run it, BEFORE
    # locking anything — a non-portable command that freezes fine only explodes
    # at measure time, when fixing it requires editing goals and re-freezing.
    # This is a probe, not a measurement: values are NOT recorded in history.
    if not args.skip_smoke:
        print("smoke-running metric commands (probe only — nothing is recorded):")
        smoke_failures = []
        for m in goals["metrics"]:
            try:
                proc = subprocess.run(
                    m["command"], shell=True, capture_output=True, text=True, cwd=root,
                    timeout=args.smoke_timeout,
                )
            except subprocess.TimeoutExpired:
                smoke_failures.append(m["id"])
                print(f"  {m['id']:<28} FAIL: timeout after {args.smoke_timeout}s")
                continue
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            try:
                value = float(lines[-1])
            except (IndexError, ValueError):
                smoke_failures.append(m["id"])
                if proc.returncode != 0:
                    print(f"  {m['id']:<28} FAIL: nonzero exit ({proc.returncode}), no numeric last line")
                else:
                    print(f"  {m['id']:<28} FAIL: no numeric last line")
                if proc.stderr.strip():
                    print("      stderr: " + proc.stderr.strip().splitlines()[-1][:200])
                continue
            print(f"  {m['id']:<28} ok (last line parsed as {value:g})")
        if smoke_failures:
            print(f"freeze: {len(smoke_failures)} metric command(s) failed the smoke-run: {', '.join(smoke_failures)}")
            print("manifest NOT written; goals remain unfrozen — fix the command(s) and re-run freeze")
            sys.exit(1)

    goals["frozen"] = True
    goals["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(os.path.join(d, GOALS_FILE), goals)

    files = {}
    for rel in state["protected_paths"]:
        base = os.path.join(root, rel)
        if not os.path.exists(base):
            continue
        for path in iter_files(base):
            files[os.path.relpath(path, root)] = sha256_file(path)
    if not files:
        die("nothing to freeze — no files found under protected paths")

    save_json(os.path.join(d, MANIFEST_FILE), {
        "frozen_at": goals["frozen_at"],
        "files": files,
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
    for relbase in state["protected_paths"]:
        base = os.path.join(root, relbase)
        if not os.path.isdir(base):
            continue
        for path in iter_files(base):
            rel = os.path.relpath(path, root)
            if rel not in manifest["files"]:
                bad.append((rel, "ADDED"))
    if bad:
        print("INTEGRITY VIOLATION — the frozen goal harness was altered:")
        for rel, kind in bad:
            print(f"  [{kind}] {rel}")
        print("restore the harness from git before scoring this cycle; scores against a modified harness are void")
        sys.exit(1)
    print("verify: OK — frozen harness intact")


# ------------------------------------------------------------- check-branch

def cmd_check_branch(args):
    root = repo_root()
    d = sd(root)
    state = load_json(os.path.join(d, STATE_FILE), "state")
    base = args.base
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{args.branch}"],
            capture_output=True, text=True, check=True, cwd=root,
        )
    except subprocess.CalledProcessError as e:
        die(f"git diff failed: {e.stderr.strip()}")
    changed = [line for line in out.stdout.splitlines() if line.strip()]
    protected_hits = []
    swarm_hits = []
    for path in changed:
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in state["protected_paths"]):
            protected_hits.append(path)
        elif path == STATE_DIR or path.startswith(STATE_DIR + "/"):
            swarm_hits.append(path)

    print(f"branch {args.branch} vs {base}: {len(changed)} changed file(s)")
    for path in changed:
        tag = "  "
        if path in protected_hits:
            tag = "!!"
        elif path in swarm_hits:
            tag = " !"
        print(f" {tag} {path}")
    if protected_hits:
        print("VETO: branch modifies the frozen goal harness — do not merge; return task to backlog")
        sys.exit(1)
    if swarm_hits:
        print(f"VETO: branch writes orchestrator-owned state under {STATE_DIR}/ — do not merge")
        sys.exit(1)
    print("check-branch: OK (review the file list above against the task's ownership map)")


# ----------------------------------------------------------- measure/record

def _append_history(root, rows):
    with open(os.path.join(sd(root), HISTORY_FILE), "a", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)


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
        try:
            proc = subprocess.run(
                m["command"], shell=True, capture_output=True, text=True, cwd=root,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            failures.append(m["id"])
            print(f"  {m['id']:<28} FAILED: timeout after {args.timeout}s")
            continue
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        try:
            value = float(lines[-1])
        except (IndexError, ValueError):
            failures.append(m["id"])
            print(f"  {m['id']:<28} FAILED to produce a number (exit {proc.returncode})")
            if proc.stderr.strip():
                print("      stderr: " + proc.stderr.strip().splitlines()[-1][:200])
            continue
        err = _error(value, float(m["target"]), m["direction"])
        rows.append([args.cycle, m["id"], value, err, ts])
        mark = "OK " if err == 0 else "   "
        print(f"  {m['id']:<28} value={value:<12g} target={m['target']:<10g} error={err:<12g} {mark}")
    _append_history(root, rows)
    if failures:
        print(f"measure: {len(failures)} metric command(s) failed: {', '.join(failures)}")
        print("a failing measurement command is itself a defect — fix the command or the code it measures")
        sys.exit(1)
    print(f"measure: recorded {len(rows)} metric(s) for cycle {args.cycle}")


def cmd_record(args):
    root = repo_root()
    d = sd(root)
    goals = load_json(os.path.join(d, GOALS_FILE), "goals")
    metric = next((m for m in goals["metrics"] if m["id"] == args.metric), None)
    if metric is None:
        die(f"unknown metric id '{args.metric}'")
    err = _error(args.value, float(metric["target"]), metric["direction"])
    _append_history(root, [[args.cycle, args.metric, args.value, err,
                            time.strftime("%Y-%m-%dT%H:%M:%S")]])
    print(f"recorded {args.metric}={args.value} (error={err}) for cycle {args.cycle}")


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


SEVERITY = {"regressing": 0, "stalled": 1, "converging_off_track": 2,
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
    max_cycles = state["max_cycles"]

    history = {}
    with open(os.path.join(d, HISTORY_FILE)) as f:
        for row in csv.DictReader(f):
            c = int(row["cycle"])
            # Analyze the run AS OF --cycle: later rows (a re-measure of a newer
            # cycle) must not leak into a re-analysis of an earlier one.
            if c > args.cycle:
                continue
            history.setdefault(row["metric_id"], []).append(
                (c, float(row["value"]), float(row["error"])))

    results = []
    for m in goals["metrics"]:
        # Keep the LAST measurement per cycle in FILE order -- history.csv is
        # append-only, so file order is recency. (Sorting the tuples first, as
        # this once did, keeps the largest value instead, so a stale optimistic
        # reading silently survives a corrective re-measure.) Sort by cycle only
        # after the dedupe.
        by_cycle = {}
        for c, v, e in history.get(m["id"], []):
            by_cycle[c] = (v, e)
        pts = [(c, v, e) for c, (v, e) in sorted(by_cycle.items())]
        if not pts:
            results.append({"id": m["id"], "verdict": "no_data"})
            continue
        err_points = [(c, e) for c, _v, e in pts]
        tol = float(m.get("tolerance", 0.0))
        verdict, slope, projected, r2 = _verdict(err_points, tol, max_cycles)
        results.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "direction": m["direction"],
            "target": m["target"],
            "latest_cycle": pts[-1][0],
            "latest_value": pts[-1][1],
            "latest_error": pts[-1][2],
            "n_points": len(pts),
            "error_slope_per_cycle": slope,
            "r2": r2,
            "projected_error_at_final_cycle": projected,
            "verdict": verdict,
            "history": [{"cycle": c, "value": v, "error": e} for c, v, e in pts],
        })

    results.sort(key=lambda r: (SEVERITY.get(r["verdict"], 9), -float(r.get("latest_error") or 0)))
    n_at_target = sum(1 for r in results if r["verdict"] == "at_target")
    all_done = n_at_target == len(results) and results

    out = {
        "cycle": args.cycle,
        "max_cycles": max_cycles,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "all_targets_met": bool(all_done),
        "metrics": results,
        "note": ("Linear fit over per-cycle error. With <=10 points this is trend "
                 "detection, not inference; treat slope/projection as a steering "
                 "signal, not a statistical claim."),
    }
    jpath = os.path.join(d, ANALYSIS_DIR, f"cycle-{args.cycle}.json")
    save_json(jpath, out)

    lines = [f"# Cycle {args.cycle}/{max_cycles} analysis", ""]
    lines.append(f"Targets met: {n_at_target}/{len(results)}")
    lines.append("")
    lines.append("| metric | verdict | value | target | error | slope/cycle | proj. final error |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        if r["verdict"] == "no_data":
            lines.append(f"| {r['id']} | no_data | – | – | – | – | – |")
            continue
        fmt = lambda x: "–" if x is None else f"{x:g}"
        lines.append(
            f"| {r['id']} | {r['verdict']} | {fmt(r['latest_value'])} | {fmt(r['target'])} "
            f"| {fmt(r['latest_error'])} | {fmt(r['error_slope_per_cycle'])} "
            f"| {fmt(r['projected_error_at_final_cycle'])} |")
    lines.append("")
    if all_done:
        lines.append("**ALL TARGETS MET — act-dependent: during the build this is the "
                     "build→audit transition; during the audit act it is the expected "
                     "steady state (see SKILL.md Phase 7).**")
    else:
        worst = [r for r in results if r["verdict"] in ("regressing", "stalled", "converging_off_track")]
        if worst:
            lines.append("Priority for next cycle (worst first): " + ", ".join(r["id"] for r in worst))
    mpath = os.path.join(d, ANALYSIS_DIR, f"cycle-{args.cycle}.md")
    with open(mpath, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines))
    print(f"\nwrote {os.path.relpath(jpath, root)} and {os.path.relpath(mpath, root)}")
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
    for relbase in state["protected_paths"]:
        base = os.path.join(root, relbase)
        if not os.path.isdir(base):
            continue
        for path in iter_files(base):
            rel = os.path.relpath(path, root)
            if rel not in manifest["files"]:
                bad.append(f"[ADDED] {rel}")
    return ("VIOLATED", bad) if bad else ("intact", [])


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
    measured = set()
    hpath = os.path.join(d, HISTORY_FILE)
    if os.path.exists(hpath):
        with open(hpath) as f:
            for row in csv.DictReader(f):
                measured.add(int(row["cycle"]))
    analyzed, all_met = set(), False
    adir = os.path.join(d, ANALYSIS_DIR)
    if os.path.isdir(adir):
        for name in os.listdir(adir):
            if name.startswith("cycle-") and name.endswith(".json"):
                c = int(name[len("cycle-"):-len(".json")])
                analyzed.add(c)
    if analyzed:
        latest_analysis = load_json(os.path.join(adir, f"cycle-{max(analyzed)}.json"), "analysis")
        all_met = latest_analysis.get("all_targets_met", False)
    checkpointed = {h["cycle"] for h in state["head_history"]}
    reported = set()
    rdir = os.path.join(d, "reports")
    if os.path.isdir(rdir):
        for name in os.listdir(rdir):
            if name.startswith("cycle-") and name.endswith(".md"):
                reported.add(int(name[len("cycle-"):-len(".md")]))
    final_report = os.path.exists(os.path.join(rdir, "final-report.md"))

    # --- git situation
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, cwd=root).stdout

    worktrees = [ln.split(" ", 1)[1] for ln in git("worktree", "list", "--porcelain").splitlines()
                 if ln.startswith("worktree ") and f"{STATE_DIR}/worktrees" in ln]
    merged = set(b.strip().lstrip("*+ ") for b in git("branch", "--merged", "main", "--list",
                                                     "task/*", "integration/*").splitlines() if b.strip())
    allb = set(b.strip().lstrip("*+ ") for b in git("branch", "--list",
                                                   "task/*", "integration/*").splitlines() if b.strip())
    unmerged = sorted(allb - merged)
    dirty = bool(git("status", "--porcelain").strip())
    integrity, bad = _verify_quiet(root, d, state)

    # --- report
    print(f"=== swarm-loop resume report: {state['project']} ===")
    print(f"budget          : cycle {state['current_cycle']}/{state['max_cycles']} checkpointed"
          f" · stall {state['stall_count']}/{state['stall_limit']} · autonomy {state['autonomy']}"
          f" · push {state['push_remote']}")
    print(f"goals           : {'frozen' if goals.get('frozen') else 'NOT FROZEN'} ({len(goals.get('metrics', []))} metrics)"
          f" · harness {integrity}")
    for b in bad:
        print(f"                  {b}")
    hi = max(measured | analyzed | checkpointed | reported | {0})
    print("cycle artifacts : (m)easured (a)nalyzed (c)heckpointed (r)eported")
    for c in range(0, hi + 1):
        flags = "".join(x if c in s else "-" for x, s in
                        [("m", measured), ("a", analyzed), ("c", checkpointed), ("r", reported)])
        print(f"                  cycle {c}: {flags}")
    print(f"working tree    : {'DIRTY — inspect git status first' if dirty else 'clean'}")
    print(f"live worktrees  : {len(worktrees)}" + ("".join(f"\n                  {w}" for w in worktrees)))
    print(f"unmerged branches: {len(unmerged)}" + ("".join(f"\n                  {b}" for b in unmerged)))

    # --- recommendation (heuristic — confirm against the latest cycle report)
    rec, terminal = None, False
    if final_report:
        rec, terminal = "final report already written — this run is closed; start a new run with init --force", True
    elif integrity == "VIOLATED":
        rec = "restore the frozen harness from git before anything else — scores are void until 'verify' passes"
    elif not goals.get("frozen"):
        rec = "Phase 2 incomplete: finish goals + acceptance tests, freeze, then baseline (measure/checkpoint --cycle 0)"
    elif all_met:
        # All-green is act-dependent: during the build it is the build->audit
        # transition, and mid-audit-act it is the expected steady state. Only a
        # clean loop or an exhausted loop budget makes all-green terminal.
        budget = state.get("audit_loops", 0)
        done = state.get("audit_loops_completed", 0)
        if state.get("clean_audit"):
            rec, terminal = ("clean audit loop recorded — run Phase 9 (prune) if not yet done, "
                             "then go to Termination and write the final report"), True
        elif budget > 0 and done < budget:
            rec = (f"all targets met ({done}/{budget} audit loops done) — NOT terminal: "
                   "if the ticket ledger is empty this is the build→audit transition / next "
                   "audit loop; if build tickets remain open, keep building")
        elif budget > 0:
            rec, terminal = (f"all targets met and audit budget exhausted ({done}/{budget}) — "
                             "go to Termination and write the final report"), True
        else:
            rec, terminal = "all targets met per latest analysis — go to Termination and write the final report", True
    elif state["stall_count"] >= state["stall_limit"]:
        rec, terminal = "kill switch already fired — go to Termination and write the final report", True
    else:
        L = max(measured | analyzed | {state["current_cycle"]})
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

def cmd_checkpoint(args):
    root = repo_root()
    d = sd(root)
    spath = os.path.join(d, STATE_FILE)
    state = load_json(spath, "state")
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True, cwd=root).stdout.strip()
    tracked_files = len(subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--name-only"],
        capture_output=True, text=True, check=True, cwd=root,
    ).stdout.splitlines())
    prev_entry = state["head_history"][-1] if state["head_history"] else None
    prev = prev_entry["head"] if prev_entry else None
    changed = head != prev
    state["head_history"].append({"cycle": args.cycle, "head": head, "changed": changed,
                                  "tracked_files": tracked_files})
    state["stall_count"] = 0 if changed else state["stall_count"] + 1
    state["current_cycle"] = args.cycle
    save_json(spath, state)

    print(f"checkpoint cycle {args.cycle}: HEAD {head[:12]} ({'changed' if changed else 'UNCHANGED'})")
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
    print(json.dumps({
        "project": state["project"],
        "cycle": f"{state['current_cycle']}/{state['max_cycles']}",
        "audit_loops": f"{state.get('audit_loops_completed', 0)}/{state.get('audit_loops', 0)}"
                       + (" CLEAN" if state.get("clean_audit") else ""),
        "autonomy": state["autonomy"],
        "push_remote": state["push_remote"],
        "stall": f"{state['stall_count']}/{state['stall_limit']}",
        "goals_frozen": goals.get("frozen", False),
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
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("freeze", help="lock goals.json + acceptance tests")
    s.add_argument("--skip-smoke", action="store_true",
                   help="skip the metric smoke-run (not recommended)")
    s.add_argument("--smoke-timeout", type=int, default=600,
                   help="per-command smoke-run timeout in seconds (default matches measure)")
    s.set_defaults(func=cmd_freeze)

    s = sub.add_parser("verify", help="check the frozen harness is intact")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("check-branch", help="veto branches touching protected/orchestrator paths")
    s.add_argument("--branch", required=True)
    s.add_argument("--base", default="main")
    s.set_defaults(func=cmd_check_branch)

    s = sub.add_parser("measure", help="run all metric commands and record")
    s.add_argument("--cycle", type=int, required=True)
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(func=cmd_measure)

    s = sub.add_parser("record", help="record one metric value manually")
    s.add_argument("--cycle", type=int, required=True)
    s.add_argument("--metric", required=True)
    s.add_argument("--value", type=float, required=True)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("analyze", help="regression + verdict per metric")
    s.add_argument("--cycle", type=int, required=True)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("checkpoint", help="record HEAD; drive stall kill switch")
    s.add_argument("--cycle", type=int, required=True)
    s.set_defaults(func=cmd_checkpoint)

    s = sub.add_parser("audit-loop-done", help="record a completed audit-fix loop (exit 2 when terminal)")
    s.add_argument("--clean", action="store_true",
                   help="this loop's whole-codebase audit minted zero tickets (terminal via prune)")
    s.set_defaults(func=cmd_audit_loop_done)

    s = sub.add_parser("resume", help="reconstruct run state after an interrupted session")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("status", help="print loop state")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
