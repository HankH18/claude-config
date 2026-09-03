# Task Packets — the subagent prompt contract

A subagent's entire world is its prompt. It has no access to the conversation, the plan, the map, or other agents — only the packet and the worktree. Anthropic's production lesson on delegation holds here: a worker needs **an objective, an output format, guidance, and boundaries**, and missing any of the four produces drift, not disobedience. The packet template below encodes all four; fill every section, delete none.

Underspecification is the dominant failure mode. When a packet is vague, the subagent doesn't stop — it guesses, and its guess conflicts with another agent's guess at merge time. When in doubt, include the extra paragraph of context; packet tokens are cheap compared to a rejected branch.

## The packet is two files: an invariant preamble plus a per-ticket half

Measured: wave 1's five packets were **~73 KB of orchestrator output over 3m43s**, written one at a time, and the first worker was three minutes into its task before the last one started. Every one of those packets repeated the same preamble verbatim. The preamble is not per-ticket text, and writing it five times buys nothing but latency and orchestrator context.

**Split it.**

- **`.swarm-loop/worker-preamble.md` — the invariant half, written ONCE per run.** Everything true of every worker in this run regardless of ticket: the isolation contract and the whole **Working method** section (prove-the-environment-is-yours, worktree-relative commands, never commit a provisioned directory), the **Your discretion** tiers, the entire **Boundaries** block including the repo-global git prohibitions and TESTS ARE APPEND-ONLY, the commit/merge protocol as the worker sees it (commit in coherent increments; the orchestrator merges branches and runs all measurement, you do neither), the **Report format**, and the standing ruling that **your ticket's own `verify` is vacuous until proven otherwise — the frozen acceptance suite is the grader, not your green.** Plus the standing rulings this run has minted: conform-or-replace decisions, shared-surface conventions, a design ruling carried over from an earlier batch.
- **The packet — the per-ticket half, minted per dispatch.** ID and epoch; Objective; Done-means with this ticket's frozen test commands; the worktree path and branch; **Files you own**; the ticket's public-surface extract; its dependencies' **landed** API (what actually merged, not what was planned); the map excerpt and only the learnings relevant to *this* area; the prior-attempt notes if it is a retry; and the CURRENT baseline number as measured at dispatch.
- **The packet's first line points at the preamble by path**, as an instruction and not a citation: *"Read `.swarm-loop/worker-preamble.md` in full before you touch anything — it is the contract this packet is written against. Read it; never edit it."*

**Who writes it and when: the orchestrator, once, at first dispatch (Phase 4) — and it must be COMMITTED to `main` before the first `git worktree add`.** A worktree carries only tracked files at the commit it branched from, so an uncommitted preamble is a preamble that does not exist for any worker; it is the same trap that otherwise leaves a worker without its acceptance tests. Put it at `.swarm-loop/worker-preamble.md`, at the top level of `.swarm-loop/` and **never inside `.swarm-loop/acceptance/`** — that directory is a protected path whose ADDED scan reports any unhashed file as an INTEGRITY VIOLATION and voids the cycle's scores. It is orchestrator-owned state like everything else under `.swarm-loop/`, so a worker branch that edits it is vetoed by `check-branch` before it is read; say that in the preamble itself, because a rule with its enforcement stated is the one that holds.

**A standing ruling that changes mid-run:** edit the preamble in the primary checkout, commit it to `main`, and stamp the change at the top under a dated `## Changelog` line naming the epoch it took effect. Every packet dispatched after that commit picks it up for free — that is the whole point of the split. **Workers already in flight do not**: their worktree is pinned to an older commit and their context already holds the old text, so a ruling that must reach them is sent to each running subagent directly, in the same turn as the edit, quoting only the delta; a ruling that can wait goes in the retry packet or the next dispatch. Never edit the preamble to retroactively legitimize a branch already rejected under the old rule — the changelog stamp is what lets a later reader tell which version a report was written against.

## Template

