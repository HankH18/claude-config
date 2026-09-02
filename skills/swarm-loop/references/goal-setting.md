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

**Pair every rate with a count.** Every pass-rate metric gets a companion frozen collected-count metric, because **a rate is gameable by shrinking its denominator and a count is not**. Four things about it, each its own rule:

- **Be exact about which half of the defense does the work.** The count command prescribed here is itself hermetic, so under the `addopts` exploit reproduced below it reads **3, not 2** — the wrapper's `-o addopts=` neutralized that exploit before the count could catch it, and a naive `--collect-only` (the one that reads 2) is gameable by the next config edit, which is why it is not what is prescribed. **The hermetic count does not catch `addopts`; hermeticity does.**
- **What the count *does* catch** is the denominator shrinkage no wrapper can neutralize — a deleted or renamed test file, a trimmed parametrize list, a suite quietly gutted while its pass rate reads 100 — turning an invisible shrink into a *visible* regression that `analyze` reports on its own, with no new machinery and nobody having to notice anything.
- **The pytest shape:** `<interpreter> -m pytest <path> --collect-only -q -o addopts= | tail -1 | awk '{print $1}'` — verified on pytest 8.4.1 and 6.2.4, whose last line is `N tests collected in 0.00s`, so `tail -1 | awk '{print $1}'` yields `N`. Set `target` to the cycle-0 count with `direction: maximize`, so adding tests stays free and any shrink is an error.
- **Two verified limits, and both say this belongs inside the frozen wrapper** rather than pasted raw into `goals.json`. (1) That footer counts items collected *before* `pytest_collection_modifyitems`, so the `conftest.py` filter described below shrinks the real denominator while this number holds steady — count the listed node ids (`| grep -c '::'`) if you want the post-filter number, at the cost of a quiet `0` when collection collapses entirely. (2) When nothing collects at all the footer reads `no tests collected, 1 error`, which is not a number, so the pre-implementation exception below applies to this metric too.

**Per-feature (one per atomic feature, or per feature group if there are many):** count of that feature's acceptance criteria currently passing, target = total count. This is what makes the analysis *diagnostic* — a global pass rate says "something is wrong"; per-feature metrics say "auth is regressing and search is stalled".

**Situational (only if the brief demands):** performance benchmark (ms, target from the brief), bundle size (KB, minimize to a stated budget), API response schema conformance count. Avoid vanity metrics (LOC, commit count) — they measure motion, not progress, and agents will happily generate motion.

