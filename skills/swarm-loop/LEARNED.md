# LEARNED.md — distilled cross-run lessons

This file is the skill's persistent memory across runs. Read it at the start of
every run; entries here override conflicting defaults in SKILL.md. Append to it
at every run's termination (Phase: Termination), following these rules:

- Entries must be **durable and project-agnostic** — orchestration lessons, not
  project facts. "Wave width backed off under merge-review overload on
  frontend-heavy batches" belongs here; "the ClearView API uses snake_case" does not.
- Write entries **imperative and specific**, with the failure that motivated them,
  so a future run can judge whether the lesson applies.
- **Curate on every append**: merge duplicates, delete entries superseded by
  SKILL.md edits, keep the file under ~40 entries. An overgrown learnings file
  degrades every future run more than it helps.
- If a lesson is important enough that it should change SKILL.md itself, propose
  that edit in the run's final report instead of (not in addition to) burying it here.
- **Capacity gate — directed at the agent whose append makes this file 40
  entries:** stop and warn the user in your next user-facing message: LEARNED.md
  is full. The right response is an improvement pass on the skill and the
  harness — promote every encodable entry into SKILL.md, the references, or
  `swarmloop.py` itself, and curate what remains (the 2026-08-31 pass is the
  template) — before moving forward with further runs. Do not silently trim to
  make room; a full learnings file means the skill itself has a backlog.

Every rule above is now enforced mechanically, because for the whole life of this
file they were prose and this file stayed empty across every run. The append is
`scripts/swarmloop.py distil --to-skill --lesson "..." --because "..."`: it
refuses an entry that names a project, a path, a host, a product or a ticket;
refuses one that is not imperative, is a stub, or duplicates an entry already
here; refuses the 41st entry with the capacity message above rather than trimming
to make room; and appends under a lock, byte-for-byte after the file it read, so
two runs terminating at once cannot SILENTLY lose each other's entry — the loser
of the race is REFUSED with a message naming the lockfile and told to re-run, and
nothing of its is written. Nothing below is ever rewritten or reordered by the
harness. Curation is still yours.

Slate wiped 2026-08-31 after a full improvement pass: every prior entry was
promoted into SKILL.md, the references, or the harness — or deliberately retired.
The historical sets are archived at
`~/.claude/skills-disabled/swarm-loop-LEARNED-archive-2026-08-31.md`.

---

## Entries

<!-- Appended newest-last by `distil --to-skill` at Termination. Empty is a
     real state and the harness reports it: `resume` and `checkpoint` print the
     entry count every cycle, so a run that ends without distilling says so. -->

Seeded 2026-09-02 from the proxyshop run, cycle 10 — mid-run rather than at termination,
because this file has been empty across every run so far and the checkpoint has flagged it
every cycle. Entries below are the durable, project-agnostic subset of that run's
`.swarm-loop/learnings.md`; nine further entries were examined and deliberately NOT promoted
(already encoded in SKILL.md, project-specific, or better filed as a SKILL.md edit).

## A lane's own sub-agents are READ-ONLY in the lane's worktree

The isolation invariant is written about the ORCHESTRATOR's waves. The same physics applies one level down, and no packet said so. Measured: a build lane did exactly what its packet asked — ran its own adversarial pass — but spawned sub-agents that mutated the SAME worktree it was building in. Two of them collided: the test auditor watched `if False:` appear in a source file between its own restore and its next read (a sibling's concurrent mutation testing), and its entire first run's results were unusable. Worse, one sub-agent had been told it could revert source files with `git checkout`, which would have silently destroyed 112 lines of uncommitted fix sitting in the tree; the PreToolUse git-guard is the only thing that stood between that instruction and the loss.

APPLY: every packet that tells a lane to spawn its own agents must also say — your sub-agents are READ-ONLY in your worktree; a sub-agent that must mutate in order to test (sabotage, mutation testing) works on its own copy OUTSIDE your tree, never in yours; and no sub-agent may run a repo-global git op, ever. Disjoint file scopes are not disjoint state, at any depth of the spawn tree.

*Coverage: NOT in SKILL.md. The isolation invariant (Phase 4) and the packet prohibitions (references/task-packets.md) both address the worker itself and are silent about the agents it spawns. Propose adding this clause to the packet prohibitions; delete this entry when it lands.*

## Pinning a branch tip is necessary and NOT sufficient — assert the worktree is CLEAN

A tip pins what was committed and says nothing about what sits uncommitted beside it. Measured: a lane's verifier found the worktree dirty at the moment it was briefed — two files carried 112 uncommitted lines relative to the commit it was told to judge — and the "verified facts" it had been handed (46 tests green, the verify script exit 0) had been measured against the DIRTY tree. The pinned SHA was therefore not what produced the numbers, and nothing in the collection sequence would have revealed that.

