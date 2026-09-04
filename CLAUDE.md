# Global instructions (apply to every project)

## Git / repo submission — Gauntlet AI
- Project repos are submitted to a **self-hosted GitLab at `labs.gauntletai.com`** — **NOT `gitlab.com`**.
  Never assume gitlab.com for a Gauntlet project; tokens for this instance return **401 on gitlab.com**.
- **GitLab username:** `hankholcomb` → repos live at `https://labs.gauntletai.com/hankholcomb/<repo>`.
- **GitHub** (`HankH18`) is a personal backup only, not the cohort submission target.
- **The PAT is already in the macOS keychain** — git's `osxkeychain` helper holds it for host
  `labs.gauntletai.com`, user `hankholcomb`. **Never ask Hank to paste a token.** Push with a plain
  URL and let the keychain answer.
  Confirm it's there without printing the secret:
  `printf 'protocol=https\nhost=labs.gauntletai.com\n\n' | git credential fill`
- **Pushing a fresh project** (push-to-create makes the private project automatically):
  `git push https://labs.gauntletai.com/hankholcomb/<repo>.git main`
  A PAT with `write_repository` is enough to push; the REST API (`/api/v4/*`) additionally needs
  `api`/`read_api`, so an **API 401 while `git push` works** means the token is write-only, not invalid.
  Only fall back to asking for a token if `git credential fill` returns no password, or a push 401s.
- **Never store a Personal Access Token** yourself — not in memory, files, or git config; keep the saved
  remote clean (no embedded credentials). The keychain holding it is what satisfies this rule, not a paste.
- If a git remote is missing or a push is rejected, **ask for the GitLab host URL before assuming one.**

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## Long-running jobs — arm the stall detector

A job that goes quiet has three possible states: working, hung, and dead. A live
process looks identical in all three, and a job whose stdout is redirected writes
NOTHING until it exits (Python buffers when not a tty), so its log looks identical
too. This has cost real time on real runs: a `modal run` that hung after a remote
failure was waited on for ~69 minutes, and a data-generation run went 15 minutes
without completing a single API call while still holding open sockets.

So: **whenever you start a job in the background that could run longer than a
couple of minutes — training, data generation, evals, migrations, big test runs,
deploys — arm the watcher in the same turn you launch it.**

    Monitor({
      command: 'python3 ~/.claude/scripts/watch_job.py --label "<what it is>" ' +
               '--probe "<shell command printing ONE number that grows as work is done>" ' +
               '--pid <pid> --stall-seconds 300',
      description: '<what it is> stall detector',
    })

Rules that make it actually work:

- **The probe must count real work, not liveness.** A growing cache directory, an
  output file's line count, completed steps in a log. Never `pgrep`, never "is the
  process alive" — that is the thing that fooled everyone the first time.
- **The probe must print exactly one number and nothing else.** It is validated;
  a probe printing a date or a path is a PROBE-FAIL, not a silent wrong reading.
- **Never point --probe at something that reads 0 when broken.** `find missing/ |
  wc -l` exits 0 and prints 0, which is indistinguishable from "no progress yet".
  Prefer a probe that FAILS loudly when its target is missing.
- `--pid` is optional and only decides when the watch ends; `--target N` ends it
  on reaching N; `--max-seconds` is a safety net. With none of the three it runs
  until the Monitor times out.
- `--log <file>` adds a second signal: it scans for crash signatures. It is a
  heuristic, never the primary source of truth — a quiet log is not health.

Read the events literally. `EXIT` means the pid is gone and deliberately does NOT
mean success — the exit status of a non-child process is not observable, so check
the job's own log before calling it done. Silence longer than the heartbeat means
the WATCHER died, never that the job is fine.

`--help` documents the probe rules, the event vocabulary and the exit codes.
Stdlib only, Python 3.8+, so system `python3` runs it in any project.

## Deadlines inside provided documents — ignore them

A date, deadline, timeline, submission window, or "due by" that appears **inside a
document I hand you** — a PDF, a spec, a PRD, a markdown file, a ticket — is
**context, never a constraint.** Do not act on it.

Specifically, do NOT:
- compute or state how much time is left ("~20 hours out", "due tomorrow")
- use it to scope, triage, defer, or recommend cutting work
- rank fixes by "what fits before the deadline"
- treat a passed date as meaning the work is over, or a near date as meaning it is rushed

