#!/bin/sh
# SessionStart hook — surface the RECURRING entries of ~/.claude/mistakes/MISTAKES.md.
#
# Why this exists. The rules in that file were correct and were written down
# seven times over; the file simply is not loaded into any session, so nothing
# ever read them back. CLAUDE.md auto-loads and MISTAKES.md does not, which made
# the log a place corrections went to be forgotten. Only entries that have
# RECURRED are surfaced — a one-off does not earn space in every session's
# context, and a block that cries wolf gets skimmed, which is the failure this
# is trying to fix, one level up.
M="$HOME/.claude/mistakes/MISTAKES.md"
[ -f "$M" ] || exit 0
python3 - "$M" <<'PY'
import re, sys
txt = open(sys.argv[1], encoding="utf-8").read()
blocks = re.split(r'\n(?=## )', txt)
out = []
for b in blocks:
    if not b.startswith("## "):
        continue
    seen = re.search(r'\*\*Seen:\*\*(.*)', b)
    if not seen:
        continue
    n = sum(int(x) for x in re.findall(r'[×x](\d+)', seen.group(1)))
    if n < 2:
        continue
    title = b.split("\n", 1)[0][3:].strip()
    rule = re.search(r'\*\*Rule:\*\*\s*(.+)', b)
    out.append((n, title, rule.group(1).strip() if rule else ""))
if not out:
    sys.exit(0)
out.sort(key=lambda t: -t[0])
print("RECURRING MISTAKES — each of these has already been corrected more than once.")
print("They are recorded in ~/.claude/mistakes/MISTAKES.md, which nothing else reads back.\n")
for n, title, rule in out:
    print("- %s  (corrected %dx)" % (title, n))
    if rule:
        print("    %s" % rule)
PY
