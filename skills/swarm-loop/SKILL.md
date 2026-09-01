---
name: swarm-loop
description: The central build skill — autonomously turns a planned doc set or project description into working software in two acts. Act one schedules every ticket by dependency readiness and blast radius (waves are emergent, not planned) across a maximally parallel swarm of Claude Code subagents in isolated worktrees, with machine-checkable goals frozen at the start, a per-batch adversarial verification ladder, and a linear-regression evaluation steering priorities. Act two runs a user-set number of whole-codebase audit-fix loops — every loop audits everything in parallel, findings become tickets, tickets get resolved, repeat. Use whenever the user wants an autonomous build or overnight agentic development against a spec, PRD, or build-docs doc set (SPEC.md / DESIGN.md / TASKS.md / EXECUTION.md — the docs plan, this skill builds), wants to "swarm" a project, or says things like "break this spec into atomic tasks for parallel agents", "set goals and loop until they're hit", or "kick off the swarm loop". Covers ticket scheduling, git-worktree isolation, dispatch and monitoring, merge integration, the tamper-proof frozen metric harness, the verification ladder, audit-fix loops, a no-progress kill switch, and self-improvement of its own playbook, plus optional real-time Jira scrum-board mirroring of every ticket.
---

# Swarm Loop

This skill turns a project description — ideally a build-docs doc set — into working software in **two acts**, both metric-driven against goals **defined and frozen at the very start**:

1. **The build** — every ticket runs through a readiness scheduler: a ticket dispatches the moment its dependencies are closed, its blast radius overlaps no in-flight writer, and merge-review bandwidth allows. Waves are *emergent batches*, not planned rounds, and the swarm stays as wide as non-interference permits. Each integrated batch passes the verification ladder; a "cycle" survives as the measurement epoch — the heartbeat at which the frozen metrics are measured, the regression re-fit, and priorities re-steered — not as a planning unit.
2. **The audit-fix loops** — once every ticket (including every minted bug ticket) is closed, a user-set number of loops each audit the **entire codebase in parallel**, convert findings into tickets, and resolve them through the same scheduler before the next loop re-audits everything fresh. A loop that finds nothing worth fixing ends the run early, through the prune gate.

Two feedback loops run at once, and both matter:

1. **The product loop** — the codebase converges on the frozen goals. Each cycle's regression analysis (error slope, projection to the final cycle, per-metric verdict) decides what the next cycle works on and how hard.
2. **The process loop** — the orchestration itself improves, starting inside the very first cycle. At every **wave boundary** and every **cycle boundary**, record what went wrong with *how you ran the swarm* (task sizing, prompt gaps, merge collisions, wave ordering) in a learnings file, and apply those corrections to the very next wave's packets and the next cycle's decomposition. At the end of the run, distill durable lessons into this skill's own `LEARNED.md` so future runs start ahead.

The design borrows deliberately: from the Ralph-loop school, that **progress lives in files and git history, not in the context window**, and that success criteria are defined up front so iteration has something to converge on; from Anthropic's multi-agent orchestration work, the four-part subagent contract and effort scaling; and from the reward-hacking literature, that a verifier the worker can edit is not a verifier — hence the frozen harness.

**Read `LEARNED.md` in this skill's directory before starting.** It contains distilled corrections from previous runs and overrides any conflicting default below.

## Invariants (why the loop can be trusted)

- **Goals are frozen after cycle-1 approval.** The whole point is measuring error against a fixed target. A goalpost that moves each cycle makes the regression meaningless and invites the failure mode where the system "succeeds" by redefining success. `swarmloop.py freeze` hashes `goals.json` and the acceptance tests; `verify` must pass before every scoring; a branch that touches protected paths is vetoed at merge time by `check-branch`. If verification ever fails, restore the harness from git before scoring — scores against a modified harness are void.
- **Goals are frozen; the route is not.** How a thing gets built — the design, the decomposition, the implementation approach, the plan sketched in a task packet — may change at any moment, mid-task and mid-cycle, without user input, whenever evidence says the current path is wrong. Pivoting the route is not renegotiating the goals, and confusing the two freezes is how a loop wastes cycles being timid. What stays frozen without the user: the goals, the acceptance tests, the packet boundaries (ownership, dependencies, `.swarm-loop/`), and existing assertions absent the recorded `justify-test-edit` ritual. Workers pivot freely inside their ownership (see the "Your discretion" tier in `references/task-packets.md`); the orchestrator pivots decomposition, scheduling, and approach at any moment. Stopping to ask is for boundary crossings and goal changes — never for route changes.
- **Everything is a ticket.** Build-doc tickets, workers' `BUGS-OBSERVED` reports, wave-verifier findings, audit-loop findings, red-metric fixes — one backlog, one scheduler, one lifecycle: minted → scheduled by readiness and blast radius → built → verified → closed. If work is happening that no ticket names, the orchestrator has lost the plot: mint the ticket first. This is also what makes the run resumable and auditable — the ticket ledger in `backlog.md` is the run's ground truth.
- **Subagents build; the orchestrator measures, merges, and decides.** Workers never run the scoring, never edit anything under `.swarm-loop/`, and never merge their own branches. Separation of builder and verifier is the anti-gaming defense that survives even a well-intentioned agent "fixing" a test to match its code.
- **Tests are append-only by default — the frozen harness is not the only verifier.** Freezing `.swarm-loop/acceptance/` protects the *goals*, but a worker's ordinary unit tests are just as load-bearing and fully editable, so the same "make the red thing green" pressure lands there instead. Adding a test or an assertion is free. **Modifying or deleting an existing assertion requires the `justify-test-edit` ritual** — quote the assertion, cite the requirement it encodes, and answer the discriminating question: *would this test still be wrong if I reverted my change?* If no, the code is wrong, not the test. Every disguise counts as modification: widening a tolerance, relaxing a matcher, trimming a parametrize list, `skip`/`xfail`, or regenerating a snapshot whose diff nobody read. A test edit with no recorded justification is indistinguishable from reward hacking after the fact — so the justification lives in the diff and the commit body, and the orchestrator audits every test diff at collection.
- **A green metric is evidence about the metric.** The frozen harness measures what its
  acceptance tests exercise — which is usually a stub, a fixture, or the code called the
  way the test calls it. It cannot see a feature that is built-but-unreachable, a gate that
  grades a fixture instead of the system, a real dependency whose output shape differs from
  the recording, or a deployed image missing a file the code opens at runtime. All of those
  have shipped at 12/12 green. Before believing a cycle's numbers mean the product works,
  load the **`stub-blindness`** skill and exercise the real path once. 12/12 means "built",
  never "delivered".
