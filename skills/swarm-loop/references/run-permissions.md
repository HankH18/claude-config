# Run Permissions — provisioning capability for unattended runs

The premise: in an unattended run, a permission prompt is a hung worker, and a hung worker is misdiagnosed as a failed task. Nobody is at the keyboard to approve anything, so the worker sits silent until the wave gives up on it and the task gets marked failed-hung — for a reason that had nothing to do with the task. Provisioning happens in Phase 0, before the first wave, alongside wiring the git-guard; the two share a file and are done in the same sitting.

## The checklist

1. **Enumerate what THIS build will actually touch.** Walk the plan and list the capability classes in play: browser automation, `.env` reads/writes, package installs, docker, network fetches, deploy-adjacent commands, **`git push` to the run's configured remote** (Phase 7's push is the loop's one remote-touching command — a single command at the end of an epoch, which is exactly how it gets left off a surgical allowlist and then sits on a prompt with nobody awake; skip it only under `--push-remote never`) — **and the run's worktree location, which is a capability class even though it is not a tool.** Phase 4 places worktrees BESIDE the repo by default, so every worker reads and writes outside the project root; that grant is easy to miss precisely because nothing in the plan looks like a permission (its own section below). The allowlist is per-build, not a standing grant — a build that never opens a browser should not carry browser permissions.

2. **Write the allowlist into the project's gitignored `.claude/settings.local.json`** — the SAME file that carries the git-guard PreToolUse hook. One file, both jobs. Template below. **Write it on disk BEFORE the `claude` session for this run is launched** — not because a later file cannot load, but because everything the session does before it exists runs un-allowlisted and unguarded, and no later write un-runs that. A file *created* mid-session **does** load on 2.1.259, hooks included (re-measured; *What loads when* below — this file used to claim the opposite, measured on 2.1.252, and that claim rotted). So writing it mid-run is not the no-op it was once described as; it is simply not yet evidence. A run that discovers the file missing writes it, re-runs the step-4 guard probe, and then stops with a MECHANISM rather than a sentence: `swarmloop.py escalate --kind harness --blocking --subject .claude/settings.local.json --reason "settings.local.json did not exist at session start, so every tool call up to now ran un-allowlisted and unguarded" --proposed "confirm the step-4 guard probe blocks now that the file has been written; restart the session if it does not"`. `--blocking` exits 2, and that exit is the run's one terminal condition for autonomy (`references/goal-setting.md`) — it is what makes "this run has already dispatched unguarded" something the loop obeys instead of something it narrates on its way to dispatching anyway. Dispatching anyway means dispatching unguarded *and* prompt-prone.

3. **Dry-probe one call of each kind before dispatching wave 1:** an `Edit` to `.env`, one browser tool call, one `git push --dry-run <remote> main` to the configured remote (unless the run is `--push-remote never`), one of each allowed Bash pattern — **and two more the harness itself runs on: a write into `.swarm-loop/acceptance/`, and a `pytest` against that same file.** Those two are probed as a PAIR because they were measured to disagree: against a frozen test the permission classifier BLOCKED a heredoc write, ALLOWED the identical `Edit`, then BLOCKED a read-only `pytest` against it — mutation permitted, verification refused, which is the worst of the four possible outcomes and the one no single probe catches. The rule that follows is worth stating flat, because a green cycle report is where it otherwise gets lost: **a blocked verification is not a passed verification.** An amendment whose suite cannot be RUN is unverified, and that belongs in the freeze reason, recorded as unverified — never carried as a pass on the strength of the write having succeeded. A probe that prompts costs one click now; a wave that prompts costs a misdiagnosed task plus the time to notice it. No wave should be the first to discover a prompt.

4. **Probe the git-guard itself — step 3 structurally cannot see it.** Permissions and hooks load by different paths out of the same file, so a malformed `hooks` block, a `disableAllHooks: true` at a level that outranks this file, or a `hooks` entry nobody ever wrote all leave every probe in step 3 passing green with the guard never loaded. (A file created *after* session start used to be a fourth item on that list; on 2.1.259 it is not — it loads. See *What loads when* below.) The first thing that discovers it then is a worker running `git stash` in a shared tree. Two Bash calls, in this order, through the same tool surface the workers use:

   ```bash
   # call 1 — setup. Must NOT be blocked; a block here is a false positive, report it.
   d=$(mktemp -d) && git -C "$d" init -q && git -C "$d" commit -q --allow-empty -m probe && echo "$d"

   # call 2 — the probe, ALONE in its own Bash call: a block aborts the whole call,
   # so chaining it onto call 1 means the setup never runs.
   git -C <the path printed by call 1> stash
   ```

   **Long options are matched by git's own abbreviation rule in every checker**, so `--ha`, `--f` and `--for` are caught alongside `--hard`, `--force`. Coverage is bounded by the tokenizer by construction; this is a backstop, not an exact decision procedure.