What follows is the **union of both halves** — the split above says which sections are minted per ticket and which are lifted into `.swarm-loop/worker-preamble.md` once per run. Either way every section reaches the worker exactly once.

```
You are implementing one atomic task inside an orchestrated build loop. Work ONLY
inside your worktree; commit your work; report in the format specified. The
orchestrator merges branches and runs all measurement — you do neither.

Read `.swarm-loop/worker-preamble.md` in full before you touch anything — it is
the contract this packet is written against, and it is mandatory reading, not
background. Read it; never edit it.

## Task
- ID: <task-id>   (epoch <N>)
- Objective: <one paragraph: what exists when you are done that does not exist now>
- Done means: the following acceptance tests pass when run from the repo root:
    <exact command(s) to run them, e.g. pytest .swarm-loop/acceptance/test_auth.py -k signup>
  You may RUN these tests as often as you like. You may not EDIT them — they are
  frozen goals. If you believe a test is wrong, you are not stuck with it and you
  do not work around it: say so in CONCERNS, in the shape spelled out in the
  report format below, and the orchestrator escalates it to the human who owns
  the goals. That channel is real and it is the only one you have. An objection
  you keep to yourself turns into a workaround, which gets your branch rejected.

## Where you work
- Worktree (absolute path): <path>   — do all work here
- Branch: task/<task-id>             — already created and checked out
- Branch point: <sha>                — the commit your worktree was cut from

  The shared, orchestrator-owned documents in your worktree — decisions.md,
  public-surface.md, backlog.md, learnings.md, the codebase map — are a
  SNAPSHOT taken at <sha>. The orchestrator keeps correcting those same files
  on `main` while you work. So a discrepancy you find in one of them may
  ALREADY BE FIXED on `main`: report it under SHARED-DOC-OBSERVATIONS as an
  observation, not a defect under BUGS-OBSERVED, and do not work around it and
  do not "fix" it. Your own source files are not affected by this — only the
  shared documents.

## Files you own (create/modify freely)
- <path/one>
- <path/two>
- New files under <dir> as needed

## Your discretion (decide alone — do not ask, do not stall)
The GOALS above are frozen; your ROUTE to them is not. Three tiers:
- Tier 1 — free, no permission needed: your implementation approach and design
  within your owned files, including abandoning the plan sketched in this
  packet when the evidence says it is wrong (say so in DECISIONS); new files
  within your scope; adding tests and assertions (always free); fixing any bug
  you find INSIDE your ownership even when it is not part of this task —
  commit that fix separately and list it in BUGS-OBSERVED, marked fixed.
- Tier 2 — proceed, then report: anything surprising that does not cross a
  boundary — a convention the map got wrong, a dependency of your task that
  behaves differently than described. Handle it, record it in
  DECISIONS/CONCERNS.
- Tier 3 — stop that thread and report (the ONLY stop tier): boundary
  crossings — files outside your ownership, dependency changes, anything under
  .swarm-loop/, and editing an existing assertion WITHOUT the recorded
  `justify-test-edit` ritual (with the ritual, the edit is permitted — see
  TESTS ARE APPEND-ONLY below for exactly what it requires).
Stopping is for boundaries, never for approach; a worker that stalls awaiting
permission to change its own route has misread this packet.

## Boundaries (violations cause your branch to be rejected)
- Do not modify any file outside your ownership list. If correctness truly
  requires an outside change, STOP that thread of work and state exactly what
  change is needed and why in your report — the orchestrator will schedule it.
- Fix inside, report everywhere: you may never FIX a defect outside your
  ownership, but you must ALWAYS report one you noticed — in BUGS-OBSERVED,
  whether or not it relates to this task. An unreported defect you noticed is
  a process failure; there is no penalty for reporting, only for silence.
- Do not touch anything under .swarm-loop/ (goals, acceptance tests, state).
  This boundary has no exception and no ritual that unlocks it — not even
  `justify-test-edit`, which covers the project's own tests and never the frozen
  ones. It is also not a dead end. A frozen test you believe is WRONG (not
  merely hard) gets REPORTED in CONCERNS, and the orchestrator carries it to the
  user, who is the only one who can change it. You may not change it; you are
  not stuck with it. Report it and keep working the parts of the task that do
  not depend on it.
- Shared surfaces (<e.g. src/routes.ts, shared types>): additive-only — add a
  new file and import/register it; never restructure the shared file.
- Do not add, remove, or upgrade dependencies. Needed dependency: report it.
- Do not run the project's deploy, publish, or release commands.
- NEVER run a REPO-GLOBAL git op: `git stash` (push/save/pop/apply/drop/clear),
  `git reset --hard` / `--merge` / `--keep`, `git checkout -- <path>` /
  `git checkout HEAD -- <dir>` / `git checkout .` / `git checkout -f`,
  `git switch -f`, `git restore <path>`, or `git clean -f/-d/-x`. Each of these
  reverts or deletes uncommitted work across the ENTIRE working tree — and in a
  swarm you are never alone in that tree, so it clobbers EVERY parallel agent's
  in-progress edits at once, not just yours (`refs/stash` is one ref shared by
  every worktree; a `stash`+`pop` observed live swapped two agents' trees mid-wave
  and nearly lost one's work — recoverable only from a dangling stash commit, and
  only until garbage collection reaps it. Treat that window as ZERO: that recovery
  is a lucky outcome someone noticed in time, never a mechanism you may rely on,
  and nothing in this loop guarantees the dangling commit survives). To
  compare your change against the committed baseline, use the READ-ONLY forms
  instead: `git show HEAD:<file>` for the baseline contents, or
  `git diff HEAD -- <file>` for your diff — neither touches the tree. If you truly
  need a clean tree, that is the orchestrator's job in fresh isolation: STOP and
  report it, do not stash. (A `PreToolUse` git-guard blocks these ops; if you hit
  a block, the guard is working — switch to the read-only form, do not route
  around it.) What the guard blocks is the ops that DISCARD uncommitted work —
  overwriting the tree from a commit or the index with no merge and no conflict
  markers, forcing it clean, or destroying refs — and it blocks them no matter
  how narrow the pathspec looks: `git restore <one file>` and
  `git checkout HEAD -- <one file>` are refused exactly like `git checkout .`.
  It is NOT "every verb that writes the working tree", and do not reason from
  that wider rule: `git checkout <branch>`, `git switch <branch>`, `git merge`,
  `git rebase`, `git cherry-pick` and `git revert` all rewrite the tree from a
  commit and are ALLOWED BY DESIGN — the loop needs them, and git itself aborts
  each one rather than clobbering a file you have modified. Reading from git is
  never blocked.
  **The guard's surface is WIDER than the list above, so do not read that list as
  the complete set of things that will be refused.** Also blocked, and blocked for
  the same reason: bare `git stash` and `git stash branch`; `git worktree remove --force`
  and `git worktree move` (discarding or relocating a tree another agent may be
  working in — plain `git worktree remove` without `--force` IS allowed, and is
  the orchestrator's sanctioned teardown, but it is not yours to run); a repo-wide `git rm`; `git apply -R`; `git read-tree -u`; and
  `git checkout-index -f`/`-a`. If a git command you did not expect comes back
  refused, the guard is working and the answer is a read-only form or a report —
  never a re-spelling that gets the same effect past it.
  So there is exactly one worker-side restore, and it is the redirect form:

      git show HEAD:<file> > <file>       # ALLOW (verified) — restores ONE path

  Three sanctioned git-vs-tree operations, and there are no others:
  `git show HEAD:<file>` reads the baseline, `git diff HEAD -- <file>` reads your
  diff, and `git show HEAD:<file> > <file>` restores that one file. That restore
  is a blind overwrite — no merge, no conflict markers — so it silently discards
  your uncommitted work on that path; use it only on a file YOU OWN, only inside
  your worktree, and only when you know what you are throwing away. It is never
  the repair for anything under `.swarm-loop/`: a frozen test, `goals.json` or
  manifest that looks damaged is the orchestrator's
  `swarmloop.py restore-harness` — an orchestrator-only command you never run —
  and a branch that writes under `.swarm-loop/` is vetoed by `check-branch`
  before anyone reads it. Report the damage.
- TESTS ARE APPEND-ONLY. Adding tests/assertions: free, encouraged. Modifying or
  deleting an EXISTING assertion requires the `justify-test-edit` ritual — and
  that includes every disguise: changing an expected value, widening a tolerance,
  relaxing a matcher, trimming a parametrize case, adding skip/xfail, or
  regenerating a snapshot you have not read. First answer, in your report: "would
  this test still fail if I reverted my change?" If no, YOUR CODE IS WRONG — fix
  the code, not the test. If you do edit, record the justification (the
  requirement it encodes + why it is wrong independent of your change) both as a
  comment at the assertion and in the commit body. The orchestrator reads the
  REMOVED lines of every test diff; an unjustified assertion change gets the
  branch rejected even when every metric is green.

## Context you need
<codebase-map excerpt relevant to this task: the modules you'll touch, the
conventions in force, the data model pieces involved, how to run the dev
build/tests locally in this stack>

<relevant entries from learnings.md, verbatim — e.g. "Learned cycle 2: the
config loader caches by absolute path; construct it once in tests">

<if this task was previously attempted and failed: what was tried, why it
failed, and what to do differently. Do not repeat the failed approach.>

## Working method
- **First, prove the environment is yours — before any real work, sixty seconds.**
  A worktree carries only TRACKED files, so every gitignored dependency or build
  directory starts missing, and a missing dependency does not always fail. If your
  worktree sits INSIDE the primary checkout, Node's resolver climbs UP and silently
  answers from the PRIMARY tree's `node_modules` and, under workspaces, from MAIN's
  source; `npx` finds the primary tree's bin. Your edits then look inert, and chasing
  that phantom will burn your entire context (observed). Do not reason from where
  you think your worktree is — the run picks the location and it may be beside the
  repo or inside it. Two checks, which hold either way:
  1. Print what the runtime actually loads — `node -p "require.resolve('<pkg>')"`,
     `python -c "import <pkg>; print(<pkg>.__file__)"`, `command -v <cli>`. Every
     path must be INSIDE this worktree; one that is not means you are measuring
     main's code, not yours.
  2. **Flip a value in a file you own** (a return value, a constant), re-run your
     verify, and **confirm the result moves.** Restore the flip.
  If it does not move, your commands are resolving outside your worktree — report it
  as a blocked environment; do not work around it. Nothing you measure before this
  check passes is about your code. If a dependency or build artifact is simply
  missing, that is also a report, not a repair: do not `npm install`, do not
  regenerate a lockfile, do not reach into the primary tree.
- **Write every command relative to the worktree root.** Never an absolute path to
  the primary tree's `.venv/bin/python` or `node_modules`, and never with an
  inherited `VIRTUAL_ENV`/`NODE_PATH`/`PYTHONPATH` pointing there — those are the two
  ways a command leaves your worktree without erroring, and hard-coding one makes the
  failure permanent instead of visible to the check above.
- Never commit a provisioned directory (`node_modules/`, `.venv/`, `dist/`) or a
  lockfile — `git add -A` after provisioning is exactly how those land on a branch.
- If the primary checkout has a `.codegraph/` index, use
  `codegraph explore "<symbols or question>"` BEFORE grep/find or bulk
  file-reading to understand the codebase — one query returns verbatim source,
  call paths, and blast radius, including dynamic-dispatch hops grep misses.
  Caveat: the index reflects the indexed primary tree (~1s lag behind its
  writes), not your worktree's uncommitted edits — use it to understand the
  base; read your own files directly when reviewing your own diff.
- Re-run your named acceptance tests and the project's fast checks
  (<lint/typecheck/unit command>) before finishing; do not report done with
  them failing unless you explain precisely why.
- Commit in coherent increments with clear messages. Uncommitted work is lost.

## Report format (return exactly this structure)
STATUS: complete | partial | blocked
ACCEPTANCE: <which named tests now pass / still fail>
CHANGED: <file list>
DECISIONS: <choices another agent or the orchestrator must know about —
  interfaces you defined, assumptions you made>
NEEDS: <outside-ownership changes or dependencies you need, if any. Also the
  place to say it if a frozen test you have objected to in CONCERNS is what is
  actually blocking you — name it here so the orchestrator knows the objection
  has a ticket riding on it.>
CONFIG-TOUCHED: <every build- or test-config file this ticket expects to change,
  and WHY — pyproject.toml, pytest.ini, setup.cfg, tox.ini, conftest.py,
  package.json, tsconfig.json, a Makefile/justfile, a jest/vitest/eslint/coverage
  config. Name them here even when the change is obviously innocent, and name
  them for the plan as well as the diff: `check-branch` tags exactly these files
  with an advisory `?` because they are the one class of edit that can move a
  frozen metric without touching a frozen byte, and it computes that tag AFTER
  your report exists. A `?` your report already explains is a five-second
  confirm; a `?` with nothing beside it stalls the merge on a question only you
  could have answered, and by then you are gone. Write "none" if none.>
BUGS-OBSERVED: <every defect you noticed ANYWHERE while working — in your
  files, in files you only read, in behavior you observed — whether or not it
  relates to this task. Prefix each item [open], or [fixed] if you repaired it
  in this branch (in-ownership drive-by fixes only, committed separately).
  Per item: file:line (or a reproduction), the evidence you saw, suspected
  cause if you have one. Write "none" if none.>
SHARED-DOC-OBSERVATIONS: <anything in an orchestrator-owned SHARED document
  that looks wrong or stale — decisions.md, public-surface.md, backlog.md,
  learnings.md, the codebase map. These go here and NOT in BUGS-OBSERVED,
  because your copy is a snapshot at your branch point and the orchestrator may
  already have fixed it on `main`. Quote the line and say what you expected.
  Write "none" if none.>
CONCERNS: <anything smelling wrong, including tests you believe are incorrect.
  For a FROZEN acceptance test you believe is wrong, make the objection
  actionable or it dies in this report instead of reaching the human who can
  actually change it. Give four things: (1) the test file and the exact
  assertion, quoted; (2) what it demands; (3) why that is impossible or wrong
  INDEPENDENT of your implementation — it contradicts the stated behavior, it
  contradicts another acceptance test, it needs a fixture that cannot exist;
  (4) what it should assert instead. "This test is hard to pass" is not an
  objection — that is the job. "This test is wrong, and here is the evidence"
  is one, and the orchestrator escalates it to the user.>
```

