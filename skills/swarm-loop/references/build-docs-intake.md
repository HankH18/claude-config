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
    emergent batches this produces, not planned rounds. **Disjoint per-file
    ownership** is the scheduler's readiness test; a ticket that fails it waits,
    which is the serialization EXECUTION.md is asking for. `parallel_safe` is
    **advisory only** and is not that predicate — see the ruling below.
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
| Ticket `scope` globs | File-ownership map seed — **write-ownership only** | Validated by the orchestrator every cycle |
| Ticket `owns` / `owned_files` (per-file map) | **Preferred** dispatch predicate — `check-wave` and `reads` read it *instead of* `scope` when present | Orchestrator-maintained; re-validated at every dispatch |
| Ticket `reads` (derived at intake, gate B below) | Cross-ticket read edges; outbound-obligation text in packets | Derived, re-derived at every amendment |
| `depends_on` | Readiness-scheduler input (Phase 3) — the real edge set | Re-validated at every dispatch |
| `parallel_safe` | **Advisory hint only** — never the dispatch predicate | Recorded, never trusted (see below) |
| Per-file ownership map (derived at intake) | **The** dispatch predicate: no two in-flight tickets own a file | Re-validated at every dispatch |
| T-0 bootstrap ticket | The cycle-1 scaffold task (greenfield Phase 1) | — |
| EXECUTION.md | Orchestration cross-check | This skill's protocol governs |

### The ticket-field consumer ledger

The table above maps *tiers*. This one maps *fields*, and it is the authoritative
per-field declaration: for every key in the build-docs ticket contract, WHO consumes
it and — for anything a code path reads — what happens when it is absent.

It exists because the failure it prevents does not fail at the boundary. `build-docs`
can start emitting a field and `swarmloop.py` can never read it, and nothing says so
until `freeze` refuses or `red-check` scores a gate wrong, hours into a run. **A field
with no row here is not "obviously prose" — it is undeclared, and undeclared is the
bug.** Silence has to be a decision someone wrote down.

`tests/test_contract.py` reads this table mechanically, against
`skills/build-docs/references/templates.md` on one side and `scripts/swarmloop.py` on
the other. Editing a row without the code or the contract to back it turns the row red.

Consumer classes:

- **`code`** — `scripts/swarmloop.py` reads it off a ticket. The *On absence* cell must
  say what breaks.
- **`advisory-recorded`** — read by code, printed, and deliberately **never** a
  predicate.
- **`packet-prose`** — no code path, by design. It is mapped into the worker's task
  packet as prose and a human/agent consumes it. This is a real consumer; it is just
  not a machine one.
- **`loop-minted`** — not a build-docs field at all. `swarmloop.py` writes it onto the
  tickets *it* mints from audit findings and reads it back. Declared so that "code
  reads a field the producer never emits" stays a usable alarm.

<!-- CONTRACT-LEDGER:BEGIN -->