**The commit in call 1 is load-bearing, and it is the half that gets skipped.** `git stash` in a repo with **no commits** fails on its own — *"You do not have the initial commit yet"* — and that refusal reads enough like a block to be scored as one, after which the run dispatches a mutating wave unguarded. With a commit and a clean tree an *unguarded* `git stash` instead exits 0 saying *"No local changes to save"*, which nobody mistakes for a refusal. **PASS is exactly one outcome: exit 2 whose text contains `git-guard`** (`BLOCKED (swarm-loop git-guard): ...`). Exit 0, `No local changes to save`, and git's own initial-commit error are each a **FAIL** — the guard is not loaded, no mutating wave dispatches, and the exit is a restarted session re-probed green, or the escalation in step 2. Clean up with `rm -rf "$d"`. Run the probe as a Bash **tool call**, never from inside a script file: the hook inspects the command string of the call, so `bash probe.sh` shows it only `bash probe.sh` and passes a `git stash` it never saw.

5. **Browser work has a second gate no settings file reaches:** the Chrome extension's own site-level permissions, granted in the extension itself beforehand — the localhost dev server at minimum. The settings file allows the TOOL; the extension allows the SITE. Miss the second gate and every browser call still stalls, allowlist or not.

## What loads when — re-measured on Claude Code 2.1.259

Earlier versions of this file and of `SKILL.md` asserted that **a settings file created after session start is never picked up**, stamped against 2.1.252. On **2.1.259** that is false in both halves. Both halves were re-measured on this machine, by an external process writing the file while the session under test was demonstrably alive and mid-prompt, with every timestamp produced by the session itself.

| | present AT session start | created or edited MID-session |
|---|---|---|
| bare tool name in `deny` (`"Write"`) | tool is **removed from context**: *"No such tool available: Write. Write is disabled for this session, in subagents as well as here."* | tool stays listed; the call is refused at the permission layer: *"Permission to use Write has been denied."* |
| scoped permission rule (`"Bash(rm:*)"`) | *"Permission to use Bash with command … has been denied."* | identical |
| `hooks` → `PreToolUse` entry | fires on every matching call | **fires on the very next matching call** |

**The rule that replaces the old one: the tool list is frozen at startup; permission evaluation and hook configuration are both live.** Both columns block — by different mechanisms, with different error text.

The hooks row is the half nobody had tested, and it is the half the git-guard depends on, so here is the method. A throwaway project with **no** `.claude/` at all; a session started in it; an external process that waits for a marker the session writes, then creates `.claude/settings.local.json` carrying a `PreToolUse` hook which logs every invocation and exits 2 on a sentinel command. Ordering was established three ways, not assumed: the session's own `ls -la .claude` printed *"No such file or directory"* twice before the write, the external writer logged its own `WROTE … mtime=` line, and the hook's log file — pre-created empty at setup, appended to only by the hook process itself — held **zero** entries before the write and one per tool call after it. Representative timeline, an interactive session:

```
00:01:02.811  session:  STEP1  ls -la .claude  ->  ls: .claude: No such file or directory
00:01:07.880  session:  STEP2  ls -la .claude  ->  ls: .claude: No such file or directory
00:01:14.412  EXTERNAL writer (pid 32359): WROTE .../.claude/settings.local.json size=292
00:01:16.228  hook process logs its own first invocation  (cmd: 'ts STEP4 ; echo DENY_A')
              session's tool result: "PreToolUse:Bash hook error: ... HOOKFIRE-A: blocked by probe hook A"
00:01:20.685  hook fires again on STEP5 -> blocked again
00:01:24.696  hook fires on STEP6 -> allowed (no sentinel in the command)
```

