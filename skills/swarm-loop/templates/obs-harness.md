# swarm-loop — HARNESS observations (OPEN)

Findings about the **measuring apparatus**: the swarm-loop skill itself — its
scripts, hooks, references, templates, tests, `claude-build`, the agent
protocol, the harness state machine. Findings about the project being built go
in that run's BUILD document instead.

The rule that decides which file an entry belongs in: *is this a finding about
the instrument or about the specimen?* A flaky project test is a build finding.
A verification ladder that cannot catch a flaky project test is a harness
finding. An observation that is genuinely both becomes two entries that
cross-reference each other's ids.

This file is machine-level and shared across every project on this machine. It
is written by watchdog sessions started by `claude-build watch` /
`claude-build watch-fix`, and read by fixer sessions.

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

---
