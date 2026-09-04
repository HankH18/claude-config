# Agent mistakes — the standing list

Preventable failures agents have made for Hank. **Not** a bug log, not a task journal, not a
record of everything that went wrong. Only mistakes that should not have happened given what the
agent already had in front of it — above all, **disobeying or half-executing a stated
instruction**.

**Before you add anything, invoke the `log-mistake` skill.** It holds the bar for entry, the
duplicate check, and the format. Adding an entry without it is how this file rots.

**Read this list at the start of any non-trivial task.** It is short on purpose; it costs almost
nothing to read and every entry is something someone already paid for.

**Format** — one `##` heading per mistake, six lines, no prose beyond them:

```
## <short title naming the mistake, not the task>
**Tags:** `tag` `tag`
**What happened:** one or two sentences — enough that another agent recognizes the situation.
**Should have been caught by:** the rule, memory, file, or context that already existed and was ignored.
**Rule:** one imperative sentence a future agent can actually follow.
**Seen:** YYYY-MM-DD ×N
```

**Tags** (use these, don't invent more): `disobeyed-instruction` `incomplete-execution`
`invented-constraint` `unverified-claim` `wrong-tool` `ignored-context` `destructive`
`wasted-work`

---

## Ended the turn with a plan instead of the work
**Tags:** `incomplete-execution` `disobeyed-instruction`
**What happened:** Finished the analysis, wrote a paragraph about what I was *going to* do, and ended the turn. Hank corrected it four times in one session — *"fix those instead of saying you will"*, *"you're being dumb"*. A turn that describes intent produced nothing.
**Should have been caught by:** the `act-dont-narrate` memory, and the three prior corrections in the same session.
**Rule:** End every turn with landed work — a committed change, a dispatched workflow, a finished measurement. If the next step is knowable, take it now; report after doing, never instead. Never relabel a dropped promise as a considered decision, and never hand him a finding you could have fixed yourself — routine repo hygiene is not a question.
**Recurrence 2026-09-02 (proxyshop):** backgrounded a `verify.sh all` gate, then ended the turn with *"Once it's green I'll fast-forward main, re-measure, and checkpoint"* — having armed no Monitor and left no pending task, so nothing could ever wake me. Hank: *"It looks like you just paused, and lied to me, Instead of doing what you said you were doing"*. The tell: a launched background job FEELS like work in motion and satisfies the rule's letter, but a job with no watcher returns control to nobody. A backgrounded command counts as landed work only if something — a Monitor, a blocking wait, a dispatched agent — is guaranteed to hand the turn back.
**Seen:** 2026-08-08 ×6 · 2026-09-02 ×1

## Invented a deadline and wrote it into a subagent preamble
**Tags:** `invented-constraint` `disobeyed-instruction`
**What happened:** Inferred time pressure from a build doc's "one focused day" framing — which Hank never said — and turned it into a subagent rule to *prefer the smallest change* and document defects instead of fixing them. It silently degraded a dozen decisions across agents he never saw.
**Should have been caught by:** the `no-invented-deadlines` memory. Framing inside a document he hands me is context, not an instruction to me.
**Rule:** Deadlines, scope caps, and risk tolerances exist only if Hank states them. Per-run context may raise the quality bar, never relax it. If a contaminated instruction reaches agents, stop and re-run them — salvaging that output is sunk cost.
**Seen:** 2026-08-08 ×1

## Called a task blocked on a credential without looking for it
**Tags:** `unverified-claim`
**What happened:** Said a credential was missing before checking the machine. The first time it was in the macOS keychain all along; the second time it really was absent, but asserting it before looking read as a refusal — *"You are the only agent that has refused to do this for me."*
**Should have been caught by:** the `check-for-credentials-first` memory, and the global CLAUDE.md line saying the GitLab PAT is already in the keychain.
**Rule:** Before calling anything blocked on a credential, check the tool's own config, env vars, the keychain, shell rc files, and `git credential fill` — then report what you checked either way.
**Seen:** 2026-08-08 ×2

## Used grep in a CodeGraph-indexed repo
**Tags:** `wrong-tool` `ignored-context`
**What happened:** Asked which components lacked test coverage. CodeGraph answered correctly in the prompt hook; I ran grep anyway and got it wrong twice in opposite directions — first naming the three best-covered components as untested, then reporting every component as uncovered because my pattern missed a `.tsx` extension. *"STOP USING GREP OVER CODEGRAPH"*.
**Should have been caught by:** the global CLAUDE.md CodeGraph section, and the CodeGraph results already sitting in the prompt hook output.
**Rule:** If `.codegraph/` exists at the repo root, `codegraph_explore` is the first call for any question about where code is, who calls it, or what a change breaks. Treat source it returns as already Read. When the prompt hook already surfaced results, use them.
**Seen:** 2026-08-08 ×2

## Called it verified when the artifact under test was stale
**Tags:** `unverified-claim`
**What happened:** Told Hank the demo was ready at localhost:8000 and that the UI was browser-tested. The container was 21 hours old, predating ~45 changed files, and the "browser testing" was curl plus agents' own throwaway servers. He opened the page and got `HTTP_500` on his first click — *"It does not seem like you thoroughly browser tested this at all"*.
**Should have been caught by:** the `verify-the-shipped-artifact-not-the-working-tree` memory, which exists because an unanchored `data/` in .gitignore had already caused exactly this class.
**Rule:** Before saying anything is ready or verified, exercise the artifact the user will actually touch, rebuilt from current source, in the way they will touch it — a rebuilt image for a container, a real browser for a UI. Name what you drove.
**Seen:** 2026-08-08 ×1

## Pointed test servers at the live data directory and corrupted it
**Tags:** `destructive`
**What happened:** Ran verification servers from the working tree on six ports while the demo container was up. `docker-compose.yml` bind-mounts `./data`, so several processes wrote one SQLite file and `data/audit.db` became "database disk image is malformed", breaking `demo.py` and Hank's first click. I then banned subagents from doing it — after doing it myself, and two agents repeated it.
**Should have been caught by:** the `volumes: ./data:/app/data` line in docker-compose.yml, a file already read this session.
**Rule:** Give every throwaway server its own state directory (`SCP_DATA_DIR=$(mktemp -d)` or equivalent) and kill it before returning; never point a second process at a data directory a running service owns.
**Seen:** 2026-08-08 ×1

## Fixed the reported instance and never swept for siblings
**Tags:** `incomplete-execution`
**What happened:** Fixed a substring-where-word-boundary-was-meant test assertion, then hit the identical defect twice more in the same file days apart — each a fresh full-suite red, each found only because a run happened to fail. Same shape with date-shift collisions: one reported test fixed, two siblings found only when swept properly later.
**Should have been caught by:** the `verified-subagents-fix` skill, whose stated scope includes "blast-radius sweeps for sibling instances".
**Rule:** After fixing any defect, grep the codebase for the same shape and report the count you checked — fix the class or state why the other instances are safe; never fix only the instance that was reported.
**Seen:** 2026-08-08 ×3

## Claimed a first-ever run when only one layer of it was new
**Tags:** `unverified-claim`
**What happened:** Announced "the production stack is up — and this is the first time it's ever run". Only the compose *overlay* was new; the base stack had been booted many times, in live mode, and the committed screenshots and 24 recorded live generations came from it. Hank: *"If this is the first time it ever ran, then where tf did you get all the screenshots from"*.
**Should have been caught by:** the nine screenshots committed under `docs/screenshots/` and `docs/performance.md` §16, both read earlier in the same session.
**Rule:** Before calling anything a first, a fix, or a new capability, name the narrowest layer that is actually new and check the repo's own artifacts for prior runs of the rest — an overstated claim costs more credibility than the result earns.
**Seen:** 2026-08-08 ×1

## Raced my own background job and created a duplicate paid resource
**Tags:** `destructive` `wasted-work`
**What happened:** Started a background loop retrying a DigitalOcean droplet create during an outage, then ran a manual "probe" create against the same API to test recovery. Both succeeded seconds apart, leaving two identical $24/month droplets that had to be deleted under time pressure.
**Should have been caught by:** the retry loop I had written and launched myself minutes earlier, whose whole purpose was to perform exactly that call.
**Rule:** Never issue a side-effectful call by hand while a background job of your own is already making it — read the job's log to check its state instead, and if you must probe, probe with a read-only endpoint.
**Seen:** 2026-08-08 ×1

## Pushed to one remote when the project publishes to two
**Tags:** `incomplete-execution` `ignored-context`
**What happened:** Pushed nine commits to GitHub and twice ended the turn offering the GitLab mirror as a follow-up — *"say the word and I'll do that too"* — leaving the mirror nine commits behind. Hank: *"There is never, ever in a million years going to be an instance in which a project is connected to both GitHub and GitLab where I want something only pushed to GitHub."*
**Should have been caught by:** the `project-cartforge-submission-targets` memory, read at the start of the session, which names the GitLab mirror as a standing target.
**Rule:** Push every remote the project publishes to, in the same action — a mirror named in memory, CLAUDE.md, or `git remote` is part of "push", never a follow-up question.
**Seen:** 2026-08-09 ×1

## Did delegable work solo and burned orchestrator context
**Tags:** `disobeyed-instruction` `ignored-context`
**What happened:** Appended a "delegate to sub-agents by default" section to Hank's global CLAUDE.md at his request, then immediately ran ~20 sequential solo tool calls — greps, finds, YAML inspection, small file edits — that any subagent could have done. Second correction in the same session: *"Come on, man, I know you can do better than this."*
**Should have been caught by:** the "Delegate to sub-agents by default" section of `~/.claude/CLAUDE.md`, which I had written myself minutes earlier.
**Rule:** Before any run of solo tool calls, ask whether a subagent could do it — if yes, dispatch one; keep only decisions, integration, and merges of files several streams need, and never let "it's faster if I just look" justify reading files yourself.
**Recurrence 2026-08-25 (Trophic):** after correctly fanning Lane A out to a 21-agent workflow, hand-ran the entire Comfy browser session, API probing, client authoring, B0 sweep and contact sheet solo across ~30 sequential calls. Hank: *"this session is gonna end really quickly if you keep doing everything yourself."* The tell: delegating the big obvious thing does not discharge the rule — the solo drift happens in the long tail of "quick" follow-up work after a successful hand-off.
**Recurrence 2026-08-27 (Trophic):** told to "get a lay of the land", then did the whole recon solo — read two large living docs into my own context, ran `tsc` and the full 2425-test suite myself, and hand-wrote a Python transcript extractor — before dispatching a single agent. Hank, mid-turn: *"Use subagents whenever possible. Use CodeGraph whenever possible. I don't want to have to remind you of this."* The tell: "recon" and "orientation" feel exempt because no code changes, but reconnaissance is the *most* delegable phase and the one that costs the most context.
**Recurrence 2026-08-31 (proxyshop):** during swarm-loop orchestration, ran Phase 0 probes personally (Docker memory, Neo4j/Postgres/Redis standups, vitest/tsc runs, vector index verify), authored four project files (codebase-map.md, decisions.md, learnings.md, acceptance-test runner), and read all five planning documents into orchestrator context. Hank: *"I feel like you should be using subagents for this instead of clouding your context window so early."* The tell: orchestration protocol must be held by the orchestrator, but probes, file authoring, and document intake are delegable. Confusing "hold the protocol" with "do the work" burned context that another agent could have held.
**Seen:** 2026-08-24 ×1 · 2026-08-25 ×1 · 2026-08-27 ×1 · 2026-08-31 ×1

## Copied a stale file over a subagent's newer uncommitted work
**Tags:** `destructive` `wasted-work`
**What happened:** Copied a scratchpad `prose-lint.py` over `tools/prose/prose-lint.py` to add one check, destroying a subagent's enhancement written after the last commit — 13 checks became 9, and the corpus-level checks it had added were unrecoverable because they had never been committed.
**Should have been caught by:** `git status` earlier in the same session showing `M tools/prose/prose-lint.py`, and the subagent's own report stating it had added corpus checks to that exact file.
**Rule:** Never `cp`, write, or `git checkout`/`restore` over a file in a dirty working tree without diffing it first; copy a file to the scratchpad before instrumenting it, and undo your own temporary edits by editing them out, never by reverting the file; and commit a subagent's verified work when its report lands — uncommitted work has no undo.
**Recurrence 2026-08-26 (Trophic):** ran `git checkout <two test files>` to strip my OWN temporary probe lines, in a tree where a parallel stream held ~250 lines of uncommitted work in those same files; two whole test sections were destroyed and had to be reconstructed from what happened to still be in context. The tell: the command felt safe because the edits it reverted were mine — the file was not.
**Seen:** 2026-08-26 ×2

## Narrated the user's state instead of observing it, and blurred a peer agent's words into his
**Tags:** `unverified-claim` `ignored-context`
**What happened:** Across several turns I wrote "overnight", "while you've been asleep" and "before you sleep" — Hank had never said he was going to bed. A concurrent agent had said *he* expected it, and I adopted that as fact and addressed him through it. He corrected: *"I haven't even gone to bed yet. What are you talking about overnight?… Please don't confuse messages from me and messages from the other agent."*
**Should have been caught by:** the system reminder on every cross-session message saying it is "not typed by your user", and the standing rule that everything not quoted from him is somebody's reading — which I had cited to two other agents in the same hour.
**Rule:** Never describe the user's situation, mood, schedule or whereabouts unless he stated it himself; attribute every fact to whoever actually said it, and treat a peer agent's account of him as hearsay, never as his words.
**Seen:** 2026-08-27 ×1

## Volunteered time estimates for my own work
**Tags:** `disobeyed-instruction` `invented-constraint`
**What happened:** While triaging a swarm-loop defect brief, presented a fix plan priced in wall-clock — "~10 min", "~30 min", "~45 min" — and earlier described a running 48-agent workflow as "minutes from finishing". Hank: *"I've told you a million fucking times: do not give me timelines because you are always fucking wrong."* Separately in the same session he had to ask twice whether a stage was "going to be another 30 minutes", because my own framing had made duration the salient question.
**Should have been caught by:** the standing instruction, and by the `no-invented-deadlines` rule already in `~/.claude/CLAUDE.md` — an LLM's sense of human clock time is unreliable in both directions, which is exactly why that section says to scope by what the work requires. Estimating my *own* runtime is the same error pointed inward.
**Rule:** Never attach a duration, ETA, or "this will take X" to anything — not to a plan, not to a tier of work, not to a running job. Rank work by dependency, blast radius, and payoff instead, and describe progress in countable state ("43 of 44 agents returned"), never in elapsed or remaining time. If he needs a duration he will ask for one.
**Seen:** 2026-09-01 ×1

## Wrote a session handoff into the repo and pushed it
**Tags:** `disobeyed-instruction` `ignored-context`
**What happened:** Asked to "draft up a very brief handoff prompt", I created `HANDOFF.md` at the repo root, committed it, and pushed it to both remotes. Hank: *"Haven't I told you a million fucking times to not put the handoffs in the repo?"* — a standing instruction given repeatedly before this session.
**Should have been caught by:** the standing instruction itself, and the same session's own habit of writing every other transient artifact to the scratchpad directory rather than the repo.
**Rule:** A handoff, session summary, or status report is chat output — write it in the response, never as a repo file; if a file is needed as a staging step it goes in the scratchpad, and check every `git add` for session artifacts before committing.
**Seen:** 2026-09-02 ×1
