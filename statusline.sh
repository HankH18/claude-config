#!/usr/bin/env bash
# ~/.claude/statusline.sh
# Claude Code status line — compact single-line prompt
#
# Segments (in order):
#   1. user@hostname
#   2. current directory (~ for $HOME; last 2 components if deep)
#   3. git branch (only inside a git repo)
#   4. Claude model (short label from model.display_name)
#   5. session age  (wall-clock time since session start; derived from transcript mtime)
#   6. context window remaining %
#   7. tokens this turn  (input↑ output↓ from last assistant message in transcript)
#   8. active sub-agent count (only when > 0)
#
# Receives Claude Code JSON blob on stdin.

# ---------------------------------------------------------------------------
# ANSI colors (degrade gracefully — monochrome terminals just skip escapes)
# ---------------------------------------------------------------------------
RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
MAGENTA='\033[35m'
BLUE='\033[34m'
RED='\033[31m'

SEP=" $(printf '\033[2m')│$(printf '\033[0m') "

# ---------------------------------------------------------------------------
# Read JSON from stdin once
# ---------------------------------------------------------------------------
INPUT=$(cat)

# ---------------------------------------------------------------------------
# Segment 1: user@hostname
# ---------------------------------------------------------------------------
USER_NAME=$(whoami)
HOST_NAME=$(hostname -s)
seg_user="${BOLD}${CYAN}${USER_NAME}@${HOST_NAME}${RESET}"

# ---------------------------------------------------------------------------
# Segment 2: current directory
#   Use cwd from JSON; fall back to shell $PWD.
#   Replace $HOME prefix with ~.
#   Truncate to last 2 path components when depth > 2.
# ---------------------------------------------------------------------------
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
[ -z "$CWD" ] && CWD="$PWD"
# Replace home prefix
HOME_ESC="${HOME//\//\\/}"
CWD_DISPLAY="${CWD/#$HOME/\~}"
# Count path components (split on /); tilde counts as one component
IFS='/' read -ra PARTS <<< "$CWD_DISPLAY"
PART_COUNT="${#PARTS[@]}"
if [ "$PART_COUNT" -gt 2 ]; then
    LAST2="${PARTS[$((PART_COUNT-2))]}/${PARTS[$((PART_COUNT-1))]}"
    CWD_DISPLAY=".../${LAST2}"
fi
seg_dir="${GREEN}${CWD_DISPLAY}${RESET}"

# ---------------------------------------------------------------------------
# Segment 3: git branch (best-effort; silent on error)
# ---------------------------------------------------------------------------
GIT_BRANCH=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null)
if [ -n "$GIT_BRANCH" ]; then
    seg_git="${YELLOW} ${GIT_BRANCH}${RESET}"
else
    seg_git=""
fi

# ---------------------------------------------------------------------------
# Segment 4: model label
#   Prefer display_name; fall back to id; trim known long prefixes.
# ---------------------------------------------------------------------------
MODEL_DISPLAY=$(echo "$INPUT" | jq -r '.model.display_name // .model.id // "unknown"')
# Shorten common verbose names to compact labels
# e.g. "Claude Opus 4.7 (20251101)" -> "Opus 4.7"
#      "Claude Sonnet 4.5"           -> "Sonnet 4.5"
MODEL_LABEL=$(echo "$MODEL_DISPLAY" \
    | sed 's/^Claude[[:space:]]*//' \
    | sed 's/[[:space:]]*([^)]*)$//')
seg_model="${MAGENTA}${MODEL_LABEL}${RESET}"

# ---------------------------------------------------------------------------
# Segment 5: session age
#   Source: birth/creation time of the transcript JSONL file.
#   On macOS, `stat -f %SB -t %s` gives birth time as epoch seconds.
#   Fallback: parse the "ts" field of the first line in the JSONL.
#   If unavailable (no transcript, file missing, or birth time = 0), suppress.
# ---------------------------------------------------------------------------
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
seg_age=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    NOW=$(date +%s)
    START_EPOCH=0

    # macOS stat: -f %SB with -t %s returns birth time as Unix epoch
    # GNU stat does not support birth time reliably; we catch failure silently.
    if BIRTH=$(stat -f "%SB" -t "%s" "$TRANSCRIPT" 2>/dev/null); then
        # Verify it looks like an epoch (all digits, non-zero)
        case "$BIRTH" in
            [1-9][0-9]*) START_EPOCH="$BIRTH" ;;
        esac
    fi

    # Fallback: first line of JSONL — look for a "ts" (Unix float) or "timestamp" field
    if [ "$START_EPOCH" -eq 0 ] 2>/dev/null; then
        FIRST_LINE=$(head -1 "$TRANSCRIPT" 2>/dev/null)
        if [ -n "$FIRST_LINE" ]; then
            TS=$(printf '%s' "$FIRST_LINE" | jq -r '.ts // .timestamp // empty' 2>/dev/null)
            case "$TS" in
                [1-9][0-9]*) START_EPOCH=$(printf '%.0f' "$TS") ;;
            esac
        fi
    fi

    if [ "$START_EPOCH" -gt 0 ] 2>/dev/null; then
        ELAPSED=$(( NOW - START_EPOCH ))
        if [ "$ELAPSED" -lt 60 ]; then
            # Under a minute — show "<1m"
            AGE_STR="<1m"
        elif [ "$ELAPSED" -lt 3600 ]; then
            AGE_STR="$(( ELAPSED / 60 ))m"
        elif [ "$ELAPSED" -lt 86400 ]; then
            HRS=$(( ELAPSED / 3600 ))
            MINS=$(( (ELAPSED % 3600) / 60 ))
            if [ "$MINS" -gt 0 ]; then
                AGE_STR="${HRS}h${MINS}m"
            else
                AGE_STR="${HRS}h"
            fi
        else
            DAYS=$(( ELAPSED / 86400 ))
            HRS=$(( (ELAPSED % 86400) / 3600 ))
            if [ "$HRS" -gt 0 ]; then
                AGE_STR="${DAYS}d ${HRS}h"
            else
                AGE_STR="${DAYS}d"
            fi
        fi
        seg_age="${DIM}${AGE_STR}${RESET}"
    fi
