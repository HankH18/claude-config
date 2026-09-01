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
| Acceptance tests **collected** (count) | the same suite, collect-only, print the count | cycle-0 count | maximize |
| Build succeeds (0/1) | build command, print 1 on success else 0 | 1 | maximize |
| Lint + type error count | linter + type checker, print combined count | 0 | minimize |
| Line coverage (%) | coverage tool, print % | 70–85 | maximize |
| Unit test pass rate (%) | project's own suite | 100 | maximize |

**Pair every rate with a count.** Every pass-rate metric gets a companion frozen collected-count metric, because **a rate is gameable by shrinking its denominator and a count is not**. Be exact about which half of the defense does the work, though: the count command prescribed here is itself hermetic, so under the `addopts` exploit reproduced below it reads **3, not 2** — the wrapper's `-o addopts=` neutralized that exploit before the count could catch it, and a naive `--collect-only` (the one that reads 2) is gameable by the next config edit, which is why it is not what is prescribed. **The hermetic count does not catch `addopts`; hermeticity does.** What the count catches is the denominator shrinkage no wrapper can neutralize — a deleted or renamed test file, a trimmed parametrize list, a suite quietly gutted while its pass rate reads 100 — turning an invisible shrink into a *visible* regression that `analyze` reports on its own, with no new machinery and nobody having to notice anything. For pytest the shape is `<interpreter> -m pytest <path> --collect-only -q -o addopts= | tail -1 | awk '{print $1}'` — verified on pytest 8.4.1 and 6.2.4, whose last line is `N tests collected in 0.00s`, so `tail -1 | awk '{print $1}'` yields `N`. Set `target` to the cycle-0 count with `direction: maximize`, so adding tests stays free and any shrink is an error. Two verified limits, and both say this belongs inside the frozen wrapper rather than pasted raw into `goals.json`: (1) that footer counts items collected *before* `pytest_collection_modifyitems`, so the `conftest.py` filter described below shrinks the real denominator while this number holds steady — count the listed node ids (`| grep -c '::'`) if you want the post-filter number, at the cost of a quiet `0` when collection collapses entirely; (2) when nothing collects at all the footer reads `no tests collected, 1 error`, which is not a number, so the pre-implementation exception below applies to this metric too.

**Per-feature (one per atomic feature, or per feature group if there are many):** count of that feature's acceptance criteria currently passing, target = total count. This is what makes the analysis *diagnostic* — a global pass rate says "something is wrong"; per-feature metrics say "auth is regressing and search is stalled".

**Situational (only if the brief demands):** performance benchmark (ms, target from the brief), bundle size (KB, minimize to a stated budget), API response schema conformance count. Avoid vanity metrics (LOC, commit count) — they measure motion, not progress, and agents will happily generate motion.

Practical command notes:
- Wrap anything whose native output isn't a bare number: `... | tail -1 | grep -oE '[0-9.]+'` or a tiny script — and the script lives **under `.swarm-loop/acceptance/`, never an unhashed location** (a `bin/` dir outside the protected paths would let a metric definition be edited without `verify` noticing). Wrapper scripts must freeze too, because a metric definition you can edit is a target you can move.
- **A METRIC WHOSE RESULT DEPENDS ON ANY FILE OUTSIDE THE FROZEN MANIFEST IS NOT A FROZEN METRIC.** The freeze protects the TEST; the *runner's config* decides which tests run, and config is not frozen. Reproduced twice: a worker committed a two-line `pyproject.toml` carrying `addopts = "--ignore=tests/test_hard.py"` and drove the frozen acceptance metric from 50 to 100 with the frozen test still present and still failing — it passed `check-branch` (pyproject.toml is not a protected path), passed the `'*test*'` diff audit (pyproject.toml does not match the glob), and passed `verify` ("frozen harness intact"). The files that decide what runs, and are NOT frozen: `pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, `.coveragerc`, `conftest.py`, `package.json`, jest/vitest config, `Makefile` — several of which no `*test*` glob will ever catch. So **every metric that shells to a test, coverage, or lint runner goes through a wrapper under `.swarm-loop/acceptance/`, and the wrapper neutralizes project-owned config.**
  - For pytest: `<interpreter> -m pytest <explicit path> -q --tb=no -o addopts= -p no:cacheprovider`. **Both halves matter** — the explicit path defeats `testpaths`, `-o addopts=` defeats `addopts`; verified, an explicit path *alone* still obeys a hostile `addopts` and collects nothing.
  - `-o addopts=` defeats every ini form tested (`pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`) but does **NOT** defeat `conftest.py`, which is imported *code*, not config. Verified: a `pytest_collection_modifyitems` hook that drops items turned "1 failed, 2 passed" into "2 passed" straight through `-o addopts=`. Where the layout allows, run the wrapper with a controlled rootdir / `--confcutdir` (`--confcutdir=tests` restored the dropped failure in that same repro), and treat `conftest.py` as a **metric-config file**: a worker diff touching it needs a justification at merge review, exactly like an assertion change. This countermeasure is partial by construction; hold it as partial, and say so in the cycle report rather than claiming the metric is sealed.
  - `-o addopts=` and `-p no:cacheprovider` each break legitimate suites: an `addopts`-supplied plugin a test needs, a test using the built-in `cache` fixture. The **freeze smoke-run is where such a break surfaces**, and it is resolved by moving the needed option INTO the frozen wrapper — never by dropping the neutralization. A metric you un-hermeticized to make it green is a metric a worker can move.
