# Merge Protocol — integrating a wave without corrupting main

The premise: worktrees moved conflicts from silent runtime corruption to visible merge time — this protocol is how you pay that bill in an orderly way. `main` must be green at the end of every cycle; everything here serves that.

## Procedure

0. **Step zero — PIN THE TIP, before any check reads the branch.** Resolve
   `git rev-parse task/<id>` **once**, at the start of collection, and run every
   rung-1 check, the per-ticket attribution below, and the test-diff audit against
   **that SHA** (`check-branch --branch <sha>`, `git diff main...<sha>`). Write the
   SHA into the collection record and the epoch report. **Re-assert it immediately
   before accepting: if `git rev-parse task/<id>` has moved, the collection is
   VOID — redo it against the new tip.**

   *Why this is step zero and not a footnote.* Nothing else freezes a tip, and three
   tips moved mid-wave in one run: `T-011` `edb1845 → 50af7b1` under an in-flight
   review; `T-011` `50af7b1 → 7a4d9e8` under the orchestrator's own collection, where
   the new commit was *the fix for the branch's most serious blocker*; and `T-013`
   7 → 10 commits under collection, the new ones including a real money bug
   (`ROUND_HALF_EVEN` vs `ROUND_HALF_UP` — the cart quoted 87.66 and the order
   charged 87.65). **Every one was caught by eyeballing a commit count, not by any
   mechanism**, and `check-branch --branch <name>` resolves the ref *live*, so no
   record exists of which SHA a decision was made against. A measurement taken thirty
   seconds earlier would have accepted a branch on evidence gathered before its most
   important commit existed.

   *Second rule from the same evidence:* **treat a completion notification as "may
   still be moving" until the worktree is clean AND the tip is stable.** A verifier
   dispatched at an unpinned ref audits a tree that no longer exists — one run's
   verifier reported "the branch was still moving while it audited — it caught a test
   failing mid-write" — which is also how an unbounded fan-out spends agents on
   nothing. Never dispatch verification against an unpinned ref, and never dispatch
   it unbounded; they are the same fix at two ends.

1. **Eligibility gate (per branch, from Phase 5):** `check-branch` passed; changed files ⊆ ownership map; completion report received. Ineligible branches don't enter integration at all.

   **Per-ticket attribution — measure "this ticket is done", do not judge it.** Every
   frozen metric is suite- or epic-level, so `@pytest.mark.ticket(...)` — whose whole
   stated job is naming which ticket is responsible for making a test pass — feeds no
   metric at all, and "this ticket is done" is otherwise **purely the orchestrator's
   judgment at merge review**. The data already exists and needs no amendment: the
   frozen `conftest.py` records `"ticket"` on every result and the runner's `--json`
   dumps the records. So, before accepting a branch, run the acceptance runner with
   `--json` **inside the returning branch's own worktree**, at the pinned SHA, group
   by `ticket`, and confirm that ticket's frozen tests actually moved to `passed`.

   *Two gotchas that must be written down, because both are silent:* **`--json`
   writes to STDERR**, so `2>&1` is mandatory or a naive pipe gets nothing; and it
   `return`s before the count logic, so it **ignores every other flag** you pass
   alongside it.

   *It paid off on first use.* Run against two already-collected branches it returned
   `T-012: 1 passed` and `T-014: 2 passed` — both had been collected on the strength
   of the workers' self-reports plus rung-1 checks alone. Both happened to be fine;
   that was luck, not measurement.

   **And assert the second result explicitly, every time — it is not incidental.**
   Because the check runs the WHOLE frozen suite inside each worktree, each branch's
   sibling tests must read `failed` there (T-014's tests failed inside T-012's
   worktree and vice versa). That is direct evidence of **no cross-contamination
   between isolated trees** — the property the entire parallel design rests on, and
   one that nothing else in the run ever measures. If a sibling's tests pass inside
   this worktree, the isolation is broken and this collection is void.

   **Do NOT turn this into metrics.** Adding one metric per ticket would drown
   `analyze`'s regression and multiply the frozen-command surface. It is a **query,
   not a metric** — and it gives an epic shortfall an attribution the moment it
   appears rather than at cycle end.

   **The veto is mechanical; the question it raises is not.** A branch vetoed for touching a protected path or `.swarm-loop/` goes back to the backlog exactly as it does today — the gate does not bend, nothing frozen reaches `main` through a merge decision, and the retry packet carries the frozen artifact as an unchanged constraint. But before writing the rejection off as scope creep, read the branch's report and its diff of the frozen file and ask the one thing the veto cannot: **did this worker find a real problem with the goal?** A worker that edited a frozen assertion because the assertion is genuinely impossible has told you something valuable in the worst possible way, and rejecting the branch without reading it throws the finding out with the violation. If the answer is yes, the rejection is *also* an escalation — `swarmloop.py escalate --kind frozen-test --subject <path> --reason "<the worker's evidence>" --proposed "<the change being asked for>"` — filed alongside the backlog note, non-blocking, with the rest of the batch integrating normally. If the answer is no, it is scope creep and the backlog note says so plainly, so the retry does not repeat it.

