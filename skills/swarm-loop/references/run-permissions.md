# Run Permissions — provisioning capability for unattended runs

The premise: in an unattended run, a permission prompt is a hung worker, and a hung worker is misdiagnosed as a failed task. Nobody is at the keyboard to approve anything, so the worker sits silent until the wave gives up on it and the task gets marked failed-hung — for a reason that had nothing to do with the task. Provisioning happens in Phase 0, before the first wave, alongside wiring the git-guard; the two share a file and are done in the same sitting.

## The checklist

1. **Enumerate what THIS build will actually touch.** Walk the plan and list the capability classes in play: browser automation, `.env` reads/writes, package installs, docker, network fetches, deploy-adjacent commands, **`git push` to the run's configured remote** (Phase 7's push is the loop's one remote-touching command — a single command at the end of an epoch, which is exactly how it gets left off a surgical allowlist and then sits on a prompt with nobody awake; skip it only under `--push-remote never`) — **and the run's worktree location, which is a capability class even though it is not a tool.** Phase 4 places worktrees BESIDE the repo by default, so every worker reads and writes outside the project root; that grant is easy to miss precisely because nothing in the plan looks like a permission (its own section below). The allowlist is per-build, not a standing grant — a build that never opens a browser should not carry browser permissions.

2. **Write the allowlist into the project's gitignored `.claude/settings.local.json`** — the SAME file that carries the git-guard PreToolUse hook. One file, both jobs. Template below. **The file must exist on disk BEFORE the `claude` session for this run is launched.** A file that already existed at session start picks up later edits live, but one *created* mid-session is never loaded and re-writing it does not help — there is no in-session recovery. A run that discovers the file missing writes it, then says plainly that the session must be restarted before any mutating wave; dispatching anyway means dispatching unguarded *and* prompt-prone.

3. **Dry-probe one call of each kind before dispatching wave 1:** an `Edit` to `.env`, one browser tool call, one `git push --dry-run <remote> main` to the configured remote (unless the run is `--push-remote never`), one of each allowed Bash pattern. A probe that prompts costs one click now; a wave that prompts costs a misdiagnosed task plus the time to notice it. No wave should be the first to discover a prompt.

4. **Browser work has a second gate no settings file reaches:** the Chrome extension's own site-level permissions, granted in the extension itself beforehand — the localhost dev server at minimum. The settings file allows the TOOL; the extension allows the SITE. Miss the second gate and every browser call still stalls, allowlist or not.

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

And `.env` stays gitignored; commit `.env.example` instead. The allowlist makes the secret file editable, not committable — the gitignore and the git-guard still stand between it and the remote.