Practical command notes:
- Wrap anything whose native output isn't a bare number: `... | tail -1 | grep -oE '[0-9.]+'` or a tiny script — and the script lives **under `.swarm-loop/acceptance/`, never an unhashed location** (a `bin/` dir outside the protected paths would let a metric definition be edited without `verify` noticing). Wrapper scripts must freeze too, because a metric definition you can edit is a target you can move.
- **A METRIC WHOSE RESULT DEPENDS ON ANY FILE OUTSIDE THE FROZEN MANIFEST IS NOT A FROZEN METRIC.** The freeze protects the TEST; the *runner's config* decides which tests run, and config is not frozen.

  *Reproduced twice.* A worker committed a two-line `pyproject.toml` carrying `addopts = "--ignore=tests/test_hard.py"` and drove the frozen acceptance metric from 50 to 100 with the frozen test still present and still failing — it passed `check-branch` (pyproject.toml is not a protected path), passed the `'*test*'` diff audit (pyproject.toml does not match the glob), and passed `verify` ("frozen harness intact").

  *The files that decide what runs, and are NOT frozen:* `pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, `.coveragerc`, `conftest.py`, `package.json`, jest/vitest config, `Makefile` — several of which no `*test*` glob will ever catch.

  *Therefore:* **every metric that shells to a test, coverage, or lint runner goes through a wrapper under `.swarm-loop/acceptance/`, and the wrapper neutralizes project-owned config.**
  - For pytest: `<interpreter> -m pytest <explicit path> -q --tb=no -o addopts= -p no:cacheprovider`. **Both halves matter** — the explicit path defeats `testpaths`, `-o addopts=` defeats `addopts`; verified, an explicit path *alone* still obeys a hostile `addopts` and collects nothing.
  - `-o addopts=` defeats every ini form tested (`pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`) but does **NOT** defeat `conftest.py`, which is imported *code*, not config. Verified: a `pytest_collection_modifyitems` hook that drops items turned "1 failed, 2 passed" into "2 passed" straight through `-o addopts=`. Where the layout allows, run the wrapper with a controlled rootdir / `--confcutdir` (`--confcutdir=tests` restored the dropped failure in that same repro), and treat `conftest.py` as a **metric-config file**: a worker diff touching it needs a justification at merge review, exactly like an assertion change. This countermeasure is partial by construction; hold it as partial, and say so in the cycle report rather than claiming the metric is sealed.
  - `-o addopts=` and `-p no:cacheprovider` each break legitimate suites: an `addopts`-supplied plugin a test needs, a test using the built-in `cache` fixture. The **freeze smoke-run is where such a break surfaces**, and it is resolved by moving the needed option INTO the frozen wrapper — never by dropping the neutralization. A metric you un-hermeticized to make it green is a metric a worker can move.
- **Name the interpreter; never rely on PATH.** Found physically on this machine: a frozen `conftest` byte-compiled by system Python 3.9.7 under pytest 6.2.4, in a project targeting 3.12 under `uv` — the metric was measuring a different interpreter than the product runs on, and nothing in the harness noticed. Write the prefix explicitly and **relatively** — `.venv/bin/python -m pytest ...`, `node_modules/.bin/eslint ...` — resolving from `measure`'s cwd, which is the repo root. Relative, not absolute: an absolute path bakes in one machine's layout and, run from a worktree, silently points back at the primary tree.
- **Exclude the worktree directory and `.swarm-loop/` from anything that walks the tree from the root.** Live workers keep FULL checkouts in whatever directory the dispatch step places them, and `measure` runs every command with cwd = repo root — so a root walk also walks every in-flight worker's partial, mid-edit, deliberately-broken code.

  *Which metrics this hits:* the mandatory menu directly. "Lint + type error count" is an always-include global, and linters, type-checkers and coverage tools all walk from the root by default. (pytest escapes only because its default `norecursedirs` happens to skip dot-directories — luck, not design; do not build on it.)

  *How:* pass the exclusion explicitly — `ruff check src --exclude <dir>`, `flake8 --exclude=<dir>`, `mypy --exclude '<dir>'`, `eslint --ignore-pattern '<dir>'`, `omit` in the frozen coveragerc — and prefer an explicit path argument (`src tests`) to a bare root walk, since an explicit path is the one form a later config edit cannot re-widen. Name the worktree directory *wherever the dispatch step places it* plus `.swarm-loop/`; do not hard-code today's path.

  *The diagnostic to remember:* **a metric whose value moves when a worker is dispatched is measuring the swarm, not the product** — `analyze` reads that jump as `regressing` and Phase 8 sends the loop hunting a product cause for pure orchestration noise.

  *What does NOT protect you:* the freeze smoke-run **cannot** catch this, because at freeze time no worktree exists yet. Confirm the exclusion by hand, once, on the first cycle that has a live worktree.
- Commands run from the repo root on the `main` checkout after integration. They must be deterministic: pin seeds, avoid network dependence, avoid wall-clock-sensitive assertions.
- **Metric commands run under a `/bin/sh` subprocess, not your interactive shell.** No PCRE lookahead in grep (`(?=%)` passed by hand and failed under the harness with "repetition-operator operand invalid"), no bashisms; prefer `awk` for field extraction — its `+0` coercion strips a trailing `%` for free.

  *The freeze smoke-run is the gate for this.* `freeze` smoke-runs every metric command before hashing: a command that cannot print a parseable number fails the freeze loudly (`--skip-smoke` bypasses this; not recommended), so portability failures surface before anything is frozen rather than at measure time. The smoke uses the same per-command timeout as `measure` (2400s by default; `--smoke-timeout` adjusts it); a suite that legitimately needs longer gets a bigger timeout, not `--skip-smoke`.

  *Timeouts nest, and the nesting has one direction.* **Raising `--smoke-timeout` obliges you to raise `measure --timeout` to match**, and more generally: *the outer harness timeout must strictly exceed any inner timeout inside a metric command.* A tighter outer cap converts a diagnosed failure ("the suite's own 300s timeout fired on test X") into an undiagnosed one ("the metric timed out") and orphans the inner process, which then keeps running against the tree you are about to measure. A metric may also carry its own optional `"timeout"` field, which overrides the flag for that metric alone — use it so one slow suite does not force a large cap on every metric.
- A metric command that *fails to produce a number* fails the measurement (`measure` exits 1). That's intentional: a broken measuring stick is a defect with top priority, not a skippable inconvenience.
- Corollary for wrapper scripts: distinguish "suite ran and everything failed" (print `0`) from "suite could not run at all" (print **nothing** and exit nonzero, so `measure` fails loudly). A wrapper that converts a missing test runner or a crashed runner into a quiet `0` poisons the regression with fake data — verify the runner actually executed before emitting a number.
- **The frozen suite must be HERMETIC and LAZILY-IMPORTING — this is the precondition for everything below it.** A test that imports product code at **module scope** turns every unmet goal into a *collection error*, and the runner cannot distinguish a collection error from a broken measuring stick. Two rules, both mechanical: **import inside the test function**, not at module scope; and **cut the frozen suite off from the project's root `conftest.py`** (`--confcutdir`) so no worker's fixture can reach the goals. This is the property that makes an unbuilt feature read as a test **failure** rather than as a broken scorer — and without it, the pre-implementation exception below is incoherent, because there is no way to tell "the module does not exist yet" from "the harness is broken."

- **Pre-implementation exception to that corollary — required, or the run cannot start.** Acceptance tests are written before any implementation exists (below), black-box against modules that do not exist yet, so at cycle 0 the suite fails at *import*, not at assertion.

  *Why it must exist:* read literally, the corollary makes such a wrapper print nothing, `freeze`'s smoke-run fail, and the manifest never get written — a hard stall at the one gate every downstream invariant (`verify`, `check-branch`, the whole anti-gaming story) depends on, reached minutes after the user approved the plan and went to bed, with `--skip-smoke` as the only documented escape and precisely the wrong instinct to hand a 3am orchestrator.

  *The exact carve-out, at the freeze/baseline moment:* a collection error whose exception is `ModuleNotFoundError`/`ImportError` naming a *project* module counts as a failed test and the wrapper prints `0`. Everything else stays loud — a missing test runner, a `conftest.py` that crashes, or an import error naming a *third-party* package (that one is an env-sync failure, and it must never read as a clean zero).

  *Scope it.* This exception is correct ONLY pre-implementation: once a module has existed and stops importing, that IS a real regression. Scope the branch to cycle 0, or to modules never yet built, rather than leaving it on for the run.

- **Smoke the metric RUNNER itself against synthetic pass / fail / skip / missing cases, before the freeze.** This is not the same thing as `freeze`'s smoke-run, which proves a command prints a parseable number — it does not prove the runner *behaves correctly* on the four boundary cases. Five minutes with four throwaway tests proved three properties the whole regression depends on and none of which are visible by reading the code: **a skipped test is not a passed test**; **a missing/empty selector fails loudly with empty stdout rather than returning `0`**; and **a lazy-import failure counts as an ordinary test failure** (which is what makes the exception above work). Freeze locks the commands, so a runner whose failure modes were never exercised is locked in with them for the whole run.
- **A gate's reading is uninterpretable without knowing what it did NOT run — record passed AND skipped AND deselected beside every binary gate.** A 0/1 gate is the worst offender, because it reports the same `1` in both worlds. Two measured cases from one repo, and note they are *different* mechanisms:
  - **Skips.** With the compose stack down, the project's full check is green at **95 passed / 15 skipped** — and all 15 skipped are the `@pytest.mark.docker` datastore tests, *exactly the layer the gate looks like it is proving*, and where four of the eight scaffold defects lived. `build_succeeds` reads **1** either way. Skips are silent; the number is not.
  - **Deselection.** The per-ticket `check` target *deselects* `@pytest.mark.docker` entirely, so 32 of one ticket's tests — the whole grant model and the whole Postgres writer, i.e. every behavioural proof that a named release blocker held — never execute in it at all, while the ticket's own `verify` does run them.

  **Two different greens with the same name, and the weaker one is what a hurried reader sees.** So: bring the run's real dependencies up before any measurement sweep, and write each gate's passed/skipped/deselected triple, plus the marks it deselects, beside its reading in the epoch report — never the bare 1.
- `tolerance` (optional, per metric): treat error ≤ tolerance as at-target. Use for metrics with inherent jitter (coverage ±1–2%, benchmarks ±5%). Default 0.

## Acceptance tests are the goals

Write the acceptance tests for every atomic feature in `.swarm-loop/acceptance/` **before any implementation exists**, cycle 1, pre-freeze. Baseline expectation: they all fail — and that expectation is now **checked, not assumed**. `freeze`'s smoke-run prints each metric's target and computed error and warns loudly when a metric is *already at target before any implementation exists*. Nothing used to verify this, and the failure it hides is permanent: a vacuous `assert True` suite inflates the frozen score for the whole run, and goals are immutable after freeze. **Reading instruction: at cycle 0, any metric already at target is a vacuous-test signal — investigate it before dispatching anything.** It warns and never blocks, because a brownfield metric can legitimately start green; the judgment is yours, but it has to be made rather than skipped past.

This is TDD at loop scale, and it's load-bearing for three reasons:

1. It converts "the feature works" from an opinion into a count.
2. It forces the feature decomposition to be concrete — a feature you can't write a failing test for isn't atomic yet.
3. It's the strongest anti-gaming measure available: the definition of done exists *before* the thing being judged, authored by the orchestrator, hashed at freeze, and unwritable by workers.

Keep acceptance tests black-box where possible (invoke the app's public surface — CLI, HTTP API, exported functions — not internals), so implementations remain free to vary. Import nothing from `src/` internals that a reasonable redesign would rename.

### Five authoring rules for the frozen suite, each from a live failure

- **A metric may never be graded against a file inside the graded ticket's own scope.** The fixture must be **orchestrator-owned or frozen alongside the test**. Reproduced across four epics of one run: a test compared `clarify()`'s output field-by-field against `expected_intent` in `fixtures/dialogues/*.json` while `fixtures/dialogues/**` sat in the graded ticket's own `scope`; a sibling epic did the identical thing with `fixtures/interviews/**`. Two other epics tried to close the hole with an approval digest, and it was a plain `sha256(json.dumps(manifest_minus_approval, sort_keys=True, separators=(",",":")))` — computable by whoever writes the manifest. The attack needs no bad intent at all: a worker implements, runs, pastes the output into the fixture, and scores green. **If the gradee can write the answer key, the metric measures nothing.**

- **The "must FAIL at baseline" rule extends per-TEST, not just per-METRIC.** `freeze` already enforces it per metric (a metric already at target with no implementation behind it is a vacuous-test signal). Extend it one level down: **a test that is green at cycle 0 against an empty tree either measures nothing or belongs in a lint gate — not in the scored suite.** Reproduced: two frozen tests asserted properties *of the frozen suite itself* (that no acceptance test imports product code at module scope; that every test carries an epic and ticket marker). Both are permanently green, both are real and worth enforcing — and both contributed `2/120` to the headline pass rate and `2/8` to a criteria metric **with zero product signal**. Two free points baked into the number the whole run steers by. Keep the checks; move them out of the scored suite.

- **Every NEGATIVE-SPACE test needs a sabotage companion, or it is not coverage.** A test proving something is **not** granted, **not** imported, **not** reachable is only as strong as the filter deciding what it examines — and **the filter is the untested part**. The asymmetry is the whole point: a positive test fails loudly when its subject goes missing; a negative test that filters its subject away simply **passes**. Reproduced, against an explicit release blocker: a migration test enforced "the exchange role never gets `sealed` or `vault`" through the per-statement filter

  ```python
  if "grant" not in low or not _EXCHANGE_ROLE_RE.search(low):
      continue
  ```

  `GRANT USAGE ON SCHEMA sealed TO PUBLIC` contains `grant` but not `exchange`, so it was skipped — and because the schema-union regex lived *inside* that same loop, `granted_schemas` stayed `{ledger, app}` and the test **passed**. Every role inherits `PUBLIC`, so the blocker could never fire. **Rule: any frozen test whose body contains a `continue`/`skip` guard over the corpus it scans must ship a sabotage fixture — insert the forbidden thing, assert red — or it does not count as coverage.** Related and worth carrying: where a frozen test *parses text* to infer a runtime property, expect the inference to have a hole; its green is evidence about the parse, not about the system, and a live observation of the real thing is strictly stronger.

  *Why this rule matters beyond itself:* that defect was found by the agent **writing the SQL**, not by any review pass — and so was the run's other most serious defect (an unparseable `CREATE VECTOR INDEX` that six tickets had copied verbatim). Two builder-found defects against **zero** found by the cycle-0 adversarial pass, a twelve-finding partner review, or a four-agent static audit. **Reading finds defects of commission; executing finds defects of omission**, and a suite authored entirely by reading is short on exactly the second kind.

- **Coverage is allocated by BLAST RADIUS, not by ticket size — and a ticket with ZERO frozen tests is a hard stop at freeze time.** Measured in one wave: `T-010` (a schema package) drew 8 frozen tests, `T-011` drew 2, `T-014` drew 2, `T-013` drew **0** — and `T-014` was the foundation package five downstream tickets built on. An adversarial review of the finished `task/T-014` found two critical defects, both squarely in the region its two tests cannot reach. `T-013` produced 33 files and 156 of its own tests, graded exclusively by the suite its own author wrote. At intake, cross each ticket's `unblocks` count against the number of frozen tests carrying its marker, and **flag top-quartile blast radius with bottom-quartile coverage** — either add acceptance criteria before the freeze or route the ticket to mandatory adversarial review at collection. Zero frozen tests is not a row in a table; it stops the freeze.

- **A directory-level contract check must also catch a FULLY DELETED directory.** The generic bug: a "no empty test directories" check that builds its `by_dir` index *from the files that exist* produces no entry — and therefore no failure — when every file under a test directory is deleted. It catches only the case where a non-test file survives, while its docstring claims it catches "a ticket merged with its tests removed." Any check that iterates found things to prove a property about expected things needs the expected set pinned independently (the frozen manifest's own file list, a committed inventory), or deletion is invisible to it. This is an authoring rule for whatever contract scripts a project writes; this skill ships none.

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

**What "protected" means is configurable at `init`, and it defaults to those two paths.** `swarmloop.py init --protect-path PATH` is repeatable and **REPLACES** the whole list rather than adding to it — pass every path you want protected in one `init`, including `.swarm-loop/acceptance` and `.swarm-loop/goals.json` if you still want them, because naming one other path drops both. A previous run's additions in the same repo are carried forward by `init --force`, and `--force` prints what it carried; passing `--protect-path` alongside `--force` overrides that carry-forward silently. Use it when the run's real acceptance surface lives outside `.swarm-loop/` — a checked-in golden-file directory, a contract schema, a fixture corpus that must not drift.

**`freeze --allow-missing-protected` exists because a protected path that does not exist yet otherwise refuses the freeze**, and the cost of using it is specific: the missing path is **UNPROTECTED for the whole run** — it is never hashed, so `verify` can never notice it changing — while `check-branch` still vetoes any worker branch that touches it. That asymmetry is the trap: the veto keeps working, so the run *looks* protected. If you pass it, say so in the cycle-0 report by name, and re-check whether the path exists before the first measurement rather than at the end.


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

**An amendment that changes a TARGET invalidates every measurement of that metric taken before it.** This is not a nicety — the stored `error` column was computed against the old target, so every earlier row is now fitting a line to a dead number. Measured: one amendment raised five targets at once (`acceptance_collected` 103→120, and four per-epic targets), invalidating 24 cycle-0 rows; the `acceptance_collected` rows read `value=103, error=0.0` **because 103 had been the target**, so a metric 17 short would have rendered `at_target`. That run got the right answer only because the operator re-ran `measure --cycle 0` twenty-three seconds after the amend, by their own discipline. So: **after any `freeze --amend` that moves a target, re-measure the affected cycles before running `analyze`, and say in the epoch report that you did.** Treat a trend drawn across an un-re-measured amendment as unreadable.

**And the harness now tracks this for you — `pending_remeasure` in `state.json`.** An amendment that moves a target writes the *current* cycle number into `state.json`'s `pending_remeasure` list (an optional key: absent means nothing is pending, so state written by an older version reads clean) and prints a banner saying so. While a cycle sits in that list, `analyze` **withholds all-targets-met**, so a run cannot conclude on numbers measured against a target that has since moved.

**Exactly one thing clears it: a COMPLETE `measure --cycle <n>` for that cycle.** Not `record`, which is explicit about it — a hand-entered row prints a NOTE saying it does not satisfy the pending re-measure — and not a partial measure, because a run in which some metric's command failed leaves the amended target unmeasured for that metric, which is the state the flag exists to keep visible. When the last pending cycle is measured the key is removed from `state.json` entirely. So the operator-facing rule is unchanged (re-measure after a target move) and now has a mechanism behind it: if `analyze` will not declare all targets met and the numbers look right, read `pending_remeasure` before looking for a bug.

**Regenerate every DERIVED artifact as part of the amendment that invalidates it.** Anything generated *from* the frozen suite — a public-surface / naming-contract document, a per-ticket test inventory, a coverage-by-ticket table — is stale the instant the suite changes, and **the failure is silent and in the worst direction**. Reproduced: a `public-surface.md` generated by AST-parsing the suite at 103 tests, pasted block-by-block into task packets as the naming contract, was never regenerated when an amendment took the suite to 120 — its block still described a **five**-dimension snapshot where the frozen payload asserts **six**. A worker following the stale block builds the right behaviour under the wrong name; its test keeps raising `ModuleNotFoundError`/`AttributeError`; and at baseline that is indistinguishable from "not built yet", so the goal stays red for the rest of the run with nothing to point at. **Make regeneration part of the amendment itself**, in the same sitting, or the amendment silently poisons every packet written afterwards.

And the amendment itself stays exactly where it was. Only the user, awake, amends a frozen goal, and that amendment is **mechanically gated** — which matters because this is the highest-pressure decision in the run and it used to be guarded by nothing but a sentence. A re-`freeze` on an already-frozen project exits 1 unless you pass `--amend "<reason>"`; an amended freeze prints the old and new manifest hashes plus a unified diff of exactly what changed, appends the reason to the append-only `.swarm-loop/freeze-log.jsonl`, and is surfaced in the final report. A mid-run re-freeze is a **user-awake action**. Never take one on your own judgment, and never as a convenience to make a number move. An escalation you filed is not an amendment you were granted.

**A re-freeze is NEVER the repair for a red `verify`.** A red verify means the harness on disk no longer matches the manifest; re-freezing blesses the tampering and destroys the only evidence that it happened. Repair it with `swarmloop.py restore-harness` — which exists precisely because the three canonical git spellings for this are all blocked by this skill's own `PreToolUse` git-guard (`git checkout -- <path>`, `git restore <path>`, `git checkout HEAD -- <dir>`; verified exit 2 on each). If you find yourself reaching for `freeze --amend` to turn `verify` green, stop: you are about to launder a defect into a goal. And if `restore-harness` cannot make it green — the freeze-time state was never committed, the damage predates the manifest — the exit is not a re-freeze and not a shrug: `escalate --kind harness --blocking`, because every score downstream is void until a human resolves it. Escalating a broken harness is the honest terminal condition; re-freezing one is the dishonest one.

**The one repair the harness prints that you cannot run: a lost `.swarm-loop/freeze-log.jsonl`.** This is a documented deadlock, not a hypothetical, and it is worth knowing before you meet it at 3am. The freeze log is reconciled against the manifest by `_verify_quiet`, and a **missing** log over a manifest that records a freeze is its own `[MISSING]` violation — which means `verify` is red and `measure`, `record` and `analyze` all `die()` through `_integrity_gate`. Three things then compound:

1. **`restore-harness` does not restore it.** That command rewrites only the paths listed in `manifest["files"]` — the protected acceptance suite and `goals.json`. The freeze log is not among them, so `restore-harness` reports "nothing to do" and `verify` stays red.
2. **The repair the harness prints is blocked by this skill's own guard.** `freeze` prints `git show <commit>:.swarm-loop/freeze-log.jsonl > .swarm-loop/freeze-log.jsonl`. Probed: **exit 2** — `freeze-log.jsonl` is one of the seven ledger names, and the guard blocks `>` and `>>` onto any of them. Same for `>>`.
3. **So is every git-native alternative.** `git checkout <commit> -- .swarm-loop/freeze-log.jsonl` and `git restore --source=<commit> .swarm-loop/freeze-log.jsonl` both exit 2.

**The recovery that works — probed exit 0 on both calls and run end-to-end (log deleted, restored, bytes identical):** stage it outside the ledger, then copy it in. `cp` and `install` are not in the guard's non-git table, and the redirect rule is keyed on the *target*, so a redirect to `/tmp` is fine.

```bash
git log --oneline -- .swarm-loop/freeze-log.jsonl          # find the newest commit that has it
git show <commit>:.swarm-loop/freeze-log.jsonl > /tmp/freeze-log.jsonl   # exit 0
cp /tmp/freeze-log.jsonl .swarm-loop/freeze-log.jsonl                   # exit 0
swarmloop.py verify
```

**What this recovers and what it does not.** It recovers the log **as of that commit**. Any record appended since — an amendment made after the last time the log was committed — is gone, and `verify` will then report `[UNLOGGED]` instead of `[MISSING]` because the manifest's `frozen_at` appears nowhere in the recovered log. That is not a worse failure; it is the *same* failure told accurately, and it is unrecoverable by any command in this skill. **If the log was never committed, or the recovered copy does not carry the manifest's `frozen_at`, there is no recovery**: stop and run `swarmloop.py escalate --kind harness --blocking --subject .swarm-loop/freeze-log.jsonl --reason "the append-only freeze log is gone/incomplete and cannot be reconstructed; every score after <frozen_at> is unverifiable" --proposed "<re-freeze from a clean baseline, or accept the run as unscored>"`. `--blocking` exits 2, which is the run's terminal condition. Do not re-freeze to clear it — that is exactly the laundering sequence the check exists to catch. **Prophylactic, and cheap: commit `.swarm-loop/freeze-log.jsonl` with the baseline and again after every amendment.** An uncommitted log has no recovery path at all.

## Baseline (cycle 0)

Immediately after freezing: `verify`, then `measure --cycle 0`, then `checkpoint --cycle 0`. The baseline anchors the regression (greenfield baselines are all-failing, which is correct and useful) and seeds the HEAD history so the stall counter is meaningful from cycle 1. **Read cycle 0 before dispatching, don't just record it:** the freeze smoke-run has already warned about any metric sitting at target with no implementation behind it, and cycle 0 is where you act on that warning — an already-green metric at cycle 0 is a vacuous-test signal, and the goals are immutable **to you** from here. Immutable is not untouchable: if cycle 0 convinces you a metric is vacuous, `escalate --kind frozen-goal` with the evidence *before dispatching*, rather than starting a whole run against a score you already believe is fake. Then dispatch anyway on everything the disputed metric does not judge — the escalation is the ask, not the pause. Record the `acceptance_collected` value too: it is that metric's target for the rest of the run. Then **commit `.swarm-loop/` (goals.json, acceptance/, manifest.json, state) to `main` before dispatching anything** — worktrees branch from committed state, so an uncommitted harness leaves every worker without its acceptance tests, and "restore the harness from git" is only possible if the freeze-time state was committed.

## Setting targets honestly

Targets come from the brief where stated, from convention where not (pass rates 100, error counts 0, coverage 70–85). Resist inflating a target "to be ambitious" — an unreachable target guarantees `converging_off_track` verdicts that drown real signal — and resist deflating one to guarantee a win, which defeats the exercise. If the scope is large relative to the ticket graph and audit-loop budget, say so at the checkpoint and propose scope, not lowered standards: fewer features fully done beats all features half-measured, because half-measured doesn't pass acceptance tests anyway. That conversation is free before the freeze because the user is sitting there. After the freeze it still exists — it just runs through `escalate --kind frozen-goal` with a proposed target and the evidence for it, and waits for an answer while the run continues. What never exists is the third option of quietly re-reading a target you find inconvenient.