**11 valid session-runs, zero counterexamples.** Creation mid-session: 4 (2 headless, 2 interactive) on `settings.local.json`, 1 headless on `settings.json` — all denied. Mid-session *edit* of a file that existed at start: 2, both a clean swap (the pre-existing hook fired on the 3 calls before the write, the new one on all 5 after, the old one never again — replaced, not merged). Subagents dispatched *after* the mid-session write: 2, both guarded, which is the case that matters here because swarm-loop's workers are Agent-tool subagents. Positive control (hook present at session start): fires and denies. Negative control (no settings file ever written): the identical command succeeds, hook log empty. Two further attempts were **null runs** — one where a `send-keys` truncation meant the session never ran a step, one where the auto-mode classifier blocked the step that signalled the writer, so the file was never written at all. Neither is a negative result and neither is counted; they are recorded because a null run that gets scored as a negative is exactly how a claim like this goes wrong.

**Two things this does NOT license.** It is one version on one machine: `2.1.259`, macOS, project-level settings. And the official settings documentation still says the opposite — *"Do not edit hooks during an active session — changes take effect only when a new session starts"* — so the live-reload behavior is measured, not promised, and could be withdrawn without notice. Do not build anything on it that a probe could establish instead.

**Which is the whole point, and the reason this section is stamped.** The old claim was measured, stamped, correct when written, and silently false one minor version later; nothing announced it and no test caught it, so it kept justifying a precondition long after its mechanism had evaporated. **A version-stamped measurement that drifts is recoverable — the stamp tells you what to re-run. An unstamped one is not.** Every "measured against `<version>`" in this skill is a claim about a version you are probably not running. Re-probe rather than quote.

## What the guard's scope actually is — session, not global, not repo

Phase 0 is where the guard is provisioned, so this is where its real scope belongs. Getting it wrong in either direction is expensive: overestimate it and you dispatch an unguarded wave believing it is covered; underestimate it and you go hunting for a repo-level file that does not exist.

- **The guard is SESSION-scoped.** It is registered in exactly one place — the project's gitignored `.claude/settings.local.json`, as a `PreToolUse` entry with `matcher: "Bash|Edit|Write|NotebookEdit|MultiEdit"` (the template below) — and on 2.1.259 that file is **re-read live**, so the entry can appear, change or vanish mid-session (*What loads when* below); "loaded once, when the session starts" was true of 2.1.252 and is not true now. It is **not** installed globally and it is **not** a property of the repository: other projects are unaffected, and no file inside the repo makes a command guarded. What makes a command guarded is that it was issued by a Bash tool call from **this session** (subagents of this session included). Nothing else.
- **Therefore: a worktree has no `.claude/` of its own, and a `claude` process started with cwd inside a worktree runs COMPLETELY UNGUARDED.** A worktree carries only tracked files, and `.claude/settings.local.json` is gitignored, so there is nothing there to load — and being "inside the repo's worktree" confers nothing, because the guard was never repo-scoped to begin with. **Guarded status therefore depends entirely on workers being Agent-tool subagents of the parent session.** That is an assumption the whole isolation model rests on, so state it rather than leaving it implicit: if any part of the run spawns a *separate* `claude` process against a worktree — a nested launch, a terminal opened there by hand, a script that shells out to the CLI — that process is outside the guard and the isolation invariant has no backstop under it for the whole of its life.
- **The path rules are scoped to THIS RUN's trees, which makes them INERT until `init` has run.** A tree counts as the run's on three grounds and no others: the command is running in it, it is one of that tree's worktrees, or it carries `.swarm-loop/`. Before Phase 1 `init` creates that directory, a repo the command is not running in satisfies none of the three — re-measured both ways, `rm -rf <repo>` issued **from the repo's parent** is ALLOWED (rc=0) before `init` and BLOCKED (rc=2) the moment `.swarm-loop/` exists. The scoping is deliberate, not an oversight: resolving without it made every git repository on the machine undeletable, and a false block on a throwaway clone or a pytest fixture repo wedges an unattended run. Coverage therefore holds from `init` onward — which is every moment a worktree exists, and every moment the run has state worth protecting. Stated plainly here so nobody rediscovers the Phase 0 window as a hole and "fixes" it by widening the resolution.
- **The matcher is the other half of the scope, and it is no longer `Bash`-only.** The guard now carries path-based rules, so the matcher must route the file-writing tools as well: `Bash|Edit|Write|NotebookEdit|MultiEdit`, exactly as in the template below. **`MultiEdit` is the one that gets dropped** — the guard's `FILE_TOOLS` is `{Edit, Write, NotebookEdit, MultiEdit}`, so a matcher missing it leaves the widest file-writing tool unhooked. Verified against the live hook: an `Edit`/`Write`/`MultiEdit` payload with `file_path` on a ledger file, and a `NotebookEdit` with `notebook_path` on one, all exit 2; the same tools on an ordinary source path exit 0. What the file-tool path covers is **only the ledger** (`state.json`, `goals.json`, `manifest.json`, `history.csv`, `freeze-log.jsonl`, `escalations.jsonl`, `ESCALATIONS.md`) — a frozen acceptance test edited by `Edit` still passes the hook, and enforcement against *that* remains post-hoc via `verify` and `check-branch`. `Read` is not routed and never blocked.

