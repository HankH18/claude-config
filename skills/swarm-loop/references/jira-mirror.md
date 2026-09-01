# Jira Mirror — real-time scrum-board visibility (optional)

If a Jira/Atlassian MCP server is connected, the loop mirrors itself onto a scrum board so a human can watch tickets move in real time while the run is autonomous. Three rules govern everything here:

1. **Write-only.** The loop writes to Jira; it never reads decisions back. Ticket edits, reordered backlogs, or comments made in Jira do not steer the run — goals are frozen, `.swarm-loop/` + git are the only sources of truth, and ingesting board content mid-run would be both a state-divergence bug and a prompt-injection surface. If the user wants to change course, they talk to the orchestrator, not the board.
2. **Never block.** Every Jira call is best-effort. A failure is logged and retried at the next phase boundary; the loop proceeds identically either way. After ~3 consecutive phase boundaries of total Jira failure, disable the mirror for the run, note it in the cycle report, and stop retrying.
3. **The harness stays Jira-free.** `swarmloop.py` knows nothing about Jira. Mirroring is pure orchestrator behavior, so the mirror can be swapped (Linear, GitHub Projects, nothing) without touching the deterministic core.

## Setup (Phase 0)

Check the available tools for a Jira/Atlassian MCP (tool discovery, not assumption — server implementations differ in tool names and coverage). If present, confirm with the user in the same init exchange: mirror on/off (default on when connected) and the **project key** (existing project to reuse, or one to create — creation needs admin rights; if creation fails, ask for an existing key rather than stalling). Ensure a **Scrum board** exists for the project. Create one **"Loop Control"** issue — the run's live log; cycle analyses and terminal reports land there as comments.

Record everything in `.swarm-loop/jira-map.json` (orchestrator-owned, like all of `.swarm-loop/`):

```json
{
  "enabled": true,
  "project_key": "SWRM",
  "board_id": 42,
  "control_issue": "SWRM-1",
  "sprint_support": true,
  "epics": {"F1": "SWRM-2"},
  "tasks": {"F1-T1": "SWRM-7"},
  "pending": []
}
```

`pending` holds failed mirror actions (`{action, args, failed_at}`) for retry at the next phase boundary.

**Capability probe:** many Jira MCP servers expose issue CRUD + transitions + comments but *not* sprint management. Probe once at setup and set `sprint_support` accordingly. With sprints: cycle N is a real sprint. Without: fall back to a `cycle-N` label per cycle and let the board group by label — visibility survives, ceremony doesn't.

## Phase hooks

| Loop event | Jira mirror action |
|---|---|
| Phase 2, after checkpoint approval | Epic per atomic feature; issue per backlog task in **To Do**, linked to its epic; description = the task's objective + done-condition (its named acceptance tests) |
| Phase 3, epoch opens | Create + start sprint **"Epoch N"** with the frontier's priority summary as the sprint goal; move the frontier's ticket issues into it (fallback: apply `epoch-N` label). Every minted ticket — worker bug report, verifier finding, audit finding — gets its issue the moment it enters the ledger |
| Phase 4, dispatch | Transition dispatched tickets → **In Progress** as the scheduler launches them (batch same-turn dispatches into one turn of calls) |
| Phase 5, collection | Returned → **In Review**, with a comment: STATUS line + acceptance summary from the completion report. Rejected/failed-hung → back to **To Do** with a comment naming the failure mode |
| Phase 6, integration | Merged → **Done**. A branch unwound during integration → **To Do** + comment (e.g., "design collision with F3-T2; surviving design is now a constraint") |
| Phase 7/8, evaluation + report | Comment the cycle's analysis table (verbatim from `analysis/cycle-N.md`) on the Loop Control issue; complete the sprint — unfinished issues return to the backlog, which is exactly true |
| Wave verification | The batch's verifier gets no issue; its findings that mint tickets appear as new To Do issues linked to the tickets they landed on |
| Audit-fix loop K | Its own sprint **"Audit loop K"** (fallback: `audit-loop-K` label); each auditor finding that mints a ticket becomes a To Do issue labeled `audit-finding`; the loop's coverage summary comments on Loop Control at loop close |
| Termination | Final report summary as a comment on Loop Control; close any open sprint; leave unfinished issues in To Do — the board should end truthful, not tidy |

## Operational notes

- **Real-time means per-event, not per-cycle.** Transitions fire when the loop event happens (dispatch, collect, merge), not batched to cycle end — that's the entire point of the mirror. Within a wave, batch the wave's issues into one turn of calls.
- **Comments are summaries, not dumps.** A ticket comment carries the STATUS/ACCEPTANCE lines and decisions worth a human's attention; full reports stay in `.swarm-loop/reports/`.
- **Resume:** after `swarmloop.py resume`, reconcile the board once — read `jira-map.json`, replay anything in `pending`, and correct transitions to match reconstructed reality (e.g., a task whose branch merged while the session was dead moves to Done). Board state is derived state; reconciliation is always board ← loop, never the reverse.
- **Idempotency:** before creating any project/board/epic/issue, check `jira-map.json` first — a retried phase must not double-create. Map first, create second, record immediately.