## Orchestrator-side notes

- **Waves are emergent batches, not planned rounds.** The readiness scheduler decides membership: a ticket dispatches the moment its `depends_on` are closed, its blast radius overlaps no in-flight writer, and merge-review bandwidth allows. When several tickets become ready in the same turn, spawn them with parallel Task tool calls in that turn so they actually run concurrently — but never hold a ready ticket back to round out a batch, and never wait for a whole batch to land before dispatching the next ready ticket.
- **Record every non-ticket agent with `swarmloop.py dispatch` — it is the only way `resume` and `check-wave` can see it.** `resume` reconstructs in-flight work from `git worktree list` + `git branch`, so a ticket worker in its own worktree is recoverable and **every agent doing orchestrator-owned work without a worktree is invisible**: resume reports "nothing in flight" for that whole class, and a resumed orchestrator re-dispatches it, putting two agents on the same file. Reproduced live — a compacted orchestrator found a running subagent it could not identify and had to interrogate it. So, in the same turn you spawn a recon agent, a doc regenerator, a wave verifier or an auditor:

  ```bash
  swarmloop.py dispatch --agent <how you refer to it> --kind recon|regen|verifier|audit|ticket|other \
                        --subject "<ticket id, document, or task>" \
                        --files "path/a.md,path/b.py"        # what it owns — comma-separated
  swarmloop.py dispatch --list                                # every dispatch never closed, oldest first
  swarmloop.py dispatch --close <agent ref> [--note "..."]    # when you collect it
  ```

  The file is append-only (`.swarm-loop/dispatch.jsonl` — verified against `DISPATCH_FILE` in the shipped script); nothing else in the run is changed by writing to it. `--agent`, `--kind` and `--subject` are all required unless you passed `--list` or `--close`. **`--files` is the load-bearing one:** `check-wave` intersects a candidate wave's ownership not only against in-flight *worktrees* but against these records, which is the only protection a ticket worker has against colliding with an orchestrator-owned agent. A dispatch recorded without `--files` still shows up in `--list` but contributes nothing to the wave check. Closing an agent-ref that is not open is recorded anyway with a warning rather than refused. `resume` reads the same open records, so a recorded agent survives a compaction. Run `dispatch --list` at every `resume` and at the top of every wave; a stale open record is a five-second close, and a missing one is two agents on one file.
