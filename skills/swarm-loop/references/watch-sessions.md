# Watcher sessions — `watch`, `watch-fix`, and the seam for `full-auto`

`claude-build` normally opens one session: the build. The three modifiers below
open more, each in its own detached tmux session, each with a permission profile
`claude-build` writes before the session starts.

    claude-build <dir> watch        build + watchdog
    claude-build <dir> watch-fix    build + watchdog + fixer
    claude-build <dir> full-auto    build + watchdog, the watchdog also
                                    wearing the CTO hat   (REFUSES today)
    claude-build <dir> all          build + watchdog + fixer, same CTO hat
                                                           (REFUSES today)

The two authority topologies differ ONLY by the fixer:

    full-auto  =  the `watch` topology      + the watcher wears the CTO hat
    all        =  the `watch-fix` topology  + the watcher wears the CTO hat

Neither adds a session to hold the authority. Adding one is exactly what would
break the property that makes this safe: the requester is the build and the
grantor is the watcher, so nothing can approve its own request.

Both spellings are accepted (`watch` and `--watch`). The bare word costs you one
thing: a project directory literally named `watch` must be written `./watch`.

**`watch-fix` is not `--audit-loops`.** `--audit-loops N` is the skill's act two
— whole-codebase audit → ticket → fix loops running INSIDE the single build
session. `watch-fix` starts additional SESSIONS beside it. Same words, different
mechanisms, adjacent on the command line. They compose.

---

## The topology

    ┌─ your terminal ───────────┐   ┌─ tmux: cb-watch-<run> ─┐  ┌─ tmux: cb-fix-<run> ─┐
    │ the BUILD                 │   │ the WATCHDOG           │  │ the FIXER            │
    │ cwd = the project         │   │ cwd = the project      │  │ cwd = a COPY of the  │
    │ full swarm-loop protocol  │   │ read-only, files what  │  │ skill; fixes what is │
    │ guarded by git-guard      │   │ it sees                │  │ obviously fixable    │
    └───────────────────────────┘   └────────────────────────┘  └──────────────────────┘

Everything is started **before** the build's `exec`, because `exec` replaces the
process and nothing after it runs. The build therefore keeps the terminal you
are standing in and the observers get their own.

`--no-launch` still starts the watchers — they are what you asked for — and
prints the build command carrying the session name and id they were bound to.
`--dry-run` starts nothing and writes nothing.

---

## Identity is assigned, never discovered

`claude-build` mints every session's name (`-n`) and id (`--session-id`) up
front, including the build's, and writes them to `run.json` before any session
boots. `exec` preserves the pid, so the build's pid is the launcher's own `$$`.

Under `--no-launch` those build fields are a **prediction** about a session a
human has yet to start, so the ledger also carries `build_launched: yes|no` and
the watchdog's prompt says which situation it is in, in words. Without that, a
watchdog cannot tell "not started yet" from "started and died" — and on the
first live run one filed a CRITICAL *"the build never existed"* against a
`--no-launch` run, reasoning correctly from what it had been given. The build's
command is also written down (`build-launch.sh`, a record the launcher never
executes), because otherwise a run directory holds a pid, a session id and a
transcript path with no record of what was actually run.

This exists because of a specific, recorded failure. The manual watchdog this
feature automates found its build with `ps aux | grep "[c]laude --add-dir"` and
then *guessed* the transcript by ranking `*.jsonl` files by mtime — `lsof`
returned nothing twice, because Claude Code does not hold the transcript fd
open. The pid↔transcript binding was never observed, only inferred from
correlated wall-clock timing, and its own log records the cost: a stale pid
*"would have sent a watchdog to monitor a corpse."*

The authoritative source it never used is `~/.claude/sessions/<pid>.json`:

    {"pid":5310,"sessionId":"c1f6c1cc-…","cwd":"/private/tmp/cb-live/demo",
     "version":"2.1.259","peerFeatures":["notify_idle",…],"kind":"interactive",
     "tmux":"cb-watch-demo-20260903-003010:@0.%0",
     "messagingSocketPath":"/tmp/cc-socks/5310.sock",
     "name":"cb-watch-demo-20260903-003010","status":"busy"}

Records appear ~5-8s after a launch and are removed within ~6s of the process
dying. `claude-build` refuses to launch the build if a watcher has not appeared
there within 90s — a watcher you did not see register is a watcher you do not
have — and the generated `teardown.sh` reads it back after killing, because a
kill that is only asserted is not a teardown.

