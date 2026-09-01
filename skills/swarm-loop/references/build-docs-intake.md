# Build-docs intake — consuming a planned doc set

When a run starts from a **build-docs** doc set (produced by the desktop `build-docs`
skill: `SPEC.md` → `DESIGN.md` → a task graph in `TASKS.md` or a task store, often with
an `EXECUTION.md` header), the planning half of this loop's work is already done — and
done by a skill whose sizing rules (one ticket = one verifiable outcome = one
file-cluster; 4–6 children per parent; no keystroke-level steps) are the same rules
Phase 2/3 would apply. The loop's job shifts from *decompose and define* to
**verify, derive, and execute**. Do not re-plan what the docs already pin. Do not
trust them blindly either — the intake procedure below is the trust boundary.

## Detection and precedence

- **Detect:** the user hands over the docs, or `SPEC.md`/`DESIGN.md`/`TASKS.md`/
  `EXECUTION.md` sit in the project. Any subset counts; a missing tier is produced
  by the normal phase (say which tier you generated and that it wasn't reviewed by
  the planning skill).
- **Precedence, highest first:** the user's words in this session → this skill's
  orchestration protocol (waves, worktrees, harness, merge, freeze) → `EXECUTION.md`
  where compatible → the doc set for *what* to build. The docs own content; the loop
  owns mechanics.