- **Model/effort scaling.** Straightforward, well-bounded tasks can go to a faster model if the environment supports per-subagent model selection; keep the default model for anything with design judgment in it. Matching effort to task difficulty is the main cost lever in a swarm.
- **The report is a claim.** Phase 5 checks it against the diff and Phase 7 checks it against the metrics. Treat `DECISIONS`, `NEEDS`, `CONFIG-TOUCHED`, and `BUGS-OBSERVED` as the valuable part — they feed the codebase map, the ticket backlog, and the scheduler's next dispatch decisions.
- **A worker's frozen-test objection is yours to carry, not to file away.** The worker's wall has exactly one door in it and you are it. When a report's CONCERNS names a frozen acceptance test as *wrong* with the four-part evidence — not merely hard — you neither edit it nor bury it: `swarmloop.py escalate --kind frozen-test --subject <test path> --reason "<the worker's evidence>" --proposed "<the change being asked for>" --cycle <N>`, then keep dispatching everything the disputed test does not judge. Judge the evidence before forwarding it: "this test is hard" is the job, and passing that up as an escalation trains the one human in the loop to stop reading escalations. Until the user amends it, the test stands — the retry packet carries it as an unchanged constraint, and the worker's branch still may not touch it. Reply to the worker's objection in the retry packet either way, so the same objection is not re-litigated from scratch by the next agent.
- **Every SHARED-DOC-OBSERVATION gets a `main`-side check before it becomes an action.** A long-running worker's copy of every orchestrator-owned document is pinned at its branch point while you keep correcting those documents mid-wave, so its findings are *time-shifted*, not wrong. Observed: both `T-010` and `T-011` reported that `public-surface.md` still said "the five trust dimensions" **after** an amendment had made it six and `main` already read six. Cheap per item, but it is a per-finding tax and a false alarm is one careless step from a re-fix of something already fixed — or from telling the next wave something false. Read the line on `main` first; then either answer the worker in its retry packet or fix `main` once. The separate report field exists so the whole class triages in one pass instead of one finding at a time.
- **Inject learnings selectively.** Only entries relevant to the task's area; a packet stuffed with every lesson ever learned buries the two that matter.
- **State the CURRENT baseline, never a remembered one.** Every packet carries the suite's pass count as measured at dispatch time, plus the line "other agents are in flight; prove any failure is yours by reverting YOUR file and re-running." A remembered number goes stale within minutes of other branches landing, and the worker then burns effort explaining a discrepancy that is just drift — the revert-and-rerun check is the one that matters anyway.
- **Consolidation/dedup packets are non-additive — state done as "the old pattern no longer exists."** Write the done-condition as e.g. "grep shows zero inline occurrences in these files" or "every new accessor has ≥1 caller outside its module", never "the new abstraction exists". Additive-only does NOT apply to these tasks and the packet must say so explicitly — the payoff of a consolidation IS editing the call sites, and a worker given the additive-only default once shipped six accessors with zero callers, leaving more duplication than before with every metric green. At collection, grep for callers of anything the refactor added; an addition with zero callers means the wiring half was skipped.
- **Hung agents.** If a subagent has produced nothing for an unreasonable time relative to its task size, mark the ticket failed-hung, let the rest of the batch integrate without it, and re-mint it with a smaller decomposition. A salvage or second attempt dispatches like any other ticket but earns deep verification (see the depth table below). Never merge a branch from an agent that didn't return a report.

