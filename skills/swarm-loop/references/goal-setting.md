# Goal Setting — metrics, targets, and the anti-gaming rules

Read this in full before Phase 2. The quality of everything downstream — task selection, regression verdicts, the final report — is bounded by the quality of what you define here, and none of it can be fixed later because the goals freeze.

## What counts as a goal

A goal is a **machine-checkable numeric metric with a fixed target**. "Machine-checkable" means a shell command an orchestrator can run identically every cycle whose last stdout line is a bare number. If measuring it requires judgment, it is not a goal in this system — it can still be a backlog note, but it cannot drive the regression.

Why numeric and fixed: the feedback loop's engine is *error over cycles*. Error requires a distance from a target; a trend requires the target to stay put. Fuzzy goals produce fuzzy errors, and the regression over them steers the loop into noise.

## The standard metric menu

Pick 6–12 total. Fewer than 6 under-constrains the swarm (agents optimize what's measured); more than 12 dilutes attention and slows measurement.

**Global (always include the first three):**

| metric | command sketch | target | direction |
|---|---|---|---|
| Acceptance test pass rate (%) | run the acceptance suite, print pass % | 100 | maximize |
| Build succeeds (0/1) | build command, print 1 on success else 0 | 1 | maximize |
| Lint + type error count | linter + type checker, print combined count | 0 | minimize |
| Line coverage (%) | coverage tool, print % | 70–85 | maximize |
| Unit test pass rate (%) | project's own suite | 100 | maximize |

**Per-feature (one per atomic feature, or per feature group if there are many):** count of that feature's acceptance criteria currently passing, target = total count. This is what makes the analysis *diagnostic* — a global pass rate says "something is wrong"; per-feature metrics say "auth is regressing and search is stalled".

**Situational (only if the brief demands):** performance benchmark (ms, target from the brief), bundle size (KB, minimize to a stated budget), API response schema conformance count. Avoid vanity metrics (LOC, commit count) — they measure motion, not progress, and agents will happily generate motion.

Practical command notes:
- Wrap anything whose native output isn't a bare number: `... | tail -1 | grep -oE '[0-9.]+'` or a tiny script — and the script lives **under `.swarm-loop/acceptance/`, never an unhashed location** (a `bin/` dir outside the protected paths would let a metric definition be edited without `verify` noticing). Wrapper scripts must freeze too, because a metric definition you can edit is a target you can move.
- Commands run from the repo root on the `main` checkout after integration. They must be deterministic: pin seeds, avoid network dependence, avoid wall-clock-sensitive assertions.
- Metric commands run under a `/bin/sh` subprocess, not your interactive shell. No PCRE lookahead in grep (`(?=%)` passed by hand and failed under the harness with "repetition-operator operand invalid"), no bashisms; prefer `awk` for field extraction — its `+0` coercion strips a trailing `%` for free. `freeze` now smoke-runs every metric command before hashing: a command that cannot print a parseable number fails the freeze loudly (`--skip-smoke` bypasses this; not recommended), so portability failures surface before anything is frozen rather than at measure time. The smoke uses the same 600s-per-command timeout as `measure` (`--smoke-timeout` adjusts it); a suite that legitimately needs longer gets a bigger timeout, not `--skip-smoke`.
- A metric command that *fails to produce a number* fails the measurement (`measure` exits 1). That's intentional: a broken measuring stick is a defect with top priority, not a skippable inconvenience.
- Corollary for wrapper scripts: distinguish "suite ran and everything failed" (print `0`) from "suite could not run at all" (print **nothing** and exit nonzero, so `measure` fails loudly). A wrapper that converts a missing test runner or an import crash into a quiet `0` poisons the regression with fake data — verify the runner actually executed before emitting a number.
- `tolerance` (optional, per metric): treat error ≤ tolerance as at-target. Use for metrics with inherent jitter (coverage ±1–2%, benchmarks ±5%). Default 0.

## Acceptance tests are the goals

Write the acceptance tests for every atomic feature in `.swarm-loop/acceptance/` **before any implementation exists**, cycle 1, pre-freeze. Baseline expectation: they all fail. This is TDD at loop scale, and it's load-bearing for three reasons:

1. It converts "the feature works" from an opinion into a count.
2. It forces the feature decomposition to be concrete — a feature you can't write a failing test for isn't atomic yet.
3. It's the strongest anti-gaming measure available: the definition of done exists *before* the thing being judged, authored by the orchestrator, hashed at freeze, and unwritable by workers.

Keep acceptance tests black-box where possible (invoke the app's public surface — CLI, HTTP API, exported functions — not internals), so implementations remain free to vary. Import nothing from `src/` internals that a reasonable redesign would rename.

## goals.json schema

```json
{
  "frozen": false,
  "frozen_at": null,
  "metrics": [
    {
      "id": "acceptance_pass_rate",
      "name": "Acceptance test pass rate (%)",
      "kind": "global",
      "command": "python3 .swarm-loop/acceptance/run.py --pass-rate",
      "target": 100,
      "direction": "maximize"
    },
    {
      "id": "feat_auth",
      "name": "Auth: acceptance criteria passing",
      "kind": "feature",
      "feature_id": "F1",
      "command": "python3 .swarm-loop/acceptance/run.py --feature auth --count-passing",
      "target": 6,
      "direction": "maximize"
    },
    {
      "id": "coverage",
      "name": "Line coverage (%)",
      "kind": "global",
      "command": "pytest --cov=src --cov-report=term | awk '/TOTAL/ {print $NF+0}'",
      "target": 75,
      "direction": "maximize",
      "tolerance": 2
    }
  ]
}
```

Required per metric: `id`, `command`, `target`, `direction` (`maximize`/`minimize`). Recommended: `name`, `kind`, `feature_id`, `tolerance`. `freeze` validates these.

## The freeze, and why it is absolute

`swarmloop.py freeze` hashes `goals.json` and everything under `.swarm-loop/acceptance/` into `manifest.json`. From then on:

- `verify` runs before every scoring; any modified, missing, **or added** file under protection voids the cycle's scores until restored from git.
- `check-branch` vetoes any worker branch touching protected paths or `.swarm-loop/` at all — the veto is mechanical precisely so it can't be argued with in the moment.

This mirrors the standard three-layer defense from the reward-hacking literature: the verifier is outside the worker's reach, a deterministic monitor flags any attempt to reach it, and the orchestrator holds a veto at the merge boundary. The failure being prevented is mundane, not malicious: an agent whose code fails a test will, with the best intentions, sometimes conclude the *test* is wrong. Under a frozen harness that conclusion becomes a report to the orchestrator instead of a silent edit.

If a frozen test genuinely *is* wrong (it happens — a typo'd fixture, an impossible assertion), that is a terminal condition for autonomy: note it, exclude that metric's verdict from decisions, and surface it in the cycle report and final report. Only the user, awake, amends a frozen goal — rerun `freeze` after their edit so the manifest matches, and record the amendment in the cycle report.

## Baseline (cycle 0)

Immediately after freezing: `verify`, then `measure --cycle 0`, then `checkpoint --cycle 0`. The baseline anchors the regression (greenfield baselines are all-failing, which is correct and useful) and seeds the HEAD history so the stall counter is meaningful from cycle 1. Then **commit `.swarm-loop/` (goals.json, acceptance/, manifest.json, state) to `main` before dispatching anything** — worktrees branch from committed state, so an uncommitted harness leaves every worker without its acceptance tests, and "restore the harness from git" is only possible if the freeze-time state was committed.

## Setting targets honestly

Targets come from the brief where stated, from convention where not (pass rates 100, error counts 0, coverage 70–85). Resist inflating a target "to be ambitious" — an unreachable target guarantees `converging_off_track` verdicts that drown real signal — and resist deflating one to guarantee a win, which defeats the exercise. If the scope is large relative to the ticket graph and audit-loop budget, say so at the checkpoint and propose scope, not lowered standards: fewer features fully done beats all features half-measured, because half-measured doesn't pass acceptance tests anyway.