| Field | Consumer | On absence |
|---|---|---|
| `id` | `code` | `_graph_index` SKIPS the entry entirely and `_graph_unusable` reports the count — a ticket the frontier never lists is indistinguishable from one that does not exist. |
| `title` | `code` | `frontier` and the freeze coverage report print an empty label. Nothing refuses; the graph is just harder to read. |
| `objective` | `packet-prose` | Packet's **Objective** section is empty and the worker has the acceptance criteria with no statement of why. No code path. |
| `refs` | `packet-prose` | Packet carries no R-IDs, so cycle and final reports cannot trace a ticket back to a spec requirement. No code path. |
| `acceptance` | `packet-prose` | Packet's **Done means** loses its numbered criteria, and intake step 4 has no source for `acceptance/`. Read by hand at intake, never by code. |
| `verify` | `code` | `red-check` prints `NO GATE — the ticket declares no 'verify' command` and exits non-zero; `frontier` shows the ticket with no done-condition. |
| `scope` | `code` | Fallback write-ownership. With `owns` also empty the ticket is treated as owning **everything**, so `check-wave` VETOes the wave (or REFUSES it when no member declares anything). |
| `owns` | `code` | Silent fallback to `scope` globs — `check-wave` prints `owns(scope)` and the wave check you just ran was the coarse one, in both directions. |
| `owned_files` | `code` | Alias for `owns`, read second. Absence is normal; it only matters when a project spells the per-file map with this key. |
| `reads` | `code` | `swarmloop.py reads --write` derives and fills it; until then the packet carries no "import this, never write it" edge and `freeze`'s closure gate is the first thing to notice. |
| `provides` | `packet-prose` | Packet carries no *outbound* obligation, so the ticket ships a port that satisfies only its own tests. Consumed by the **Packets** rule below, by hand. No code path, by design. |
| `conforms_to` | `packet-prose` | Intake gate **D** never fires for this ticket: no frozen conformance test is written, and a worker is free to build a parallel surface with the arguments reversed. Consumed by a person at intake, into `acceptance/`. No code path, by design. |
| `depends_on` | `code` | The ticket becomes a graph ROOT and the readiness scheduler dispatches it immediately, before the work it actually needs exists. |
| `non_goals` | `packet-prose` | Packet's **Boundaries** section is empty and the worker's scope creep has nothing to hit. No code path. |
| `parallel_safe` | `advisory-recorded` | Nothing. `check-wave` prints an advisory line when it is `false` and dispatches on per-file ownership regardless — the flag is unusable as a predicate in both directions. |
| `bootstrap` | `code` | **OPEN DEFECT — this row is red on purpose.** templates.md documents `"bootstrap": true` as exempting T-0 from the must-go-red check, and `cmd_red_check` has never heard of it: T-0's verify legitimately passes on the scaffold's own smoke test, so red-check scores the graph's sole root VACUOUS and refuses the whole run. Routed as a separate fix to `red-check`. Do NOT resolve this by demoting the row — the producer's promise is the thing that has to become true. |
| `finding_id` | `loop-minted` | Written by `swarmloop.py findings`; its absence is how the mint loop tells a build-docs ticket from one of its own, so re-minting would duplicate. |
| `severity` | `loop-minted` | Written by `swarmloop.py findings` from the audit record; the mint report cannot rank or threshold without it. |
| `location` | `loop-minted` | Written verbatim from the finding; without it the minted ticket names no file and `_finding_scope` derives no ownership. |
| `reproduction` | `loop-minted` | Written from the finding when present; the field the old formatter never read. Absent means the ticket carries no repro. |
| `scope_note` | `loop-minted` | Written only when a finding named no file — the marker that ownership must be declared by hand before the ticket joins a wave. |

<!-- CONTRACT-LEDGER:END -->

## Intake procedure (runs inside Phases 0–3)

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

   **The `parallel_safe` ruling, made once, here.** Pick ONE dispatch predicate and
   delete the other two, because three documents with three answers and no code is
   how a wave gets dispatched on the wrong one. The predicate is **per-file
   ownership**; `depends_on` supplies the edges; `parallel_safe` is an advisory
   hint that was set at planning time against a repo that has since changed, and it
   is unusable as a predicate in both directions — it marks safe pairs unsafe and
   unsafe pairs safe. Record it, never dispatch on it, and say so in the intake
   report so the next reader does not re-litigate it against `EXECUTION.md`.

   **Then run the four gates in *Four intake gates the doc set cannot supply* below**
   — executable-snippet verification, the `reads` closure check, shared-primitive
   detection, and the conform-or-replace ruling. They are part of this step; they are
   written out separately only because each carries its own evidence.

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
6. **Derive the INITIAL frontier — not a wave plan** (this step is the Phase-3 half
   of intake; steps 1–5 and 7 are Phases 0–2). `depends_on` plus the
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

## Four intake gates the doc set cannot supply (run inside step 2)

The planning skill cannot see these — each is a property of the ticket set as a whole,
or of the real dependency, and each was found the expensive way in a live run. Run all
four before the freeze.

### A. `[verified]` must mean EXECUTED

