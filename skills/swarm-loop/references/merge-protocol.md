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

   The same obligation applies at dispatch, per worktree (SKILL.md Phase 4): the installer runs there too, and there too its exit code proves nothing.

   **The same obligation applies per-worktree at dispatch** (cross-reference SKILL.md Phase 4): a fresh worktree contains only *tracked* files, so every gitignored dependency or build directory is missing, and how that missing dependency presents depends on where the worktree sits. Beside the repo (the default) it is a loud `MODULE_NOT_FOUND` — a blocked worker, but a visible one. Under the in-tree fallback it is silent and worse: Node's resolver climbs UP out of the nested worktree into the primary tree's `node_modules` and, under workspaces, into MAIN's source, so an unprovisioned worktree quietly tests main's code while the worker believes it is testing its own. Either way the fix is the same and it is the dispatcher's job: provision each worktree's environment as an explicit dispatch step, never as the worker's first guess.

9. **Land it:**
   ```bash
   git checkout main && git merge --ff-only integration/<epoch>-<batch>
   ```
   **Mandatory at every landing — tracked-file-count tripwire:**
   ```bash
   git ls-tree -r HEAD --name-only | wc -l
   ```
   Run it per batch, not per epoch: the batch is where an index wipe is *introduced*, and an epoch that lands four batches and checks once cannot say which one did it. Compare against the same count from the previous landing, against the harness's own threshold rather than a feeling: `checkpoint` fires its banner when `drop > max(25, 0.10 * prev_count)` — more than 25 files, or more than 10% of the previous count, whichever is larger. Use that same arithmetic here so the two sites cannot disagree, and know the two differences that make this step the one that actually stops anything: the harness's copy is a **warning only**, deliberately never changing `checkpoint`'s exit code, and it compares against the last strictly-earlier *cycle*, so it can see the drop but never say which batch introduced it. A drop past that line means a possible index wipe: an emptied index turns `git add <file> && git commit` into a repo wipe while `git status` shows the survivors as untracked (`??`) and the suite stays green — tests read the working tree, git reads the index, and the two can disagree totally. An unexplained drop blocks the epoch's Phase 7 push. The check exists in two places on purpose — a banner can scroll past unread; this step cannot be skipped.
   **Do NOT push here.** The "checks" that go green in step 4 are the *fast* checks — build, unit tests, lint — which is verbatim the push trigger SKILL.md's Phase 7 forbids by name, and the cadence is wrong besides: an epoch may integrate several batches while measurement is pinned to after the epoch's merges, so a per-batch push ships several times against a number nobody has read yet. Pushing is a **Phase 7 act, after `verify`/`measure`/`analyze`**. Record this batch's tracked-file count in the epoch report so Phase 7 has it. Pushing is the only remote-touching act in the loop; anything more (PRs, deploys, releases) is out of scope by design.

10. **Clean up.** Retire merged worktrees under the `--worktree-root` chosen at Phase 0 and recorded in `state.json` (`../<repo>-worktrees/<id>` by default, `.swarm-loop/worktrees/<id>` under the in-tree fallback) — one location for the whole run, and the tooling finds live worktrees by path, so a guessed path silently prunes nothing.

    **Prefer `git worktree remove <path>`.** It is the only teardown that *validates* what it is deleting: it refuses a path that is not a worktree, and it refuses a worktree with uncommitted changes. `rm -rf` makes neither check. `--force` is not an escape hatch — it discards exactly the uncommitted work the plain form exists to protect, and it stays off-limits for everyone, orchestrator included; a worktree that refuses to retire is a worktree with unsalvaged work in it, which is a *finding*, not an obstacle.

    **There is no `rm -rf` fallback any more, and this document used to say there was.** Probed against the shipped guard: plain `git worktree remove <path>` exits **0** (allowed — it is the sanctioned teardown), `--force`/`-f` exits **2**, `worktree move` exits **2**, and `rm -rf <worktree-root>/<id>` exits **2** as well, with `rm -rf "$WT/$ID"` blocked on the collapsed form because an empty `$ID` reduces it to the worktree parent and takes every sibling's uncommitted work. So the old escape hatch is itself blocked, and reaching for it mid-run just costs you a wave. Probe rather than assume — but the answer today is that the plain form is the path.

    When the plain form *refuses*, it is telling you the tree holds uncommitted work. That is a finding: salvage it (`git -C <wt> status`, commit or copy out what is sound), then retire the tree. `git worktree remove --force` is a **user-awake** act, never an orchestrator one, and if nobody is awake the answer is `swarmloop.py escalate --kind harness --subject "<worktree path>" --reason "..."` and leaving the tree in place — a stranded worktree costs disk; a forced one costs work that exists nowhere else.

    **Background the teardown by default, then verify it.** At swarm scale this is not a fast command: one run's worktree directory reached **16 GB** and the teardown **timed out at two minutes** (exit 143) mid-cycle. Run it detached, confirm afterwards with `git worktree list` and a `du -sh` of the root, and never let a teardown sit in the critical path of the next dispatch.

    Then delete merged task branches (`git branch -D task/<id>`) and the landed `integration/<epoch>-<batch>` branch; keep rejected branches until their ticket is resolved (they carry the failed attempt's diff, which informs the retry packet).

## Playbook fragments

- **Lockfile conflict:** you parallelized a dependency change — that's a planning defect. Take main's lockfile, drop the branch's dependency edits, schedule a dedicated sequenced dependency task, and write the learning.
- **Repeated hotspot conflicts on the same file across batches:** the ownership map is missing a structural fact. Either assign the file one in-flight writer at a time or refactor it into per-feature files (a legitimate ticket to schedule).
- **Scope creep discovered mid-merge** (diff touches unowned files that Phase 5 missed): stop, unwind that merge, and re-run the eligibility gate honestly. Letting one slide because it's "probably fine" is how the next three cycles' conflicts get manufactured.
- **After the final merge each cycle,** record in the cycle report: merge order used, conflicts hit and their causes, branches rejected and why. This is primary feedstock for the process loop — most learnings.md entries are born here.