fi

# ---------------------------------------------------------------------------
# Segment 6: context window remaining %
#   The JSON provides .context_window.remaining_percentage (pre-calculated).
#   Falls back to computing (context_window_size - input_tokens) / context_window_size
#   when the pre-calculated field is null (no messages sent yet).
# ---------------------------------------------------------------------------
REMAINING=$(echo "$INPUT" | jq -r '.context_window.remaining_percentage // empty')
if [ -n "$REMAINING" ]; then
    # Round to integer
    REMAINING_INT=$(printf '%.0f' "$REMAINING")
    # Color: green >= 50%, yellow >= 20%, red < 20%
    if [ "$REMAINING_INT" -ge 50 ]; then
        CTX_COLOR="$GREEN"
    elif [ "$REMAINING_INT" -ge 20 ]; then
        CTX_COLOR="$YELLOW"
    else
        CTX_COLOR="$RED"
    fi
    seg_ctx="${CTX_COLOR}ctx:${REMAINING_INT}%${RESET}"
else
    # No API call yet — skip segment silently
    seg_ctx=""
fi

# ---------------------------------------------------------------------------
# Segment 7: tokens this turn
#   Source: last assistant-type message in the transcript JSONL.
#   The JSONL lines have a "type" field; we want the last line where
#   .type == "assistant" (Claude Code transcript format).
#   Pull .message.usage.input_tokens and .message.usage.output_tokens.
#   Format: "12.3K↑ 4.5K↓"  (K = thousands, one decimal, suppress if zero)
# ---------------------------------------------------------------------------
seg_tokens=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    # Grab the last assistant line efficiently with grep + tail
    # Match both compact ("type":"assistant") and spaced ("type": "assistant") JSON
    LAST_ASST=$(grep -aE '"type"\s*:\s*"assistant"' "$TRANSCRIPT" 2>/dev/null | tail -1)
    if [ -n "$LAST_ASST" ]; then
        IN_TOK=$(printf '%s' "$LAST_ASST" | jq -r '.message.usage.input_tokens // empty' 2>/dev/null)
        OUT_TOK=$(printf '%s' "$LAST_ASST" | jq -r '.message.usage.output_tokens // empty' 2>/dev/null)
        # Only render if we got numeric values
        case "$IN_TOK" in
            ''|*[!0-9]*) IN_TOK="" ;;
        esac
        case "$OUT_TOK" in
            ''|*[!0-9]*) OUT_TOK="" ;;
        esac
        if [ -n "$IN_TOK" ] && [ -n "$OUT_TOK" ]; then
            # Format as K with one decimal: awk for float division
            IN_K=$(awk "BEGIN { printf \"%.1f\", $IN_TOK/1000 }")
            OUT_K=$(awk "BEGIN { printf \"%.1f\", $OUT_TOK/1000 }")
            seg_tokens="${CYAN}${IN_K}K↑ ${OUT_K}K↓${RESET}"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Segment 8: active sub-agent count
#
# Strategy: scan Claude's temp task directories.
# Claude Code writes per-task files at
#   /private/tmp/claude-<uid>/<encoded-project>/<session-uuid>/tasks/<task-id>.output
# (depth 5). A task is "active" if its .output file exists but the file's
# mtime is recent AND it has been touched within the last 5 minutes; we also
# rule out files whose JSONL transcript ends with an assistant `"stop_reason"`
# of `"end_turn"` (sub-agent completion). This is heuristic — if the layout
# changes the segment silently shows nothing rather than erroring.
# ---------------------------------------------------------------------------
AGENT_COUNT=0
# Use `find` to handle the variable directory depth; -mmin -5 keeps stale
# leftover files from counting. Quote so paths with spaces / special chars
# don't break us.
while IFS= read -r output_file; do
    [ -f "$output_file" ] || continue
    # Sub-agent result lines look like: `"type":"result"` — if absent the
    # agent is still streaming. Some sub-agents finalize with an Anthropic
    # stop_reason of "end_turn"; treat either marker as done.
    if grep -q -e '"type":"result"' -e '"stop_reason":"end_turn"' "$output_file" 2>/dev/null; then
        continue
    fi
    AGENT_COUNT=$((AGENT_COUNT + 1))
done < <(find /private/tmp/claude-* -maxdepth 4 -type f -name "*.output" -path "*/tasks/*" -mmin -5 2>/dev/null)

if [ "$AGENT_COUNT" -gt 0 ]; then
    seg_agents="${BLUE}agents:${AGENT_COUNT}${RESET}"
else
    seg_agents=""
fi

# ---------------------------------------------------------------------------
# Assemble the line
# ---------------------------------------------------------------------------
LINE=""
append() {
    local seg="$1"
    [ -z "$seg" ] && return
    if [ -z "$LINE" ]; then
        LINE="$seg"
    else
        LINE="${LINE}${SEP}${seg}"
    fi
}

append "$seg_user"
append "$seg_dir"
append "$seg_git"
append "$seg_model"
append "$seg_age"
append "$seg_ctx"
append "$seg_tokens"
append "$seg_agents"

printf "%b\n" "$LINE"