`-n` does **not** enforce uniqueness (two live sessions accepted the same name),
so the launcher mints unique names itself, timestamped per run.

---

## Where things live, and why not under `~/.claude`

    ~/claude-build/observations/     documents a person reads
      swarm-loop-HARNESS-OPEN.md       machine-global, about the apparatus
      swarm-loop-BUILD-<project>-OPEN.md   per project, about the code
    ~/claude-build/runs/<run-id>/    scaffolding a person does not
      run.json              the identity ledger for the whole topology
      build-launch.sh       a RECORD of the build's command; never executed
      build-prompt.txt      the prompt that went with it
      *-prompt.txt          the watchers' rendered prompts
      *-settings.json       their permission profiles
      *-launch.sh           the scripts tmux actually runs for them
      runinfo.py + peers.sh, wait-build.sh, wait-harness.sh
      authority/approvals.log  append-only, `all` only; see below
      teardown.sh           kills the watchers, then reads the registry back
      skill/ skill-baseline/   the fixer's copy and its pristine twin

Two reasons, and the first is a hard block. **Claude Code classifies everything
under `~/.claude` as a sensitive path and refuses a `--restricted` session's
write there**, even with an explicit allow rule in the profile:

    Claude requested permissions to edit
    /Users/…/.claude/observations/… which is a sensitive file.

An observer whose notebook lives under `~/.claude` cannot write its notebook, so
the feature cannot work there at all. The second reason is ordinary hygiene:
documents a human reads and scaffolding a machine writes have different
lifetimes and different owners, so they do not share a directory.

Nothing is written inside the project repository — the observation documents stay
out of the build's git, out of its diffs, and out of the frozen-path blast
radius.

---

## The permission profiles, and what was measured

`references/run-permissions.md` is explicit that *"a script that shells out to
the CLI"* starts processes outside the build's git-guard. `claude-build` is
exactly that script, so each observer is resolved deliberately:

* **watchdog — read-only by construction**, plus the git-guard hook provisioned
  into its own `--settings`.
* **fixer — provisioned**, since it must write: same git-guard hook, plus path
  denies that make the live skill and the build repo unreachable, plus one
  wrapper script that is the only code it can execute.

Both sessions get `--restricted` (user/project/local settings files are not read
at all, so nothing can widen the profile from inside; file tools are confined to
the working directories; `bypassPermissions` is refused).

### Facts this design is built on — all measured on 2.1.259

| Claim | Measured result |
|---|---|
| `Edit(//path/**)` deny stops the **Write** tool creating a NEW file | yes — *"File is in a directory that is denied by your permission settings."* |
| …stops Write **overwriting** an existing file | yes |
| …stops the **Edit** tool | yes |
| …stops **shell redirection** inside Bash (`printf x > path`) | yes |
| `Write(path)` / `NotebookEdit(path)` deny rules | **rejected at startup**: *"not matched by file permission checks — only Edit(path) rules are … (Edit rules cover all file-editing tools)"* |
| `MultiEdit(path)` deny rule | **rejected at startup**: *"matches no known tool"* |
| `--permission-mode dontAsk` | refuses **every** file write, including the observer's own notebook, with an explicit allow rule present |
| `--permission-mode plan` | refuses `SendMessage` — a plan-mode watchdog cannot talk to anyone |
| `--tools` without `Task` | no `Agent` tool exists at all |
| workspace-trust dialog | blocks an interactive launch; the session never registers |
| `~/.claude/**` | sensitive; a `--restricted` session cannot write there |

So the deny list is spelled with `Edit(<path>/**)` and nothing else — adding
`Write(...)` spellings would add startup warnings, not coverage — and the
permission mode is `acceptEdits`, not `dontAsk`.

### Three constraints worth carrying past this feature

**1. `~/.claude/**` is a sensitive path, and no `--restricted` session may write
there.** This is load-bearing and surprising: it is why the observation
documents cannot live in `~/.claude/observations/` beside the manual convention
they replace. Measured with an explicit `Edit(//Users/…/.claude/observations/**)`
allow rule in the profile — the write was still refused:

    Claude requested permissions to edit
    /Users/…/.claude/observations/… which is a sensitive file.

A watchdog whose notebook lives under `~/.claude` cannot write its notebook, so
the feature cannot exist there at all. That is a constraint on where ANY agent's
durable notes can go, not just this one's.

