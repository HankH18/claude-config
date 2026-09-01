# Run Permissions — provisioning capability for unattended runs

The premise: in an unattended run, a permission prompt is a hung worker, and a hung worker is misdiagnosed as a failed task. Nobody is at the keyboard to approve anything, so the worker sits silent until the wave gives up on it and the task gets marked failed-hung — for a reason that had nothing to do with the task. Provisioning happens in Phase 0, before the first wave, alongside wiring the git-guard; the two share a file and are done in the same sitting.

## The checklist

1. **Enumerate what THIS build will actually touch.** Walk the plan and list the capability classes in play: browser automation, `.env` reads/writes, package installs, docker, network fetches, deploy-adjacent commands. The allowlist is per-build, not a standing grant — a build that never opens a browser should not carry browser permissions.

2. **Write the allowlist into the project's gitignored `.claude/settings.local.json`** — the SAME file that carries the git-guard PreToolUse hook. One file, both jobs. Template below.

3. **Dry-probe one call of each kind before dispatching wave 1:** an `Edit` to `.env`, one browser tool call, one of each allowed Bash pattern. A probe that prompts costs one click now; a wave that prompts costs a misdiagnosed task plus the time to notice it. No wave should be the first to discover a prompt.

4. **Browser work has a second gate no settings file reaches:** the Chrome extension's own site-level permissions, granted in the extension itself beforehand — the localhost dev server at minimum. The settings file allows the TOOL; the extension allows the SITE. Miss the second gate and every browser call still stalls, allowlist or not.

## Template — .claude/settings.local.json

One JSON document, permissions and git-guard together. JSON has no comments, so the per-project guidance lives here instead: the entries below are the illustrative baseline, not a complete set — add Bash patterns like `"Bash(npm run:*)"` and network grants like `"WebFetch(domain:api.example.com)"` for whatever step 1 enumerated, per project, never copied blind.

```json
{
  "permissions": {
    "allow": [
      "Edit(.env)",
      "Edit(.env.*)",
      "Read(.env)",
      "Read(.env.*)",
      "mcp__claude-in-chrome"
    ]
  },
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

`"mcp__claude-in-chrome"` is a server-level grant — it covers every tool that browser server exposes, which is what an unattended browser task needs.

## The blunter instruments

Launching the unattended session with `--permission-mode acceptEdits`, or with full permission bypass, also prevents prompts — by being broader than the allowlist, not smarter. Acceptable only when the repo is isolated and the user has accepted that tradeoff explicitly; the allowlist is the surgical default. Bypass does not remove the need for the git-guard: the guard blocks repo-global git ops that no permission mode would prompt on anyway, and PreToolUse hooks fire regardless of mode.

## Capability is not license

Provisioning grants CAPABILITY; the packet boundaries still govern what a worker may do with it. `.env` being editable without a prompt means the task that OWNS `.env` edits it friction-free — not that every worker may touch it. A permission entry never overrides an ownership list; violations are rejected at the merge gate exactly as before.

And `.env` stays gitignored; commit `.env.example` instead. The allowlist makes the secret file editable, not committable — the gitignore and the git-guard still stand between it and the remote.