## What the guard actually blocks — the probed enumeration

`SKILL.md` gives the three-family summary; this is the list, and every line below was re-probed against the shipped `hooks/git-guard.py` by feeding it a `PreToolUse` payload (`{"tool_name","cwd","tool_input"}`) from inside a real throwaway repo. **`cwd` matters** — the guard `chdir`s to the payload's `cwd` before asking git anything, so a probe run from the wrong directory classifies operands wrongly. Exit 2 = blocked, exit 0 = allowed.

**Blocked, git verbs.** `git stash` (push/save/pop/apply/drop/clear/branch/store, and bare; `list`/`show`/`create` are allowed) · `reset --hard`/`--merge`/`--keep` · `checkout -- <path>` · `checkout .` · `checkout <tree-ish> <path>` · `checkout <one operand git will not resolve as a ref that exists on disk>` and the shapes no refname can take (`:/`, `src/`, a glob) · `checkout --pathspec-from-file` · `checkout -f/--force` · `switch -f`/`--discard-changes` · `restore <path>` (anything not `--staged`-only) · `clean` with `-f/-d/-x` unless `--dry-run`/`-n` · `checkout-index -f/-a` · `read-tree -u` · `apply -R` · `git rm` over a repo-wide pathspec (`.`, `:/`, a bare glob) · `push --force` (**not** `--force-with-lease`) · `update-ref` · `reflog expire|delete` · `gc --prune=<not never>` · `prune` · `repack -ad` · `filter-branch` · `symbolic-ref <ref> <target>` · `submodule deinit -f` · `sparse-checkout set|add|init|reapply|disable`.

**Conflict-stage selection is BLOCKED, and this is the one that surprises people.** `git checkout --ours <file>`, `--theirs <file>`, `-2 <file>` and `-3 <file>` all exit 2: `check_checkout` has no conflict-stage case, the flags strip as options, and the file operand arrives as a lone non-ref pathspec. `--ours -- <file>` exits 2 on the `--` rule and `--ours .` on the `checkout .` rule. The working substitute is `git show :2:<file> > <file>` (ours) / `:3:` (theirs) then `git add` — both exit 0, verified end-to-end on a `UU` file. `references/merge-protocol.md` step 5 carries the full note.

**Worktrees.** `worktree add`, `list` and `prune` are allowed. **Plain `git worktree remove <path>` is ALLOWED** (exit 0) — it is the sanctioned teardown, and the guard's own block message points at it. `worktree remove --force` / `-f` (exit 2) and `worktree move` (exit 2) are blocked.

**Blocked, no git verb — paths.** `rm -r` whose target is the repo root, a worktree root, any directory *containing* a worktree root, `/`, or `$HOME` — including the collapsed form, so `rm -rf "$WT_ROOT/$ID"` with `$ID` unset is blocked (exit 2) because it reduces to the worktree parent. Any `rm` (recursive or not) or `mv` of a path under `.swarm-loop/`. `find <path under .swarm-loop> … -delete`. And in-place rewrites of the run **ledger** — `state.json`, `goals.json`, `manifest.json`, `history.csv`, `freeze-log.jsonl`, `escalations.jsonl`, `ESCALATIONS.md` — via `> `, `>>`, `truncate`, `sed -i`, `dd of=`, or `tee`. Non-Bash: an `Edit`/`Write`/`MultiEdit` `file_path` or a `NotebookEdit` `notebook_path` naming one of those seven.