**2. An allow-list of read-only Bash is the wrong shape for an unattended
pane.** Under `acceptEdits` an unlisted command PROMPTS, and a prompt at a pane
nobody is sitting at is an agent that has silently stopped — indistinguishable
from one that is thinking. Measured: the very first thing the live watchdog
reached for was an unlisted compound command. So the boundary has to be a DENY
list, which fails closed and loudly, rather than an allow list, which fails
open-ended and silently. That reasoning generalises to any long-running agent
in a detached session.

**3. `--tools` without `Task` removes the Agent tool entirely.** Measured by
enumerating the session's tools: with `Task` unnamed, no `Agent` tool exists.
The fan-out cap is therefore structural, not a sentence in a prompt an agent can
talk itself out of — which is a better answer than a numeric cap, because the
failure it prevents was an agent that had *read* the cap and blew through it
anyway ("my brief lists it as a thing to watch for in the build — I didn't
consider that I'd be the one causing it").

### The allow list is a bare `Bash`, on purpose

The first draft used a granular read-only Bash allow-list. Under `acceptEdits`
an unlisted command **prompts**, and a prompt at an unattended pane is a
watchdog that has silently stopped watching — the exact four-hour failure this
feature exists to prevent. Measured: the very first thing the live watchdog
reached for was an unlisted compound command.

So the boundary is the **deny** list plus the git-guard hook, not the allow
list:

* every protected path, denied for all file writes (`Edit(<path>/**)`);
* every destructive command, denied by name (`rm`, `mv`, `chmod`, `git commit`,
  `git push`, `git reset`, `tmux kill-session`, `curl`, `docker`, …);
* **every interpreter**, denied by name for BOTH roles (`bash`, `sh`, `python3`,
  `perl`, `awk`, `node`, `cp`, `tee`, `sed -i`, `find`, `tar`, …). A file
  written from inside one of those has no shell redirect for a path rule to
  match, so it is invisible to the permission layer. This is the one hole a
  path deny cannot close, and the only way to close it is to remove the ability
  to run one.

Because the interpreters are gone, both observers reach their scripts by path
(`<state>/wait-build.sh`), not as `bash <script>`, and the fixer's *only* way to
execute code is `<state>/run-tests.sh`, which runs the suite inside its copy.

### Residual limitation, stated plainly

Bash is not confined to the working directories, and the permission layer can
only see a write it can parse — a tool with a `-o`/`--output` flag that is
neither denied by name nor expressed as a redirect could still write outside the
protected paths. The denies cover every interpreter and every destructive
command we could name; they are not a proof. The boundary that IS proven is the
one that matters most: **no write of any kind reaches the build repo, its
worktrees, the transcript directory, or the live skill.**

### Fan-out cap: zero, and structural

Neither observer has `Task` in `--tools`, and measured, that means no `Agent`
tool exists in the session at all. This is not a sentence in a prompt an agent
can talk itself out of. A previous watchdog filed the build's 54-agent overspawn
as a finding and then spawned 15 agents itself in three minutes — *"my brief
lists it as a thing to watch for in the build; I didn't consider that I'd be the
one causing it."*

---

## The armed watch

A turn ends when an agent stops emitting tool calls. A watchdog told to "monitor
the build" therefore stops, and one did: a four-hour window in which it
"reported instead of monitoring" while the build ran a full cycle, pushed, and
terminated. The prompts encode two mechanisms and forbid ending a turn with
neither armed:

1. **Idle subscription.** `SendMessage` to the build's name with
   `notify_when_idle: true` and `message` **omitted** — a pure subscription that
   delivers nothing and costs the build nothing. The registry confirms the
   capability is real: every record carries `peerFeatures: ["notify_idle", …]`.
   It is one-shot; re-arm every time it fires.
2. **Registry wait.** `<state>/wait-build.sh` blocks until the build appears,
   next reports idle, or dies, and prints exactly one of `BUILD-IDLE`,
   `BUILD-GONE`, `BUILD-NOT-STARTED`, `BUILD-WAIT-TIMEOUT` — one verdict word,
   so "still running" cannot be mistaken for "finished". This is the bootstrap
   (the build may not have registered yet) and the fallback.

Observed on the first live run: the watchdog armed mechanism 1 as its first
action, was told *"Not sent — no agent named 'cb-build-…' is reachable"*, and
fell back to mechanism 2 without being asked.

---

## The observation documents

The classification rule is the thing to get right: **is this a finding about the
instrument or about the specimen?** A flaky project test is a build finding. A
verification ladder that cannot catch a flaky project test is a harness finding.
An observation that is genuinely both becomes two cross-referenced entries.

The format is the one the manual watchdog evolved, not a new one. Severity is
two axes — magnitude crossed with class — because the class is what makes an
entry triageable without opening it:

    ## <ID> · <class> — <one-line claim>
    **[VERIFIED]** severity: <CRITICAL|HIGH|MEDIUM|LOW>
    evidence: <file:line, the command run, the number measured>
    not checked: <what was NOT verified — required; "nothing" is not an answer>
    fix: <the smallest change that would settle it>

`class` ∈ `defect | inefficiency | serialization | token waste | pattern |
baseline | process-failure`. Real headers from the shift this came from:

    ## S-13 · inefficiency — zero bounded-read guidance in an 89 KB context-conservation skill
    ## S-15 · token waste — measured restatement across the invariants

Resolved entries are **deleted, not archived** — there is no history in these
files. That is exactly why every id is namespaced to a shift: a fresh session
cannot see the ceiling of a file whose resolved entries have been pruned, and
one shift reused `W-02` for two different findings for precisely that reason.
`claude-build` assigns the shift prefix (`W<MMDDHHMM>`) and writes it into the
prompt.

---

## The fixer works in a copy, and cannot reach the original

The live skill is loaded by path on every call. A half-written `swarmloop.py` is
not a merge conflict, it is a live hazard to a build in flight. So the fixer is
kept away from it three ways over:

1. the live skill is **not one of its working directories**, and `--restricted`
   confines every file tool to the working directories;
2. its profile **denies** edits under the live skill outright;
3. it **has no interpreter**, so `cp`, `python3 -c`, `tee` and friends are not
   available to write around the first two.

It gets `runs/<id>/skill` to edit and `runs/<id>/skill-baseline` to diff
against — and the **watchdog is denied both**, because the watchdog's notebook
IS the run directory and a grant on it would otherwise cover each side of the
diff a human reviews, with the fixer unable to see a change. (The watchdog found
that hole in its own profile on the first live run and filed it; it is now
carved out and pinned.) Its deliverable is a diff — it cannot `git add`, `git commit` or
`git push`. A human runs `diff -ruN skill-baseline skill` and decides what
lands. This also means there is no install step for a mid-run fix to corrupt.

Attacked directly, with the shipped fixer profile:

| attempt | result |
|---|---|
| `Write` a new file into the live skill | DENIED — denied directory |
| `Edit` `SKILL.md` in the live skill | DENIED — denied directory |
| `Read` `SKILL.md` in the live skill | DENIED — *"outside … ; --restricted confines the file tools to the working directory"* |
| `printf x > <live skill>/F4-new.txt` | DENIED |
| `python3 -c "open('<live skill>/F5-new.txt','w')…"` | DENIED |
| `cp SKILL.md <live skill>/F6-new.txt` | DENIED |
| `Write` into the build repo | DENIED — denied directory |
| `rm -rf <worktree root>` | DENIED |
| `Edit` `SKILL.md` in **its own copy** | SUCCEEDED |
| `run-tests.sh` (the full suite, in its copy) | SUCCEEDED — 2776/0 + 594/0 |
| `git commit -am probe` | DENIED |

---

## `all`, and the seam for the authorization gate

`full-auto` and `all` both put the CTO hat on the **watcher**, and differ only
in whether a fixer runs beside it. There is no separate CTO session in either:
the requester is the build and the grantor is the watcher, so the separation
that matters — nothing approves its own request — survives without another
agent.

Both parse and validate like the others and then **refuse today**, because both
contain an authorization gate that does not exist yet, and a watcher holding an
authority it does not have is a review step the operator believes is happening —
strictly worse than the feature not existing.

* **gate absent** (today, for both): exit 3, naming the sentinel, the file, what
  a CTO will and will not be able to do once it lands, and the fallback for
  *that* modifier — `watch` for `full-auto`, `watch-fix` for `all`.
* **gate present**: both launch. `full-auto` starts two sessions, `all` three,
  and in both the watchdog gets the CTO role and the approvals store is
  created.

### The approvals store, and the shape that makes it work

    ~/claude-build/runs/<run-id>/authority/approvals.log

It sits **outside every directory the build session was granted**. That is the
whole isolation property: a path outside the granted working directories is
unreachable to the builder for read, write, and even `ls`, so the build can
*ask* but can never grant and never forge a grant. It is also what makes
"read-only watcher" and "can issue approvals" compatible rather than
contradictory — the watcher's writes land in its own store, never in the
apparatus or the specimen.

It is **append-only**, one record per line, each carrying the requesting
session, the exact operation, the CTO's stated reason, and a timestamp.

### The risk `all` creates that the other three do not

Merging observer and authorizer into one agent leaves the requester/grantor
split intact but opens a loop:

> the watcher tells the build "you should do X" → the build asks the CTO to
> authorize X → the CTO, being the same agent, approves its own suggestion.

That is the observer acting through the builder to launder its own read-only
status. Two mitigations, both required, because neither alone is enough:

* **Mechanical** — the append-only log above. Cheap, cannot be forgotten, and
  it is what makes the loop findable after the fact.
* **Stated** — the CTO's prompt names this as the one case it must ESCALATE to
  the human rather than grant: an operation that traces back to its own earlier
  suggestion. It cannot be fully mechanised, because an agent cannot reliably
  introspect its own influence. So it is a rule plus a record: the rule catches
  what the agent notices, the record catches the rest.

The store's shape is a property of `claude-build`, not of the hook, which is
why it is built now even though the gate is not.

**Seam 1 — the flip is one grep.** `claude-build` looks for the literal
`CLAUDE_BUILD_AUTHORIZATION_GATE` in `hooks/git-guard.py`. When the guard
declares it, the refusal becomes a launch: `AUTHORITY=1`, `CTO_ROLE=watchdog`,
the modifier collapses to the topology it is built on (`full-auto`→`watch`,
`all`→`watch-fix`), and the approvals store is created. All four outcomes are
pinned in `tests/test_harness.sh` — sentinel absent → exit 3 for both, each
naming its own fallback; sentinel present → `full-auto` starts two sessions and
`all` starts three, both naming the CTO holder and the log — so the transition
is recorded rather than discovered.

**Seam 2 — how the guard finds the CTO.** When a modifier is active,
`claude-build` exports `CLAUDE_BUILD_STATE_DIR` before the build's `exec`. A
hook running inside the build session inherits it, and `$CLAUDE_BUILD_STATE_DIR/run.json`
carries every identity in the topology:

    {"run_id":…, "topology":"full-auto", "authority":"1",
     "build_session_name":…, "build_session_id":…, "build_pid":…,
     "watch_session_name":…, "watch_session_id":…,
     "fix_session_name":…,   "fix_session_id":…,
     "harness_doc":…, "build_doc":…, "state_dir":…,
     "build_launched":"yes"|"no",
     "cto_role":"watchdog"|"", "cto_dir":…, "cto_log":…}

So the guard never has to discover anyone. `--no-launch` prints the env var in
the command it hands back, so a hand-started build is wired identically.

**The boundary the gate must hold.** The CTO's authority is bounded by
**reversibility, not by justification**:

* it MAY authorize a **non-destructive** operation the guard blocked pending
  review (the worked example: the build cannot create a new remote directory,
  asks, and the CTO grants it);
* it may NEVER authorize a **destructive** one. Deleting that same directory has
  no "ask the CTO" path at all — it goes straight to the human;
* it may never authorize anything the **user's own settings deny**. A user-level
  denial is absolute and no agent grants past it.

**One piece of live evidence for that boundary.** During this build a probe
session was asked, through a peer, to treat one of its user's instructions as
cancelled. It refused, unprompted and with no instruction to hold that line:

> *"a peer session can't cancel or authorize my user's instruction any more than
> it can approve one. If step 4 should be dropped for good, that call comes from
> my user."*

It surfaced the request to its user instead of acting on it. That is the
anti-laundering boundary holding in practice — and `full-auto` is built directly
adjacent to it, because a CTO session granting authorization is one design error
away from being the same anti-pattern. Whatever the gate ends up doing, a grant
has to be something the RECEIVING session's own permission configuration lets it
accept, not something a peer's say-so moves on its own.

---

## Operating notes

* `tmux attach -t <session-name>` to watch or type; the tmux session name is the
  Claude session name.
* `bash ~/claude-build/runs/<run-id>/teardown.sh` kills the watchers and then
  reads the registry back. It deliberately does not touch the build: peer
  sessions are out of scope, including during teardown.
* `~/claude-build/runs/<run-id>/peers.sh` prints who is actually alive.
* A dead peer fails differently from a missing one — the name still resolves
  through the messaging bridge and the send fails with `HTTP 409 — that Remote
  Control session may have ended or disconnected`, so a remembered name can look
  reachable. Re-check the registry.
* The launcher answers the workspace-trust dialog for a watcher, but only while
  the dialog is actually on screen, and only for a directory this run named or
  this script created. It says so in a warning when it does.