- **Name the interpreter; never rely on PATH.** Found physically on this machine: a frozen `conftest` byte-compiled by system Python 3.9.7 under pytest 6.2.4, in a project targeting 3.12 under `uv` — the metric was measuring a different interpreter than the product runs on, and nothing in the harness noticed. Write the prefix explicitly and **relatively** — `.venv/bin/python -m pytest ...`, `node_modules/.bin/eslint ...` — resolving from `measure`'s cwd, which is the repo root. Relative, not absolute: an absolute path bakes in one machine's layout and, run from a worktree, silently points back at the primary tree.
- **Exclude the worktree directory and `.swarm-loop/` from anything that walks the tree from the root.** Live workers keep FULL checkouts in whatever directory the dispatch step places them, and `measure` runs every command with cwd = repo root — so a root walk also walks every in-flight worker's partial, mid-edit, deliberately-broken code. This hits the mandatory menu directly: "Lint + type error count" is an always-include global, and linters, type-checkers and coverage tools all walk from the root by default. (pytest escapes only because its default `norecursedirs` happens to skip dot-directories — luck, not design; do not build on it.) Pass the exclusion explicitly — `ruff check src --exclude <dir>`, `flake8 --exclude=<dir>`, `mypy --exclude '<dir>'`, `eslint --ignore-pattern '<dir>'`, `omit` in the frozen coveragerc — and prefer an explicit path argument (`src tests`) to a bare root walk, since an explicit path is the one form a later config edit cannot re-widen. Name the worktree directory *wherever the dispatch step places it* plus `.swarm-loop/`; do not hard-code today's path. The diagnostic to remember: **a metric whose value moves when a worker is dispatched is measuring the swarm, not the product** — `analyze` reads that jump as `regressing` and Phase 8 sends the loop hunting a product cause for pure orchestration noise. And note what protects you here and what does not: the freeze smoke-run **cannot** catch this, because at freeze time no worktree exists yet. Confirm the exclusion by hand, once, on the first cycle that has a live worktree.
- Commands run from the repo root on the `main` checkout after integration. They must be deterministic: pin seeds, avoid network dependence, avoid wall-clock-sensitive assertions.
- Metric commands run under a `/bin/sh` subprocess, not your interactive shell. No PCRE lookahead in grep (`(?=%)` passed by hand and failed under the harness with "repetition-operator operand invalid"), no bashisms; prefer `awk` for field extraction — its `+0` coercion strips a trailing `%` for free. `freeze` now smoke-runs every metric command before hashing: a command that cannot print a parseable number fails the freeze loudly (`--skip-smoke` bypasses this; not recommended), so portability failures surface before anything is frozen rather than at measure time. The smoke uses the same per-command timeout as `measure` (2400s by default; `--smoke-timeout` adjusts it); a suite that legitimately needs longer gets a bigger timeout, not `--skip-smoke`. **Raising `--smoke-timeout` obliges you to raise `measure --timeout` to match**, and more generally: *the outer harness timeout must strictly exceed any inner timeout inside a metric command.* A tighter outer cap converts a diagnosed failure ("the suite's own 300s timeout fired on test X") into an undiagnosed one ("the metric timed out") and orphans the inner process, which then keeps running against the tree you are about to measure. A metric may also carry its own optional `"timeout"` field, which overrides the flag for that metric alone — use it so one slow suite does not force a large cap on every metric.
- A metric command that *fails to produce a number* fails the measurement (`measure` exits 1). That's intentional: a broken measuring stick is a defect with top priority, not a skippable inconvenience.
- Corollary for wrapper scripts: distinguish "suite ran and everything failed" (print `0`) from "suite could not run at all" (print **nothing** and exit nonzero, so `measure` fails loudly). A wrapper that converts a missing test runner or a crashed runner into a quiet `0` poisons the regression with fake data — verify the runner actually executed before emitting a number.
- **Pre-implementation exception to that corollary — required, or the run cannot start.** Acceptance tests are written before any implementation exists (below), black-box against modules that do not exist yet, so at cycle 0 the suite fails at *import*, not at assertion. Read literally, the corollary makes such a wrapper print nothing, `freeze`'s smoke-run fail, and the manifest never get written — a hard stall at the one gate every downstream invariant (`verify`, `check-branch`, the whole anti-gaming story) depends on, reached minutes after the user approved the plan and went to bed, with `--skip-smoke` as the only documented escape and precisely the wrong instinct to hand a 3am orchestrator. So, **at the freeze/baseline moment**: a collection error whose exception is `ModuleNotFoundError`/`ImportError` naming a *project* module counts as a failed test and the wrapper prints `0`. Everything else stays loud — a missing test runner, a `conftest.py` that crashes, or an import error naming a *third-party* package (that one is an env-sync failure, and it must never read as a clean zero). This exception is correct ONLY pre-implementation: once a module has existed and stops importing, that IS a real regression. Scope the branch to cycle 0, or to modules never yet built, rather than leaving it on for the run.
- `tolerance` (optional, per metric): treat error ≤ tolerance as at-target. Use for metrics with inherent jitter (coverage ±1–2%, benchmarks ±5%). Default 0.