## Wave-verifier packet variant (rung 2 of the verification ladder)

One adversarial verifier per integration batch — the default unit of verification.
Dispatch it the moment the batch lands on `main`, concurrent with the next batch's
build, so verification costs no wall-clock. Stripped packet: read-only — no branch,
no worktree, no code changes, propose-don't-fix. The verifier MUST NOT have built
anything in the batch it judges (a builder confirming its own work finds nothing;
nearly every significant defect on record was found by an agent that did NOT write
the code), and every batch gets a FRESH verifier — never a carried-over context
that has already concluded the batch is fine.

**The orchestrator pastes in:** the batch's ticket packets (once packets are split,
the preamble path plus each ticket's per-ticket half); the doers' completion reports,
labeled explicitly as CLAIMS TO TEST, not facts; the integrated diff — **`--stat`
first, always**; current metric values; the learnings relevant to the batch's area.

```bash
git diff --stat <pre-batch-main>..main      # size it BEFORE pasting anything
git diff <pre-batch-main>..main -- <path>   # then per-file, for what the stat says matters
```

A five-ticket batch diff is routinely 10k+ lines. Pasting one whole is the largest
single context sink in the loop, and it truncates — and a truncated tail is exactly
where an unjustified assertion deletion hides. **Cap it:** paste the full text only
under ~1,500 lines; past that, paste the `--stat` plus per-file diffs for what it
flags (every test file, every shared surface, everything outside the batch's
ownership map), and hand the verifier the range so it can pull the rest itself.
Keep the **two-dot** form — both endpoints sit on `main`'s first-parent line, which
is what makes `..` the correct operator here; it has been verified twice, do not
"fix" it to three-dot.