**Execute every executable snippet in the decision record before the freeze.** A pinned decision that carries a runnable artifact (SQL, Cypher,
a shell command, an API call, a config fragment) is copied *verbatim* into every
packet that quotes it, so a wrong one is wrong N times. Reproduced: a decision
carrying a `[verified]` header shipped a `CREATE VECTOR INDEX` statement that does
not parse — Cypher map keys must be identifiers or backtick-quoted and the
decision single-quoted them (`Neo.ClientError.Statement.SyntaxError: Invalid input
''vector.dimensions'': expected an identifier or '}'`, column 109, neo4j 5.26.30
Community). **Six tickets copied it verbatim.** It survived the cycle-0
adversarial pass *and* a twelve-finding partner review — both read it, neither ran
it — while the container was up and reachable the whole time, and it was caught
only when a *builder* executed it. Three rules follow:
- **Redefine the tag.** `[verified]` means *an executable artifact in this
     decision WAS EXECUTED*, with the command and its output recorded. Anything else
     is `[reasoned — not executed]`. The `stub-blindness` and `verify-the-deploy`
     skills already state this; the decision-record convention does not inherit it,
     so state it here.
- **Run them at intake, before the freeze**, against the real dependency. A
     decision quoted into N packets is worth N times the cost of executing it once.
- **Record the execution environment beside the claim** (`neo4j:5.26.30
     Community`, `postgres:16.4`, the runtime version) — a statement that parses on
     one engine version may not on another.

**And quote the NEGATIVE half of a verified decision exactly; re-derive the
positive half.** A decision verified in one direction has not been verified in the
other. Reproduced: a decision proved live that two schemas were *unreachable* —
exact, load-bearing, correct — and then *summarised* the permitted side as
"schema-level `USAGE` only". `USAGE` on a schema grants **name resolution, not row
access**, so the ticket that followed it literally granted `USAGE` and then failed
every read with `permission denied for table`. The part that was executed is
quotable verbatim; the part that was summarised is a hypothesis. A `[verified]`
tag therefore needs **both** an executed artifact **and** a statement of which
direction the execution covered.

### B. Give tickets a `reads` field

**`scope` encodes write-ownership ONLY.** It is used to answer two different questions — *what may this worker write?* and
*what does its acceptance need to exist?* — and only the first is in the data, so
a ticket's obligation to **ship a port that satisfies other tickets' tests** is
invisible. Populate `reads` at intake by walking each frozen test's imports and
subtracting the marked ticket's own `scope`; the same AST pass that builds the
public-surface document already yields these edges.

**The validator, and the exact rule.** Not *"imports must be in scope"* — that is
wrong and fires on every legitimate cross-ticket read. The rule is: **a
cross-ticket import is legal iff the owning ticket is in the importing ticket's
transitive dependency closure** (self-ownership, and the closed scaffold ticket,
count as satisfied). Measured on a real graph: AST-parsing all 120 frozen tests
and checking closure membership produced **exactly one violation** and **zero**
false positives against the legitimate cross-ticket reads the project documented
by hand. That one violation was a test marked `T-065` importing `T-062`'s module
while `T-062.depends_on` already contained `T-065` — so `T-065` is graded before
the module exists and 1 of its 6 frozen tests can never pass, and it is
**unfixable by adding an edge** (the reverse edge exists, so the fix would create
a cycle). That is exactly why this is an INTAKE gate and not a dispatch-time one:
at intake it is a re-marker or a ticket split; at dispatch it is a dead ticket.

**And it is now MECHANICAL: `freeze` REFUSES on a read violation.** This is not advisory any more, and an unattended run that does not expect it reads as a hang. `swarmloop.py freeze` calls the read gate before it locks anything; if any frozen test imports a module owned by a ticket outside its own dependency closure, freeze prints the full `ILLEGAL READ:` map and then `die()`s with *"freeze REFUSED: N frozen test(s) import a module owned by a ticket that is not in their own dependency closure."* Nothing is frozen and the run cannot proceed. Two things follow:

- **`swarmloop.py reads` is the diagnostic**, and it is a separate subcommand — run it *before* freeze rather than discovering the refusal at the freeze. It prints the same map, plus the cycle-detection note when the edge that would fix a violation cannot be added.
- **`freeze --allow-read-violations` is the escape**, and it is a decision, not a flag you reach for to get moving. It freezes over the violations and prints how many it froze over. Those tests can never pass — the module will not exist when they are graded — so passing it means you have decided to carry known-dead acceptance tests for the run. Record the count and the reason in the cycle-0 report, and prefer the re-mark or the ticket split.

The gate is a **silent no-op for a project with no ticket graph** (greenfield / plain-description intake), so this only ever fires under build-docs intake.

### B2. `owns` / `owned_files` — the per-file map the tooling PREFERS over `scope`