**Allowed, and worth knowing so you do not route around a block that is not there.** `rm -rf node_modules`, `rm -rf /tmp/scratch-$USER`, `rm -rf "$V"` with `$V` unset (the shell hands `rm` an empty string and `rm` refuses on its own) · `cat > .swarm-loop/worker-preamble.md` and any other non-ledger file under `.swarm-loop/` — the redirect rule is scoped to the seven ledger names, not the directory · `cp` and `install` onto **any** path, ledger included: neither program is in the guard's non-git table, which is the gap the freeze-log recovery in `references/goal-setting.md` deliberately walks through · `Read` of anything.

**Not a set, and not exact.** Coverage is bounded by what the tokenizer can see as an argv, and the guard fails open on any parse or logic error. A heredoc body piped to a shell, `eval "$var"`, `python3 -c 'os.system("git stash")'`, a script file, and `$X stash` where `$X` does not mention git all pass through by design — widening any of them costs false blocks, and a false block wedges an unattended run. Re-probe rather than quoting this list at a version you have not checked.

## The worktree location is a permission scope

Phase 4 puts each worker's worktree at `../<repo>-worktrees/<task-id>` — a **sibling of the repo, outside the project root** — because a worktree inside the repo lands inside every root-walking metric command's scan. That fixes the measurement and creates a permission problem in the same move: `git worktree add` writes there, every worker then reads, edits and commits there, and `swarmloop.py retire` calls `git worktree remove` against it (**not** `rm -rf` — that form is guard-blocked, exit 2). A session scoped to the project root prompts on the first of those, which under this skill's premise is not one prompt — it is **every worker in the first mutating wave hung at once**, each one eventually marked failed-hung for a reason that has nothing to do with its task.

Three rules, in order:

1. **Grant the sibling directory BEFORE the session starts, alongside the repo itself.** Same precondition as the git-guard, and it stays a precondition — but not for the reason this file used to give. "Settings are read at session start" no longer holds in general (*What loads when* above), and **`additionalDirectories` specifically was NOT re-measured**, so treat a late grant as unproven rather than as known-good: grant it before the session, and let the probe in rule 2 be what says it worked. Add the worktree parent to `permissions.additionalDirectories` in `.claude/settings.local.json` (below), or launch with `--add-dir ../<repo>-worktrees`. Grant the **parent** directory once, not per-task-id — the ids are minted as the run goes, and a per-id grant is a prompt waiting for the task nobody predicted. Because the directory may not exist yet at session start, create it in Phase 0 rather than letting the first `git worktree add` create it.

2. **Probe it before dispatch; never assume the path is permitted.** Same doctrine as step 3 of the checklist — no wave should be the first to discover a prompt — and the probe is concrete because the failure is silent until a worker sits on it:

   ```bash
   git worktree add ../<repo>-worktrees/_probe -b task/_probe main
   echo probe > ../<repo>-worktrees/_probe/_probe.txt   # write
   cat ../<repo>-worktrees/_probe/_probe.txt            # read
   git worktree remove ../<repo>-worktrees/_probe && git branch -D task/_probe
   ```

   Run it through the same tool surface the workers will use, and watch for a prompt on **every** line — a grant that covers reads but not writes passes the first half and hangs the wave on the second. If any line prompts, the grant is wrong; fix it and re-probe. A probe that prompts costs one click now.

3. **If the parent directory cannot be granted, fall back in-tree — and say what that costs.** Use `.swarm-loop/worktrees/<task-id>`, one location for the whole run, written into every packet and both teardown steps. Understand what you have given up: the worktree-exclusion rule in `references/goal-setting.md` — excluding the worktree directory from every root-walking metric command — stops being belt-and-braces and becomes **the sole defense** for the frozen metrics. It is then load-bearing, and it is not checked by anything: the freeze smoke-run cannot catch a missing exclusion because no worktree exists at freeze time. Verify it by hand on the first cycle with a live worktree, and record in the cycle report that the run is on the fallback, so a later reader knows which single rule the metrics are resting on.

## Template — .claude/settings.local.json

One JSON document, permissions and git-guard together. JSON has no comments, so the per-project guidance lives here instead: the entries below are the illustrative baseline, not a complete set — add Bash patterns like `"Bash(npm run:*)"` and network grants like `"WebFetch(domain:api.example.com)"` for whatever step 1 enumerated, per project, never copied blind.