**Duties:**

- Exercise each ticket's real path at least once through a real entry point — a
  route, a CLI invocation, the surface a user actually hits, not the ticket's own
  tests — and state in the report what each green is evidence OF (the
  stub-blindness question). Green proves the code behaves as the test calls it,
  not that the wired product works; a suite can be green while the product is
  still fully broken.
- Hunt cross-branch interactions: shared registries, schemas, route tables, global
  assumptions. These collisions exist only after integration — no per-branch check
  (rung 1) can see them, which is exactly why this packet exists.
- Check resolution provenance — did this batch's greens execute the branch's own
  source? For at least one claim per ticket, print the path the runtime actually
  loaded (`require.resolve(…)`, `module.__file__`, `command -v` for a CLI) and
  confirm it sits inside the tree under test. A green resolved out of the primary
  checkout's `node_modules` or `.venv` is evidence about the wrong code, and rung 1
  is structurally blind to it: `check-branch` inspects a diff, not a resolution path.
- Find callers of everything the batch added — CodeGraph when a `.codegraph/`
  index exists, grep otherwise. An addition with zero production callers outside
  itself and its tests means the wiring half was skipped at least as often as it
  means dead code.
- Audit every doer claim against the diff: a CHANGED list that omits a changed
  file, an ACCEPTANCE claim the suite contradicts, a DECISIONS entry the code does
  not reflect — each one is a finding.