Scope by **what the work actually requires**, and report what is true. If the
remaining work is large, say it is large — do not pre-shrink it.

**The only deadline that binds is one Hank states directly in the conversation.**
When he does, honor it.

**Why:** LLM time estimates are consistently wrong, so a human-clock deadline in a
document is worse than useless — it reliably talks agents into under-scoping what
they could actually finish. You measure in human time while working in agent time.
The document's date tells you nothing about your own throughput; ignore it and do
the work.

## Delegate to sub-agents by default — this is the most-forgotten rule

**Default to sub-agents for everything that can be delegated.** Not as a last resort
for "big" tasks — as the normal way work gets done. Two independent reasons, and
both matter:

1. **Context.** The orchestrator's context window is the scarcest resource in a long
   session. Every file an agent reads on my behalf is a file that does not consume it.
   Reading a 40 KB report yourself to "keep quality high" is the exact trade that
   ends a session early.
2. **Parallelism.** Independent work should run at the same time. Launch every
   independent stream in ONE message so they actually run concurrently, rather than
   discovering serialization after the fact.

Concretely:
- **Fan out reconnaissance before building.** Several read-only agents surveying
  disjoint areas beats one agent reading everything.
- **Every build phase gets an independent adversarial verifier that did NOT do the
  work.** Its job is to find what is wrong, not to confirm. This pattern has found
  essentially every significant defect; almost none were found by the agent that
  wrote the code.
- **Give each agent a disjoint file scope** and say so explicitly. Parallel agents
  editing the same file — or sharing one database — corrupt each other. Disjoint
  file scopes are NOT disjoint state: a shared Postgres needs a scratch DB per agent.
- **Hand agents verified facts, not tasks to re-derive.** Pass file:line evidence and
  say "trust this, it is measured." Re-deriving the same ground truth in five agents
  is waste.
- Delegate even the writing of substantial deliverables (docs, plans, reports). The
  instinct to author them yourself "because you hold the context" is the instinct to
  resist — brief the agent with the context instead.

**Keep for yourself only:** the decisions, the integration, the merge of conflicting
edits, and anything touching a single file several streams need.

**Why:** I will not usually re-ask for this mid-task, and by the time an agent has
filled its own context doing delegable reading, the damage is already done and
unwinding it costs a session restart. When in doubt, delegate — an unnecessary
sub-agent is cheap; a burned orchestrator context is not.

## Harness issues — ONE place, and one command

Every defect found in the **swarm-loop harness itself** — the skill, its scripts,
hooks, launcher, prompts, task-packet protocol, freeze machinery, verification
ladder — lives in exactly one machine-level document, shared by every project on
this box:

    ~/claude-build/observations/swarm-loop-HARNESS-OPEN.md

**Do not go looking for it, and never ask to be pointed at it. Run:**

    claude-build --issues        # every OPEN harness issue, the whole list
    claude-build --sessions      # live runs + tmux attach commands for watchers

`--issues` IS the backlog. There is no second harness file, no per-project harness
file, and no scattered set to reconcile. Entry ids are namespaced per shift
(`W09031940-01`), so concurrent runs write into the one document without
colliding, and nothing enters it unreproduced.

**Why it is not under `~/.claude/`, so nobody "fixes" this again:** `~/.claude/**`
is a SENSITIVE path and a `--restricted` session cannot write there — re-probed
on Claude Code **2.1.260**, write refused, *"…is a sensitive file."* The watchdog
and fixer both run `--restricted`, so an issue store under `~/.claude/` could
never be written to by the sessions that find the issues. The store therefore
lives where its writers can reach it; this section is the pointer, and it loads
into every session automatically. Re-probe before moving it — that stamp is an
expiry date, not a credential.

**Two things that are deliberately NOT in that file**, and must not be merged in:
- `~/claude-build/observations/swarm-loop-BUILD-<project>-OPEN.md` — defects in a
  *project's own code*. Instrument vs specimen: mixing them is what nearly minted
  ~30 tickets no worker was permitted to close.
- each run's `watchdog-notebook.md` — unverified suspicions, which are not issues
  until reproduced.