```json
{
  "permissions": {
    "additionalDirectories": ["../<repo>-worktrees"],
    "allow": [
      "Edit(.env)",
      "Edit(.env.*)",
      "Read(.env)",
      "Read(.env.*)",
      "Bash(git push:*)",
      "mcp__claude-in-chrome"
    ]
  },
  "disableAllHooks": false,
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|NotebookEdit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/skills/swarm-loop/hooks/git-guard.py\""
          }
        ]
      }
    ]
  }
}
```

`additionalDirectories` is what lets tools reach outside the project root; it is the sibling-worktree grant from the section above, and it is omitted only when the run is on the in-tree fallback. `"mcp__claude-in-chrome"` is a server-level grant — it covers every tool that browser server exposes, which is what an unattended browser task needs. `"Bash(git push:*)"` covers Phase 7's push and nothing else; drop it under `--push-remote never`. `"disableAllHooks": false` is pinned rather than left to the default: hook *entries* merge across settings levels, so nothing a worker writes can delete the git-guard entry — but a single `disableAllHooks: true` at any level that outranks this file switches every hook off at once, the guard included, and pinning it here is what makes that a conflict instead of a silent win.

## The blunter instruments

Launching the unattended session with `--permission-mode acceptEdits`, or with full permission bypass, also prevents prompts — by being broader than the allowlist, not smarter. Acceptable only when the repo is isolated and the user has accepted that tradeoff explicitly; the allowlist is the surgical default. Bypass does not remove the need for the git-guard: the guard blocks repo-global git ops that no permission mode would prompt on anyway, and PreToolUse hooks fire regardless of mode.

## Capability is not license

Provisioning grants CAPABILITY; the packet boundaries still govern what a worker may do with it. `.env` being editable without a prompt means the task that OWNS `.env` edits it friction-free — not that every worker may touch it. A permission entry never overrides an ownership list; violations are rejected at the merge gate exactly as before.

And `.env` stays gitignored; commit `.env.example` instead. The allowlist makes the secret file editable — and **nothing downstream makes it uncommittable.** The git-guard has path rules — the ledger, `.swarm-loop/`, the repo and worktree roots, all enumerated above — but **not one of them is keyed on what a commit or a push CONTAINS**, and that is the narrower claim to carry. Its git table is keyed on *subcommands* (`stash`, `reset`, `checkout`, `switch`, `restore`, `clean`, `worktree`, plus the plumbing equivalents) and it has no entry for `add` or `commit` at all. Its `push` entries are keyed on **two** things, and neither is the payload: most of them on force/delete *spellings* (`--force`, `--mirror`, `--delete`, `:<ref>`, `+<src>:<dst>`), and **one on repo STATE** — the only rule in the guard that reads the working tree to decide. That one refuses a push of the run's `main` while `<tree>/.swarm-loop` holds anything uncommitted, which is Phase 7's last push condition finally given a mechanism. It is still not a content check: it counts uncommitted paths under `.swarm-loop/` and never looks at what any commit contains, so a staged `.env` is neither counted nor noticed. Measured against the hook, one command string, three repos: `git add -f .env && git commit -m x && git push origin main` is **rc=0 with a clean ledger, rc=0 in a repo with no `.swarm-loop` at all, and rc=2 with a dirty ledger** — and that rc=2 is about the missing epoch evidence, not about the secret, so it is luck rather than protection. Whenever the ledger is clean the whole chain runs start to finish. The gitignore stops the accident (`git add .`); it does not stop `-f`, and the guard stops neither. The only thing between `.env` and the remote is a check somebody runs, so run it at both moments it can fire, and treat a match as a STOP:

```bash
# before every commit — is the secret staged?
git diff --cached --name-only | grep -qxF '.env' && echo 'REFUSE: .env is staged'
# before Phase 7's push — a push ships HEAD, not the index
git ls-tree -r HEAD --name-only | grep -qxF '.env' && echo 'REFUSE: .env is tracked at HEAD'
```

`grep` exits 0 on a MATCH, so silence is the pass and any output is the refusal — that polarity is what gets read backwards at 3am, which is why the message is printed rather than left implicit in an exit code. `-F` is not decoration: `grep -qx '.env'` reads the `.` as *any character* and fires on a file named `aenv`, and a gate with false stops in it is a gate that gets ignored. Run the same pair for every path this run's gitignore covers as a secret (`.env.*`, key files, service-account JSON). If either fires, the repair is `git rm --cached <path>` plus a `.gitignore` fix **before** the push; after the push the repair is a credential rotation, because a pushed secret is not un-pushed.
