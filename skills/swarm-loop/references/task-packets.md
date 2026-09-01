# Task Packets — the subagent prompt contract

A subagent's entire world is its prompt. It has no access to the conversation, the plan, the map, or other agents — only the packet and the worktree. Anthropic's production lesson on delegation holds here: a worker needs **an objective, an output format, guidance, and boundaries**, and missing any of the four produces drift, not disobedience. The packet template below encodes all four; fill every section, delete none.

Underspecification is the dominant failure mode. When a packet is vague, the subagent doesn't stop — it guesses, and its guess conflicts with another agent's guess at merge time. When in doubt, include the extra paragraph of context; packet tokens are cheap compared to a rejected branch.

## Template

```
You are implementing one atomic task inside an orchestrated build loop. Work ONLY
inside your worktree; commit your work; report in the format specified. The
orchestrator merges branches and runs all measurement — you do neither.

## Task
- ID: <task-id>   (epoch <N>)
- Objective: <one paragraph: what exists when you are done that does not exist now>
- Done means: the following acceptance tests pass when run from the repo root:
    <exact command(s) to run them, e.g. pytest .swarm-loop/acceptance/test_auth.py -k signup>
  You may RUN these tests as often as you like. You may not EDIT them — they are
  frozen goals. If you believe a test is wrong, say so in your report with your
  reasoning; do not work around it.

## Where you work
- Worktree (absolute path): <path>   — do all work here
- Branch: task/<task-id>             — already created and checked out

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
- Shared surfaces (<e.g. src/routes.ts, shared types>): additive-only — add a
  new file and import/register it; never restructure the shared file.
- Do not add, remove, or upgrade dependencies. Needed dependency: report it.
- Do not run the project's deploy, publish, or release commands.
- NEVER run a REPO-GLOBAL git op: `git stash` (push/save/pop/apply/drop/clear),
  `git reset --hard` / `--merge` / `--keep`, `git checkout -- <path>` /
  `git checkout .` / `git checkout -f`, `git switch -f`,
  `git restore <path>`, or `git clean -f/-d/-x`. Each of these reverts or deletes
  uncommitted work across the ENTIRE working tree — and in a swarm you are never
  alone in that tree, so it clobbers EVERY parallel agent's in-progress edits at
  once, not just yours (`refs/stash` is one ref shared by every worktree; a
  `stash`+`pop` observed live swapped two agents' trees mid-wave and nearly lost
  one's work, recoverable only from a dangling stash commit before `git gc`). To
  compare your change against the committed baseline, use the READ-ONLY forms
  instead: `git show HEAD:<file>` for the baseline contents, or
  `git diff HEAD -- <file>` for your diff — neither touches the tree. If you truly
  need a clean tree, that is the orchestrator's job in fresh isolation: STOP and
  report it, do not stash. (A `PreToolUse` git-guard blocks these ops; if you hit
  a block, the guard is working — switch to the read-only form, do not route
  around it.)
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
NEEDS: <outside-ownership changes or dependencies you need, if any>
BUGS-OBSERVED: <every defect you noticed ANYWHERE while working — in your
  files, in files you only read, in behavior you observed — whether or not it
  relates to this task. Prefix each item [open], or [fixed] if you repaired it
  in this branch (in-ownership drive-by fixes only, committed separately).
  Per item: file:line (or a reproduction), the evidence you saw, suspected
  cause if you have one. Write "none" if none.>
CONCERNS: <anything smelling wrong, including tests you believe are incorrect>
```

## Orchestrator-side notes

- **Waves are emergent batches, not planned rounds.** The readiness scheduler decides membership: a ticket dispatches the moment its `depends_on` are closed, its blast radius overlaps no in-flight writer, and merge-review bandwidth allows. When several tickets become ready in the same turn, spawn them with parallel Task tool calls in that turn so they actually run concurrently — but never hold a ready ticket back to round out a batch, and never wait for a whole batch to land before dispatching the next ready ticket.
- **Model/effort scaling.** Straightforward, well-bounded tasks can go to a faster model if the environment supports per-subagent model selection; keep the default model for anything with design judgment in it. Matching effort to task difficulty is the main cost lever in a swarm.
- **The report is a claim.** Phase 5 checks it against the diff and Phase 7 checks it against the metrics. Treat `DECISIONS`, `NEEDS`, and `BUGS-OBSERVED` as the valuable part — they feed the codebase map, the ticket backlog, and the scheduler's next dispatch decisions.
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

**The orchestrator pastes in:** the batch's ticket packets; the doers' completion
reports, labeled explicitly as CLAIMS TO TEST, not facts; the integrated diff range
(`git diff <pre-batch-main>..main`); current metric values; the learnings relevant
to the batch's area.

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
