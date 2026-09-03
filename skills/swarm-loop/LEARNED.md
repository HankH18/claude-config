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

Slate wiped again 2026-09-03 after the second such pass, over the fourteen entries
seeded from the proxyshop run. Every one was ground-truthed against the bytes before
anything was written, and that step paid: **four entries were already fixed and their
own `Coverage:` notes were stale** — the code-intelligence index in a worktree (SKILL.md
Phase 0/1, rung 2, Phase 9 and the packet all scope it to the primary checkout), the
combined-diff deletion audit and its `'*test*'` scoping (Phase 5 step 4, rung 1, rung 2,
merge protocol step 9), the stale-base semantic-dependency case (Phase 5 step 1 and merge
protocol step 3), and the interpreter caches under a protected path (`IGNORED_DIRS`,
`is_artifact_path` and `iter_files` have skipped them all along, and
`restore-harness --remove-added` clears an ADDED file, both contrary to what that entry
asserted). Promoting a lesson the skill already carries is how a document grows a second,
drifting copy of its own rule — read the file, never the note.

Ten entries were promoted. Four became MECHANISMS rather than prose, which is this
skill's own standing preference: `check-branch --worktree <path>` refuses a branch whose
worktree is dirty or is not that branch's tree, and catches a tip that MOVED past the SHA
you pinned — the failure merge protocol step 0 says was only ever caught by eyeballing a
commit count; `red-check` files a timeout as **HUNG** rather than as a shade of WEAK,
because a gate that deadlocks prints no red at all in CI and so reads as absent;
`findings` PARTITIONS every recorded finding on who may fix it and refuses to mint a
ticket under a protected path or anywhere under `.swarm-loop/`; and `check-branch` no
longer calls an empty file intersection "low-risk". Two things were deliberately NOT
built and are the open decisions: a `repeat_of` signal in `findings` keyed on the
finding's file (it fires falsely on a record-then-mint and on a first-ever ticket in a
file that already carried below-threshold findings, and its key is wrong in both
directions), and a resolution assertion inside the frozen acceptance `conftest.py` for
the recorded-absolute-entry route. Both lessons are carried as prose today.

---

## Entries

<!-- Appended newest-last by `distil --to-skill` at Termination. Empty is a
     real state and the harness reports it: `resume` and `checkpoint` print the
     entry count every cycle, so a run that ends without distilling says so. -->

Empty as of the 2026-09-03 pass above, and that is the correct state: every entry
this file held has landed in SKILL.md, the references, or `swarmloop.py`, and the
file's own rule is to delete what a skill edit supersedes rather than to keep a
second copy of it.