APPLY: at collection, run `git -C <worktree> status --porcelain` and require it EMPTY before accepting a branch or dispatching a verifier at it. Non-empty means the lane has work it never committed — ask for the commit, re-pin, and re-measure, rather than merging a tip whose reported greens came from other bytes. Never forward a number to a verifier without having established that its provenance is the pinned commit.

*Coverage: PARTIAL. The merge protocol's step 0 already says to treat a completion notification as "may still be moving" until the worktree is clean AND the tip is stable — but it names no command, puts no check at collection, and does not carry this failure (facts measured against uncommitted bytes). Propose mechanizing it there; delete this entry when that edit lands.*

## A gitignored code-intelligence index does not exist in a worktree — and the CLI answers anyway

An index directory that is untracked (a `.codegraph/` and its equivalents) does not come along with `git worktree add`, and the CLI then silently resolves some OTHER index rather than failing. Measured side by side on the same query: from the primary checkout, 48 symbols across 3 files, correctly naming the project's own module and its 11 callers; from a worktree, the same query returned 25 symbols in 1 file of a third-party library's internals — none of it this project. One lane noticed and fell back to direct reads; any lane that did not would act on another codebase's symbols while believing it was reading its own. This is worse than an unavailable tool, because it answers.

APPLY: index-backed navigation is a PRIMARY-CHECKOUT capability. Every worktree packet says: use `git grep` and direct reads; if you run the index CLI at all, verify that every path it returns is inside your own tree before believing a word of it. The orchestrator keeps the index for its own primary-tree work, where it is correct. Generalize past this one tool — any capability that lives in a gitignored directory is absent in a worktree, and the ones that fail silently are the expensive ones.

*Coverage: SKILL.md currently instructs the OPPOSITE — Phase 0 and Phase 1 make the index "the run's default codebase navigation for the orchestrator and every packet (Phase 1, Phase 4, Phase 9)", and Phase 4 packets run in worktrees. This entry overrides that default until SKILL.md is amended to scope it to the primary checkout.*

## A shared scratchpad path is not lane isolation

Every lane in a wave was handed the same scratch-directory path, with nothing in the brief saying it was shared. Two lanes wrote a probe script at the same filename, and one of them executed it with ANOTHER lane's worktree as cwd. It died on a nonexistent path and forensics confirmed no damage — that outcome was luck, not design; the same collision against a live path is one lane's script mutating another lane's tree, which is precisely what the worktrees exist to prevent.

APPLY: give each lane a private subdirectory under the scratch root (`<scratch>/<lane-id>/`), and state in the packet that the parent is shared, that filenames must be lane-unique, and that every command runs with the lane's OWN worktree as cwd. A shared temp directory is shared mutable state and inherits none of the worktree's isolation — the same reasoning that forbids a shared working tree forbids a shared scratch path with guessable filenames.

*Coverage: NOT in SKILL.md or any reference — the scratch directory is not mentioned at all, in any phase. Propose naming it in the dispatch section alongside the worktree-root decision.*

## "Nothing calls this" is a defect only when a FROZEN TEST already passes because of it

One cycle refuted seven findings whose entire content was "this landed symbol has no production caller", then minted two HIGH findings of apparently the same shape hours later; only a ledger regeneration caught the contradiction. Adjudicated at ground truth, the discriminator is a single question: **does a frozen acceptance test currently pass because of this thing?**

If YES it is a measurement-credibility defect regardless of callers. The finding that stood: a frozen release-blocker test passed by comparing a URL's host against a BIDDER-CONTROLLED field, because the registry that was supposed to decide it was never wired — an attacker who lies consistently in both fields defeats it, and the test cannot see that because it never wires a registry. The metric was counting a security property as met that nothing enforced.

If NO it is unfinished work, and the answer is a SCHEDULE, not a ticket — the consumer is a ticket nobody has run yet. And if no ticket in the graph owns the missing consumer, THAT gap is what to record, because nothing else will ever schedule it.

APPLY: ask the question before accepting or refuting any zero-caller finding, and record which answer you got, so the next pass cannot re-litigate it.

*Coverage: SKILL.md states both halves in different places and never reconciles them — rung 2 says "zero callers means the wiring half was skipped", while Phase 9 says "a correct implementation with zero call sites is unfinished, not dead". Propose folding this discriminator into rung 2's caller check.*

## Audit removed lines on the COMBINED diff too, never only per branch