Undocumented until now, which meant every project silently fell through to the glob path. `scripts/swarmloop.py` resolves a ticket's ownership through `_ticket_owns(t)`, and it reads two keys before it ever looks at `scope`:

```jsonc
{ "id": "T-042",
  "scope":       ["src/api/**"],            // write-ownership by glob — the FALLBACK
  "owns":        ["src/api/routes.py",      // the per-FILE map — WINS if non-empty
                  "src/api/schemas.py"] }   // `owned_files` is accepted as an alias
```

**The rule, from the code:** `owns` is read first, then `owned_files`; if either yields a non-empty list those patterns *replace* `scope` entirely for ownership purposes, and the source is printed as `owns(owns)` rather than `owns(scope)`. It is not a union and not a narrowing filter — a file inside `scope` but absent from a non-empty `owns` is **not** owned. Both keys are normalized through the same pattern normalizer `scope` uses, so a glob is still legal inside `owns`; the point is that it can be an exact file list.

**Who reads it.** `frontier` (prints each ready ticket's `owns` and its source), `check-wave` (the pairwise ownership-intersection veto, and the intersection against in-flight dispatches that hold no worktree), and the `reads` read-edge gate. `parallel_safe` is read by none of them, by design.

**Why it exists, and why the default is a trap.** "Ownership is PER FILE, not per glob" is a live-run finding: two tickets whose `scope` globs merely *look* disjoint can still both write the same file, and two tickets whose globs overlap may own entirely different files inside it. A wave check run against globs is coarse in both directions — it false-blocks disjoint work and passes a genuine collision. **So: build the per-file map at intake (it is the same AST/file walk that produces the public-surface document and the `reads` edges), write it to `owns`, and let `check-wave` print `owns(owns)`.** If it prints `owns(scope)` for the tickets in a wave, the map was never built and the wave check you just ran was the coarse one. A ticket with `owns: []` or no key at all falls back to `scope` silently — absence is not an error, which is why a coarse `owns(scope)` map still has to be caught by eye once per wave. That eye is a backstop now, not the only check: when `owns` *and* `scope` are both empty the ticket is treated as owning **everything**, so `check-wave` VETOES the wave (or REFUSES it outright when no member declares anything) instead of passing it clean.

### C. Detect shared primitives before they are invented twice

Disjoint scopes are what make parallel dispatch safe — and the same disjointness stops two tickets
that need the same **new** primitive from ever finding each other. Reproduced: two
tickets in one wave independently wrote **three** RFC-8785 JCS canonicalizers
(`apps/trust/src/ledger/canonical.py`; `packages/contracts/src/signing.py`;
`packages/contracts/src/ts/signing.ts`), each unaware of the others — one's
docstring even asserted sole ownership. Byte-exactness is the entire point of that
spec, so any divergence in the ES6 number rule, UTF-16 sort order or string
escaping is a silent interoperability break **neither branch's tests can see**,
because each tests only itself.

*The gate:* at intake, flag any specification or algorithm named in the acceptance
criteria or the decision record of **two or more tickets with non-intersecting
scopes** — canonicalization, hashing, signing, serialization, time formatting, ID
generation are the usual suspects. Give it **its own ticket, built first**, and
name it as a dependency in every consumer's packet with *"import this, do not
reimplement."* The signal is already in the intake data: the decision record named
RFC-8785 and both tickets' criteria referenced it. Nothing looked. Failing that,
any packet whose criteria name a wire-format spec carries: *"if you find yourself
writing a canonicalizer, stop and report it — someone else may be writing it too."*

### D. Conform-or-replace, ruled at intake

**Any ticket that builds a thing the scaffold already provides a version of gets an explicit conform-or-replace ruling.** Reproduced: the scaffold froze
`proxyshop_support/llm_double.py` — `LLMDouble` with `.queue()`/`.when()`, and an
`LLMCall(role, prompt, …)` record — and a later ticket was told to "build the LLM
doubles". The worker built a parallel pair with the first two `LLMCall` fields
**reversed** and neither method, so `LLMCall("buyer", "hi")` silently swaps its
arguments depending on which module a test imported. **Nothing noticed**, because
nothing in the data represents "this frozen shared-runtime symbol is the contract
a ticket must satisfy". This is the third distinct failure mode alongside 2b (the
ticket needs to READ another ticket's module) and 2c (two tickets independently
invent the same thing): here the obligation is to **conform to** an existing
surface. Rule it explicitly at intake, and rule it **as a frozen conformance test**
(`the ticket's double IS-A LLMDouble`) rather than prose — prose in a packet is
read once; a test is checked every cycle.