## Acceptance tests are the goals

Write the acceptance tests for every atomic feature in `.swarm-loop/acceptance/` **before any implementation exists**, cycle 1, pre-freeze. Baseline expectation: they all fail — and that expectation is now **checked, not assumed**. `freeze`'s smoke-run prints each metric's target and computed error and warns loudly when a metric is *already at target before any implementation exists*. Nothing used to verify this, and the failure it hides is permanent: a vacuous `assert True` suite inflates the frozen score for the whole run, and goals are immutable after freeze. **Reading instruction: at cycle 0, any metric already at target is a vacuous-test signal — investigate it before dispatching anything.** It warns and never blocks, because a brownfield metric can legitimately start green; the judgment is yours, but it has to be made rather than skipped past.

This is TDD at loop scale, and it's load-bearing for three reasons:

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
      "command": ".venv/bin/python .swarm-loop/acceptance/run.py --pass-rate",
      "target": 100,
      "direction": "maximize"
    },
    {
      "id": "acceptance_collected",
      "name": "Acceptance tests collected (count)",
      "kind": "global",
      "command": ".venv/bin/python .swarm-loop/acceptance/run.py --collected-count",
      "target": 24,
      "direction": "maximize"
    },
    {
      "id": "feat_auth",
      "name": "Auth: acceptance criteria passing",
      "kind": "feature",
      "feature_id": "F1",
      "command": ".venv/bin/python .swarm-loop/acceptance/run.py --feature auth --count-passing",
      "target": 6,
      "direction": "maximize"
    },
    {
      "id": "coverage",
      "name": "Line coverage (%)",
      "kind": "global",
      "command": ".venv/bin/python -m pytest tests -q -o addopts= --cov=src --cov-config=.swarm-loop/acceptance/coveragerc --cov-report=term | awk '/TOTAL/ {print $NF+0}'",
      "target": 75,
      "direction": "maximize",
      "tolerance": 2,
      "timeout": 1800
    }
  ]
}
```

Note what the coverage command is doing, because the naive spelling (`pytest --cov=src --cov-report=term`) is *not* a frozen metric: the explicit path and `-o addopts=` neutralize project config, `--cov-config` points at a coveragerc that lives **under the frozen dir** — a bare `--cov=src` reads `[tool.coverage.run] omit` out of `pyproject.toml`, so a worker can exclude its own untested file from coverage without touching a protected path — and that coveragerc is where the worktree-directory `omit` lives too. `acceptance_collected` is the companion count for `acceptance_pass_rate`; its target is the cycle-0 count.

Required per metric: `id`, `command`, `target`, `direction` (`maximize`/`minimize`). Recommended: `name`, `kind`, `feature_id`, `tolerance`, and `timeout` (per-metric override of `measure --timeout`, so one slow suite does not raise the cap for all). `freeze` validates these.

## The freeze — a locked door, not a wall

`swarmloop.py freeze` hashes `goals.json` and everything under `.swarm-loop/acceptance/` into `manifest.json`. From then on:

- `verify` runs before every scoring; any modified, missing, **or added** file under protection voids the cycle's scores until restored — with `swarmloop.py restore-harness`, not by hand and not by re-freezing (see below).
- `check-branch` vetoes any worker branch touching protected paths or `.swarm-loop/` at all — the veto is mechanical precisely so it can't be argued with in the moment. **It cannot be argued with, and it can be appealed — and the appeal is an escalation to the user, never a workaround.** That is the whole shape of the freeze in one sentence: a locked door, not a wall. Nothing passes it by argument, and the person holding the key can still open it.

The mechanics above do not change. What changes is what you are supposed to *do* when you believe a frozen thing is wrong, and that answer has three levels. Do not let them blur into each other:

- **Workers — a hard wall.** A worker never touches anything frozen, for any reason, however certain it is that the goal is broken. Its route for an objection is the packet's `NEEDS`/`CONCERNS` channel and nothing else. This is the property every anti-gaming claim in this skill rests on; it does not bend by one word, and no escalation machinery reaches down to a worker.
- **Orchestrator — a permission boundary.** You may not amend a frozen artifact and you may not route around one. When you believe a frozen artifact is wrong, you must **escalate**: surface it to the user, loudly, in writing, with a specific proposed change. Silently working around it, silently re-freezing, and silently going on scoring against a goal you have privately concluded is broken are three spellings of the same failure.
- **User — the only one who can authorize.** `freeze --amend "<reason>"` is the only thing that actually changes a frozen artifact, and only the user, awake, runs it.

**Escalating is asking. It is never granting.** The failure this framing exists to prevent is "requires permission" quietly decaying into "the agent decided it had permission." Filing an escalation unblocks nothing, approves nothing, and moves no number — which is exactly why it is safe to put in an unattended orchestrator's hands. If you ever find yourself treating an escalation you filed as the permission you were waiting for, you have reinvented the silent workaround with extra paperwork.

This mirrors the standard three-layer defense from the reward-hacking literature: the verifier is outside the worker's reach, a deterministic monitor flags any attempt to reach it, and the orchestrator holds a veto at the merge boundary. The failure being prevented is mundane, not malicious: an agent whose code fails a test will, with the best intentions, sometimes conclude the *test* is wrong. Under a frozen harness that conclusion becomes a report to the orchestrator instead of a silent edit — and your job with that report is to carry it to the user, not to sit on it and not to act on it alone.

**When you believe a frozen goal genuinely is wrong** — it happens: a typo'd fixture, an impossible assertion, a metric that contradicts the brief — the response is to escalate. Not to fix it, and not to stop:

```bash
swarmloop.py escalate --kind frozen-test \
    --subject "<metric id, or the acceptance test's path>" \
    --reason "<what it demands, why that is wrong, with the evidence>" \
    --proposed "<the exact change you are requesting>" \
    --cycle <N>
