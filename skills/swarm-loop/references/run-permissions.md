# Run Permissions — provisioning capability for unattended runs

The premise: in an unattended run, a permission prompt is a hung worker, and a hung worker is misdiagnosed as a failed task. Nobody is at the keyboard to approve anything, so the worker sits silent until the wave gives up on it and the task gets marked failed-hung — for a reason that had nothing to do with the task. Provisioning happens in Phase 0, before the first wave, alongside wiring the git-guard; the two share a file and are done in the same sitting.

## The checklist

1. **Enumerate what THIS build will actually touch.** Walk the plan and list the capability classes in play: browser automation, `.env` reads/writes, package installs, docker, network fetches, deploy-adjacent commands, **`git push` to the run's configured remote** (Phase 7's push is the loop's one remote-touching command — a single command at the end of an epoch, which is exactly how it gets left off a surgical allowlist and then sits on a prompt with nobody awake; skip it only under `--push-remote never`) — **and the run's worktree location, which is a capability class even though it is not a tool.** Phase 4 places worktrees BESIDE the repo by default, so every worker reads and writes outside the project root; that grant is easy to miss precisely because nothing in the plan looks like a permission (its own section below). The allowlist is per-build, not a standing grant — a build that never opens a browser should not carry browser permissions.

2. **Write the allowlist into the project's gitignored `.claude/settings.local.json`** — the SAME file that carries the git-guard PreToolUse hook. One file, both jobs. Template below. **The file must exist on disk BEFORE the `claude` session for this run is launched.** A file that already existed at session start picks up later edits live, but one *created* mid-session is never loaded and re-writing it does not help — there is no in-session recovery. A run that discovers the file missing writes it, then stops with a MECHANISM rather than a sentence: `swarmloop.py escalate --kind harness --blocking --subject .claude/settings.local.json --reason "settings.local.json did not exist at session start, so neither the allowlist nor the git-guard hook is loaded" --proposed "restart the session so the file loads, then re-run the Phase 0 probes"`. `--blocking` exits 2, and that exit is the run's one terminal condition for autonomy (`references/goal-setting.md`) — it is what makes "the session must be restarted" something the loop obeys instead of something it narrates on its way to dispatching anyway. Dispatching anyway means dispatching unguarded *and* prompt-prone.

3. **Dry-probe one call of each kind before dispatching wave 1:** an `Edit` to `.env`, one browser tool call, one `git push --dry-run <remote> main` to the configured remote (unless the run is `--push-remote never`), one of each allowed Bash pattern. A probe that prompts costs one click now; a wave that prompts costs a misdiagnosed task plus the time to notice it. No wave should be the first to discover a prompt.

4. **Probe the git-guard itself — step 3 structurally cannot see it.** Permissions and hooks load by different paths out of the same file, so a malformed `hooks` block, a `disableAllHooks: true` at a level that outranks this file, or a file created after session start all leave every probe in step 3 passing green with the guard never loaded. The first thing that discovers it then is a worker running `git stash` in a shared tree. Two Bash calls, in this order, through the same tool surface the workers use:

   ```bash
   # call 1 — setup. Must NOT be blocked; a block here is a false positive, report it.
   d=$(mktemp -d) && git -C "$d" init -q && git -C "$d" commit -q --allow-empty -m probe && echo "$d"

   # call 2 — the probe, ALONE in its own Bash call: a block aborts the whole call,
   # so chaining it onto call 1 means the setup never runs.
   git -C <the path printed by call 1> stash
   ```

   **The commit in call 1 is load-bearing, and it is the half that gets skipped.** `git stash` in a repo with **no commits** fails on its own — *"You do not have the initial commit yet"* — and that refusal reads enough like a block to be scored as one, after which the run dispatches a mutating wave unguarded. With a commit and a clean tree an *unguarded* `git stash` instead exits 0 saying *"No local changes to save"*, which nobody mistakes for a refusal. **PASS is exactly one outcome: exit 2 whose text contains `git-guard`** (`BLOCKED (swarm-loop git-guard): ...`). Exit 0, `No local changes to save`, and git's own initial-commit error are each a **FAIL** — the guard is not loaded, no mutating wave dispatches, and the exit is a restarted session re-probed green, or the escalation in step 2. Clean up with `rm -rf "$d"`. Run the probe as a Bash **tool call**, never from inside a script file: the hook inspects the command string of the call, so `bash probe.sh` shows it only `bash probe.sh` and passes a `git stash` it never saw.