- **Every number comes from `scripts/swarmloop.py`.** Never eyeball a trend or hand-compute a slope — the script's output is the single source of truth for verdicts, projections, the stall counter, and termination signals.
- **File ownership is mapped before dispatch, not discovered at merge.** Tickets that need the same files are serialized by the scheduler, never in flight together. This single planning step prevents most merge pain.
- **Keep the swarm saturated — gates are checkpoints, not a metronome.** Every moment where no agent is running and no integration is proceeding is waste. The gates in this skill exist to catch defects, never to set the pace, and none of them requires idleness: concurrency is bounded by blast-radius interference, not by a headcount (read-only agents are never capped and run concurrently with everything; mutating agents run as wide as non-interference allows); collection is pipelined per returning branch, not batched behind the last straggler; the next wave's recon, packet drafting, and non-interfering dependency-free dispatches overlap the current wave's build and merge. Slowness is not caution — an orchestrator that leaves the swarm idle to feel careful is spending wall-clock without buying any verification.
- **Every agent in a MUTATING wave runs in its own isolation — never a shared working tree.** Any wave whose agents write files gives each agent a dedicated git worktree (`git worktree add`, or the Agent tool's `isolation: 'worktree'`). A shared tree is unsafe not merely from file overlap (the ownership map handles that) but because a single *repo-global* git op — `git stash`, `reset --hard`, `checkout -- <path>`, `checkout .`, `clean -f` — reverts or deletes **every** agent's uncommitted work at once, across the whole tree. This is not hypothetical: a `git stash`+`pop` by one fix-agent silently reverted five siblings' in-progress edits mid-wave, and the pop then aborted on a conflict, stranding their work in a dangling stash. Read-only auditor waves (no branch, no writes) may share the tree; nothing that mutates ever may. The `PreToolUse` git-guard hook (Phase 4) is the backstop for when this rule is bypassed — but the isolation *is* the fix; the hook is only a net under it.
- **The run is finite, and the ticket ledger is what bounds it.** The build act ends when the ticket set closes; the audit act ends at the user-set loop count or the first clean loop; the kill switch (3 consecutive changeless checkpoints — exit 2) and the `--max-cycles` safety ceiling guard against a run that spins without closing tickets. Never talk yourself into "one more loop" past a terminal signal — and never end early for a reason no signal names (context pressure included).
- **Audit coverage is whole-project every loop — never partitioned across loops.** Build-act scheduling runs a prioritized frontier, because each feature is built once. That logic must never leak into the audit act: when the job is finding defects, **every loop re-sweeps the entire codebase.** Fan auditors out in parallel, but only if their union covers everything and the partitions rotate and overlap across loops, so no region is ever examined by one set of eyes on one loop only. Every auditor returns a coverage map; named gaps become mandatory targets. The redundancy is not waste — it *is* the guarantee that nothing was missed. Auditing stops only when a whole-project loop comes back with nothing worth fixing (the Phase 9 gate), never when "each area has been touched once."

## Workflow at a glance

- **Phase 0 — Initialize** (once, interactive): read the docs, ask the audit-loop count, confirm autonomy, detect greenfield/brownfield, provision permissions, `init`.
- **Phase 1 — Context** (every epoch): full codebase survey first (brownfield) or scaffold (greenfield); incremental map update from merges thereafter.
- **Phase 2 — Goals** (once): tickets seeded from the docs (or decomposed), machine-checkable metrics with numeric targets, acceptance tests written *before any implementation* → **CHECKPOINT: plan approval** → `freeze` → baseline.
- **Phase 3 — Schedule**: compute the ready frontier (dependencies closed, blast radius clear); dispatch is rolling, waves are emergent.
- **Phase 4 — Dispatch**: one worktree + branch per ticket; spawn every ready, non-interfering ticket with a complete packet.
- **Phase 5 — Collect** (pipelined per returning branch): integrity, scope, test-diff audit, report triage; bug reports mint tickets.
- **Phase 6 — Integrate + verify**: merge one branch at a time through an integration branch, tests green after each; then main; **one adversarial verifier per integrated batch** (the ladder); push if configured.
- **Phase 7 — Evaluate** (each measurement epoch): `verify`, `measure`, `analyze`, `checkpoint` — regression verdicts and terminal signals.
- **Phase 8 — Adapt**: reprioritize the frontier from the analysis (product loop); update the learnings file (process loop); write the epoch report; loop to Phase 3 while tickets remain.
- **The build → audit transition**: when every ticket is closed and the metrics are green (or every red is explained and escalated), the build act ends.
- **Audit-fix loops (×N, user-set)**: each loop audits the entire codebase in parallel, mints tickets from findings, and resolves them through Phases 3–7. A loop that mints nothing ends the run early via Phase 9.
- **Phase 9 — Prune** (only after a clean audit loop): propose removals, have a
  DIFFERENT agent try to prove each is alive, then approve personally. Uncertain ⇒ keep.
- **Termination**: final report with full metric trajectories; distill learnings into `LEARNED.md`.

---

## Phase 0 — Initialize (once)

Read the project description first — usually a PDF (`pdftotext <file> -`; if scanned, read pages visually) or a markdown brief. **If the intake is a build-docs doc set** — `SPEC.md` → `DESIGN.md` → a task graph (`TASKS.md` or a task store), often with an `EXECUTION.md` header, produced by the desktop `build-docs` planning skill — the planning half is already done: follow **`references/build-docs-intake.md`** (tier→loop mapping, precedence, and the derive-don't-invent procedure for Phases 0–3). Two intake rules bind immediately: a non-empty *Open questions* section in SPEC.md stops everything until the user answers, and the doc set owns *what* to build while this skill owns *how* the build runs. Then, in one exchange, ask the user:

1. **How many audit-fix loops after the build?** Suggest 2 for a small scope, 3 as a sane default, 5 for a large or safety-critical one. The build act itself is not budgeted in cycles — it runs until the ticket set closes. `--max-cycles` still gets a value at `init`, but as a generous **runaway safety ceiling** (a sane default: 2× the ticket count + 3× the audit-loop count + 5), not a plan; the kill switch and the ticket ledger govern the real shape. Pass the loop count as `--audit-loops N`.
2. **Autonomy mode.** Default is *approve the cycle-1 plan, then fully autonomous*; confirm it explicitly (the user may want per-cycle approval or full autonomy). Also confirm push behavior: default is push `main` to the remote after each cycle whose integration is conflict-free and green; `--push-remote never` keeps everything local. Credential-holding actions beyond `git push` to an already-configured remote (deploys, secrets, account changes) are never the loop's job.

Detect the codebase situation: inspect the working directory for `.git/`, dependency manifests, a populated `src/`. Brownfield if a real project exists; greenfield otherwise — say what you found and let the user correct you.

Also check for a **`.codegraph/` index** at the repo root. If present, CodeGraph is the run's default codebase navigation — for the orchestrator and every packet (Phase 1, Phase 4, Phase 9). If absent, ask the user once in the same init exchange whether to index first — indexing is the user's decision, and without an index CodeGraph is skipped entirely for the run.

Also check whether a Jira/Atlassian MCP server is connected. If so, confirm the mirror in the same exchange (on by default; project key to use or create) and follow the setup in **`references/jira-mirror.md`** — a scrum board per project, sprints per cycle, issues per task, moving in real time as the loop runs. If no Jira MCP is connected, skip silently; the loop is identical without it. Then:

```bash
python3 <skill>/scripts/swarmloop.py init --max-cycles <ceiling> --audit-loops <N> --autonomy <mode> --push-remote <mode>
```

Greenfield with no repo yet: `git init -b main`, commit a baseline, then `init`. Re-running `init --force` on top of a prior run archives that run's `history.csv` and `analysis/` into `.swarm-loop/archive/run-<timestamp>/` (gitignored) so the regression never mixes two runs' data.

**Provision permissions before the first wave.** In an unattended run, a permission prompt is a hung worker, and a hung worker is misdiagnosed as a failed task. Enumerate what this build will actually touch (browser automation? `.env` writes? package installs? docker? network fetches?), write the allowlist into the project's gitignored `.claude/settings.local.json` — the same file that carries the git-guard hook, so wire both now in one sitting (Phase 4 only checks the guard is in place before the first mutating wave) — and dry-probe one call of each kind before dispatching wave 1. Browser work has a second gate no settings file reaches: the Chrome extension's own site-level permissions, granted in the extension beforehand. Template and checklist: **`references/run-permissions.md`**.

## Phase 1 — Context (first entry in full; map upkeep every epoch)

The map lives at `.swarm-loop/codebase-map.md` and is what task packets quote from, so keep it accurate and compact — components, entry points, data model, conventions, test layout, known hazards (hotspot files everyone touches, fragile modules).

- **First entry, brownfield:** do a genuine onboarding, not a skim — README/docs, dependency manifests, directory shape, entry points, schema, integrations, existing tests (tests reveal intended behavior). Use parallel read-only subagents for any survey larger than a few files — one per area, uncapped, since read-only work costs no merge review. Write the map.
- **Navigate with CodeGraph when indexed.** `codegraph explore "<question or symbol names>"` (or the codegraph MCP tool) returns verbatim line-numbered source plus the call paths between symbols and a blast-radius summary in one query — including dynamic-dispatch hops grep can't follow. Reach for it before grep/find or bulk file-reading, for the initial survey and for every incremental map update. A direct CodeGraph answer is one to a few calls; a grep/read exploration of the same question is dozens.
- **First entry, greenfield:** scaffold the skeleton (structure, tooling, test runner, lint/type config, CI-style check script), commit, and write the map from what you created. The scaffold — usually the T-0 ticket — is what makes the baseline metrics measurable at all.
- **Every later epoch (from Phase 8's loop):** don't re-scan. Read the previous epoch's report and merge log, diff `main` against the previous epoch's HEAD (recorded in `state.json` → `head_history`), and update only the changed parts of the map. Note anything a subagent reported that contradicts the map.

## Phase 2 — Goals (once, before any building) · CHECKPOINT

This is the most consequential phase — everything downstream regresses against what you set here. Read **`references/goal-setting.md`** before writing anything; it covers metric design, the `goals.json` schema, and the anti-gaming rules in full.

**With a build-docs intake, derive — don't invent.** The ticket graph seeds `backlog.md` verbatim (T-IDs, dependencies, scopes, non-goals preserved; T-0 is the scaffold task); `goals.json` metrics come from SPEC success criteria and per-ticket acceptance counts, with R-ID/T-ID traceability; `acceptance/` is written from the tickets' acceptance criteria and DESIGN's verification strategy. Run the adversarial pass against the *codebase*, not just the prose — scope globs and delta-spec claims are claims until checked against the tree. Full procedure: `references/build-docs-intake.md`. The checkpoint then presents the docs' plan with every intake divergence flagged, rather than a re-derivation.

The short version:

1. **Decompose the description into atomic features** — each independently implementable, testable, and small enough for one subagent context. Record them in `.swarm-loop/backlog.md` with IDs, descriptions, dependencies, and the files each will own.
2. **Write acceptance tests first**, into `.swarm-loop/acceptance/`, before any implementation exists. Tests-as-goals is the mechanism that makes "error" measurable: a feature's completion metric is literally its passing test count. Expect them all to fail on cycle 1 — that *is* the baseline.
3. **Define `goals.json`**: a handful of global metrics (acceptance pass rate, lint/type error count, build success, coverage if meaningful) plus per-feature metrics, each with a shell `command` that prints a bare number as its last stdout line, a numeric `target`, and a `direction`. 6–12 metrics total; more dilutes signal.
4. **Present the plan and get approval, then freeze, then baseline — in that order.** The checkpoint below comes first (goals freeze *after* cycle-1 approval, per the invariant). Then `swarmloop.py freeze` — it smoke-runs every metric command first (same 600s-per-command timeout as `measure`; `--smoke-timeout` adjusts), and a command that cannot print a parseable number fails the freeze loudly (fix the command *before* anything is hashed; `--skip-smoke` bypasses, not recommended). Then the cycle-0 baseline: `verify`, `measure --cycle 0`, `checkpoint --cycle 0` (the checkpoint seeds the kill switch's HEAD history). **Then commit `.swarm-loop/` to `main`** — goals.json, acceptance/, manifest.json, state — **before any dispatch.** Worktrees see only committed state: an uncommitted harness means every wave-1 worker's "Done means" points at acceptance tests that do not exist in its tree, and restoring a violated harness from git requires exactly this freeze-time commit.

**Checkpoint:** present the ticket graph, the metrics with targets, the audit-loop count, and the initial frontier the scheduler derived. Get explicit approval (per the autonomy mode) before dispatching anything. After approval, the goals do not change — if mid-run evidence shows a goal was genuinely wrong (not merely hard), stop the loop and say so rather than quietly renegotiating with yourself; changing goals is the user's call, made awake.

## Phase 3 — Schedule the frontier

Waves are **computed, not planned**. From the ticket ledger, a ticket is **ready** when every `depends_on` is closed; a ready ticket **dispatches** the moment its blast radius overlaps no in-flight writer and merge-review bandwidth allows. The worked shape: six tickets where 2 waits on 1, 4 waits on 3, and 6 waits on 5 — the first frontier is {1, 3, 5}, all at once; {2, 4, 6} follow as their blockers clear. And *rolling*: if ticket 1 closes while 3 and 5 are still building, ticket 2 dispatches immediately — it waits for its blocker, never for a round. Analyze the blast radius of every ticket **once, up front, at intake** (scope globs plus the shared surfaces globs can't see — registries, schemas, route tables) and record overlaps as scheduling constraints; recompute readiness continuously as branches land.

**Priority within the frontier** follows the latest analysis: tickets touching regressing metrics first, then stalled, then converging-off-track; among those, the largest error contributions first. Before any analysis exists, order by dependency depth — unblock the most downstream work first.

For each ticket confirm atomicity before dispatch: one clear objective, done-condition tied to named acceptance tests, fits one subagent context, owns a specific file set. Split anything that fails the test — oversized tickets are the top historical cause of failed batches. Under a build-docs intake, validate `scope` globs and `parallel_safe` against the real tree rather than taking them on faith; they were set at planning time against a repo that has since changed.

The **blast-radius map** (file ownership plus shared surfaces) governs concurrent dispatch:

- No two in-flight tickets share a file. Overlaps → serialized by the scheduler, or redesign one ticket to be additive-only (new file + import) instead of editing the shared file.
- Hotspot files (route tables, config, shared types, barrel exports) get one in-flight writer at a time. Dependency-manifest/lockfile changes get their own serialized ticket — never parallel.
- Wave width is bounded by **interference, not a headcount**. The rule: no two concurrent writers may share a blast radius — and blast radius means more than files (the ownership map's file lists are necessary, not sufficient): a shared registry, schema, route table, or global assumption two tasks both touch is the same collision wearing different files. Within that rule, run as many agents as the work offers. 3–5 mutating branches per *integration batch* is a merge-review heuristic, not a ceiling — when tasks genuinely don't affect each other's work, go wider and split integration into more batches rather than idling agents. Read-only agents (recon, auditors, verifiers) are **uncapped always** and run concurrently with mutating waves; they cost no merge review at all.

During the build act there is no separate review cadence: drift-hunting belongs to the **wave verifiers** (Phase 6, every integrated batch) and, after the build converges, to the **audit-fix loops** — where whole-codebase coverage rules live.

## Phase 4 — Dispatch the swarm

Per task: create the isolation, then spawn.

```bash
git worktree add .swarm-loop/worktrees/<task-id> -b task/<task-id> main
```

**Isolation is mandatory for every mutating wave — the single most important dispatch rule, and the one an orchestrator is most tempted to skip.** Each agent that will WRITE files gets its *own* isolation: the `git worktree add` above (one per task-id), or, when dispatching through the Agent tool, its own `isolation: 'worktree'`. **Never run a wave of file-mutating agents in the shared primary tree** — not even when the file-ownership map proves they touch disjoint files. The ownership map prevents *file* collisions; it does nothing against a *repo-global* git op. One agent running `git stash`, `git reset --hard`, `git checkout -- <path>`, `git checkout .`, or `git clean -f` in a shared tree reverts or deletes the uncommitted work of **every** other agent in that tree simultaneously — the exact incident this rule exists to prevent (a `git stash`+`pop` silently reverted five siblings' mid-wave edits; the orchestrator then had to reconstruct the true tree state by hand). The only wave that may share the primary tree is a purely **read-only auditor** wave — no branch, no writes. If any agent in the wave will commit, the whole wave is isolated.

**Install the git-guard before the first mutating wave of a run.** As a backstop for when isolation is bypassed or an agent strays outside its worktree, wire this skill's `hooks/git-guard.py` as a `PreToolUse` Bash hook. It argv-parses each bash command (quotes respected, so a commit *message* mentioning "stash" is fine) and blocks exactly the destructive repo-global git ops — `git stash` (push/save/pop/apply/drop/clear/branch), `reset --hard`/`--merge`/`--keep`, `checkout`/`switch`/`restore` targeting working-tree paths, `checkout .`, `checkout -f`/`switch -f`, `clean -f/-d/-x`, `worktree remove`/`move` (long options matched by git's abbreviation rule, so `--ha` and `--f` are caught too) — while allowing all normal loop and auditor git (`add`, `commit`, `push`, mixed/soft `reset`, `checkout <branch>`/`-b`, `switch <branch>`, `diff`, `show`, `log`, `status`, `ls-*`, `stash list`/`show`/`create`, `worktree add`/`list`). Activate it for the session in the project's **gitignored `.claude/settings.local.json`** — never the committed `settings.json`, and never the user's global `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command",
                     "command": "python3 \"$HOME/.claude/skills/swarm-loop/hooks/git-guard.py\"" } ] }
    ]
  }
}
```

The guard **fails open** — a JSON parse error, unbalanced quotes, or any internal fault allows the command — so it can never wedge the loop; blocking is reserved for a positively-identified destructive op (exit 2 with a message pointing at the read-only alternative). It is a net under the structural isolation rule, not a substitute for it.

Spawn all subagents for a wave in parallel (one Task tool call each, same turn). A subagent only knows what its prompt contains — there is no shared context — so every packet must be complete. Use the template in **`references/task-packets.md`**; the non-negotiable contract is: objective + done-condition (named acceptance tests), output format (commits on its branch + a structured completion report), boundaries (owned files; the prohibitions), and guidance (relevant codebase-map excerpt + current learnings). The prohibitions every packet carries: never modify `.swarm-loop/` anything; never edit files outside your ownership; additive-only for shared surfaces; never touch dependency manifests unless the task *is* the dependency task; **never run a repo-global git op (`git stash`/`reset --hard`/`checkout -- <path>`/`checkout .`/`clean -f` — they clobber every parallel agent's tree; use read-only `git show HEAD:<file>` or `git diff HEAD -- <file>` instead);** **report every defect you notice, in or out of your scope — you may never FIX outside your ownership, but silence about a bug you saw is the only wrong move (the packet's `BUGS-OBSERVED` report field exists for exactly this);** commit your work — uncommitted work is lost work; and **tests are append-only — add all you like, but do not modify or delete an existing assertion without the `justify-test-edit` ritual recorded in the diff and the commit body (a red test after your change means your code is wrong until proven otherwise; the orchestrator audits every test diff and rejects unjustified ones).**

## Phase 5 — Collect and check

Run these checks **per branch, as it returns — never batched behind the last straggler**. A wave's stragglers are drafting time: while they finish, check the returned branches, draft the next wave's packets, and update the map. Before a branch is eligible for merge:

1. `swarmloop.py check-branch --branch task/<id>` — automatic veto on harness or orchestrator-state modifications.
2. Compare the changed-file list against the task's ownership map. Scope creep (drive-by refactors of adjacent files) is grounds for rejection even when well-intentioned — it's the top source of cross-branch conflicts.
3. **Audit every test diff for weakened assertions** — this is the check nothing else performs, and the one a green wave hides. `git diff main..task/<id> -- '*test*'` and read the **removed** lines, not the added ones. Any modified/deleted assertion, widened tolerance, relaxed matcher, dropped parametrize case, new `skip`/`xfail`, or regenerated snapshot must carry a recorded `justify-test-edit` justification naming the requirement and answering *"would this still fail if the change were reverted?"*. **No justification → reject the branch**, even if every metric is green; a worker that quietly edits a test to fit its code has broken the verifier, and the metrics cannot see it. Additive tests need no justification — the asymmetry is the point.
4. Read the completion report; spot-check claims against the diff. A subagent saying "done" is a claim, not a fact — the measurement phase is the fact.
5. **Triage `BUGS-OBSERVED` and `NEEDS` from every report — including from rejected branches.** An `[open]` defect that blocks this cycle's integration or explains a currently red metric gets a **same-cycle hotfix dispatch** (fresh worktree, tight packet, the reporter's evidence pasted in); the rest go to the backlog with the evidence attached. A `[fixed]` item needs no dispatch — confirm the fix in the branch diff during this collection pass (it is a separate commit by contract). Never sit on a reported bug until "next cycle" out of habit — the report channel only works if reporting visibly causes action.

Failed, hung, or rejected tasks: log what happened and *why* in the cycle report, return the task to the backlog annotated with the failure mode, and retire the worktree via the guard-safe path — `rm -rf .swarm-loop/worktrees/<id> && git worktree prune && git branch -D task/<id>` (the git-guard blocks `git worktree remove` for everyone, the orchestrator included; `rm` + `prune` is the sanctioned equivalent). Don't redispatch the *same approach* in the same cycle — but a **different, named approach may go out the same cycle** when the failure mode is understood: say in the new packet what was tried and why it failed. Only an unexplained failure waits for the next cycle's fresh decomposition.

**External-limit deaths are not task faults.** A worker killed by a session cap, a credit limit, or an API 529 is *recovered*, not treated as a failed approach: inspect its worktree (`git -C <wt> status`) and salvage sound uncommitted artifacts (an `npm install` result is worth keeping); integrate finished peers first so re-dispatches branch off the richer HEAD; then re-dispatch the dead task with the same spec. And **never inherit a dead agent's green**: a worker that died before running its own verification leaves a hypothesis wearing a green suite — re-run the verification it didn't reach and write the tests it didn't write before believing the work.

**Wave-boundary tick (process loop).** Before dispatching the cycle's next wave, spend a moment on what this wave just taught you about the orchestration — a packet that lacked context, a boundary that was ambiguous, an ownership overlap the map missed, a task that was clearly two tasks. Append it to `.swarm-loop/learnings.md` and apply it to the next wave's packets *now*, in this cycle. This is what makes the loop self-improving from wave 1 of run 1 rather than from cycle 2 onward — the cheapest lessons are the ones applied minutes after they're learned.

## Phase 6 — Integrate

Follow **`references/merge-protocol.md`**. The shape: create `integration/<epoch>-<batch>` from `main` (one branch per integration batch); merge accepted branches **one at a time in dependency order** (most foundational first); after each merge run the fast checks (build + tests); resolve conflicts in the integration worktree yourself — never inside an agent's branch; a branch whose conflicts imply a design collision (not just textual overlap) gets rejected back to the backlog rather than hand-blended. When integration is green: merge to `main`, `checkpoint` happens in Phase 7, and push per the configured mode. If integration cannot be made green, merge what is green, return the rest, and let the measurement record the truth.

Integration is serial; the loop around it is not. While merging a batch, tickets whose blast radius **and** dependencies are disjoint from every unmerged branch may already dispatch off current `main` — by construction they need nothing from the branches still in flight. What may never overlap: the measurement itself — Phase 7 runs against a settled `main`, after the epoch's merges are done.

### The verification ladder — every integrated batch, one unit, no exceptions

Terminology first, because the word is overloaded: **"verify"** names three things in this skill — the harness command (`swarmloop.py verify`, freeze integrity), **this ladder** (adversarial verification of built work), and the Phase 9 refuter. When any other document says "every build phase gets a verifier," read: **every integration batch.** Two anti-patterns are outlawed by name:

- **One verifier per build agent.** It duplicates rung 1's mechanized checks, is structurally blind to the cross-branch collisions that are the *actual* dominant failure mode (they only exist after integration), and doubles headcount to buy coverage of the wrong layer.
- **One verifier for the whole run or a multi-cycle "phase."** Unbounded context makes verification shallow, and defects surface late — a bug found five batches after it merged has been built on five times.

**Rung 1 — mechanized checks, every branch (Phase 5).** Deterministic, orchestrator-run: `check-branch`, scope audit, test-diff removed-lines audit, report-vs-diff. Necessary, never sufficient — no rung-1 pass is ever cited as "verified."

**Rung 2 — one adversarial verifier per integration batch. The default unit; always.** A read-only subagent that built *nothing* in the batch it judges, dispatched the moment the batch lands on `main` and running concurrently with the next batch's build (read-only is uncapped — this rung costs zero wall-clock). Its packet (variant in `references/task-packets.md`) carries the batch's ticket packets, the doers' reports **labeled as claims to test, not facts**, the integrated diff range, current metric values, and relevant learnings. Its duties: exercise each ticket's real path once through a real entry point and state *what the green is evidence of*; hunt cross-branch interactions (registries, schemas, shared assumptions); find callers of everything the batch added (CodeGraph when indexed) — zero callers means the wiring half was skipped; audit every claim against the diff — a dead or lazy agent leaves a hypothesis wearing a green suite; and at deep depth, **sabotage in a scratch copy**: introduce the plausible wrong fix (never mere deletion), purge caches between variants, and prove the sabotage reached the code path before believing any result. It returns findings **with reproductions**, a **coverage map** (examined / not reached — named gaps become tickets), and a per-ticket verdict **with the evidence that earned it**. A verdict without evidence is not a verification: reject it and re-dispatch.

**Depth scales with the batch's contents, never with anxiety:**

| Batch contains | Depth |
|---|---|
| Docs, comments, purely additive tests | Rung 1 + a spot-check |
| Ordinary feature code | Standard: real-path exercise + claims audit + caller check |
| Shared interfaces, data models, migrations, auth/security, concurrency, anything in a frozen metric's blast radius | Deep: standard + sabotage + adversarial fixtures |
| Dead-agent salvage; a second attempt after rejection | Deep + rerun the verification the first attempt claimed to have done |

**Rung 3 — per-ticket deep verification, by trigger only.** Never as a default. Triggers: the deep-row categories above when a single ticket dominates a batch, a rung-2 coverage map naming a gap, or the orchestrator's own prescribed check turning out vacuous (validate the verification, not just run it).

**Rung 4 — the audit-fix loops and the frozen harness.** The harness sees only what acceptance exercises; the audit loops arrive after the build. Neither substitutes for rung 2, and rung 2 does not substitute for them.

Findings mint tickets through the Phase 5 triage machinery. A finding that invalidates a merged branch mints a fix ticket at the front of the frontier — or a revert per `references/merge-protocol.md` when the design itself is wrong. Verifiers rotate: a fresh verifier per batch, and no agent re-verifies work shaped by its own earlier findings without a second set of eyes at the next audit loop.

## Phase 7 — Evaluate (the feedback in the loop)

If the cycle merged any dependency-manifest change (`package.json`/lockfile, `pyproject.toml`, …), **re-sync the primary measurement env first** (`npm ci`, `uv pip install -e '.[dev]'`, stack-appropriate) — a stale-but-present dep dir reads as a false ZERO on the affected metrics, and it is an env-sync issue, never a code regression. Then, strict order, all through the script:

```bash
python3 <skill>/scripts/swarmloop.py verify            # harness intact? if not: restore, else scores are void
python3 <skill>/scripts/swarmloop.py measure --cycle N # runs every metric command, records value+error
python3 <skill>/scripts/swarmloop.py analyze --cycle N # OLS per metric: slope, R², projection, verdict
python3 <skill>/scripts/swarmloop.py checkpoint --cycle N  # records HEAD, drives kill switch + budget
```

`analyze` fits ordinary least squares over each metric's per-cycle error and issues a verdict: `at_target`, `converging_on_track` (negative slope, projected error ≈ 0 by the final cycle), `converging_off_track` (improving, but the projection says it won't arrive in budget), `stalled`, `regressing`. Honest framing baked into the output: with ≤10 points this is trend detection for steering, not statistical inference — treat slope and projection as a signal, never as a proof.

**Exit semantics under the two-act shape.** `checkpoint` exit 2 (kill switch or safety-ceiling) is **always terminal** — go to Termination. `analyze` exit 2 (all targets met) is act-dependent: during the **build**, all-green plus an empty ticket ledger is the **build → audit transition**, not termination (all-green with tickets still open just means keep building); during the **audit-fix loops**, all-green is the expected steady state and decides nothing — only the loop budget or a clean audit ends the run. One honest caveat: with `--max-cycles` set as a generous ceiling, `analyze`'s projection-to-final-cycle is lenient — under dynamic scheduling, steer by the **verdict ordering** (regressing → stalled → off-track), not by the projection. And read exit 2 with its message: the terminal exits always print KILL SWITCH / budget-exhausted / all-met text, while a bad flag or subcommand exits **3** with a usage message — a usage error is never a terminal signal; fix the command and re-run.

**Never push before this phase.** Pushing on "tests green + types clean" skips the only
thing that speaks for the frozen goals; a regression found after the push costs a
correction across every remote and host, and it shipped in between. Measure, read the
number, then push. When the loop pushes or deploys anything, load the
`verify-the-deploy` skill — verify each remote's SHA and the deploy target's HEAD
rather than trusting a command's exit or your own success message.

## Phase 8 — Adapt (close both loops)

**Product loop** — rewrite the backlog priorities from the verdicts:

- `regressing` → top priority; something merged this cycle broke it. Diff-hunt the cause; create a targeted fix task.
- `stalled` → the current approach isn't working; mandate a *different* approach in the new task packet (different design, different decomposition, more context), and say explicitly what was already tried. Re-running the same task is how stalls become kill-switch terminations.
- `converging_off_track` → the projection says effort is insufficient: allocate more of the next cycle to it, or split its tasks finer.
- `converging_on_track` / `at_target` → don't over-invest; protect them (regressions get caught next measure).

**Process loop** — append to `.swarm-loop/learnings.md`: what about *the orchestration* failed or worked this cycle (task too big; packet missing context X; files Y and Z collided because the ownership map missed an import chain; wave 2 should have been sequenced). Next cycle's Phases 3–4 must apply these. Keep entries imperative and specific — they get injected into task packets.

Write `.swarm-loop/reports/cycle-N.md`: tickets closed this epoch (with their resolution, one line each), tickets minted (with source — worker report, verifier finding, audit), the analysis table, decisions for the next epoch. Then loop to Phase 3 while tickets remain; when the ledger is empty and the metrics are green (or every red is explained and escalated), take the build → audit transition.

**Ledger hygiene — no doc becomes an endless bug log.** The ticket ledger (`backlog.md`) holds **open tickets and scheduling constraints only**; a closed ticket leaves the ledger the epoch it closes, surviving only as its one-line entry in that epoch's report and in git history, and a scheduling constraint leaves the ledger with the last ticket it names. Raw `BUGS-OBSERVED` text is triaged at collection — minted into a ticket or dismissed with a one-line reason in the epoch report — and is never copied into any standing document; the ticket is the record. `learnings.md` stays process-only, imperative, and curated (merge duplicates as you append; ~30 entries is the ceiling). There is **no** "known issues" file, no cumulative bug register, no append-forever list anywhere in the repo: bounded history lives in the per-epoch reports, full history lives in git, and the final report summarizes both. Any file that only ever grows is a defect in the run — fix it like one.

**Close the loop with a tool call, not with prose.** The orchestrator is re-invoked only by a pending task — if the turn that closes cycle N ends with a summary and no dispatched agent, nothing wakes you, the loop silently halts, and it looks exactly like an API freeze. Dispatch the next cycle's first agent in the **same turn** you close the current one, *before* writing the closing summary. "I'll continue" is a statement; a dispatched agent is continuation.

## The audit-fix loops (after the build converges)

The build act ends with every ticket closed and the goals green — which proves the *tickets*, not the product. The audit act exists because the frozen metrics see only what acceptance exercises, and the wave verifiers saw only their own batches. The user set the loop count `N` at Phase 0; run exactly this per loop:

1. **Audit everything, in parallel, this loop and every loop.** Fan out read-only auditors (uncapped) whose partitions' union covers the entire codebase; rotate and overlap partitions across loops per the coverage invariant. Use the audit-packet variant in `references/task-packets.md`: adversarial framing (find what is wrong, not confirm), evidence and reproductions per finding, and a mandatory coverage map whose named gaps become targets.
2. **Mint tickets from findings.** Each finding becomes a ticket with evidence, blast radius, and dependencies — or is dismissed with a one-line reason in the loop's report. No third state, and no standing findings list.
3. **Resolve the loop's tickets through the same machinery** — Phases 3–7: frontier scheduling, isolated worktrees, fix-ticket packets (reproduce-first, root cause, sibling sweep), integration, wave verification, measurement. The frozen goals are re-measured; a fix that turns a metric red is caught the same epoch.
4. **Close the loop** when its ticket set is empty: write `.swarm-loop/reports/audit-loop-K.md`, then record it — `swarmloop.py audit-loop-done` (add `--clean` when the audit minted zero tickets). The command tracks the act's progress in `state.json` and **exits 2 when terminal** (clean loop, or budget exhausted) exactly like the other terminal signals. Otherwise start the next loop **from scratch** — a fresh whole-codebase audit that assumes nothing from the last one.

**Early exit:** a loop whose audit mints zero tickets is a **clean loop** — the signal that auditing is done. Run Phase 9 below, then Termination, regardless of loops remaining. **Exhaustion:** finishing loop N without a clean loop also ends the run — the final report says so plainly, listing what the last loop still found; it does not talk itself into loop N+1.

## Phase 9 — The reverse loop: prune, only after a clean audit loop

Run this **only once an audit loop comes back with nothing worth fixing.** While real
defects are still being found, every hour spent on tidiness is an hour not spent on a
bug that reaches a user. Cleanliness is the reward for correctness, never a substitute.

The loop runs backwards: instead of *what is missing or wrong*, ask *what is here that
should not be*. Dead files, functions nobody calls, abandoned half-refactors, settings
nothing reads, tests that assert nothing, duplicated helpers, comments describing code
that no longer exists.

**The governing asymmetry — this is the whole design.** Leaving dead code costs
tidiness. Removing live code costs an outage, and it lands later, in production,
usually on the one path the suite never covered. **These are not comparable, so the
process is not symmetric:** a removal must be *positively proven dead*, and "I could
not find a caller" is not proof — it is the absence of proof, which is exactly what a
grep-shaped search returns for every dynamically-reached symbol.

### Three stages, all mandatory

1. **Propose (read-only).** Fan out auditors over disjoint areas. Each returns
   candidate removals, and each candidate MUST carry: what it is, why it is believed
   dead, **how the belief was established**, and the blast radius if the belief is
   wrong. No batch removals, no "delete this whole module" without a per-symbol case.
2. **Refute (independent, adversarial).** A *different* agent — never the proposer, and
   never seeded with the proposer's confidence — tries to prove each candidate is
   **alive**. Its job is to save the code, not to agree. It must actively hunt the ways
   static search lies:
   - dynamic dispatch, reflection, `getattr`/`__getattr__`, string-keyed registries
   - DI containers, plugin/entry-point registration, decorators that self-register
   - templates, config files, migrations, serialized data referencing a symbol by name
   - the public API surface (`__all__`, exported packages) — an external caller you
     cannot see is still a caller
   - tests are callers too; so are docs, examples, and the deploy
   - a symbol reached only in a mode this environment does not run (a flag that is off
     here, a dialect not used here, an OS branch)
   When a `.codegraph/` index exists, querying it for the candidate's callers and
   references is **mandatory before any grep-based deadness argument is admissible** —
   CodeGraph follows exactly the dynamic-dispatch hops (callbacks, registries,
   re-exports, JSX/DI wiring) that make grep-shaped searches lie about liveness.
   A candidate survives **only** if the refuter fails to find life AND says what it
   tried. "No refutation found" from an agent that only ran the same grep is worthless.
3. **Approve (the orchestrator, personally).** Read the proposal and the refutation and
   decide. Delete nothing you cannot explain the deadness of in one sentence. When the
   two agents disagree, **go to ground truth yourself** — disagreement is signal, and
   averaging two opinions about whether code is reachable is how live code dies.

### Bias every judgment call toward keeping

- **Uncertain ⇒ keep.** An unremoved dead function costs nothing but a line count.
- **Recently added ⇒ keep.** It may be half-wired rather than dead — a mechanism whose
  callers are still coming. (This is a real and common shape: a correct implementation
  with zero call sites is *unfinished*, not *dead*, and deleting it destroys the work.)
- **Load-bearing-looking ⇒ keep.** Guards, validators, fail-closed branches, error paths
  and telemetry frequently look "unused" precisely because they only run when something
  goes wrong. **The success path is not the only path.**
- **Frozen/goal/acceptance anything ⇒ never touch.** Not even to tidy.
- **Prefer proving deadness at runtime**: instrument, deploy, and observe zero hits over
  a real interval. That beats every static argument. If that is impractical, say so and
  keep.

### Land removals so they are cheap to undo
One concern per commit, never a mega-sweep; a commit body stating the evidence for
deadness and how it was refuted; the full suite AND the frozen metrics green after each.
A removal that cannot be reverted in one `git revert` is too big. Then **watch the
deployed system** — a deletion's failure mode is silence until the one caller you missed
runs.

## Termination and final report

**Context pressure is never a terminal signal.** The harness compacts and hands the work forward; everything load-bearing lives in `.swarm-loop/` and git precisely so that no session's context limit can end the run. The only terminal signals are the ones named below — if context is long, write the state down and keep going; do not editorialize a budget into a stop.

On any terminal signal — audit loops exhausted, a clean audit loop (via Phase 9), kill switch, the `--max-cycles` safety ceiling, an unrestorable harness violation, or the user's explicit stop — write `.swarm-loop/reports/final-report.md`:

- Outcome per metric: baseline → final value, target, met or not, and the full per-cycle trajectory (the analysis JSONs have it all).
- What was built, what was attempted and abandoned (with why), and what remains on the backlog.
- If the kill switch fired: the diagnosis — three changeless cycles means the loop was spinning, and the report should say what it was stuck on.
- **Self-improvement distillation:** compress `.swarm-loop/learnings.md` into durable, project-agnostic lessons and append them to `LEARNED.md` in this skill's directory (if writable; otherwise put them in the final report under "proposed skill updates"). Keep `LEARNED.md` curated: merge duplicates, drop project-specific noise, cap it at roughly 40 entries — a bloated learnings file degrades every future run.

Present the final report to the user. Do not push anything non-green as a parting gesture.

## Jira mirror (optional, when a Jira MCP is connected)

The loop mirrors itself onto a Jira scrum board so its progress is watchable in real time: epics per feature, a sprint per measurement epoch (and one per audit-fix loop, named "Audit loop K"), an issue per ticket transitioning **To Do → In Progress → In Review → Done** in lockstep with dispatch, collection, and merge, and the cycle analysis table posted to a Loop Control issue every cycle. Apply the hook table in **`references/jira-mirror.md`** at each phase boundary. Three invariants, argued fully in that reference: the mirror is **write-only** (board edits never steer the loop — talk to the orchestrator to change course), **never blocking** (Jira failures are queued and retried; the loop proceeds identically), and **harness-free** (`swarmloop.py` knows nothing about Jira, so the mirror layer is swappable).

## Resuming an interrupted run

Everything load-bearing lives in `.swarm-loop/` and git, not in any session's context — so a crashed or closed orchestrator session loses nothing but its place. To rejoin a run:

```bash
python3 <skill>/scripts/swarmloop.py resume
```

It reconstructs the situation from artifacts alone (no reliance on the dead session having marked anything): per-cycle completion flags (measured/analyzed/checkpointed/reported), harness integrity, live worktrees, unmerged `task/*` and `integration/*` branches, working-tree cleanliness — and prints a recommended re-entry point, including terminal conditions it detects (a clean or exhausted audit act, kill switch fired, safety ceiling hit, final report already written). All-green alone is **not** terminal: with audit loops still owed, `resume` reports it as the build→audit transition or the audit act's expected steady state — never as "go to Termination".

The recommendation is a heuristic; confirm it before acting. Read the latest cycle report, `learnings.md`, `backlog.md`, and the codebase map to reload the run's judgment, then rejoin at the indicated phase. Treat leftovers conservatively: run `check-branch` and the Phase 5 checks on any live branch before merging it; a worktree whose branch has no commits is a failed-hung task — reclaim it to the backlog. If the harness is reported violated, restoring it from git precedes everything else. If a cycle was interrupted between measure and checkpoint, complete the evaluation sequence in order before adapting — never plan a cycle off a half-evaluated one. If the Jira mirror is enabled, reconcile the board once after resuming (board ← loop, per the mirror reference) so tickets match reconstructed reality.

## Bundled resources

- `scripts/swarmloop.py` — the deterministic harness (state, freeze/verify, branch veto, measurement, regression, kill switch, resume). Run it; don't reimplement it.
- `hooks/git-guard.py` — `PreToolUse` Bash hook that blocks destructive repo-global git ops (stash/reset --hard/checkout-path/clean -f/worktree remove) while allowing all normal git. Wire it in `.claude/settings.local.json` before the first mutating wave (Phase 4). Fails open.
- `references/goal-setting.md` — read in full during Phase 2.
- `references/task-packets.md` — the subagent prompt contract and template, Phase 4.
- `references/merge-protocol.md` — the integration procedure and conflict playbook, Phase 6.
- `references/jira-mirror.md` — real-time scrum-board mirroring hooks and failure policy, all phases (optional).
- `references/run-permissions.md` — permission provisioning for unattended runs: the `settings.local.json` allowlist template and the pre-dispatch probe checklist, Phase 0.
- `references/build-docs-intake.md` — consuming a build-docs doc set (SPEC/DESIGN/task graph/EXECUTION): tier→loop mapping, precedence, derive-don't-invent, Phases 0–3.
- `LEARNED.md` — distilled cross-run lessons; read at start, append at termination.
