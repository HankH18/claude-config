# Build Doc Templates

Exact structures for each tier. Keep section order; drop sections that are genuinely empty rather than padding them.

## Table of contents

1. SPEC.md template (Tier 1)
2. Delta-spec variant (long-lived repos)
3. DESIGN.md template (Tier 2)
4. Ticket contract (Tier 3) — markdown and JSON forms
5. TASKS.md template (small builds)
6. EXECUTION.md template
7. Worked example: right-sized vs over-decomposed

---

## 1. SPEC.md template (Tier 1)

```markdown
# [Project/Feature Name] — Spec

## Problem & intent
What this is for and why it's being built. 2–5 sentences.

## User-visible behavior
What a user can do/see when this is done. Concrete, observable statements.
Use "WHEN [condition], THE SYSTEM SHALL [behavior]" phrasing for anything
that must be testable.

## Constraints
Hard limits: stack, hosting, budget/credit constraints, performance floors,
compatibility requirements.

## Non-goals
What this explicitly does NOT do. Be aggressive here — non-goals prevent
scope creep by agents trying to be helpful.

## Success criteria
How we know the whole build is done. Each criterion must be checkable —
if you can't state how it would be verified, rewrite it.

## Open questions
Anything the user must answer before Tier 2. If this section is non-empty,
STOP and ask before proceeding.
```

Target ~150–250 lines total. Every requirement gets an ID (R1, R2, …) so tickets can reference them.

## 2. Delta-spec variant (long-lived repos)

Same sections, but:

- **Problem & intent** describes the *change*, not the system.
- Add a **Current behavior** section: what the system does today, cited by file path — do not re-document it.
- **User-visible behavior** describes only what changes.
- Success criteria include "no regression in [linked existing test suites]".

## 3. DESIGN.md template (Tier 2)

```markdown
# [Project/Feature Name] — Design

## Architecture
Components and their responsibilities. A mermaid diagram if structure is
non-obvious.

## Interfaces (contracts between tickets)
Every boundary that two or more tickets share, pinned exactly:
function signatures, API routes + request/response shapes, event schemas,
DB schemas. This section is the anti-drift mechanism — if two tickets
touch a boundary not pinned here, the design is incomplete.

## Data models
Types/tables/schemas with field-level definitions.

## Decisions & rationale
Non-obvious choices and why (so an agent doesn't "improve" them away).
Include rejected alternatives when an agent would plausibly reach for them.
Any decision carrying a runnable artifact (SQL, Cypher, shell, API call,
config) is tagged one of:
  [executed: <runtime:version> — <command> → <result>]
  [reasoned — not executed]
Never [verified] on the strength of a read. If one direction was executed
(e.g. "X is denied"), quote that half verbatim and re-derive the other half.

## Verification strategy
What CI gates exist, what test layers cover what, and which checks are
required to merge. Ticket verification commands must map onto these.
Pin the test-attribution convention here — how a test names the ticket it
grades. Default: `@pytest.mark.ticket("T-0xx")` on a test/class, or
module-level `pytestmark = pytest.mark.ticket("T-0xx")`. Other stacks: the
equivalent tag or name prefix. One convention, stated once.
```

## 4. Ticket contract (Tier 3)

Markdown form (for TASKS.md or ticket descriptions):

