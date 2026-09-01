# Merge Protocol — integrating a wave without corrupting main

The premise: worktrees moved conflicts from silent runtime corruption to visible merge time — this protocol is how you pay that bill in an orderly way. `main` must be green at the end of every cycle; everything here serves that.

## Procedure

1. **Eligibility gate (per branch, from Phase 5):** `check-branch` passed; changed files ⊆ ownership map; completion report received. Ineligible branches don't enter integration at all.

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

5. **Textual conflicts** (same lines, compatible intents): resolve them yourself in the integration worktree. You have both branches' reports and the ownership map; this is usually mechanical. Never resolve by editing an agent's branch — the branch is the audit record of what that agent produced.

6. **Design collisions** (both branches "work" but embody incompatible decisions — two auth session shapes, two conflicting registrations of the same route): do not hand-blend at the merge prompt. Pick the branch that better serves the frozen acceptance tests, merge it, reject the other back to the backlog with a note naming the surviving design as its new constraint. Hand-blended designs pass the merge and fail the measurement, which wastes a cycle discovering what you already knew.

7. **A branch that cannot be made green** inside integration gets unwound (`git merge --abort` or revert of its merge commit) and returned to the backlog with the failure note. Partial delivery of a wave is normal and fine — the metrics will record exactly how partial.

8. **Sync the measurement environment.** If any integrated branch changed a dependency manifest (`package.json`/lockfile, `pyproject.toml`, …), re-sync the primary measurement environment now — `npm ci`, `uv pip install -e '.[dev]'`, whatever the stack's clean install is — as an explicit step BEFORE the cycle's measurement. A stale-but-present dep dir reads as a false ZERO on the affected metrics: a `node_modules` predating newly added devDeps measured 0/6 until `npm ci` made it 6/6, and a venv missing new `pyproject.toml` deps did the same. When that zero appears it is an env-sync issue, never a code regression — diagnose it as such before touching any code.

   **The same obligation applies per-worktree at dispatch** (cross-reference SKILL.md Phase 4): a fresh worktree contains only *tracked* files, so every gitignored dependency or build directory is missing, and how that missing dependency presents depends on where the worktree sits. Beside the repo (the default) it is a loud `MODULE_NOT_FOUND` — a blocked worker, but a visible one. Under the in-tree fallback it is silent and worse: Node's resolver climbs UP out of the nested worktree into the primary tree's `node_modules` and, under workspaces, into MAIN's source, so an unprovisioned worktree quietly tests main's code while the worker believes it is testing its own. Either way the fix is the same and it is the dispatcher's job: provision each worktree's environment as an explicit dispatch step, never as the worker's first guess.

9. **Land it:**
   ```bash
   git checkout main && git merge --ff-only integration/<epoch>-<batch>
   ```
   **Mandatory at every landing — tracked-file-count tripwire:**
   ```bash
   git ls-tree -r HEAD --name-only | wc -l
   ```
   Run it per batch, not per epoch: the batch is where an index wipe is *introduced*, and an epoch that lands four batches and checks once cannot say which one did it. Compare against the same count from the previous landing. A large unexplained drop means a possible index wipe: an emptied index turns `git add <file> && git commit` into a repo wipe while `git status` shows the survivors as untracked (`??`) and the suite stays green — tests read the working tree, git reads the index, and the two can disagree totally. An unexplained drop blocks the epoch's Phase 7 push. The harness's `checkpoint` now records this count and prints a warning banner on a big drop; the check exists in two places on purpose — a banner can scroll past unread, this step cannot be skipped.
   **Do NOT push here.** The "checks" that go green in step 4 are the *fast* checks — build, unit tests, lint — which is verbatim the push trigger SKILL.md's Phase 7 forbids by name, and the cadence is wrong besides: an epoch may integrate several batches while measurement is pinned to after the epoch's merges, so a per-batch push ships several times against a number nobody has read yet. Pushing is a **Phase 7 act, after `verify`/`measure`/`analyze`**. Record this batch's tracked-file count in the epoch report so Phase 7 has it. Pushing is the only remote-touching act in the loop; anything more (PRs, deploys, releases) is out of scope by design.

10. **Clean up:** retire merged worktrees via the guard-safe path — `rm -rf <the run's worktree directory>/<id> && git worktree prune`, using whatever location Phase 4 chose for this run (`../<repo>-worktrees/<id>` by default, `.swarm-loop/worktrees/<id>` under the in-tree fallback); one location for the whole run, and the tooling finds live worktrees by path, so a guessed path silently prunes nothing. The git-guard blocks `git worktree remove` for the orchestrator too, which is why `rm -rf` + `git worktree prune` is the sanctioned equivalent rather than a workaround. Then delete merged task branches (`git branch -D task/<id>`) and the landed `integration/<epoch>-<batch>` branch; keep rejected branches until their ticket is resolved (they carry the failed attempt's diff, which informs the retry packet).

## Playbook fragments

- **Lockfile conflict:** you parallelized a dependency change — that's a planning defect. Take main's lockfile, drop the branch's dependency edits, schedule a dedicated sequenced dependency task, and write the learning.
- **Repeated hotspot conflicts on the same file across batches:** the ownership map is missing a structural fact. Either assign the file one in-flight writer at a time or refactor it into per-feature files (a legitimate ticket to schedule).
- **Scope creep discovered mid-merge** (diff touches unowned files that Phase 5 missed): stop, unwind that merge, and re-run the eligibility gate honestly. Letting one slide because it's "probably fine" is how the next three cycles' conflicts get manufactured.
- **After the final merge each cycle,** record in the cycle report: merge order used, conflicts hit and their causes, branches rejected and why. This is primary feedstock for the process loop — most learnings.md entries are born here.
