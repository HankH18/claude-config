---
name: build-docs
description: Generate the complete set of build docs (spec, design, task graph) for handing work off to autonomous coding agents. Use this skill whenever the user finishes planning and wants build docs, a build doc, a PRD-to-handoff conversion, a task breakdown, tickets for coding agents, or any document meant to be executed by Claude Code, sub-agents, or an orchestrator — even if they only say "write up the plan" or "get this ready for the agents." Also use it when reviewing or restructuring an existing build doc.
---

# Build Docs for Autonomous Coding Agents

Produce a three-tier document set — **Spec → Design → Task Graph** — instead of one monolithic build doc. This structure exists because agent failure is driven by two forces pulling in opposite directions:

1. **Under-specification**: the agent guesses intent, architecture drifts, acceptance is ambiguous.
2. **Over-decomposition**: every extra sequential dependent step is a multiplicative failure term (an N-step chain succeeds ~p^N, and per-step accuracy *degrades* as the context fills with prior steps and errors). Measured task success peaks around 4–6 subtasks per parent and declines beyond it. Over-detailed plans are the most common plan defect, not missing steps.

The job of these docs is to sit at the optimum between those forces. Every rule below serves that.

## Workflow

### Step 0 — Classify the build before writing anything

Answer these; they change the output:

- **Greenfield or long-lived repo?** Greenfield gets a full Tier 1 spec. Long-lived repos get a *delta* spec (what changes and why, against current behavior) — never re-describe the whole system, and lean on code-as-spec (below).
- **Bounded or exploratory work?** Bounded/deterministic work (CRUD, migrations, API integrations, ports) rewards more spec detail. Exploratory work (novel features, architecture decisions) rewards *less* — specify boundaries and non-goals, not prescribed solutions. Calibrate per work item, not per project; one flat granularity level is itself a defect.
- **Small or large build?** Under ~10 tickets: Tier 3 is a single `TASKS.md`. Larger, multi-session, or multi-agent: Tier 3 belongs in a queryable task store (Claude Code native Tasks, Beads, or the project's ticket DB), because markdown plans bit-rot and force re-parsing the whole graph to answer "what's next."

### Step 1 — Tier 1: SPEC.md (intent)

What and why: user-visible behavior, constraints, success criteria, explicit non-goals. No implementation detail. Target ~150–250 lines; shorter specs get implemented faster and drift less. Use the template in `references/templates.md`.

### Step 2 — Adversarial spec review (do not skip)

Before writing anything downstream, re-read the spec as a hostile reviewer and fix what you find:

- Contradictions between requirements
- Claims with no way to test them (rewrite until each is checkable)
- Hidden assumptions the planner made that the user never stated — surface these back to the user as questions
- Requirements that are actually two conflicting requirements

Defects caught here are cheap; the same defects discovered mid-execution invalidate the task graph and waste whole agent runs.

### Step 3 — Tier 2: DESIGN.md (contracts)

Architecture, component boundaries, interfaces, data models, and the contracts *between* work items. This tier exists to prevent interface drift — the failure where worker B builds on its own interpretation of worker A's output. Any interface two tickets share must be pinned here, not left for the tickets to negotiate. Include the verification strategy (what CI/tests gate what), and in it pin the **test-attribution convention**: how a test declares which ticket it grades (default: `@pytest.mark.ticket("T-0xx")` / module-level `pytestmark`; other stacks use their equivalent tag or name prefix). The orchestrator freezes the acceptance suite before implementation and grades by that marker, so it must be one convention, stated once.

**Decision records: executed or reasoned, never "verified" by reading.** A decision that carries a runnable artifact — SQL, Cypher, a shell command, an API call, a config fragment — is copied into every ticket that quotes it, so a wrong one is wrong N times. Reading it (Step 2, partner review) does not catch an artifact that does not run. Before pinning such a decision, execute the artifact and record the command, output, and runtime version beside it (`[executed: neo4j:5.26.30 — CREATE VECTOR INDEX … → ok]`); a statement that parses on one engine version may not on another. If you cannot execute it (runtime not reachable from the planning environment), tag it `[reasoned — not executed]` and add "execute pinned artifacts before copying them" to the acceptance of the first ticket that uses it. Never write `[verified]` for something that was only read. And a decision verified in one direction is not verified in the other: if you executed the proof that X is *denied*, quote that half verbatim and re-derive what is *permitted* from scratch — do not summarize the unexecuted half from memory.

### Step 4 — Tier 3: Task graph (tickets, not prose)

Decompose the design into tickets sized by the rules below, with explicit dependency edges. This is the **only** granular tier. Do not add a fourth "atomic micro-step" layer below it — a ticket is handed to one agent whole, and that agent plans its own steps. Scripting the agent's keystrokes converts it from a reasoner into a brittle instruction-follower that executes wrong instructions instead of noticing them.

**Ticket sizing — the inflection rules:**

- One ticket = **one verifiable outcome** = one PR-sized change = one file-cluster of blast radius = one focused agent loop.
- If a ticket can't be closed by a **single acceptance check**, it's too big — split it.
- A parent decomposes into **4–6 children**. If you're producing more than ~6–8 sequential dependent children under one parent, you've crossed the inflection point — merge until you're back under it. Prefer fewer, larger, independently-verifiable tickets over many tiny sequential ones.
- Changes that are coupled (must land together or the interfaces between them are unstable) go in **one ticket**, even if that makes it larger.

**Every ticket carries the full contract** (fields and format in `references/templates.md`):

- Objective — the outcome and why, not a step sequence
- Acceptance criteria — each one written so it becomes a test attributable to this ticket (one ticket ID per test, via the convention in DESIGN §Verification strategy). A criterion that cannot be expressed as a markable test is not acceptance; move it to the spec's success criteria as a tracked note. Every ticket, including T-0, must yield at least one attributed test or the orchestrator refuses to freeze the suite.
- Verification command — the exact test/build/lint invocation that closes the ticket
- File scope — write-ownership: which file-cluster it may touch (`scope`, globs). Where DESIGN determines the exact file set, also emit `owns` — the per-file list. Parallel-dispatch safety is checked per file, not per glob (disjoint globs can share a file; overlapping globs can own disjoint files), and `owns` wins over `scope` when present — so `owns` must be **complete or absent**. Never emit a partial list; a partial `owns` hides the files it omits from the collision check.
- Reads — modules/interfaces this ticket imports but never writes (`reads`). `scope` answers "what may I write"; `reads` answers "what must exist for my tests to run." Every entry must resolve to a ticket inside this ticket's transitive `depends_on` closure — a read edge outside the closure is a ticket that can never pass. Include what the frozen acceptance tests import, not just what the implementation imports.
- Provides — interfaces this ticket ships that *other* tickets' tests consume (`provides`: DESIGN anchor + consumer ticket IDs). "You are shipping this port for T-032's and T-033's tests, not only your own" is the obligation that otherwise goes unstated and produces a port that satisfies only its author.
- Conforms to — when the scaffold or an earlier ticket already provides a version of what this ticket builds, state whether it must **conform to** that surface or **replace** it (`conforms_to`), and express conformance as a test in acceptance (`the ticket's double IS-A LLMDouble`), never as prose. Prose is read once; a test is checked every cycle.
- Dependencies — explicit edges to other tickets
- Non-goals — what this ticket must not do

**Verification integrity rules** (a task graph that fails these is incomplete):

- Every `verify` command must be runnable given only work completed by upstream tickets. Test harnesses, lint/CI config, sandboxes, and markers don't exist for free — large builds start with a bootstrap ticket (T-0: repo skeleton + test harness + CI stub) that all former roots depend on. If any verify command assumes scaffolding no ticket creates, add the ticket or the graph deadlocks on its first verification.
- **A `verify` command must discriminate, not merely run.** It must fail before the ticket is built and pass after. The orchestrator runs each `verify` in a clean worktree with the ticket's work absent; exit 0 there means the gate cannot tell built from not-built and the ticket is refused. Write the command against the ticket's own acceptance tests, then ask: *what makes this go red today?* If the answer is "nothing," the ticket has no gate yet.
- Corollary: **a whole-suite invocation cannot discriminate**, because its result is dominated by tests unrelated to the ticket — it goes red for someone else's breakage and green while this ticket's defect is live. Worse, a project wrapper (`./scripts/verify.sh check` → `pytest -m 'not slow'`) can silently deselect the ticket's own grading test. Name the ticket's own tests in `verify` (by marker, path, or `-k`). If a wrapper is unavoidable, the ticket's tests must be provably inside its selection.
- **Bootstrap exemption, by data not by title:** T-0 is the one ticket whose legitimate outcome is "the pipeline runs green on an empty suite," which is exactly the shape refused above. Mark it `bootstrap: true` so the orchestrator exempts exactly one ticket. T-0 still carries at least one attributed smoke test (e.g. `test_harness_runs`) so the suite freeze has something to grade it by.
- Nontrivial fixtures (seeded repos, sandbox environments, e2e datasets) are real work. Budget them explicitly: in the consuming ticket's scope and acceptance, or as their own upstream ticket. Un-budgeted fixtures surface as mid-execution scope creep the orchestrator can't account for.
- Acceptance criteria must reference ground truth **external** to the artifact under test. "Classifier matches its own rule table" is circular — it passes even when the rules are wrong. For policy, classifier, and config tickets, acceptance references a human-approved fixture set or spec examples, and obtaining that approval is itself a step in the ticket (or an upstream one) — not an assumption.

**Dependency structure:** mark which tickets are genuinely independent (disjoint file scopes). Only those may run in parallel; everything else serializes. Waves are *computed* from the dependency graph at execution time — never hand-shard the task list into sequential document blobs.

**Graph checks before finalizing Tier 3** (run all three; each catches a failure that is invisible once tickets are dispatched in parallel):

1. **Shared primitives across disjoint scopes.** Disjointness is what makes parallel dispatch safe, and it is also what stops two tickets that need the same *new* primitive from finding each other. Scan the acceptance criteria and decision records of every pair of tickets with non-intersecting scopes for a shared named specification or algorithm — canonicalization, hashing, signing, serialization, time formatting, ID generation are the usual suspects. Any hit gets its own ticket, built first, named in `depends_on` and `reads` of every consumer with "import this, do not reimplement." (Measured failure: two parallel tickets shipped three RFC-8785 canonicalizers, each tested only against itself; byte-exactness was the whole point.)
2. **Read edges resolve.** For every ticket, every entry in `reads` (including what its frozen tests import) is owned by a ticket in its transitive `depends_on` closure. A violation found now is a re-mark or a split; found at dispatch it is a ticket that can never pass, and adding the missing edge often creates a cycle.
3. **`owns` is complete or absent.** Any ticket with an `owns` list has every file it will write in that list; otherwise drop `owns` and let `scope` carry it.

### Step 5 — Execution header

Prepend a short EXECUTION.md (template in references) stating, for the orchestrator:

- Pull one ready ticket at a time; load only that ticket's contract plus its file scope — never the whole task graph.
- The plan is mutable external state: on failure or discovered reality, re-plan by rewriting the un-started tail of the graph, not by pushing through a stale plan.
- Judgment calls (done-ness, failure handling, re-planning) escalate to the strongest available model or a deterministic check — never a cheap model's opinion. Cheap models execute leaf tickets; they do not judge them. Push every done-ness decision that a test can make into the verification command instead of any model's judgment.
- After a failed attempt on a ticket, prefer reverting to the ticket's starting state over iterating on top of the failure — success probability drops sharply once an agent's own errors enter its context.

## Over-specification tripwires

While writing any tier, stop and coarsen if you catch yourself:

- Writing code or pseudo-code an agent should write (pin *interfaces* in Tier 2; never implementations)
- Giving line-level or click-level instructions
- Restating what the codebase already says — link the file path instead
- Exceeding roughly 150–200 total instructions across the doc set an agent sees at once — instruction-following reliability degrades beyond that
- Hardcoding brittle if-this-then-that logic to cover edge cases — state the heuristic and the boundary instead

## Code-as-spec (long-lived repos)

Once typed interfaces and tests exist, they carry spec load. Shrink the docs accordingly: point tickets at the interface/test files rather than duplicating their content, and treat "make the types and tests pass" as acceptance wherever it truly captures intent. The docs shrink over a project's life; growing docs on a mature repo is a smell.

## Templates

Read `references/templates.md` before writing the docs — it has the exact structure for SPEC.md, DESIGN.md, EXECUTION.md, TASKS.md, and the ticket contract (markdown and JSON forms), plus a worked example of a right-sized vs over-decomposed breakdown.