```markdown
### T-014: [Outcome-phrased title, e.g. "Session tokens persist across restarts"]
- **Objective**: [The outcome and why it matters. 1–3 sentences. Not a step list.]
- **Refs**: [Spec requirement IDs + Design sections this implements, e.g. R3, Design §Interfaces/auth]
- **Acceptance**: [Machine-checkable criteria. Numbered.]
- **Verify**: `[exact command naming THIS ticket's tests, e.g. pytest -m 'ticket("T-014")' && ruff check src/auth]`
- **Scope**: [write-ownership globs, e.g. src/auth/**, tests/auth/**]
- **Owns**: [exact files, when DESIGN determines them — complete list or omit the line]
- **Reads**: [modules imported, never written — incl. what the acceptance tests import, e.g. src/contracts/session.py (T-011)]
- **Provides**: [interfaces shipped for other tickets' tests, e.g. Design §Interfaces/session → T-016, T-017 | none]
- **Conforms to**: [existing symbol this must satisfy or replace, e.g. conform: tests/doubles.LLMDouble | replace: … | none]
- **Depends on**: [T-011, T-012 | none]
- **Non-goals**: [what this ticket must not touch or attempt]
```

`Acceptance` is numbered so each item maps to one attributed test. `Verify` must go red in a worktree without this ticket's work — if it doesn't, it isn't a gate.

JSON form (for a task store / queue / Claude Code Tasks import):

```json
{
  "id": "T-014",
  "title": "Session tokens persist across restarts",
  "objective": "...",
  "refs": ["R3", "DESIGN#interfaces-auth"],
  "acceptance": ["...", "..."],
  "verify": "pytest -m 'ticket(\"T-014\")' && ruff check src/auth",
  "scope": ["src/auth/**", "tests/auth/**"],
  "owns": ["src/auth/session.py", "tests/auth/test_session.py"],
  "reads": ["src/contracts/session.py"],
  "provides": [{"interface": "DESIGN#interfaces-session", "consumers": ["T-016", "T-017"]}],
  "conforms_to": [],
  "depends_on": ["T-011", "T-012"],
  "non_goals": ["..."],
  "parallel_safe": false,
  "bootstrap": false
}
```

Field semantics:
- `scope` — write-ownership by glob. Always present.
- `owns` — write-ownership by file. Wins over `scope` when present, so it must be complete or omitted (`owned_files` is an accepted alias).
- `reads` — imports, never writes. Every entry must be owned by a ticket in this ticket's transitive `depends_on` closure.
- `provides` — interfaces other tickets' tests consume. Optional; omit when none.
- `conforms_to` — existing shared symbols this ticket must satisfy (or, prefixed `replace:`, supersede). Conformance appears as a test in `acceptance`.
- `bootstrap` — `true` on T-0 only; exempts it from the must-go-red check.
- `parallel_safe: true` only when the ticket's scope is disjoint from every other open ticket's scope. Advisory — the orchestrator dispatches on per-file ownership, not this flag.

**Bootstrap ticket (large builds):** the graph's sole root is T-0 — repo skeleton, test harness, lint/CI config, the `ticket` marker registration, anything downstream verify commands invoke. It is the one ticket whose outcome is legitimately "pipeline green on an empty suite," which would be refused as vacuous anywhere else — so it carries `"bootstrap": true`, and it still ships one attributed smoke test (`test_harness_runs`, marked `ticket("T-0")`) so the suite freeze can grade it. Example: `Verify: pytest -m 'ticket("T-0")' && ruff check src`. Every previously-rootless ticket depends on T-0.

**Fixture budgeting:** if a ticket's acceptance mentions a fixture, sandbox, or seeded dataset, that artifact appears in the ticket's `scope` (and its creation in acceptance), or as a dependency ticket. **External ground truth:** acceptance for policy/classifier/config tickets cites a human-approved fixture or a spec requirement ID — never the artifact's own definition.

## 5. TASKS.md template (small builds, <~10 tickets)

```markdown
# [Project] — Task Graph

## Dependency graph
[mermaid graph of ticket dependencies]

## Tickets
[Ticket contracts in dependency order, markdown form above]
```

## 6. EXECUTION.md template

```markdown
# Execution Notes (for the orchestrator)

1. Work the graph, not the doc: pull ONE ready ticket (all dependencies
   closed), load only its contract + files in its scope. Do not load the
   full task graph or other tickets' contracts.
2. A ticket is closed only by its Verify command passing. No model
   judgment substitutes for a passing check.
3. Parallelize only tickets marked parallel_safe. Everything else
   serializes in dependency order.
4. On failure: revert to the ticket's starting state before retrying —
   do not iterate on top of a failed attempt.
5. On discovered reality (the plan is wrong): stop, rewrite the
   un-started tail of the graph with the strongest available model,
   then resume. Never push through a stale plan.
6. Escalation: done-ness disputes, repeated failures (2+ on one ticket),
   and re-planning go to [strongest model / human gate], not to the
   executing model.
```

## 7. Worked example: right-sized vs over-decomposed

Feature: "Add password-reset via email."

**Right-sized (4 tickets):**

- T-1: Reset-token model + issuance endpoint (verify: `pytest -m 'ticket("T-1")'` — token TTL/single-use) — provides `ResetToken` → T-2, T-3
- T-2: Email dispatch on reset request (verify: `pytest -m 'ticket("T-2")'` with mail stub) — depends on T-1; reads `auth/tokens.py`
- T-3: Reset-completion endpoint + password update (verify: `pytest -m 'ticket("T-3")'` — happy-path + expired-token) — depends on T-1; reads `auth/tokens.py`
- T-4: Rate limiting + abuse guards on both endpoints (verify: `pytest -m 'ticket("T-4")'` — throttle tests) — depends on T-1, T-3

Each is one PR, one file-cluster, one verify command that names its own tests. T-2 and T-3 are parallel-safe (disjoint owns) once T-1 closes. Note the shared-primitive check: if T-2 and T-3 both needed, say, a constant-time token comparator, that is a T-1 deliverable both `reads` — not two implementations.

**Over-decomposed (the anti-pattern — do not produce this):**

- Create the migration file
- Add the token column
- Write the token generator function
- Write the token validator function
- Add the route
- Add the route handler
- Write the email template
- Wire the mailer
- …

Twelve sequential dependent steps, none independently verifiable, every handoff a chance for drift, and the executing agent is reduced to following keystrokes it can't sanity-check. If a breakdown looks like this, merge upward until each item closes with one meaningful check.