The per-branch removed-lines audit is necessary and not sufficient. Measured: the orchestrator read all seven of one branch's deletions and signed them off as benign. One was not — a deletion removed the failure path from a price parse, so the caller swallowed the exception and coerced the resulting empty value to zero, and a price that cannot be parsed became a free item. It was caught only on the COMBINED diff of the integrated batch, and only after the sign-off had already cleared it. A deletion that is locally benign becomes a fail-open in combination with what the rest of the batch — or an earlier one — leaves around it.

APPLY: after a batch lands, re-run the removed-lines audit over the batch's whole integrated range, and give the batch's adversarial verifier the same instruction in its packet: for each removed line, name what now happens on the path that line used to guard. A per-branch clearance is a first pass, not a verdict, and the orchestrator's own sign-off is the one that most needs a second reader.

**And the audit is scoped to TEST files, which is why that deletion was never eligible to be caught.** The prescribed commands filter the diff with `-- '*test*'`. The fail-open above was a SOURCE-side deletion, so the glob could not match it on any branch at any rung — the orchestrator "audited the removed lines" and the removed line was never in the set it read. This is the SECOND time that same scoping has been walked through: the frozen-metric bypass (a two-line config change that drove a metric from 50 to 100) also passed the diff audit precisely because the file did not match the glob. A filter that decides what is *eligible* to be reviewed is a policy about what can be found, not a convenience.

APPLY: audit removed lines over the WHOLE diff, and use the test glob only to ORDER the reading — tests first, because a weakened assertion is the highest-value find, then source, where a deleted raise, guard or validation is the thing to look for. Never let the glob decide what is examined at all.

*Coverage: SKILL.md Phase 5 step 3 mandates the per-branch removed-lines audit and nothing re-reads deletions at integration; the commands at SKILL.md:211-212 and the rung-1 description are all scoped `-- '*test*'`, and SKILL.md:59 already records the same glob missing the metric bypass. Propose (a) widening those commands to the full diff with the glob as a reading order only, and (b) adding the combined-diff pass to rung 2's duties.*

## A stale-base advisory that reasons by file intersection under-reports

The usual staleness check intersects the files that moved on the base with the files the lane owns, and clears the lane when the intersection is empty. That misses the case that actually breaks: a lane whose TESTS depend on semantics a sibling changed — a validation rule tightened, a default flipped, an error type swapped, a serialization narrowed — in files the lane never touches. Textual mergeability is unaffected by any of it, so the merge is clean and the check is silent; the branch then fails at runtime, or worse, passes a weaker assertion than the one the base now requires.

APPLY: treat file intersection as the cheap first pass, never the answer. Where the intersection is empty, scan the lane's own directory for REFERENCES to the changed modules, and read the base's diff for behavioural change rather than for moved paths, before clearing the lane. And when you do warn a lane, hand it the ancestry fact and let it measure the impact — an inference about impact shipped as if it were measured is its own defect (a warning that a rewritten module's "public surface may have moved" was wrong: the exported surface was byte-identical and only a line number had drifted).

*Coverage: PARTIAL. SKILL.md describes the branch check's staleness advisory in exactly the intersection form, as an advisory and never a veto; this entry is the known limit of that form. Propose stating the semantic-dependency case beside it.*

## Write every brief as a hypothesis, and require each agent to report the premises it found false

In one wave the orchestrator's briefs carried three factual errors, all measured: (1) a file:line citation with a warning that a rewritten module's public surface "may have moved" — the surface was byte-identical and only the line number had drifted; (2) a recovered defect forwarded WITH a prescribed fix that did not cover the reporting lane's own broader repro, so both halves were needed and only one was sent; (3) an instruction to build and test against a stub service that was structurally incapable of serving the routes the ticket needed. None of the three cost real work, because every lane measured the premise instead of accepting it. Separately, a lane told to implement a recorded "repair" proved the recorded guidance vacuous — its second assertion was implied by the first, and the degenerate implementation it claimed to kill passed both.

A lane told only to "follow the brief" builds the wrong thing and reports the failure as its own.

APPLY: say in every packet that file:line references may have drifted, that a named test or target may be the wrong one, and that a prescribed fix is a starting point rather than a solution — then require, in the completion report, a list of any premise of the brief the agent found to be false. Hand over the reproduction and the constraint; let the lane derive the fix. The agent executing a brief is the one positioned to falsify it, and the only cost of asking is a line in the packet.

*Coverage: NOT in SKILL.md or references/task-packets.md — the four-part packet contract (objective, output format, guidance, boundaries) has no falsified-premise report field, and no phase asks for one. Propose a `PREMISES-FALSIFIED` field in the packet template alongside `BUGS-OBSERVED` / `NEEDS` / `CONFIG-TOUCHED`.*