- **EXECUTION.md reconciliation.** Most of its rules restate this loop's own
  (verification closes a ticket, not model judgment; parallel only when safe; revert
  over iterating on a failure; re-plan the un-started tail). Two mappings where the
  wording differs:
  - *"Pull ONE ready ticket at a time"* becomes **the readiness scheduler** — a
    ticket dispatches the moment its `depends_on` are closed, its blast radius
    overlaps no in-flight writer, and merge-review bandwidth allows; waves are the
    emergent batches this produces, not planned rounds. `parallel_safe` plus
    disjoint blast radius is exactly the scheduler's readiness test; a ticket that
    fails it waits, which is the serialization EXECUTION.md is asking for.
  - *"Escalate to the strongest model / human gate"* maps to the **orchestrator**
    (and to the user, per the run's autonomy mode). Workers never judge their own
    done-ness here either.

## The tier → loop mapping

| Build-docs artifact | Swarm-loop consumer | Mutability in this loop |
|---|---|---|
| SPEC success criteria + R-IDs | `goals.json` metrics + acceptance tests (Phase 2) | **Frozen** at freeze |
| SPEC non-goals | Packet non-goals / boundaries | Honored run-long |
| SPEC "Open questions" | **Phase 0 gate**: non-empty → stop and ask before `init` | — |
| DESIGN interfaces + data models | Codebase-map contracts section; packet context excerpts | Orchestrator-owned; a change is a recorded design decision, never a worker's call |
| DESIGN verification strategy | Metric/wrapper design | Frozen once encoded in `acceptance/` |
| Task-graph tickets | `backlog.md` entries, **T-IDs preserved** | Mutable — reprioritized per analysis like any backlog |
| Ticket `verify` command | Task done-condition; per-feature metric candidate | Frozen only if wrapped into `acceptance/` |
| Ticket `scope` globs | File-ownership map seed | Validated by the orchestrator every cycle |
| `depends_on` + `parallel_safe` | Readiness-scheduler inputs (Phase 3) | Re-validated at every dispatch |
| T-0 bootstrap ticket | The cycle-1 scaffold task (greenfield Phase 1) | — |
| EXECUTION.md | Orchestration cross-check | This skill's protocol governs |

## Intake procedure (runs inside Phases 0–2)

1. **Read every tier.** If SPEC's *Open questions* section is non-empty, stop and put
   the questions to the user before `init` — the planning skill left them open on
   purpose, and answering them yourself is silent goal-setting. The same init
   exchange asks for the AUDIT-LOOP COUNT — N whole-codebase audit-fix loops after
   the build converges, passed as `swarmloop.py init --audit-loops N` — instead of
   a cycle budget; set `--max-cycles` generously, as a runaway safety ceiling, not
   a plan.
2. **Run the adversarial pass anyway — against the codebase, not just the prose.**
   The planning skill reviewed the spec against itself; you review it against
   reality: a delta-spec's "current behavior" claims versus what the code actually
   does; scope globs versus the real tree (a glob that matches nothing, or two
   "disjoint" tickets whose globs collide through a barrel file or shared config);
   verify commands versus the harness rules below. Blast radius goes beyond globs:
   flag every ticket pair that touches the same registry, schema, or route table
   even when their globs are disjoint — those collisions exist only after
   integration — and record each pair as a scheduling constraint (never in flight
   together). The docs are a claim. Findings go back to the user with the
   cycle-1 plan, not silently patched.
3. **Derive `goals.json` — do not invent.** Global metrics as usual (acceptance pass
   rate, build, lint/type). Machine-checkable SPEC success criteria become metrics;
   per-feature metrics come from ticket acceptance counts (target = criteria total).
   Keep traceability: metric ids/names carry the R-ID or T-ID they encode. A success
   criterion that is *not* machine-checkable becomes a tracked backlog note — it is
   not regressed, and the final report must say whether it was met.
4. **Write `acceptance/` from the tickets, before implementation.** Ticket acceptance
   criteria and the DESIGN verification strategy are the source. Ticket `verify`
   commands may be wrapped as metric commands, but they must satisfy
   `goal-setting.md`'s rules — bare number on the last stdout line, `/bin/sh`-portable,
   deterministic. `freeze` smoke-runs every metric command; a ticket verify that
   can't pass the smoke gets a wrapper, fixed *before* the freeze.
5. **Seed `backlog.md` from the task graph verbatim** — T-IDs, dependency edges,
   scopes, non-goals all preserved. T-0 becomes the scaffold task and stays the sole
   root. Do not re-decompose tickets that satisfy the atomicity check; split only a
   ticket that genuinely fails it, and record the split (T-014 → T-014a/b) in the
   backlog and the cycle report.
6. **Derive the INITIAL frontier — not a wave plan.** `depends_on` plus the
   scheduling constraints from step 2 give the readiness scheduler its starting
   set; later "rounds" are never planned, because waves are emergent batches.
   Worked example: six tickets where 2 waits on 1, 4 waits on 3, and 6 waits on 5
   yield frontier one = {1, 3, 5}, with {2, 4, 6} following as blockers clear —
   but ROLLING: if ticket 1 closes early, ticket 2 dispatches immediately, without
   waiting for 3 and 5. Hand-planning "{1,3,5} then {2,4,6}" as fixed rounds
   forfeits exactly that head start.
7. **The cycle-1 checkpoint presents the DOCS' plan**, not a re-derivation: the
   ticket graph as the feature list, the derived initial frontier, the derived
   metrics with targets, and — flagged explicitly — every divergence intake
   introduced (splits, wrapper substitutions, adversarial-pass findings). Then
   freeze and baseline as normal.

## Per-cycle rules under intake

- **Phase 3:** waves are emergent, not computed as rounds — the readiness scheduler
  dispatches each ticket the moment its `depends_on` close, its blast radius clears,
  and merge-review bandwidth allows; a "wave" is whatever happens to be in flight
  together. Never hand-shard the graph, and never take `parallel_safe` on faith: it
  was set at planning time against a repo that has since changed — validate it
  against the real tree at dispatch time.
- **Packets:** ticket fields map field-for-field into the packet template
  (`objective`→Objective, `acceptance`+`verify`→Done means, `scope`→Files you own,
  `non_goals`→Boundaries). The packet then adds what the ticket cannot know: the
  worktree, the git prohibitions, the discretion tiers, `BUGS-OBSERVED`, the current
  baseline. Include the ticket's R-IDs so reports trace to requirements.
- **Re-planning:** discovered reality that invalidates un-started tickets → rewrite
  the un-started tail of the backlog (EXECUTION.md and this loop agree on this). It
  never licenses touching frozen goals; reality contradicting a *frozen* success
  criterion is the "goal genuinely wrong" terminal case — stop and ask.
- **Reporting:** cycle reports and the final report reference T-IDs and R-IDs
  throughout, and the final report carries a **plan-divergence section** — tickets
  split, merged, dropped, or added, and any success criterion unmet or reinterpreted
  — as feedstock for the user's next build-docs session. The loop does not edit the
  doc set itself; planning docs belong to the planning skill.