5. **Browser work has a second gate no settings file reaches:** the Chrome extension's own site-level permissions, granted in the extension itself beforehand — the localhost dev server at minimum. The settings file allows the TOOL; the extension allows the SITE. Miss the second gate and every browser call still stalls, allowlist or not.

## The worktree location is a permission scope

Phase 4 puts each worker's worktree at `../<repo>-worktrees/<task-id>` — a **sibling of the repo, outside the project root** — because a worktree inside the repo lands inside every root-walking metric command's scan. That fixes the measurement and creates a permission problem in the same move: `git worktree add` writes there, every worker then reads, edits and commits there, and the cleanup step `rm -rf`s it. A session scoped to the project root prompts on the first of those, which under this skill's premise is not one prompt — it is **every worker in the first mutating wave hung at once**, each one eventually marked failed-hung for a reason that has nothing to do with its task.

Three rules, in order:

1. **Grant the sibling directory BEFORE the session starts, alongside the repo itself.** Same precondition as the git-guard, same reason: settings are read at session start. Add the worktree parent to `permissions.additionalDirectories` in `.claude/settings.local.json` (below), or launch with `--add-dir ../<repo>-worktrees`. Grant the **parent** directory once, not per-task-id — the ids are minted as the run goes, and a per-id grant is a prompt waiting for the task nobody predicted. Because the directory may not exist yet at session start, create it in Phase 0 rather than letting the first `git worktree add` create it.

2. **Probe it before dispatch; never assume the path is permitted.** Same doctrine as step 3 of the checklist — no wave should be the first to discover a prompt — and the probe is concrete because the failure is silent until a worker sits on it:

   ```bash
   git worktree add ../<repo>-worktrees/_probe -b task/_probe main
   echo probe > ../<repo>-worktrees/_probe/_probe.txt   # write
   cat ../<repo>-worktrees/_probe/_probe.txt            # read
   rm -rf ../<repo>-worktrees/_probe && git worktree prune && git branch -D task/_probe
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
        "matcher": "Bash",
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

And `.env` stays gitignored; commit `.env.example` instead. The allowlist makes the secret file editable — and **nothing downstream makes it uncommittable.** The git-guard has no path awareness of any kind: its check table is keyed on git *subcommands* (`stash`, `reset`, `checkout`, `switch`, `restore`, `clean`, `worktree`, plus the plumbing equivalents) and carries no entry for `add`, `commit` or `push`, so `git add -f .env && git commit -m x && git push origin main` runs start to finish — verified ALLOW against the hook. The gitignore stops the accident (`git add .`); it does not stop `-f`, and the guard stops neither. The only thing between `.env` and the remote is a check somebody runs, so run it at both moments it can fire, and treat a match as a STOP:

```bash
# before every commit — is the secret staged?
git diff --cached --name-only | grep -qxF '.env' && echo 'REFUSE: .env is staged'
# before Phase 7's push — a push ships HEAD, not the index
git ls-tree -r HEAD --name-only | grep -qxF '.env' && echo 'REFUSE: .env is tracked at HEAD'
```

`grep` exits 0 on a MATCH, so silence is the pass and any output is the refusal — that polarity is what gets read backwards at 3am, which is why the message is printed rather than left implicit in an exit code. `-F` is not decoration: `grep -qx '.env'` reads the `.` as *any character* and fires on a file named `aenv`, and a gate with false stops in it is a gate that gets ignored. Run the same pair for every path this run's gitignore covers as a secret (`.env.*`, key files, service-account JSON). If either fires, the repair is `git rm --cached <path>` plus a `.gitignore` fix **before** the push; after the push the repair is a credential rotation, because a pushed secret is not un-pushed.
