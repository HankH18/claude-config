# @@PROJECT@@ — BUILD observations (OPEN)

Findings about the **thing being measured**: the code in `@@DIR@@` — its
defects, its tests, its architecture, its dependencies, its performance.
Findings about the swarm-loop skill, its scripts, hooks, tests or `claude-build`
go in the HARNESS document instead.

The rule that decides which file an entry belongs in: *is this a finding about
the instrument or about the specimen?* A flaky project test is a build finding.
A verification ladder that cannot catch a flaky project test is a harness
finding. An observation that is genuinely both becomes two entries that
cross-reference each other's ids.

This file lives outside the project repository on purpose: it stays out of the
build's git history, out of its diffs, and out of the frozen-path blast radius,
so nothing a watchdog writes can contaminate what is being measured.

## Entry format

    ## <ID> · <class> — <one-line claim, specific enough to triage without opening it>
    **[VERIFIED]** severity: <CRITICAL|HIGH|MEDIUM|LOW>
    evidence: <file:line, the command that was run, the number measured>
    not checked: <what was NOT verified — required; "nothing" is not an answer>
    fix: <the smallest change that would settle it>

`class` is one of: `defect`, `inefficiency`, `serialization`, `token waste`,
`pattern`, `baseline`, `process-failure`.

Severity is two axes on purpose. Magnitude says how much it costs; class says
who fixes it and how. The class is what makes an entry triageable without
reading it.

Status markers: `[VERIFIED]`, `[RESOLVED]`, `[RETRACTED]`. Nothing enters this
file unverified — an unreproduced suspicion stays in the watchdog's own notebook
until it is reproduced.

## Two rules that are not negotiable

**Resolved entries are DELETED, not archived. There is no history in this file.**

**Ids are namespaced per shift.** Each watchdog session is given a shift prefix
by `claude-build` and mints `<PREFIX>-01`, `<PREFIX>-02`, … Without that, a
fresh session cannot see the ceiling of a file whose resolved entries have been
pruned, and two different findings end up sharing an id — which has happened.
Never renumber, never reuse, and never edit another shift's entry except to mark
it `[RESOLVED]` after measuring that it is fixed.

No agent in this topology may edit `@@DIR@@`. An entry here is a report to the
build, never a change to it.

---
