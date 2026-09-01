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

Slate wiped 2026-08-31 after a full improvement pass: every prior entry was
promoted into SKILL.md, the references, or the harness — or deliberately retired.
The historical sets are archived at
`~/.claude/skills-disabled/swarm-loop-LEARNED-archive-2026-08-31.md`.

---

## Entries

(none yet — this file refills from run terminations)