2. **Create the integration branch — one per integration batch, not per epoch** (an epoch
   may integrate several batches; reusing one name makes the second `checkout -b` fail):
   ```bash
   git checkout -b integration/<epoch>-<batch> main   # e.g. integration/e3-b2
   ```

3. **Order the merges.** Most foundational first: shared types/interfaces → data layer → services/logic → routes/UI → tests/docs. Dependencies merge before dependents. Each successful merge shrinks the conflict surface for the rest.

4. **Merge one branch at a time, verify after each:**
   ```bash
   git merge --no-ff task/<id>
   <fast checks: build + unit tests + lint>
   ```
   Green → next branch. Red → diagnose before merging anything else; a broken integration branch plus more merges is how a cycle's whole output gets entangled.

   **Then the check no per-ticket gate can perform: TWO LANES THAT SHARE A SURFACE, IN ONE PROCESS.** After each merge that brings a second consumer of any shared runtime surface — a lock, a connection pool, a global registry, a singleton client, a module-level cache, a temp directory, a fixed port — run the two lanes **together, in one process**, not one after the other. Reproduced: `make verify` **self-deadlocked for 600 seconds** the moment two lanes each took a session-scoped `fcntl.flock` inside a single process. **Every individual ticket's verify passed**; the defect existed only in the PAIR. Mid-run it detonates looking like a hang — the worst possible shape to diagnose, and worse still when the suite's own `--timeout` is shorter than the lock's, so it surfaces as a confusing `pytest-timeout` inside the lock helper instead of the legible lock-timeout error the lock was written to produce. **Treat single-ticket green as saying nothing about this class**, and make the pair-run an explicit step rather than something the full-suite run is assumed to cover.

   **And gate any shared-`conftest.py` / shared-fixture change with a repo-wide collect.** Shared conftest changes are *integration* changes, not local ones:

   ```bash
   pytest --collect-only -q <every test root>   # not just the touched directory
   ```

   Treat any collection **ERROR** as a veto, not merely a count change. Reproduced: a **duplicate fixture name** killed an entire test directory at conftest-import time, taking already-merged tickets' tests down with it — and a count-only check reads that as "fewer tests", which is the wrong severity for "an import blew up".

