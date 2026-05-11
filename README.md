# claude-config

My personal [Claude Code](https://claude.com/claude-code) config — versioned so I can re-create my setup on a new Mac in one `git clone`.

Only **three files** are tracked. Everything else under `~/.claude/` (session transcripts, history, policy-limit state, paste cache, telemetry, downloads, plugin caches) is excluded by an allowlist-default `.gitignore` so an accidental `git add .` can't leak anything sensitive.

## What's here

| File | What it does |
|---|---|
| `settings.json` | Claude Code top-level config. Hooks: `PreToolUse` runs a danger-guard, `PostToolUse` formats edited files, `Stop` runs tests when a turn ends. `statusLine` points at `statusline.sh`. |
| `statusline.sh` | Custom status-bar renderer. 8 segments, ANSI-colored, separator `│`. See below. |
| `.gitignore` | Allowlist default — `/*` ignores everything; `!/file` re-includes safe entries. |

## Status line

Composed left → right; segments are suppressed gracefully when their data isn't available:

```
user@host │ cwd │ git-branch │ ClaudeModel │ session-age │ ctx:% │ tokens-this-turn │ agents:N
```

| Segment | Color | Source | Notes |
|---|---|---|---|
| `user@host` | bold cyan | `whoami` + `hostname -s` | always renders |
| `cwd` | green | stdin `.cwd` | `~`-shortened; truncated to `.../parent/leaf` when deep |
| ` branch` | yellow | `git -C "$CWD" symbolic-ref --short HEAD` | hidden outside a repo |
| `Model` | magenta | stdin `.model.display_name` | strips `"Claude "` prefix |
| `age` | dim | transcript JSONL birth-time → first-line timestamp fallback | hidden until first turn |
| `ctx:%` | green/yellow/red @ 50%/20% | stdin `.context_window.remaining_percentage` | hidden before first API call |
| `tokens-this-turn` | cyan | last `type=assistant` JSONL line's `message.usage.{input,output}_tokens` | format `12.3K↑ 4.6K↓` |
| `agents:N` | blue | scans `/private/tmp/claude-*/*/*/tasks/*.output`, filters by `"type":"result"` and `mmin -5` | hidden when 0 |

To tweak, edit `statusline.sh` directly or ask Claude to update it via the `statusline-setup` agent.

## Reproduce on a new Mac

```bash
cd ~ && rm -rf .claude.bak && mv .claude .claude.bak 2>/dev/null || true
gh repo clone HankH18/claude-config ~/.claude
chmod +x ~/.claude/statusline.sh
```

That's it. Open Claude Code; the hooks, status line, and config are live.

## What's deliberately NOT tracked

The allowlist gitignore keeps the following out of git:

- `projects/` — full Claude Code session JSONL transcripts (every conversation, every tool call, every file you read or edited)
- `sessions/`, `history.jsonl` — session metadata and prompt history
- `policy-limits.json`, `mcp-needs-auth-cache.json` — rate-limit + auth state
- `paste-cache/`, `downloads/`, `cache/` — transient
- `backups/`, `file-history/`, `shell-snapshots/`, `session-env/` — transient/recovery state
- `telemetry/`, `plugins/`, `tasks/`, `plans/` — agent/plugin runtime state
- Any `*.bak.*` snapshot files

If you want to extend the tracked surface (custom agents, slash commands, hooks), uncomment the matching lines at the bottom of `.gitignore`. Audit anything you add — agent prompts in particular sometimes carry context you didn't intend to publish.

## License

Personal config — not licensed for redistribution. Fork freely for your own use.