**The field is `conforms_to`, and this gate IS its consumer.** build-docs emits it
(`"conforms_to": ["conform: tests/doubles.LLMDouble"]`, or prefixed `replace:`) and
`scripts/swarmloop.py` reads it nowhere — deliberately, because the ruling this gate
makes is not a dispatch decision the harness could take. What consumes `conforms_to`
is a person running this gate at intake and turning each entry into a test in
`acceptance/`, where `freeze` then grades it every cycle. A ticket that carries
`conforms_to` and no matching frozen conformance test has had this gate skipped, and
the T-0 scaffold's surface is unguarded for that ticket.


## Per-cycle rules under intake

- **Phase 3:** waves are emergent, not computed as rounds — the readiness scheduler
  dispatches each ticket the moment its `depends_on` close, its blast radius clears,
  and merge-review bandwidth allows; a "wave" is whatever happens to be in flight
  together. Never hand-shard the graph, and **never dispatch on `parallel_safe`** —
  it is an advisory hint, superseded by the per-file ownership map (`swarmloop.py
  check-wave` is the mechanical check; see the ruling above).
- **Packets:** ticket fields map field-for-field into the packet template
  (`objective`→Objective, `acceptance`+`verify`→Done means, `scope`→Files you own,
  `non_goals`→Boundaries). The packet then adds what the ticket cannot know: the
  worktree, the git prohibitions, the discretion tiers, `BUGS-OBSERVED`, the current
  baseline. Include the ticket's R-IDs so reports trace to requirements. **Add the
  two edges `scope` cannot carry:** the ticket's `reads` ("import these, never write
  them") and its `provides` — the *outbound* obligation ("you are shipping this port
  for T-032's and T-033's tests, not only your own"). `provides` has **no code
  consumer and is not supposed to have one**; it is prose the worker reads out of the
  packet, and writing it into the packet by hand is the only thing that consumes it.
  An unstated cross-ticket obligation is exactly
  how a ticket ships a port that satisfies only itself.
- **Re-planning:** discovered reality that invalidates un-started tickets → rewrite
  the un-started tail of the backlog (EXECUTION.md and this loop agree on this). It
  never licenses touching frozen goals; reality contradicting a *frozen* success
  criterion is the "goal genuinely wrong" terminal case — stop and ask.
- **The graph itself is frozen** — `freeze` puts it in `protected_paths` and hashes it,
  so `verify` fails on a moved byte and `check-branch` vetoes any lane that touches it.
  This is **not** a ban on re-planning: rewriting the un-started tail is still normal,
  it just goes through `freeze --amend "<reason the user approved>"`, which prints the
  old → new hash with a unified diff and writes it to the append-only freeze log.
  `swarmloop.py findings` re-seals its own mints without an amendment (a `graph-mint`
  record naming the minted ids), so the audit loop is never forced to choose between
  minting tickets and a green `verify`. **Why the graph and not just the suite:**
  `red-check` reads `bootstrap: true` out of it to exempt a ticket from must-go-red, so
  the graph is an input to a gate, and an unhashed input to a gate is a gate a worker
  can edit around — a worker adding `bootstrap: true` to its own ticket exempts itself
  from the check that would have caught its defect. The git-guard cannot reach it (the
  canonical graph is repo-root `tickets.json`, outside `.swarm-loop/`), which is why the
  answer is evidence rather than prevention. Keep the graph **inside the repo**: an
  out-of-repo path (`init --ticket-graph ../elsewhere.json`) cannot be frozen at all —
  freeze says so loudly and the bootstrap exemption goes ungraded for the whole run.
- **Reporting:** cycle reports and the final report reference T-IDs and R-IDs
  throughout, and the final report carries a **plan-divergence section** — tickets
  split, merged, dropped, or added, and any success criterion unmet or reinterpreted
  — as feedstock for the user's next build-docs session. The loop does not edit the
  doc set itself; planning docs belong to the planning skill.