5. **Textual conflicts** (same lines, compatible intents): resolve them yourself, in the **primary checkout, on the integration branch step 2 created**. This protocol never creates an integration *worktree* and should not: step 8's environment re-sync and the cycle's measurement both run in the primary checkout, and a second worktree carved inside the tree is exactly what Phase 4 moves worktrees out of the repo to avoid. You have both branches' reports and the ownership map; this is usually mechanical.

    **Taking one side wholesale: `git checkout --ours/--theirs <file>` is BLOCKED by this skill's git-guard.** Probed against the live hook inside a real conflicted merge: `--ours <file>`, `--theirs <file>`, `-2 <file>` and `-3 <file>` all exit 2 with ``BLOCKED … `git checkout <pathspec>` overwrites those working-tree files``. `check_checkout` has no conflict-stage case at all; the flags are stripped as options, so the file operand arrives as a lone non-ref pathspec and trips the pathspec rule. `git checkout --ours -- <file>` exits 2 on the `--` rule, and `git checkout --ours .` on the `checkout .` rule. Do not write the flag form into a runbook — an orchestrator that follows it is stuck mid-merge with a conflicted index.

    The working form reads the conflict stage out of the index and writes it over the file, which is the same operation without the porcelain (both probed, exit 0; run end-to-end on a `UU` file, then `git add` + `git commit`, tree clean):

    ```bash
    git show :2:<file> > <file>    # OURS   — stage 2 = the integration branch (HEAD)
    git show :3:<file> > <file>    # THEIRS — stage 3 = the branch being merged in
    git add <file>
    ```

    Two limits, both real. If the file does not exist on one side the stage is absent and `git show` errors — that is a **modify/delete** conflict, which is a design question for the two owners, not a side to pick. And if the target is a run ledger under `.swarm-loop/` the redirect itself is blocked (see the guard's ledger rule); a ledger should never be in a merge conflict, and one that is, is an escalation. Never resolve by editing an agent's branch — the branch is the audit record of what that agent produced.

6. **Design collisions** (both branches "work" but embody incompatible decisions — two auth session shapes, two conflicting registrations of the same route): do not hand-blend at the merge prompt. Pick the branch that better serves the frozen acceptance tests, merge it, reject the other back to the backlog with a note naming the surviving design as its new constraint. Hand-blended designs pass the merge and fail the measurement, which wastes a cycle discovering what you already knew.

7. **A branch that cannot be made green** inside integration gets unwound (`git merge --abort` or revert of its merge commit) and returned to the backlog with the failure note. Partial delivery of a wave is normal and fine — the metrics will record exactly how partial.

8. **Sync the measurement environment — and ASSERT THE EFFECT, never the exit code.** If any integrated branch changed a dependency manifest (`package.json`/lockfile, `pyproject.toml`, …), re-sync the primary measurement environment now — `npm ci`, `uv sync`, `uv pip install -e '.[dev]'`, whatever the stack's clean install is — as an explicit step BEFORE the cycle's measurement. A stale-but-present dep dir reads as a false ZERO on the affected metrics: a `node_modules` predating newly added devDeps measured 0/6 until `npm ci` made it 6/6, and a venv missing new `pyproject.toml` deps did the same. When that zero appears it is an env-sync issue, never a code regression — diagnose it as such before touching any code.

   **A package manager's exit 0 is not proof the environment matches the manifest.** Reproduced: **`uv sync --frozen` exits 0 against a CHANGED manifest** — a dependency addition became a silent no-op, the command succeeded, the environment did not match, and nothing said so. `--locked` is the flag that fails loudly when the lock no longer matches; `--frozen` is the more obvious one and is what the next author will reach for. So finish every re-sync with an **effect assertion**, not a return code: import the newly added module, and print its version and confirm it is the pinned one.

   ```bash
   uv sync --locked                                          # fails loudly on lock/manifest drift
   .venv/bin/python -c "import <newpkg>, importlib.metadata as m; \
       print(<newpkg>.__file__, m.version('<newpkg>'))"      # the effect, asserted
   ```

   **The same obligation applies per-worktree at dispatch** (cross-reference SKILL.md Phase 4): a fresh worktree contains only *tracked* files, so every gitignored dependency or build directory is missing, and how that missing dependency presents depends on where the worktree sits. Beside the repo (the default) it is a loud `MODULE_NOT_FOUND` — a blocked worker, but a visible one. Under the in-tree fallback it is silent and worse: Node's resolver climbs UP out of the nested worktree into the primary tree's `node_modules` and, under workspaces, into MAIN's source, so an unprovisioned worktree quietly tests main's code while the worker believes it is testing its own. Either way the fix is the same and it is the dispatcher's job: provision each worktree's environment as an explicit dispatch step, never as the worker's first guess.

9. **Land it — rebase onto `main` FIRST, then fast-forward:**
   ```bash
   git fetch . main                                          # no-op locally; keeps the form honest
   git rebase main integration/<epoch>-<batch>               # replay the batch onto current main
   # re-run THIS BATCH'S verify here — see the paragraph below; a rebase invalidates it
   git checkout main && git merge --ff-only integration/<epoch>-<batch>
   ```
   **The bare `--ff-only` on its own provably cannot run, and this is why the rebase is not optional.**
   Phase 8 commits `.swarm-loop/` to `main` at every epoch boundary (SKILL.md), which moves `main`;
   the moment it has, `main` and the integration branch have diverged and `--ff-only` aborts —
   reproduced: `main` ahead 1, integration ahead 2, `git merge-tree` **clean**, and
   `git merge --ff-only` exits **128** with *"Not possible to fast-forward"*. A clean `merge-tree`
   is not permission to land: it says the texts do not collide, nothing about whether the base the
   batch was built on still exists. Rebasing answers the second question, and keeps the linear
   history that makes `--ff-only` worth prescribing at all.

   **A rebase invalidates the batch's own green, so re-run its `verify` after the rebase and before
   the merge.** The suite that went green in step 4 ran against the pre-rebase base; the whole point
   of moving the batch onto current `main` is that `main` changed underneath it. `swarmloop.py
   check-branch --branch integration/<epoch>-<batch>` will now also print the branch's commit counts
   — `0 commit(s) ahead` is a refusal (there is nothing to land), and a non-zero *behind* count before
   the rebase is exactly the condition this step exists to clear, so it should read `/ 0 behind` by
   the time you merge.

   **Do NOT commit the epoch ledger onto the integration branch to dodge the divergence.** It is the
   obvious shortcut and it is blocked one gate later: `check-branch` refuses any branch whose diff
   touches `.swarm-loop/` — reproduced, `VETO: branch writes orchestrator-owned state under
   .swarm-loop/`, exit 1. The ledger belongs to `main` and to Phase 8; the batch rebases around it.
   **Mandatory at every landing — tracked-file-count tripwire:**
   ```bash
   git ls-tree -r HEAD --name-only | wc -l
   ```
   Run it per batch, not per epoch: the batch is where an index wipe is *introduced*, and an epoch that lands four batches and checks once cannot say which one did it. Compare against the same count from the previous landing, against the harness's own threshold rather than a feeling: `checkpoint` fires its banner when `drop > max(25, 0.10 * prev_count)` — more than 25 files, or more than 10% of the previous count, whichever is larger. Use that same arithmetic here so the two sites cannot disagree, and know the two differences that make this step the one that actually stops anything: the harness's copy is a **warning only**, deliberately never changing `checkpoint`'s exit code, and it compares against the last strictly-earlier *cycle*, so it can see the drop but never say which batch introduced it. A drop past that line means a possible index wipe: an emptied index turns `git add <file> && git commit` into a repo wipe while `git status` shows the survivors as untracked (`??`) and the suite stays green — tests read the working tree, git reads the index, and the two can disagree totally. An unexplained drop blocks the epoch's Phase 7 push. The check exists in two places on purpose — a banner can scroll past unread; this step cannot be skipped.
   **Do NOT push here.** The "checks" that go green in step 4 are the *fast* checks — build, unit tests, lint — which is verbatim the push trigger SKILL.md's Phase 7 forbids by name, and the cadence is wrong besides: an epoch may integrate several batches while measurement is pinned to after the epoch's merges, so a per-batch push ships several times against a number nobody has read yet. Pushing is a **Phase 7 act, after `verify`/`measure`/`analyze`**. Record this batch's tracked-file count in the epoch report so Phase 7 has it. Pushing is the only remote-touching act in the loop; anything more (PRs, deploys, releases) is out of scope by design.

10. **Clean up — RUN THE COMMAND, do not narrate it.** Teardown was prose in this document for its whole life, and prose is exactly what silently did not happen: one run finished a wave, landed it, pushed it, and still had all five `task/T-0xx` worktrees *and* the integration tree on disk when the next wave added six more — **13 trees × ~1.0 GB, six of them finished work that should have been retired right here.**

    ```bash
    python3 <skill>/scripts/swarmloop.py retire --dry-run   # the plan, and a reason per tree; mutates NOTHING
    python3 <skill>/scripts/swarmloop.py retire             # do it
    ```

    The rest of the flags, all with defaults you should have a reason to change:

    | Flag | Default | What it is for |
    |---|---|---|
    | `--base REF` | `state.base_branch` if set, else `main` | the branch work must have **landed on**. Merged into an integration branch is *not* landed. Nothing in the harness *writes* `base_branch` today, so in practice the default is `main` — pass `--base` on a run whose trunk is not `main`. |
    | `--idle-seconds N` | `900` | keep any tree whose git activity is newer than N seconds. `0` disables it — see the re-entry hazard below. |
    | `--timeout SEC` | `120` | bound on ONE `git worktree remove`. A timeout is reported as a **refusal** and is never escalated to `--force`. |
    | `--du-seconds SEC` | `60` | bound on sizing the root; `0` skips. Sizing is not free — `du -sk` of a live **14.7 GB** root measured **38.6s cold**. |
    | `--keep-branches` | off | retire the trees, delete no branches. |
    | `--json` | off | machine-readable report, same exit codes. |

    `retire` walks the run's `--worktree-root` — chosen at Phase 0 and recorded in `state.json` (`../<repo>-worktrees/<id>` by default, `.swarm-loop/worktrees/<id>` under the in-tree fallback), one location for the whole run — and retires every worktree whose work is DONE. Reading the root out of `state.json` is half the point: the tooling finds live worktrees **by path**, so a guessed path silently prunes nothing.

    **Retirable means ALL of** — anything failing one is *kept*, with a reason printed beside it that is a sentence you can act on:

    - **not the primary checkout** (excluded structurally — git always lists it first — and again by path). It is where the frozen metric commands run and the one tree whose loss would end the run;
    - **owned by this run**: under the recorded `worktree_root`, or the legacy in-tree `.swarm-loop/worktrees`, or sitting on a `refs/heads/task/` branch. Same ownership union `resume` uses, because Phase 4's move to a sibling directory made a single-spelling match blind to every run on the other layout. Trees this run does not own are not reported at all;
    - **has a branch** — a detached HEAD is kept, since nothing proves that work landed;
    - **not locked and not prunable** — `git worktree lock` is a deliberate claim, and a prunable record is a half-deleted tree that `git worktree prune` clears; `retire` guesses at neither;
    - **the branch is an ancestor of `--base`** (`git merge-base --is-ancestor <branch> <base>`) — the work LANDED. **Merged into an integration branch is NOT landed**;
    - **no open `dispatch.jsonl` record names it**, and **no git activity inside `--idle-seconds`**.

    **Cleanliness is deliberately NOT pre-checked.** `retire` calls plain `git worktree remove` and lets its refusal of a dirty tree be the check. There is no `--force` path in that function and there must never be one — the plain form's refusal is the only thing between an agent's uncommitted work and a mechanical loop that runs every cycle.

    **Exit 0 = nothing left to retire, or everything retirable was retired. Exit 1 = something refused** — which is a finding, not a failure of the command (see below); the human report prints a ready-made `escalate` line per refusal. **Exit 3 = usage error**, and like everywhere else in this harness a usage error is never a terminal signal. `--json` prints the per-tree decision (`retirable` / `retired` / `refused` / `kept`, each with its reason), which is what goes in the epoch report.

    **This step is the SWEEP, not the first attempt.** The call that should already have retired most of these fires in SKILL.md's Phase 6, the moment a batch lands on `main` and its rung-2 verdicts are in: that is when a tree stops having a consumer, and waiting until here holds a gigabyte per tree across the rest of the epoch. **Do not "optimize" the ancestor-of-`main` test down to merged-into-integration.** On one run, fix agents re-entered a ticket's worktree *after* adjudication, and re-verification had to run at the new tips — a tree retired at integration-merge time would have been deleted out from under the work that was about to happen in it. Ancestor-of-`main` plus no live agent is the conservative test, and it is conservative on purpose.

    **The re-entry hazard has TWO mechanical guards — know which one is holding a tree.** A fix agent minted from a rung-2 finding re-enters its ticket's worktree while the branch is still an ancestor of `main`, which is exactly the condition `retire` retires on. Both guards exist for that one case:

    - the **open-dispatch guard** — an open `dispatch.jsonl` record matching the tree's path, branch or ticket id holds it, and `swarmloop.py dispatch --close <agent-ref>` releases it. This is the better signal, because it says so in words instead of inferring from a clock. It is also **optional**: a run with no `dispatch.jsonl` at all (the live run had none) gets nothing from it, which is precisely when the second guard is load-bearing;
    - the **idle guard** — `--idle-seconds` (default 900) keeps any tree whose git admin files were touched more recently than that.

    So `retire` is safe to call the moment a batch's verdicts are in, and a tree it holds back tells you which guard held it. Once every agent for that tree is collected, `retire --idle-seconds 0` reclaims it. Neither guard is a substitute for the convention SKILL.md's Phase 5 triage already states — **a hotfix dispatch gets a FRESH worktree** — but they are what makes a mechanical loop safe when someone re-enters the old one anyway.

    **And a skipped teardown is now visible in the run's own output.** At every cycle boundary `checkpoint` prints a `WORKTREE TEARDOWN OVERDUE` banner listing every tree that has LANDED and is still on disk, the worktree root, and its size — so an accumulating root shows up there instead of showing up on the disk. It is **advisory, never fatal**: it prints and returns, it does not change `checkpoint`'s exit code, it is not a push blocker, and its whole body is wrapped so a disk-hygiene notice can never be the thing that crashes a checkpoint. It also prints *before* `checkpoint`'s terminal exits, so a kill-switch cycle still shows you the trees. When the banner appears: run `retire`, then re-read it.

    **The banner's count DIFFERS from `retire`'s, on purpose — do not "fix" it.** `checkpoint` scans with `idle_seconds=0`, so it reports landed trees that `retire` would currently hold back, and annotates each one with the guard holding it (`(touched Ns ago — retire holds this one)` / `(open dispatch … )`). The idle guard exists to protect a *removal*; a printed line cannot hurt a busy tree. Run under the guard, the banner went **silent for fifteen minutes after every wave landed** — precisely the moment a batch of finished trees hits the disk, and precisely what it exists to make visible. If every listed tree is held, the banner says so and tells you to come back with `--idle-seconds 0`.

    **Sizing is bounded everywhere, because it is not free.** `du -sk` of the live 14.7 GB worktree root measured **38.6 seconds cold**. `checkpoint` therefore gives it **3 seconds** and falls back to the size the last `retire` cached in `state.json` (`worktree_root_size_kb` / `_at`, printed with its timestamp), and failing that prints `not measured` and points at `retire --dry-run`. A `disk: not measured` line is a timed-out `du`, not an empty root.

    **Underneath it is plain `git worktree remove <path>`, and that is the entire design.** It is the only teardown that *validates* what it is deleting: it refuses a path that is not a worktree, and it refuses a worktree with uncommitted changes. `rm -rf` makes neither check. `--force` is not an escape hatch — it discards exactly the uncommitted work the plain form exists to protect, and it stays off-limits for everyone, orchestrator included; a worktree that refuses to retire is a worktree with unsalvaged work in it, which is a *finding*, not an obstacle.

    **There is no `rm -rf` fallback any more, and this document used to say there was.** Probed against the shipped guard: plain `git worktree remove <path>` exits **0** (allowed — it is the sanctioned teardown), `--force`/`-f` exits **2**, `worktree move` exits **2**, and `rm -rf <worktree-root>/<id>` exits **2** as well, with `rm -rf "$WT/$ID"` blocked on the collapsed form because an empty `$ID` reduces it to the worktree parent and takes every sibling's uncommitted work. So the old escape hatch is itself blocked, and reaching for it mid-run just costs you a wave. Probe rather than assume — but the answer today is that the plain form is the path.

    When the plain form *refuses* — which is what `retire`'s exit 1 is reporting, per tree — it is telling you the tree holds uncommitted work. That is a finding: salvage it (`git -C <wt> status`, commit or copy out what is sound), then retire the tree. `git worktree remove --force` is a **user-awake** act, never an orchestrator one, and if nobody is awake the answer is `swarmloop.py escalate --kind harness --subject "<worktree path>" --reason "..."` and leaving the tree in place — a stranded worktree costs disk; a forced one costs work that exists nowhere else.

    **Background `retire` by default, then verify it.** At swarm scale this is not a fast command: one run's worktree directory reached **16 GB** and the teardown **timed out at two minutes** (exit 143) mid-cycle. Run it detached (probed guard-allowed: `git worktree remove <path> &` and the `nohup … &` form both exit 0), confirm afterwards with `git worktree list` and a `du -sh` of the root, and never let a teardown sit in the critical path of the next dispatch. Verifying afterwards is not optional: `retire`'s exit code is unreadable once it is detached. The two-minute number is now the **`--timeout` default, applied per removal** — and a tree that hits it is reported as a **REFUSAL**, never escalated to `--force`, so a slow giant tree looks exactly like a dirty one in the report. That is the safe confusion to have, and the fix is `--timeout 600`, never `--force`.

    **`retire` deletes the landed branches itself, and its reach is slightly WIDER than the trees it retired.** It sweeps every `task/*` and `integration/*` ref that is an ancestor of `--base` **and checked out nowhere**, and `git branch -D`s it — so a landed `integration/<epoch>-<batch>` whose tree is already gone gets reaped on a later run rather than accumulating. Ancestor-of-base is what makes `-D` lossless: every commit on the branch is already reachable from base. **A non-ancestor is never touched** — a rejected or in-flight ticket keeps its diff for the retry packet. `--keep-branches` turns the whole sweep off. By hand it is the same `git branch -D` (probed guard-allowed, exit 0).

## Playbook fragments

- **Lockfile conflict:** you parallelized a dependency change — that's a planning defect. Take main's lockfile, drop the branch's dependency edits, schedule a dedicated sequenced dependency task, and write the learning.
- **Repeated hotspot conflicts on the same file across batches:** the ownership map is missing a structural fact. Either assign the file one in-flight writer at a time or refactor it into per-feature files (a legitimate ticket to schedule).
- **Scope creep discovered mid-merge** (diff touches unowned files that Phase 5 missed): stop, unwind that merge, and re-run the eligibility gate honestly. Letting one slide because it's "probably fine" is how the next three cycles' conflicts get manufactured.
- **After the final merge each cycle,** record in the cycle report: merge order used, conflicts hit and their causes, branches rejected and why. This is primary feedstock for the process loop — most learnings.md entries are born here.