- At deep depth, sabotage — in a SCRATCH COPY, never in the tree other agents
  share. Introduce the plausible wrong fix a reasonable person would actually
  commit ("check the limit AFTER doing the work"), not mere deletion; purge caches
  between variants; and prove each sabotage actually reached the code path before
  believing any result — a sabotage that changed nothing produces the same green
  as a working guard.

**Depth scales with batch contents, not anxiety:**

| Batch contains | Depth |
|---|---|
| Docs, comments, pure-additive tests | Rung-1 checks only, plus a spot-check |
| Ordinary feature code | Standard: real-path exercise + claims audit + caller check |
| Shared interfaces, data models, migrations, auth/security, frozen-metric blast radius, concurrency | Deep: standard + sabotage + adversarial fixtures |
| Dead-agent salvage, or a second attempt after a rejection | Deep, plus rerun the verification the first attempt claimed to have done |

**Report format (return exactly this structure):**

```
FINDINGS: numbered — each with a reproduction; a finding without a repro is an
  opinion, and an opinion mints nothing
COVERAGE: what was examined, at what depth, and what was NOT reached — named gaps
  become tickets (or trigger per-ticket deep verification, rung 3)
PER-TICKET: verdict per ticket WITH the evidence that earned it; "looks good" is
  not evidence
```