```

`--kind` is one of `frozen-goal|frozen-test|harness|blocked-ticket|other`. It appends to the append-only `.swarm-loop/escalations.jsonl` and mirrors a human-readable `.swarm-loop/ESCALATIONS.md`; `resume`, `status` and `analyze` all surface anything still open, so a filed escalation cannot go quietly missing between cycles or across a resume, and `escalate --list` prints them on demand (`escalate --resolve <ID> [--note "..."]` closes one once the user has answered). **It changes nothing else** — no goal moves, no metric is rescored, no manifest is rewritten. That inertness is the feature: it is what keeps this reframe from becoming a hole in the goalposts.

**A non-blocking escalation does NOT stop the run.** File it, then keep working. Exclude the disputed metric's verdict from steering decisions — a measuring stick you have formally disputed steers nothing — surface it in the cycle report and the final report, and keep executing every ticket that does not depend on the disputed thing. Reserve `--blocking` (which exits 2, and *is* the terminal condition for autonomy) for the case where nothing left in the backlog can proceed until a human answers. One wrong acceptance test is almost never that; a harness that cannot measure at all might be.

**HARD is not WRONG.** A hard goal is the job — the whole point of freezing it is that you cannot renegotiate it at 3am when it is inconvenient. Escalation is for a goal that is *incorrect*, and the evidence bar is deliberately high: quote the assertion, state what it demands, state why that is impossible or wrong **independent of the current implementation** (it contradicts the brief, it contradicts another frozen test, it depends on a fixture that cannot exist), and say what it should assert instead. "We are not passing it yet" is evidence of remaining work and nothing else. An escalation stream that cries wolf trains the one human in the loop to stop reading it, and then the real one goes unread.

And the amendment itself stays exactly where it was. Only the user, awake, amends a frozen goal, and that amendment is **mechanically gated** — which matters because this is the highest-pressure decision in the run and it used to be guarded by nothing but a sentence. A re-`freeze` on an already-frozen project exits 1 unless you pass `--amend "<reason>"`; an amended freeze prints the old and new manifest hashes plus a unified diff of exactly what changed, appends the reason to the append-only `.swarm-loop/freeze-log.jsonl`, and is surfaced in the final report. A mid-run re-freeze is a **user-awake action**. Never take one on your own judgment, and never as a convenience to make a number move. An escalation you filed is not an amendment you were granted.

**A re-freeze is NEVER the repair for a red `verify`.** A red verify means the harness on disk no longer matches the manifest; re-freezing blesses the tampering and destroys the only evidence that it happened. Repair it with `swarmloop.py restore-harness` — which exists precisely because the three canonical git spellings for this are all blocked by this skill's own `PreToolUse` git-guard (`git checkout -- <path>`, `git restore <path>`, `git checkout HEAD -- <dir>`; verified exit 2 on each). If you find yourself reaching for `freeze --amend` to turn `verify` green, stop: you are about to launder a defect into a goal. And if `restore-harness` cannot make it green — the freeze-time state was never committed, the damage predates the manifest — the exit is not a re-freeze and not a shrug: `escalate --kind harness --blocking`, because every score downstream is void until a human resolves it. Escalating a broken harness is the honest terminal condition; re-freezing one is the dishonest one.

## Baseline (cycle 0)

Immediately after freezing: `verify`, then `measure --cycle 0`, then `checkpoint --cycle 0`. The baseline anchors the regression (greenfield baselines are all-failing, which is correct and useful) and seeds the HEAD history so the stall counter is meaningful from cycle 1. **Read cycle 0 before dispatching, don't just record it:** the freeze smoke-run has already warned about any metric sitting at target with no implementation behind it, and cycle 0 is where you act on that warning — an already-green metric at cycle 0 is a vacuous-test signal, and the goals are immutable **to you** from here. Immutable is not untouchable: if cycle 0 convinces you a metric is vacuous, `escalate --kind frozen-goal` with the evidence *before dispatching*, rather than starting a whole run against a score you already believe is fake. Then dispatch anyway on everything the disputed metric does not judge — the escalation is the ask, not the pause. Record the `acceptance_collected` value too: it is that metric's target for the rest of the run. Then **commit `.swarm-loop/` (goals.json, acceptance/, manifest.json, state) to `main` before dispatching anything** — worktrees branch from committed state, so an uncommitted harness leaves every worker without its acceptance tests, and "restore the harness from git" is only possible if the freeze-time state was committed.

## Setting targets honestly

Targets come from the brief where stated, from convention where not (pass rates 100, error counts 0, coverage 70–85). Resist inflating a target "to be ambitious" — an unreachable target guarantees `converging_off_track` verdicts that drown real signal — and resist deflating one to guarantee a win, which defeats the exercise. If the scope is large relative to the ticket graph and audit-loop budget, say so at the checkpoint and propose scope, not lowered standards: fewer features fully done beats all features half-measured, because half-measured doesn't pass acceptance tests anyway. That conversation is free before the freeze because the user is sitting there. After the freeze it still exists — it just runs through `escalate --kind frozen-goal` with a proposed target and the evidence for it, and waits for an answer while the run continues. What never exists is the third option of quietly re-reading a target you find inconvenient.