A verdict without evidence is rejected and the verification re-dispatched.
Findings mint tickets into the one backlog: a finding that invalidates a merged
branch mints a fix ticket at the front of the frontier — or a revert per the merge
protocol when the defect is fundamental.

## Fix-ticket packet variant

Fix tickets — minted from BUGS-OBSERVED reports, wave-verifier findings,
audit-loop findings, or a red metric — use the full build template plus these
deltas:

- **Reproduce FIRST.** The packet carries a failing reproduction, or its opening
  demand is to produce one, before any code change. "Could not reproduce" is a
  legitimate report outcome (STATUS: blocked, with what was tried and where the
  original evidence pointed) — never a license to patch blind.
- **Fix the root cause, not the symptom — and say in DECISIONS which you fixed and
  how you know.** A patch that silences the repro while the cause survives re-mints
  the same ticket next audit loop, minus the trust.
- **Sweep the blast radius for SIBLING instances of the same defect class**
  (CodeGraph when indexed, grep otherwise) and list every one in the report.
  Fixing one of five instances is a report line, not a completion.
- **The reproduction becomes a permanent test, committed with the fix.** Tests are
  append-only, so adding it is always free — and a fix without its repro test is
  free to regress silently.
- **Documentation is never the repair.** Updating a comment or a README —
  including deleting a caveat — closes nothing; the defect has to stop happening.

## Audit packet variant (audit-fix loops)

For the whole-codebase audit-fix loops that follow build convergence, use a
stripped packet: no worktree, no branch, read-only; propose, don't fix. Auditors
run as PARALLEL PARTITIONS whose union covers the ENTIRE codebase every loop — no
sampling, no "just the risky parts" — with partitions rotated and overlapped
across loops so the seam between two auditors' scopes never falls in the same
place twice.

The framing is adversarial: the job is to find what is wrong, not to confirm the
work. Ask of every green thing what the green is evidence OF. Hunt false claims,
stubs wearing green suites, mechanisms with zero production callers, and the drift
no frozen metric can see — duplication, architecture erosion, dead code,
convention violations, hotspot files accumulating unrelated responsibilities, test
smells outside the frozen suite. Audit against the codebase map AND the spec: what
was promised, what the doers claimed, what the code actually does.

Report format — the same FINDINGS/COVERAGE contract as the wave verifier:

```
FINDINGS: numbered — each with location, what's wrong, why it matters, and a
  reproduction; a finding without a repro is an opinion
COVERAGE: the auditor's coverage map — what was examined, at what depth, and what
  was NOT reached; named gaps become mandatory targets (ideally tickets), never
  silent holes
METRIC-LINKED: findings that plausibly explain a regressing/stalled metric, with
  the metric named
HAZARDS: structural facts the codebase map should record
```

The orchestrator converts findings into TICKETS — each with a blast radius and
`depends_on` — and the readiness scheduler resolves them exactly like build work;
metric-linked findings go to the front of the frontier. A loop whose auditors mint
zero tickets ends the run early via the prune phase: full coverage plus nothing
found is the one honest form of done.
