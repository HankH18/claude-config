#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# swarm-loop — permanent regression battery for scripts/swarmloop.py
#
# Every case here corresponds to a defect that was found and fixed. A red line
# in this file means the defect came back; it does NOT mean the assertion is
# wrong. Do not weaken an assertion to make it green — fix swarmloop.py, or
# report the finding.
#
# Constraints this file deliberately honours:
#   * bash 3.2 (macOS system bash) — no associative arrays, no ${x^^},
#     no mapfile, no `readarray`.
#   * pure bash + stdlib python3. No pytest, no pip, no network.
#   * the skill is located by RELATIVE path, so the suite survives a clone or
#     a move to another machine.
#   * ONE mktemp -d of its own. Every throwaway git repo lives inside it.
#     Nothing outside that directory is ever written to or deleted.
#   * no `set -e`: the whole point is running commands that exit nonzero.
#
# Deliberately NOT ported from the original scratch suite (scratchpad/e2e/a5.sh):
# the orphan-reaping / Ctrl-C battery. It depends on an external helper script,
# and it reaps by `pkill -9 -f <pattern>` on the developer's own machine, which
# is not something a regression suite gets to do. Its two cost-free halves (the
# argparse defaults and the per-metric timeout validation) ARE ported below.
#
# Usage:  bash tests/test_harness.sh
#         SWARMLOOP_TESTS_KEEP=1 bash tests/test_harness.sh   # keep the temp dir
# Final line is exactly:  TEST_HARNESS: PASS=<n> FAIL=<n>
# Exit 0 iff FAIL==0.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SL="$SCRIPT_DIR/../scripts/swarmloop.py"
# Exported so a case may import swarmloop.py as a MODULE — used only where the
# assertion is about an attacker who has the same tooling the harness does.
SL_DIR="$SCRIPT_DIR/../scripts"
export SL_DIR

if [ ! -f "$SL" ]; then
  echo "test_harness: cannot find swarmloop.py at $SL" >&2
  echo "TEST_HARNESS: PASS=0 FAIL=1"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "test_harness: python3 not on PATH" >&2
  echo "TEST_HARNESS: PASS=0 FAIL=1"
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "test_harness: git not on PATH" >&2
  echo "TEST_HARNESS: PASS=0 FAIL=1"
  exit 1
fi

# --- hermetic-ish environment ----------------------------------------------
# Not required for correctness, but it keeps the developer's global git config
# (hooks paths, rename detection, templates) from steering the assertions.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_TERMINAL_PROMPT=0
export PYTHONDONTWRITEBYTECODE=1
export LC_ALL=C
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE CDPATH

TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarmloop-tests.XXXXXX")" || {
  echo "test_harness: mktemp -d failed" >&2
  echo "TEST_HARNESS: PASS=0 FAIL=1"
  exit 1
}
OUT="$TMP/.out"
: > "$OUT"

cleanup() {
  cd / 2>/dev/null || true
  [ "${SWARMLOOP_TESTS_KEEP:-}" = "1" ] && return 0
  case "$TMP" in
    */swarmloop-tests.*) rm -rf "$TMP" ;;   # belt and braces: never rm -rf $HOME
  esac
  return 0
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

START_TS=$(date +%s)
PASS=0
FAIL=0
RC=0

# --- assertion vocabulary ---------------------------------------------------
# Every assertion is (name, expected, actual). A failure prints all three plus
# the captured output, because a bare count is not debuggable at 3am.

_show_out() {
  if [ -s "$OUT" ]; then
    echo "         --- captured output (last 25 lines) ---"
    tail -25 "$OUT" | sed 's/^/         | /'
  else
    echo "         --- captured output: (empty) ---"
  fi
}

pass_() { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }

fail_() {  # fail_ <name> <expected> <actual>
  FAIL=$((FAIL + 1))
  printf '  FAIL  %s\n' "$1"
  printf '         expected: %s\n' "$2"
  printf '         actual  : %s\n' "$3"
  _show_out
}

ck() {  # ck <name> <expected> <actual>
  if [ "x$2" = "x$3" ]; then pass_ "$1"; else fail_ "$1" "$2" "$3"; fi
}

ck_exit() {  # ck_exit <name> <expected-code> <actual-code>
  if [ "x$2" = "x$3" ]; then pass_ "$1 (exit $3)"; else fail_ "$1" "exit $2" "exit $3"; fi
}

ck_has() {  # ck_has <name> <fixed-string>   (searched in $OUT)
  if grep -q -F -- "$2" "$OUT"; then
    pass_ "$1"
  else
    fail_ "$1" "output CONTAINS \"$2\"" "pattern absent from output"
  fi
}

ck_hasnt() {  # ck_hasnt <name> <fixed-string>
  if grep -q -F -- "$2" "$OUT"; then
    fail_ "$1" "output does NOT contain \"$2\"" \
          "PRESENT: $(grep -n -F -- "$2" "$OUT" | head -2 | tr '\n' ' ')"
  else
    pass_ "$1"
  fi
}

ck_file() {  # ck_file <name> <path>
  if [ -f "$2" ]; then pass_ "$1"; else fail_ "$1" "file exists: $2" "missing"; fi
}

ck_nofile() {  # ck_nofile <name> <path>
  if [ -f "$2" ]; then fail_ "$1" "file absent: $2" "still present"; else pass_ "$1"; fi
}

section() { echo; echo "== $1"; : > "$OUT"; }

# --- runners ----------------------------------------------------------------

sl() {  # run swarmloop; combined output in $OUT, exit code in $RC
  python3 "$SL" "$@" > "$OUT" 2>&1
  RC=$?
  return 0
}

gq() { git "$@" >/dev/null 2>&1; return 0; }   # quiet git

jget() {  # jget <json-file> <python expression over `d`>
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2" 2>&1
}

sha_of() {  # sha_of <path>  -> sha256 or MISSING
  python3 -c '
import hashlib, os, sys
p = sys.argv[1]
print(hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else "MISSING")' "$1"
}

set_state() {  # set_state <key> <python-literal>
  python3 -c '
import json, sys
p = ".swarm-loop/state.json"
s = json.load(open(p))
s[sys.argv[1]] = json.loads(sys.argv[2])
json.dump(s, open(p, "w"), indent=2)' "$1" "$2"
}

write_acceptance_test() {   # the pristine bytes mk_repo froze
  mkdir -p .swarm-loop/acceptance
  cat > .swarm-loop/acceptance/test_feature.py <<'EOF'
def test_feature():
    assert 1 + 1 == 2
EOF
}

write_goals() {   # the 2-metric harness: accept (maximize 100), lint (minimize 0)
  cat > .swarm-loop/goals.json <<'EOF'
{
  "frozen": false,
  "frozen_at": null,
  "metrics": [
    {"id": "accept", "name": "acceptance score", "command": "cat score.txt",
     "target": 100, "direction": "maximize"},
    {"id": "lint", "name": "lint errors", "command": "cat lint.txt | tail -1",
     "target": 0, "direction": "minimize", "timeout": 120}
  ]
}
EOF
}

mk_repo() {  # mk_repo <name>  — builds $TMP/<name> and cds into it
  local R="$TMP/$1"
  rm -rf "$R"
  mkdir -p "$R"
  cd "$R" || return 1
  gq init -q -b main .
  git config user.email "tests@swarm-loop.invalid"
  git config user.name "swarm-loop tests"
  git config commit.gpgsign false
  git config core.hooksPath /dev/null
  mkdir -p src
  echo "print('hi')" > src/app.py
  echo 40 > score.txt
  echo 3 > lint.txt
  gq add -A
  gq commit -qm seed
  python3 "$SL" init --max-cycles 6 --audit-loops 1 --stall-limit 3 --project e2e >/dev/null 2>&1
  mkdir -p .swarm-loop/reports
  write_acceptance_test
  write_goals
  return 0
}

echo "swarm-loop test harness"
echo "  skill under test : $(cd "$SCRIPT_DIR/.." && pwd)"
echo "  temp workspace   : $TMP"

# ===========================================================================
section "sanity: the harness builder itself"
mk_repo sanity
sl freeze
ck_exit "freeze a fresh 2-metric harness" 0 "$RC"
ck_has "freeze reports the protected file count" "frozen:"
sl verify
ck_exit "verify is green straight after freeze" 0 "$RC"

# ===========================================================================
# (a) stale verdict   (b) genuinely all-green   (A1b) partial-cycle resume
# (l) checkpoint replay idempotency
# ===========================================================================
section "analyze verdicts + checkpoint replay  [repo: rcycle]"
mk_repo rcycle
sl freeze
ck_exit "freeze" 0 "$RC"
gq add -A; gq commit -qm harness

sl measure --cycle 0
ck_exit "cycle 0 measure (red metrics)" 0 "$RC"
sl analyze --cycle 0
ck_exit "cycle 0 analyze on red metrics exits 0" 0 "$RC"
sl checkpoint --cycle 0
ck_exit "cycle 0 checkpoint" 0 "$RC"

# --- (b) a genuinely all-green cycle
echo "# real work" >> src/app.py
echo 100 > score.txt
echo 0 > lint.txt
gq add -A; gq commit -qm "make it green"
sl measure --cycle 1
ck_exit "cycle 1 measure (all green)" 0 "$RC"
sl analyze --cycle 1
ck_exit "(b) a genuinely all-green analyze exits 2" 2 "$RC"
ck_has "(b) ALL TARGETS MET is printed" "ALL TARGETS MET"
ck_hasnt "(b) no spurious STALE on a fully measured cycle" "STALE"
ck_hasnt "(b) no spurious MANUAL on a fully measured cycle" "MANUAL"
ck "(b) analysis json: all_targets_met / stale / manual" "True [] []" \
   "$(jget .swarm-loop/analysis/cycle-1.json "'%s %s %s' % (d['all_targets_met'], d['stale_metrics'], d['manual_records'])")"
sl checkpoint --cycle 1
ck_exit "cycle 1 checkpoint" 0 "$RC"

# --- (a) a metric with no row at the requested cycle
# lint's newest surviving row is cycle 1 == 0 == AT TARGET. A naive analyze
# therefore reports ALL TARGETS MET (exit 2) off a measurement that never ran.
rm -f lint.txt
sl measure --cycle 2
ck_exit "(a) measure with one broken metric command exits 1" 1 "$RC"
ck_has "(a) measure names the failing metric" "measure: 1 metric command(s) failed: lint"
sl analyze --cycle 2
ck_exit "(a) analyze on a partially measured cycle exits 1, NOT 2" 1 "$RC"
ck_has "(a) a STALE verdict is rendered" "STALE"
ck_has "(a) the stale metric is named" "NO MEASUREMENT AT CYCLE 2 for: lint"
ck_hasnt "(a) no false ALL TARGETS MET off a stale green row" "ALL TARGETS MET"
ck_has "(a) stale metric leads the priority list" "Priority for next cycle (worst first): lint"
ck "(a) analysis json records stale_metrics + withholds all_targets_met" "['lint'] False" \
   "$(jget .swarm-loop/analysis/cycle-2.json "'%s %s' % (d['stale_metrics'], d['all_targets_met'])")"

# --- resume flags the partial cycle
sl resume
ck_exit "resume runs on a partially measured cycle" 0 "$RC"
ck_has "resume marks cycle 2 partial with '!' in the m column" "cycle 2: !a--"
ck_has "resume explains the PARTIAL measurement" "PARTIALLY measured"

# --- (l) checkpoint replay must not move the stall counter
echo 0 > lint.txt
sl measure --cycle 2
ck_exit "(l) re-measure of cycle 2 succeeds once the command is fixed" 0 "$RC"
sl checkpoint --cycle 2
ck_exit "(l) first checkpoint of cycle 2" 0 "$RC"
S1="$(jget .swarm-loop/state.json "d['stall_count']")"
sl checkpoint --cycle 2; R_A=$RC
sl checkpoint --cycle 2; R_B=$RC
sl checkpoint --cycle 2; R_C=$RC
S2="$(jget .swarm-loop/state.json "d['stall_count']")"
ck "(l) replay #1 exit code" 0 "$R_A"
ck "(l) replay #2 exit code" 0 "$R_B"
ck "(l) replay #3 exit code" 0 "$R_C"
ck_has "(l) a replay says so out loud" "already checkpointed"
ck "(l) stall_count unchanged across 3 replays" "$S1" "$S2"
ck "(l) exactly one head_history entry for cycle 2" "1" \
   "$(jget .swarm-loop/state.json "len([h for h in d['head_history'] if h['cycle'] == 2])")"

# ===========================================================================
# (m) the kill switch fires on bookkeeping-only cycles
# ===========================================================================
section "(m) kill switch on changeless cycles  [repo: rkill]"
mk_repo rkill
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
ck_exit "(m) baseline measure" 0 "$RC"
sl checkpoint --cycle 0
ck_exit "(m) baseline checkpoint" 0 "$RC"
LAST_RC=0
for c in 1 2 3; do
  sl measure --cycle "$c"
  echo "report $c" > ".swarm-loop/reports/cycle-$c.md"
  gq add -A
  gq commit -qm "cycle $c: .swarm-loop bookkeeping only"
  sl checkpoint --cycle "$c"
  LAST_RC=$RC
done
ck_exit "(m) kill switch fires after 3 product-code-free cycles" 2 "$LAST_RC"
ck_has "(m) KILL SWITCH is printed" "KILL SWITCH"
# Both switches print "KILL SWITCH", and in this scenario BOTH counters reach
# 3/3 — the codebase one fires only because it is checked first. So the generic
# string no longer identifies which fired; assert this switch's own words, and
# assert the other one's are absent, or a precedence flip goes undetected.
ck_has "(m) and it is the CODEBASE switch, by its own message" "no codebase changes for"
ck_hasnt "(m) not the goal-progress switch, which is also at its limit here" \
   "no graded metric has improved"
ck "(m) stall_count reached the limit" "3" "$(jget .swarm-loop/state.json "d['stall_count']")"

# ===========================================================================
# (c) nan / inf rejected at freeze AND at measure
# ===========================================================================
section "(c) non-finite metric readings  [repo: rnan]"
mk_repo rnan
echo nan > score.txt
sl freeze
ck_exit "(c) freeze's smoke run rejects a nan-printing command" 1 "$RC"
ck_has "(c) freeze names the failing metric" "accept"
echo inf > score.txt
sl freeze
ck_exit "(c) freeze's smoke run rejects inf too" 1 "$RC"
echo 40 > score.txt
sl freeze
ck_exit "(c) freeze succeeds once the command prints a finite number" 0 "$RC"
gq add -A; gq commit -qm harness
echo nan > score.txt
sl measure --cycle 0
ck_exit "(c) measure rejects a nan reading" 1 "$RC"
ck_has "(c) measure says the reading was not a finite number" "FAILED to produce a finite number"
ck "(c) no nan row landed in history.csv" "0" "$(grep -c -i nan .swarm-loop/history.csv)"
echo inf > score.txt
sl measure --cycle 0
ck_exit "(c) measure rejects an inf reading" 1 "$RC"
ck "(c) no inf row landed in history.csv" "0" "$(grep -c -i -w inf .swarm-loop/history.csv)"

# ===========================================================================
# (d) git mv veto   (e) git rm veto   (f) legitimate src/ rename passes
# plus: __pycache__ is not a tamper, metric-config files are advisory only
# ===========================================================================
section "check-branch veto surface  [repo: rmv]"
mk_repo rmv
sl freeze
gq add -A; gq commit -qm harness

# --- (d) git mv smuggling a protected file out from under the veto
gq switch -q -c task/sneaky
gq mv .swarm-loop/acceptance/test_feature.py src/test_feature.py
gq commit -qm "relocate the acceptance test"
sl check-branch --branch task/sneaky
ck_exit "(d) 'git mv' of a frozen acceptance test VETOES the branch" 1 "$RC"
ck_has "(d) the veto names the SOURCE path, not just the destination" ".swarm-loop/acceptance/test_feature.py"
ck_has "(d) the veto banner is printed" "VETO: branch modifies the frozen goal harness"
gq switch -q main

# --- (e) a plain git rm of the same file
gq switch -q -c task/deleter
gq rm -q .swarm-loop/acceptance/test_feature.py
gq commit -qm "drop the acceptance test"
sl check-branch --branch task/deleter
ck_exit "(e) 'git rm' of a frozen acceptance test still VETOES the branch" 1 "$RC"
ck_has "(e) the deleted protected path is named" ".swarm-loop/acceptance/test_feature.py"
gq switch -q main

# --- (f) a rename entirely inside src/ is ordinary work
gq switch -q -c task/rename
gq mv src/app.py src/application.py
gq commit -qm "rename a module"
sl check-branch --branch task/rename
ck_exit "(f) a rename entirely inside src/ is NOT vetoed" 0 "$RC"
ck_has "(f) check-branch reports OK" "check-branch: OK"
ck_hasnt "(f) no veto banner on an ordinary rename" "VETO"
gq switch -q main

# --- __pycache__ written by running the frozen suite is not a tamper
mkdir -p .swarm-loop/acceptance/__pycache__
echo "bytes" > .swarm-loop/acceptance/__pycache__/test_feature.cpython-39.pyc
sl verify
ck_exit "verify ignores bytecode under a protected path" 0 "$RC"
gq switch -q -c task/pycache
echo "x = 1" > src/newmod.py
gq add -f src/newmod.py .swarm-loop/acceptance/__pycache__/test_feature.cpython-39.pyc
gq commit -qm "worker ran the frozen suite"
sl check-branch --branch task/pycache
ck_exit "__pycache__ under a protected path does NOT veto the branch" 0 "$RC"
ck_has "the bytecode path is reported as ignored, not as a change" "tool-cache/bytecode path(s) ignored"
gq switch -q main
rm -rf .swarm-loop/acceptance/__pycache__

# --- metric-config churn is advisory, never a veto
gq switch -q -c task/config
cat > pyproject.toml <<'EOF'
[tool.pytest.ini_options]
addopts = ""
EOF
echo "x = 2" > src/other.py
echo 55 > score.txt
gq add -A
gq commit -qm "scaffold config"
sl check-branch --branch task/config
ck_exit "metric-config churn is advisory, not a veto" 0 "$RC"
ck_has "pyproject.toml carries the ? advisory tag" "? pyproject.toml"
ck_has "score.txt (named in a metric command) carries the ? tag" "? score.txt"
ck_has "the advisory banner says it is not a veto" "ADVISORY (not a veto)"
gq switch -q main

# ===========================================================================
# (j) record is gated   (k) manual rows withhold all-targets-met
# ===========================================================================
section "(j)(k) manual 'record' gates  [repo: rrec]"
mk_repo rrec
sl record --cycle 0 --metric accept --value 100
ck_exit "(j) record is REFUSED before the harness is frozen" 1 "$RC"
ck_has "(j) the refusal names the freeze requirement" "not frozen"
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
ck_exit "(j) measure of cycle 0" 0 "$RC"

sl record --cycle 0 --metric accept --value 100
ck_exit "(k) record accepted post-freeze" 0 "$RC"
ck_has "(k) record marks the row as hand-entered" "marked source=record"
sl record --cycle 0 --metric lint --value 0
ck_exit "(k) second record accepted" 0 "$RC"

sl analyze --cycle 0
ck_exit "(k) analyze WITHHOLDS all-targets-met off hand-entered rows (exit 0, not 2)" 0 "$RC"
ck_has "(k) the table tags the manual rows" "(MANUAL)"
ck_has "(k) the manual rows are explained" "HAND-ENTERED VALUES"
ck_hasnt "(k) no ALL TARGETS MET off hand-entered rows" "ALL TARGETS MET"
ck "(k) analysis json records manual_records + withholds all_targets_met" "['accept', 'lint'] False" \
   "$(jget .swarm-loop/analysis/cycle-0.json "'%s %s' % (d['manual_records'], d['all_targets_met'])")"

sl analyze --cycle 0 --trust-manual-records
ck_exit "(k) --trust-manual-records restores the exit-2 terminal signal" 2 "$RC"
ck_has "(k) with --trust-manual-records the target is met" "ALL TARGETS MET"

echo "def test_smuggled(): pass" >> .swarm-loop/acceptance/test_feature.py
sl record --cycle 1 --metric accept --value 100
ck_exit "(j) record is REFUSED against a tampered harness" 1 "$RC"
ck_has "(j) the refusal names the integrity violation" "INTEGRITY VIOLATION"
write_acceptance_test
sl verify
ck_exit "(j) verify green again once the harness bytes are restored" 0 "$RC"

# ===========================================================================
# (g) re-freeze needs --amend   (h) init --force un-freezes   (i) restore-harness
# ===========================================================================
section "(g)(h)(i) freeze amendment, restore, re-init  [repo: ramend]"
mk_repo ramend
sl freeze
gq add -A; gq commit -qm harness
echo "def test_free(): assert True" >> .swarm-loop/acceptance/test_feature.py

sl verify
ck_exit "(g) verify catches the tamper" 1 "$RC"
ck_has "(g) verify points at restore-harness" "restore-harness"
sl freeze
ck_exit "(g) a bare re-freeze of a tampered harness is REFUSED" 1 "$RC"
sl freeze --amend ""
ck_exit "(g) --amend with an empty reason is a usage error (exit 3)" 3 "$RC"
sl freeze --amend "user approved widening the acceptance suite"
ck_exit "(g) --amend with a reason is ACCEPTED" 0 "$RC"
ck_has "(g) the amendment is announced as a moved goalpost" "GOALPOSTS MOVED"
ck_has "(g) the old->new hash diff is shown" "MODIFIED] .swarm-loop/acceptance/test_feature.py"
ck_has "(g) the unified diff of the changed bytes is shown" "+def test_free"
ck_has "(g) the freeze-log is named" "freeze-log.jsonl"
ck_file "(g) .swarm-loop/freeze-log.jsonl exists" ".swarm-loop/freeze-log.jsonl"
ck "(g) the freeze-log's last line is a JSON object naming the reason" "OK" \
   "$(python3 -c '
import json
lines = [l for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()]
rec = json.loads(lines[-1])
print("OK" if isinstance(rec, dict) and "widening" in json.dumps(rec) else "BAD: %r" % (rec,))' 2>&1)"
sl verify
ck_exit "(g) verify is green after the amendment" 0 "$RC"

gq add -A; gq commit -qm "amended harness"
sl measure --cycle 0
ck_exit "(g) measure after the amendment" 0 "$RC"
sl analyze --cycle 0
ck_has "(g) the amendment surfaces in the cycle analysis" "GOALPOSTS MOVED"

# --- (i) restore-harness repairs a tampered + deleted harness
echo "TAMPERED" >> .swarm-loop/acceptance/test_feature.py
rm -f .swarm-loop/goals.json
sl verify
ck_exit "(i) verify is red after tamper + delete" 1 "$RC"
sl restore-harness
ck_exit "(i) restore-harness succeeds" 0 "$RC"
sl verify
ck_exit "(i) verify is green after restore-harness" 0 "$RC"

# --- (h) init --force un-freezes so freeze works again
echo "final" > .swarm-loop/reports/final-report.md
sl init --max-cycles 4 --force
ck_exit "(h) init --force succeeds against a frozen run" 0 "$RC"
ck_has "(h) init --force says the goals were un-frozen" "un-frozen"
ck_nofile "(h) a stale final-report.md is archived away by --force" ".swarm-loop/reports/final-report.md"
ck "(h) goals.json is unfrozen and carries no amendment count" "False True" \
   "$(jget .swarm-loop/goals.json "'%s %s' % (d['frozen'], 'amendments' not in d)")"
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = 90
json.dump(g, open(p, "w"), indent=2)'
sl freeze
ck_exit "(h) freeze works again after init --force (new run, edited goals)" 0 "$RC"
ck_hasnt "(h) the fresh freeze is NOT reported as a goalpost amendment" "GOALPOSTS MOVED"

# ===========================================================================
# a metric already at target at freeze time: warn loudly, never block
# ===========================================================================
section "vacuous metric warning  [repo: rvac]"
mk_repo rvac
echo 0 > lint.txt
sl freeze
ck_exit "freeze still SUCCEEDS with a metric already at target" 0 "$RC"
ck_has "the already-at-target warning fires" "ALREADY AT TARGET"
ck_has "the vacuous-metric question is asked" "vacuous"

# ===========================================================================
# a symlinked directory under a protected path is a hard error
# ===========================================================================
section "symlinked directory under protection  [repo: rsym]"
mkdir -p "$TMP/sym-elsewhere"
echo "def test_hidden(): assert True" > "$TMP/sym-elsewhere/t.py"
mk_repo rsym
ln -s "$TMP/sym-elsewhere" .swarm-loop/acceptance/hidden
sl freeze
ck_exit "freeze REFUSES a symlinked dir under a protected path" 1 "$RC"
ck_has "the refusal explains why a symlinked dir is invisible" "symlinked directory under a protected path"
rm -f .swarm-loop/acceptance/hidden
sl freeze
ck_exit "freeze succeeds once the symlink is gone" 0 "$RC"
ln -s "$TMP/sym-elsewhere" .swarm-loop/acceptance/hidden
sl verify
ck_exit "verify flags a symlink that appears AFTER the freeze" 1 "$RC"
rm -f .swarm-loop/acceptance/hidden

# ===========================================================================
# history.csv: self-heals when missing, dies with a reason when headerless
# ===========================================================================
section "history.csv resilience  [repo: rhdr]"
mk_repo rhdr
sl freeze
gq add -A; gq commit -qm harness
rm -f .swarm-loop/history.csv
sl measure --cycle 0
ck_exit "measure self-heals a missing history.csv" 0 "$RC"
# csv.writer emits CRLF line terminators, so strip the CR before comparing.
ck "the self-healed file has the canonical header" \
   "cycle,metric_id,value,error,timestamp,source,selected,deselected" \
   "$(head -1 .swarm-loop/history.csv | tr -d '\r')"
sl analyze --cycle 0
ck_exit "analyze reads the self-healed file" 0 "$RC"
grep -v '^cycle,' .swarm-loop/history.csv > .swarm-loop/h2 && mv .swarm-loop/h2 .swarm-loop/history.csv
sl analyze --cycle 0
ck_exit "a genuinely headerless history.csv dies with exit 1" 1 "$RC"
ck_hasnt "a headerless history.csv produces NO raw traceback" "Traceback"
ck_has "the failure names history.csv" "history.csv"

# ===========================================================================
# resume: terminal conditions outrank metric colour
# ===========================================================================
section "resume terminal conditions  [repo: rterm]"
mk_repo rterm
sl freeze
gq add -A; gq commit -qm harness
echo 100 > score.txt
echo 0 > lint.txt
sl measure --cycle 0
ck_exit "all-green measure" 0 "$RC"
sl analyze --cycle 0
ck_exit "all-green analyze exits 2" 2 "$RC"
set_state stall_count 3
set_state stall_limit 3
sl resume
ck_exit "resume runs with a fired kill switch" 0 "$RC"
ck_has "a fired kill switch outranks all-green metrics" "kill switch already fired"
ck_hasnt "all-green is not the recommendation once the kill switch fired" "all targets met per latest"

set_state stall_count 0
set_state audit_loops 2
set_state audit_loops_completed 2
echo 40 > score.txt
echo 3 > lint.txt
sl measure --cycle 1
sl analyze --cycle 1
sl resume
ck_has "an exhausted audit budget outranks red metrics" "audit budget exhausted"

# ===========================================================================
# a half-written freeze is diagnosed rather than left mysterious
# ===========================================================================
section "half-frozen state  [repo: rhalf]"
mk_repo rhalf
sl freeze
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["frozen"] = False
g["frozen_at"] = None
json.dump(g, open(p, "w"), indent=2)'
sl measure --cycle 0
ck_exit "measure refuses when the frozen flag is down" 1 "$RC"
sl resume
ck_exit "resume runs against a half-frozen repo" 0 "$RC"
ck_has "resume diagnoses the half-written freeze" "freeze died between writing the manifest"

# ===========================================================================
# resume finds live worktrees in all three layouts
# ===========================================================================
section "worktree discovery  [repo: rwt]"
mk_repo rwt
sl freeze
gq add -A; gq commit -qm harness
gq worktree add -q "$TMP/rwt-worktrees/T-01" -b task/T-01 main
mkdir -p .swarm-loop/worktrees
gq worktree add -q .swarm-loop/worktrees/T-02 -b task/T-02 main
gq worktree add -q "$TMP/rwt-odd-location" -b task/T-03 main
sl resume
ck_exit "resume runs with live worktrees" 0 "$RC"
ck_has "sibling worktree found (current Phase 4 layout)" "rwt-worktrees/T-01"
ck_has "legacy in-tree worktree found" ".swarm-loop/worktrees/T-02"
ck_has "off-pattern worktree on a task/ branch still found" "rwt-odd-location"
gq worktree remove --force "$TMP/rwt-worktrees/T-01"
gq worktree remove --force .swarm-loop/worktrees/T-02
gq worktree remove --force "$TMP/rwt-odd-location"
gq worktree prune
sl resume
ck_has "resume reports zero worktrees once they are reclaimed" "live worktrees  : 0"

# ===========================================================================
# (n) escalate CHANGES NOTHING   (o) --blocking exits 2
# ===========================================================================
section "(n)(o) escalation is inert  [repo: resc]"
mk_repo resc
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
sl analyze --cycle 0
sl checkpoint --cycle 0

for f in goals.json manifest.json history.csv state.json; do
  echo "$f $(sha_of ".swarm-loop/$f")"
done > "$TMP/esc-before.txt"

sl escalate --kind frozen-goal --subject accept \
   --reason "the acceptance target cannot be reached without changing a frozen test" \
   --proposed "lower the accept target from 100 to 80"
ck_exit "(n) a non-blocking escalation exits 0" 0 "$RC"
ck_has "(n) the escalation is announced as awaiting a USER decision" "AWAITING USER DECISION"
ck_has "(n) filing is asking, never granting" "AN ESCALATION YOU FILED IS NOT AN AMENDMENT YOU WERE GRANTED."
ck_file "(n) escalations.jsonl was written" ".swarm-loop/escalations.jsonl"
ck_file "(n) the ESCALATIONS.md mirror was written" ".swarm-loop/ESCALATIONS.md"

for f in goals.json manifest.json history.csv state.json; do
  echo "$f $(sha_of ".swarm-loop/$f")"
done > "$TMP/esc-after.txt"
: > "$OUT"
for f in goals.json manifest.json history.csv state.json; do
  B="$(grep "^$f " "$TMP/esc-before.txt" | awk '{print $2}')"
  A="$(grep "^$f " "$TMP/esc-after.txt" | awk '{print $2}')"
  ck "(n) escalate left .swarm-loop/$f byte-identical" "$B" "$A"
done

sl escalate --list
ck_exit "(n) --list of a non-blocking escalation exits 0" 0 "$RC"
ck_has "(n) --list shows the open request" "ESC-001"

sl escalate --blocking --kind blocked-ticket --subject T-42 \
   --reason "the ticket depends on a decision only the user can make" \
   --proposed "decide whether T-42 is in scope"
ck_exit "(o) a --blocking escalation exits 2" 2 "$RC"
ck_has "(o) the blocking banner is printed" "[BLOCKING]"
ck_has "(o) the loop is told to stop" "no further progress is possible without a decision"

sl verify
ck_exit "(n) verify is still green after two escalations" 0 "$RC"
sl analyze --cycle 0
ck_exit "(n) analyze's exit code is unmoved by an open escalation" 0 "$RC"
ck_has "(n) analyze flags the disputed goal without changing its verdict" "DISPUTED GOALS"

# ===========================================================================
# (p) a blocking escalation outranks all-green metrics AND a fired kill switch
# ===========================================================================
section "(p) blocking escalation outranks everything below it  [repo: rblock]"
mk_repo rblock
sl freeze
gq add -A; gq commit -qm harness
echo 100 > score.txt
echo 0 > lint.txt
sl measure --cycle 0
ck_exit "(p) all-green measure" 0 "$RC"
sl analyze --cycle 0
ck_exit "(p) all-green analyze exits 2" 2 "$RC"
set_state stall_count 3
set_state stall_limit 3
sl resume
ck_has "(p) CONTROL: with no escalation the kill switch is the recommendation" \
       "recommendation  : kill switch already fired"
sl escalate --blocking --kind frozen-test --subject .swarm-loop/acceptance/test_feature.py \
   --reason "the frozen test asserts the wrong thing and no code change can satisfy it" \
   --proposed "replace the assertion, then re-freeze with an amendment"
ck_exit "(p) the blocking escalation exits 2" 2 "$RC"
sl resume
ck_exit "(p) resume runs" 0 "$RC"
ck_has "(p) the BLOCKING ESCALATION becomes the recommendation" \
       "recommendation  : BLOCKING ESCALATION open"
ck_hasnt "(p) it outranks the fired kill switch" "kill switch already fired"
ck_hasnt "(p) it outranks the all-green transition" "all targets met"
ck_has "(p) resume still reports the escalation loudly in the body" "1 OPEN (1 BLOCKING)"

# ===========================================================================
# (q) BACKWARD COMPAT: every command works with no escalations.jsonl at all
# ===========================================================================
section "(q) backward compatibility with pre-escalation runs  [repo: rcompat]"
mk_repo rcompat
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
sl analyze --cycle 0
sl checkpoint --cycle 0
sl escalate --kind other --subject scope \
   --reason "a decision is needed about scope" --proposed "confirm the scope"
ck_exit "(q) escalation filed (so the files definitely exist to delete)" 0 "$RC"
ck_file "(q) escalations.jsonl exists before the deletion" ".swarm-loop/escalations.jsonl"

rm -f .swarm-loop/escalations.jsonl .swarm-loop/ESCALATIONS.md
ck_nofile "(q) escalations.jsonl deleted" ".swarm-loop/escalations.jsonl"
ck_nofile "(q) ESCALATIONS.md deleted" ".swarm-loop/ESCALATIONS.md"

: > "$TMP/compat-all.txt"
sl status;                ck_exit "(q) status with no escalations.jsonl"     0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
sl resume;                ck_exit "(q) resume with no escalations.jsonl"     0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
ck_has  "(q) resume reports 'none open' rather than failing" "escalations     : none open"
sl verify;                ck_exit "(q) verify with no escalations.jsonl"     0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
sl measure --cycle 1;     ck_exit "(q) measure with no escalations.jsonl"    0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
sl analyze --cycle 1;     ck_exit "(q) analyze with no escalations.jsonl"    0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
ck_hasnt "(q) analyze reports no disputed goals once the log is gone" "DISPUTED GOALS"
sl checkpoint --cycle 1;  ck_exit "(q) checkpoint with no escalations.jsonl" 0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
sl escalate --list;       ck_exit "(q) escalate --list on a run that never filed one" 0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
sl escalate --open-asks;  ck_exit "(q) escalate --open-asks with no escalations.jsonl" 0 "$RC"; cat "$OUT" >> "$TMP/compat-all.txt"
cp "$TMP/compat-all.txt" "$OUT"
ck_hasnt "(q) not one traceback across every command" "Traceback"

# ===========================================================================
# (r) THE END-OF-TURN BLOCK
#
# The defect: a non-blocking escalation is filed, the loop is told to keep
# building, and the ask sits in a file the user never opens while hours of
# output scroll past it. The ask had a durable home and no path to the eyes of
# the one person allowed to answer it — this project's recurring failure class
# (a mechanism with no red path) in a new hat.
#
# So `escalate --open-asks` ends every user-facing turn, and the property that
# makes a rule like that followable is SILENCE when nothing is waiting. Both
# halves are pinned below: a command that always printed nothing would satisfy
# the silence assertion on its own, so every negative here has a positive
# counterpart that proves it prints when it should.
# ===========================================================================
section "(r) --open-asks: the end-of-turn block  [repo: rasks]"
mk_repo rasks
sl freeze
gq add -A; gq commit -qm harness

sl escalate --open-asks
ck_exit "(r) --open-asks with nothing open exits 0" 0 "$RC"
ck "(r) --open-asks with nothing open prints NOTHING AT ALL" "" "$(cat "$OUT")"

# POSITIVE COUNTERPART to the silence above.
sl escalate --kind frozen-goal --subject accept \
   --reason "the accept target cannot be reached without editing a frozen test" \
   --proposed "lower the accept target from 100 to 80"
ck_exit "(r) a non-blocking ask is filed" 0 "$RC"
sl escalate --open-asks
ck_exit "(r) --open-asks with one non-blocking ask exits 0" 0 "$RC"
ck_has "(r) the open ask is printed"                     "ESC-001"
ck_has "(r) ... naming what it is about"                 "frozen-goal"
ck_has "(r) ... what was asked"                          "the accept target cannot be reached"
ck_has "(r) ... the specific change proposed"            "lower the accept target from 100 to 80"
ck_has "(r) ... and the command that closes it"          "escalate --resolve ESC-001"
ck_hasnt "(r) a non-blocking ask is not labelled BLOCKING" "[BLOCKING]"

# the block is a READ: printing what is waiting changes nothing
for f in goals.json manifest.json history.csv state.json escalations.jsonl ESCALATIONS.md; do
  echo "$f $(sha_of ".swarm-loop/$f")"
done > "$TMP/asks-before.txt"
sl escalate --open-asks
for f in goals.json manifest.json history.csv state.json escalations.jsonl ESCALATIONS.md; do
  echo "$f $(sha_of ".swarm-loop/$f")"
done > "$TMP/asks-after.txt"
: > "$OUT"
for f in goals.json manifest.json history.csv state.json escalations.jsonl ESCALATIONS.md; do
  B="$(grep "^$f " "$TMP/asks-before.txt" | awk '{print $2}')"
  A="$(grep "^$f " "$TMP/asks-after.txt"  | awk '{print $2}')"
  ck "(r) --open-asks left .swarm-loop/$f byte-identical" "$B" "$A"
done

# an unanswered ask comes back EVERY turn — that is the whole mechanism
sl escalate --open-asks
ck_has "(r) the unresolved ask is still there on the second call" "ESC-001"
sl escalate --open-asks
ck_has "(r) ... and on the third"                                 "ESC-001"

# blocking is distinguished, loudly, and it is not a failure
sl escalate --blocking --kind blocked-ticket --subject T-42 \
   --reason "the ticket needs a scope call only the user can make" \
   --proposed "decide whether T-42 is in scope"
ck_exit "(r) the blocking ask exits 2 when filed" 2 "$RC"
sl escalate --open-asks
ck_exit "(r) --open-asks exits 2 while a BLOCKING ask is open" 2 "$RC"
ck_has "(r) the blocking ask is marked"                     "[BLOCKING]"
ck_has "(r) it says the run is waiting on a human"          "WAITING ON A HUMAN"
ck_has "(r) ... which is neither done nor failed"           "neither done nor failed"
ck_has "(r) the non-blocking ask is still listed alongside" "ESC-001"

# ORDER: newest first, but blocking above everything regardless of age. The
# third ask is filed AFTER the blocking one precisely so "newest first" alone
# cannot pass this.
sl escalate --kind other --subject scope \
   --reason "a scope decision is needed and nothing blocks on it" \
   --proposed "confirm the scope"
ck_exit "(r) a third, newer, non-blocking ask is filed" 0 "$RC"
sl escalate --open-asks
ck_exit "(r) --open-asks still exits 2 with three open and one blocking" 2 "$RC"
ck "(r) the BLOCKING ask prints first though it is NOT the newest" \
   "ESC-002" "$(grep -o 'ESC-00[0-9]' "$OUT" | uniq | sed -n 1p)"
ck "(r) ... then the newest non-blocking one" \
   "ESC-003" "$(grep -o 'ESC-00[0-9]' "$OUT" | uniq | sed -n 2p)"
ck "(r) ... then the oldest" \
   "ESC-001" "$(grep -o 'ESC-00[0-9]' "$OUT" | uniq | sed -n 3p)"
ck_has "(r) the count is stated so the block cannot be skimmed past" "3 awaiting YOUR decision"

# RESOLUTION. An agent may ask; only the user may grant — so an ask cannot be
# closed without a recorded decision.
sl escalate --resolve ESC-001
ck_exit "(r) --resolve with no --note is refused" 3 "$RC"
ck_has "(r) ... because an undecided close is indistinguishable from a retirement" \
       "retiring its own unanswered question"
sl escalate --open-asks
ck_has "(r) the refused resolve left the ask OPEN" "ESC-001"

sl escalate --resolve ESC-001 --note "declined - the accept target stands"
ck_exit "(r) --resolve with a recorded decision is accepted" 0 "$RC"
sl escalate --open-asks
ck_hasnt "(r) a resolved ask stops appearing in the block" "ESC-001"
ck_has   "(r) ... while the still-open blocking ask keeps appearing" "ESC-002"

# WITHDRAWAL. The loop may retract a question that became moot. It may never
# retract a BLOCKING one — that would be the loop answering itself.
sl escalate --withdraw ESC-002 --note "not relevant any more"
ck_exit "(r) the loop may NOT withdraw a blocking ask" 1 "$RC"
ck_has "(r) ... and is told why"                      "the loop may not withdraw it"
sl escalate --open-asks
ck_exit "(r) the blocking ask survived the withdrawal attempt" 2 "$RC"
ck_has "(r) ... and is still printed"                          "ESC-002"

sl escalate --resolve ESC-002 --note "granted - T-42 is out of scope"
ck_exit "(r) the user's decision closes the blocking ask" 0 "$RC"
sl escalate --open-asks
ck_exit "(r) with the blocking ask decided the block exits 0 again" 0 "$RC"
ck_has "(r) ... and the third ask is still waiting" "ESC-003"

sl escalate --withdraw ESC-003
ck_exit "(r) --withdraw with no --note is refused" 3 "$RC"
sl escalate --open-asks
ck_has "(r) the refused withdrawal left the ask OPEN" "ESC-003"
sl escalate --withdraw 3 --note "route pivoted; nothing depends on the answer now"
ck_exit "(r) a non-blocking ask may be withdrawn WITH a reason" 0 "$RC"
ck_has "(r) withdrawing is recorded as never answered" "NEVER ANSWERED"
sl escalate --open-asks
ck_exit "(r) a withdrawn ask stops appearing, and the block exits 0" 0 "$RC"
ck "(r) ... leaving it silent again" "" "$(cat "$OUT")"

sl escalate --list --all
ck_has "(r) the withdrawal is on the permanent record" "WITHDRAWN, never answered,"
: > "$OUT"; cat .swarm-loop/ESCALATIONS.md > "$OUT"
ck_has "(r) the mirror labels it a retraction, not a decision" "WITHDRAWN by the loop"
ck_has "(r) ... and says nobody answered it" "NEVER ANSWERED. The loop withdrew the question"

sl escalate --resolve ESC-001 --withdraw ESC-001 --note "both at once"
ck_exit "(r) --resolve and --withdraw together are refused" 3 "$RC"

# ===========================================================================
# argparse defaults + per-metric timeout handling (the cost-free half of A5)
# ===========================================================================
section "timeout defaults and per-metric overrides  [repo: rtmo]"
: > "$OUT"
DEFAULTS="$(python3 - "$SL" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
found = re.findall(r'add_argument\("--(timeout|smoke-timeout)", type=int, default=(\d+)', src)
print(" ".join("%s=%s" % t for t in sorted(found)))
PY
)"
ck "measure --timeout and freeze --smoke-timeout both default to 2400" \
   "smoke-timeout=2400 timeout=2400" "$DEFAULTS"

mk_repo rtmo
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "slow", "name": "slow metric",
                 "command": "python3 -c \"import time; time.sleep(60)\"",
                 "target": 0, "direction": "minimize", "timeout": 2}]
json.dump(g, open(p, "w"), indent=2)'
sl freeze --skip-smoke
ck_exit "freeze --skip-smoke on a deliberately slow metric" 0 "$RC"
gq add -A; gq commit -qm harness
sl measure --cycle 0 --timeout 900
ck_exit "measure exits 1 when the per-metric timeout fires" 1 "$RC"
ck_has "the per-metric timeout (2s) beats the CLI default (900s)" "FAILED: timeout after 2s"

python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["timeout"] = "banana"
g["frozen"] = False
json.dump(g, open(p, "w"), indent=2)'
sl freeze --skip-smoke
ck_exit "freeze REJECTS a malformed per-metric timeout" 1 "$RC"
ck_has "the rejection says the timeout must be a positive integer" \
       "timeout must be a positive integer number of seconds"

# ===========================================================================
# A1 · manifest.json is the trust root, so something must authenticate IT
# (P-2 / H-31: recompute one digest and verify said "intact" forever)
# ===========================================================================
section "A1 manifest authentication  [repo: rauth]"
mk_repo rauth
sl freeze
gq add -A; gq commit -qm harness
sl verify
ck_exit "A1 control: verify green on an untampered run" 0 "$RC"
ck_has "A1 verify says what it reconciled the manifest against" "freeze-log.jsonl"

echo "def test_weakened(): pass" >> .swarm-loop/acceptance/test_feature.py
python3 - <<'PY'
import hashlib, json
p = ".swarm-loop/manifest.json"
m = json.load(open(p))
t = ".swarm-loop/acceptance/test_feature.py"
m["files"][t] = hashlib.sha256(open(t, "rb").read()).hexdigest()
json.dump(m, open(p, "w"), indent=2)
PY
sl verify
ck_exit "A1 a recomputed manifest digest is CAUGHT (used to read 'intact' forever)" 1 "$RC"
ck_has "A1 the forged digest is named as such" "[FORGED]"
ck_has "A1 the forgery names the file it covers for" "test_feature.py"
sl measure --cycle 0
ck_exit "A1 measure refuses to score against a forged manifest" 1 "$RC"

# a manifest claiming a freeze the log never recorded
write_acceptance_test
python3 - <<'PY'
import json
p = ".swarm-loop/manifest.json"
m = json.load(open(p))
import hashlib
t = ".swarm-loop/acceptance/test_feature.py"
m["files"][t] = hashlib.sha256(open(t, "rb").read()).hexdigest()
m["frozen_at"] = "2099-01-01T00:00:00"
json.dump(m, open(p, "w"), indent=2)
PY
sl verify
ck_exit "A1 a manifest whose frozen_at is in no freeze-log record is CAUGHT" 1 "$RC"
ck_has "A1 the unlogged freeze is named" "[UNLOGGED]"

# rewriting the append-only log is caught by git
git show HEAD:.swarm-loop/manifest.json > .swarm-loop/manifest.json
sl verify
ck_exit "A1 control: verify green again once the manifest is restored" 0 "$RC"
echo '{"action":"initial","amendment":0,"frozen_at":"2099-02-02T00:00:00"}' > .swarm-loop/freeze-log.jsonl
sl verify
ck_exit "A1 a REWRITTEN append-only freeze log is caught" 1 "$RC"
git show HEAD:.swarm-loop/freeze-log.jsonl > .swarm-loop/freeze-log.jsonl
rm -f .swarm-loop/freeze-log.jsonl
sl verify
ck_exit "A1 a DELETED freeze log is an integrity violation, not a missing nicety" 1 "$RC"
ck_has "A1 the deleted log is named" "freeze-log.jsonl"

# ===========================================================================
# A2 / P-11 · the crash-recovery hatch is not forgeable, and --amend is
# never silently downgraded to an unrecorded initial freeze
# ===========================================================================
section "A2 the forgeable crash hatch  [repo: rlaunder]"
mk_repo rlaunder
sl freeze
gq add -A; gq commit -qm harness
LAUNDER_SHA="$(sha_of .swarm-loop/acceptance/test_feature.py)"

# the four-step laundering sequence, verbatim from the finding
echo "def test_weakened(): pass" >> .swarm-loop/acceptance/test_feature.py
rm -f .swarm-loop/freeze-log.jsonl
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p)); g["frozen"] = False
json.dump(g, open(p, "w"), indent=2)'
: > .swarm-loop/goals.json.tmp
sl freeze
ck_exit "A2 the four-step laundering sequence is REFUSED" 1 "$RC"
ck_has "A2 the missing freeze log is named as the violation" "is GONE"
ck_hasnt "A2 nothing was locked in over the tampered bytes" "goals and acceptance tests are now locked"
ck_nofile "A2 the refused freeze wrote no fresh 'initial' record" ".swarm-loop/freeze-log.jsonl"

sl freeze --amend "user approved widening the acceptance suite"
ck_exit "P-11 --amend over a destroyed freeze log is REFUSED too" 1 "$RC"
ck_hasnt "P-11 the approved reason is not silently downgraded to an initial freeze" "amendment #"

git show HEAD:.swarm-loop/freeze-log.jsonl > .swarm-loop/freeze-log.jsonl
sl freeze
ck_exit "A2 with the log back, a zero-byte goals.json.tmp is not crash evidence" 1 "$RC"
ck_has "A2 the refusal is the goalpost gate" "REFUSED"

# a GENUINE half-written freeze must still re-freeze: the hatch is narrowed,
# not sealed. (manifest written, log line never appended, staged .tmp present)
python3 - <<'PY'
import json
g = json.load(open(".swarm-loop/goals.json"))
staged = dict(g); staged["frozen"] = True; staged["frozen_at"] = "2099-03-03T00:00:00"
json.dump(staged, open(".swarm-loop/goals.json.tmp", "w"), indent=2)
g["frozen"], g["frozen_at"] = False, None
json.dump(g, open(".swarm-loop/goals.json", "w"), indent=2)
m = json.load(open(".swarm-loop/manifest.json")); m["frozen_at"] = "2099-03-03T00:00:00"
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
sl freeze
ck_exit "A2 a GENUINE half-written freeze still re-freezes (hatch open where it should be)" 0 "$RC"

section "P-11 --amend on a never-frozen repo records the reason  [repo: ramdfresh]"
mk_repo ramdfresh
sl freeze --amend "user approved this harness before the first freeze"
ck_exit "P-11 --amend on a never-frozen repo is an INITIAL freeze (exit 0)" 0 "$RC"
ck_has "P-11 it says the reason went onto the record" "recorded on the initial freeze-log record"
ck "P-11 the approved reason really is in the freeze log" "OK" \
   "$(python3 -c '
import json
rec = json.loads([l for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()][0])
print("OK" if "user approved this harness" in (rec.get("reason") or "") else "BAD: %r" % rec.get("reason"))' 2>&1)"

# ===========================================================================
# A3 / A4 · measure and analyze assert integrity; a deleted manifest fails
# CLOSED for every caller, resume included
# ===========================================================================
section "A3/A4 the scoreboard commands are gated  [repo: rgate]"
mk_repo rgate
sl freeze
gq add -A; gq commit -qm harness
echo 100 > score.txt
echo 0 > lint.txt
sl measure --cycle 0
ck_exit "A3 control: measure on an intact harness" 0 "$RC"
sl analyze --cycle 0
ck_exit "A3 control: an intact all-green analyze still exits 2" 2 "$RC"
ck_has "A3 analyze STATES the integrity result beside the verdict" "Harness integrity at analysis time"
ck_has "A3 ALL TARGETS MET is still reachable on a clean harness" "ALL TARGETS MET"

echo "def test_weakened(): pass" >> .swarm-loop/acceptance/test_feature.py
sl measure --cycle 1
ck_exit "A3 measure REFUSES against a weakened frozen test" 1 "$RC"
ck_has "A3 measure names the integrity violation" "INTEGRITY VIOLATION"
sl analyze --cycle 0
ck_exit "A3 analyze REFUSES against a weakened frozen test" 1 "$RC"
ck_hasnt "A3 no ALL TARGETS MET off a tampered harness" "ALL TARGETS MET"

write_acceptance_test
rm -f .swarm-loop/manifest.json
sl resume
ck_exit "A4 resume still runs with the manifest deleted" 0 "$RC"
ck_has "A4 a deleted manifest reads as VIOLATED for resume too" "harness VIOLATED"
ck_hasnt "A4 resume no longer contradicts itself with 'harness not frozen'" "harness not frozen"
ck_has "A4 the recommendation is to restore, not to dispatch" "restore the frozen harness"
sl analyze --cycle 0
ck_exit "A4 analyze fails closed on a deleted manifest" 1 "$RC"

# ===========================================================================
# A5 · a sourceless .pyc OUTSIDE a cache dir is importable code, so freeze
# hashes it, verify flags it and check-branch vetoes it — while a legitimate
# __pycache__/*.pyc still passes all three
# ===========================================================================
section "A5 sourceless bytecode under protection  [repo: rpyc]"
mk_repo rpyc
printf 'x' > .swarm-loop/acceptance/helpers.pyc
sl freeze
ck_exit "A5 freeze succeeds with a sourceless .pyc under protection" 0 "$RC"
ck_has "A5 freeze HASHES the sourceless .pyc (it is importable code)" "acceptance/helpers.pyc"
gq add -A; gq add -f .swarm-loop/acceptance/helpers.pyc; gq commit -qm harness
sl verify
ck_exit "A5 verify green with the .pyc frozen" 0 "$RC"
printf 'y' > .swarm-loop/acceptance/helpers.pyc
sl verify
ck_exit "A5 verify catches a MODIFIED sourceless .pyc" 1 "$RC"
ck_has "A5 the modified .pyc is named" "helpers.pyc"
printf 'x' > .swarm-loop/acceptance/helpers.pyc
printf 'z' > .swarm-loop/acceptance/extra.pyc
sl verify
ck_exit "A5 verify flags an ADDED sourceless .pyc" 1 "$RC"
ck_has "A5 the added .pyc is reported as ADDED" "[ADDED] .swarm-loop/acceptance/extra.pyc"
gq switch -q -c task/pyc
gq add -f .swarm-loop/acceptance/extra.pyc
gq commit -qm "smuggle a sourceless .pyc"
sl check-branch --branch task/pyc
ck_exit "A5 check-branch VETOES a .pyc added under a protected path" 1 "$RC"
ck_has "A5 the veto names the protected-path breach" "VETO: branch modifies the frozen goal harness"
gq switch -q main
rm -f .swarm-loop/acceptance/extra.pyc
# ...and the worker flow that motivated the filter still works
mkdir -p .swarm-loop/acceptance/__pycache__
echo "bytes" > .swarm-loop/acceptance/__pycache__/test_feature.cpython-39.pyc
sl verify
ck_exit "A5 a legitimate __pycache__/*.pyc is STILL ignored by verify" 0 "$RC"
gq switch -q -c task/cache
gq add -f .swarm-loop/acceptance/__pycache__/test_feature.cpython-39.pyc
gq commit -qm "worker ran the frozen suite"
sl check-branch --branch task/cache
ck_exit "A5 a legitimate __pycache__/*.pyc still does NOT veto the branch" 0 "$RC"
gq switch -q main
rm -rf .swarm-loop/acceptance/__pycache__

# ===========================================================================
# A6 · the smoke run is arbitrary shell and it ran BETWEEN the delta and the
# hashes that were frozen
# ===========================================================================
section "A6 a metric command may not edit what is being frozen  [repo: rsmoke]"
mk_repo rsmoke
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["command"] = ("echo \"def test_free(): pass\" >> "
                              ".swarm-loop/acceptance/test_feature.py; cat score.txt")
json.dump(g, open(p, "w"), indent=2)'
sl freeze
ck_exit "A6 freeze REFUSES when a metric command edited a protected file" 1 "$RC"
ck_has "A6 the smoke-run mutation is announced" "A METRIC COMMAND MODIFIED THE FILES BEING FROZEN"
ck_has "A6 the mutated file is named" "[MODIFIED] .swarm-loop/acceptance/test_feature.py"
ck_nofile "A6 no manifest was written" ".swarm-loop/manifest.json"

# ===========================================================================
# A7 · protected_paths survive init --force; an absent one blocks the freeze
# ===========================================================================
section "A7 protected-path bookkeeping  [repo: rprot]"
mk_repo rprot
mkdir -p scripts
echo "echo ok" > scripts/verify.sh
set_state protected_paths '[".swarm-loop/acceptance", ".swarm-loop/goals.json", "scripts/verify.sh"]'
sl freeze
ck_exit "A7 freeze with a run-added third protected path" 0 "$RC"
ck_has "A7 the extra path is frozen" "scripts/verify.sh"
gq add -A; gq commit -qm harness
sl init --max-cycles 4 --force
ck_exit "A7 init --force succeeds" 0 "$RC"
ck "A7 init --force CARRIES protected_paths forward (was: silently dropped)" "True" \
   "$(jget .swarm-loop/state.json "'scripts/verify.sh' in d['protected_paths']")"
ck_has "A7 --force says which extra paths it carried" "carried this repo's extra protected path"
rm -f scripts/verify.sh
sl freeze
ck_exit "A7 freeze REFUSES while a protected path does not exist" 1 "$RC"
ck_has "A7 the absent path is named loudly" "PROTECTED PATH(S) DO NOT EXIST"
sl freeze --allow-missing-protected
ck_exit "A7 --allow-missing-protected freezes anyway, on the record" 0 "$RC"

# ===========================================================================
# D1 / D2 · an amendment that moves a target invalidates its own baseline
# ===========================================================================
section "D1/D2 amendment invalidates the baseline  [repo: ramd2]"
mk_repo ramd2
sl freeze
gq add -A; gq commit -qm harness
echo 100 > score.txt
echo 0 > lint.txt
sl measure --cycle 0
sl analyze --cycle 0
ck_exit "D1 control: all-green before the amendment" 2 "$RC"
sleep 1
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = 120
json.dump(g, open(p, "w"), indent=2)'
sl freeze --amend "user approved raising the acceptance target 100 -> 120"
ck_exit "D2 the amendment is accepted" 0 "$RC"
ck_has "D2 the amend announces the invalidated baseline" "THE BASELINE IT MOVED IS NOW INVALID"
ck "D2 pending_remeasure is recorded in state.json" "[0]" \
   "$(jget .swarm-loop/state.json "d.get('pending_remeasure')")"
sl analyze --cycle 0
ck_exit "D1 the pre-amendment rows no longer read as all-green" 1 "$RC"
ck_hasnt "D1 no ALL TARGETS MET off rows measured against the old target" "ALL TARGETS MET"
ck_has "D1 the error was recomputed against the CURRENT target" "recomputed from"
ck_has "D1 the pre-amendment rows are named" "MEASURED BEFORE THE LAST AMENDMENT"
ck_has "D2 the owed re-measure is stated" "RE-MEASURE OWED"
echo 120 > score.txt
sl measure --cycle 0
ck_exit "D2 the re-measure runs" 0 "$RC"
ck_has "D2 measure clears pending_remeasure" "pending_remeasure cleared"
sl analyze --cycle 0
ck_exit "D2 all-green returns once the baseline is re-measured" 2 "$RC"

# ===========================================================================
# D6 · a string target, and duplicate metric ids
# ===========================================================================
section "D6 target coercion + duplicate ids  [repo: rtgt]"
mk_repo rtgt
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = "100"
json.dump(g, open(p, "w"), indent=2)'
sl freeze
ck_exit "D6 freeze accepts a numeric-string target" 0 "$RC"
ck_has "D6 the string target is called out while goals are still editable" "not a JSON number"
gq add -A; gq commit -qm harness
sl measure --cycle 0
ck_exit "D6 measure survives a string target (was: crash AFTER the command ran)" 0 "$RC"
ck "D6 the cycle's rows were actually written" "2" "$(grep -c '^0,' .swarm-loop/history.csv)"
sl analyze --cycle 0
ck_exit "D6 analyze survives a string target" 0 "$RC"
ck_hasnt "D6 no format-code traceback anywhere" "Unknown format code"

mk_repo rdupe
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"].append(dict(g["metrics"][0], command="cat lint.txt", target=5))
json.dump(g, open(p, "w"), indent=2)'
sl freeze
ck_exit "D6 freeze REJECTS duplicate metric ids" 1 "$RC"
ck_has "D6 the duplicated id is named" "duplicate metric id"

# ===========================================================================
# H-1 · an escalation whose subject is a pytest nodeid links to its amendment
# ===========================================================================
section "H-1 nodeid escalation linking  [repo: rnode]"
mk_repo rnode
sl freeze
gq add -A; gq commit -qm harness
sl escalate --kind frozen-test --subject "test_feature.py::test_feature" \
   --reason "the frozen assertion cannot be satisfied by any implementation" \
   --proposed "replace the assertion and re-freeze"
ck_exit "H-1 the nodeid escalation is filed" 0 "$RC"
echo "def test_more(): assert True" >> .swarm-loop/acceptance/test_feature.py
sl freeze --amend "user approved fixing the unsatisfiable frozen test"
ck_exit "H-1 the amendment is accepted" 0 "$RC"
ck_has "H-1 the nodeid escalation IS linked to the amendment" "OPEN ESCALATION(S) THIS AMENDMENT ANSWERS"
ck_has "H-1 the exact resolve command is printed" "escalate --resolve ESC-001"
ck "H-1 the freeze-log record carries the link" "OK" \
   "$(python3 -c '
import json
rec = json.loads([l for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()][-1])
print("OK" if rec.get("escalations") == ["ESC-001"] else "BAD: %r" % (rec.get("escalations"),))' 2>&1)"

# a structured --path escalation links too
sl escalate --kind frozen-test --subject "criterion C1 is unreachable" \
   --path .swarm-loop/acceptance/test_feature.py \
   --reason "the criterion cannot be reached" --proposed "amend the test"
echo "def test_more2(): assert True" >> .swarm-loop/acceptance/test_feature.py
sl freeze --amend "user approved the second fix"
ck_has "H-1 a prose subject still links via the structured --path" "ESC-002"

# ===========================================================================
# P-14 · a zero-byte history.csv must not take down the recovery command
# ===========================================================================
section "P-14 resume resilience  [repo: rzero]"
mk_repo rzero
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
: > .swarm-loop/history.csv
sl resume
ck_exit "P-14 resume survives a zero-byte history.csv" 0 "$RC"
ck_has "P-14 the empty history is reported loudly" "is EMPTY (0 bytes)"
ck_hasnt "P-14 no traceback from the recovery command" "Traceback"

# ===========================================================================
# B1-B4 / B6b · the readiness scheduler the skill claimed to have
# ===========================================================================
section "B1-B4 ticket graph scheduler  [repo: rsched]"
mk_repo rsched
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true",
  "scope": ["**"], "depends_on": [], "parallel_safe": false},
 {"id": "T-001", "title": "alpha", "verify": "echo 'collected 3 items'; exit 1",
  "scope": ["packages/alpha/**"], "depends_on": ["T-000"], "parallel_safe": true},
 {"id": "T-002", "title": "beta", "verify": "echo 'collected 2 items'; exit 1",
  "scope": ["packages/beta/**"], "depends_on": ["T-000"], "parallel_safe": false},
 {"id": "T-003", "title": "alpha-sub", "verify": "exit 5",
  "scope": ["packages/alpha/sub/**"], "depends_on": ["T-001"], "parallel_safe": true},
 {"id": "T-004", "title": "orphan", "verify": "exit 1",
  "scope": ["packages/gamma/**"], "depends_on": ["T-999"], "parallel_safe": true}
]}
EOF
sl frontier
ck_exit "B1 frontier exits 1 on a dangling dependency" 1 "$RC"
ck_has "B1 the dangling dep is named, with why it matters" "T-004 depends on T-999"

python3 -c '
import json
d = json.load(open("tickets.json"))
d["tickets"] = [t for t in d["tickets"] if t["id"] != "T-004"]
d["tickets"].append({"id": "T-005", "title": "cyc-a", "verify": "exit 1",
                     "scope": ["x/**"], "depends_on": ["T-006"]})
d["tickets"].append({"id": "T-006", "title": "cyc-b", "verify": "exit 1",
                     "scope": ["y/**"], "depends_on": ["T-005"]})
json.dump(d, open("tickets.json", "w"), indent=2)'
sl frontier
ck_exit "B1 frontier exits 1 on a dependency cycle" 1 "$RC"
ck_has "B1 the cycle is named" "dependency CYCLE"

python3 -c '
import json
d = json.load(open("tickets.json"))
d["tickets"] = [t for t in d["tickets"] if t["id"] not in ("T-005", "T-006")]
json.dump(d, open("tickets.json", "w"), indent=2)'
sl frontier --closed T-000
ck_exit "B1 frontier on a valid graph" 0 "$RC"
ck_has "B1 the ready set is printed" "READY (dispatchable now"
ck_has "B1 blast radius (transitive unblocks) ranks the frontier" "unblocks"
ck_has "B1 a ticket blocked by an open dep is not in the ready set" "T-003     waiting on T-001"

sl check-wave T-001 T-002
ck_exit "B2 disjoint ownership passes check-wave" 0 "$RC"
ck_has "B2 parallel_safe is reported as ADVISORY, never as the predicate" "advisory: parallel_safe"
ck_hasnt "B2 a parallel_safe:false member is not vetoed for the flag alone" "VETO"
sl check-wave T-001 T-003
ck_exit "B2 nested ownership globs are VETOED" 1 "$RC"
ck_has "B2 the offending pair and the intersecting patterns are named" "T-001 <-> T-003"

gq add -A; gq commit -qm "ticket graph"
sl red-check --ticket T-000,T-001,T-003
ck_exit "B3 red-check FAILS a gate that is already green" 1 "$RC"
ck_has "B3 the vacuous gate is named" "VACUOUS"
ck_has "B3 a gate that collects nothing is called weak, not red" "WEAK"
ck_has "B3 a genuinely red gate is recognised as a real gate" "a real gate"

cat > .swarm-loop/backlog.md <<'EOF'
# Backlog — the human view

Graph re-derived: 9 tickets, 9 edges, acyclic.

| ticket | state |
|---|---|
| T-000 | closed |
| T-001 | open |
| T-002 | open |
EOF
sl resume
ck_exit "B4 resume runs with a ticket graph present" 0 "$RC"
ck_has "B4 resume reports graph/ledger DRIFT instead of 'harness intact'" "in the graph, missing from the ledger"
ck_has "B4 the stale stated count is caught" "never states the graph's actual ticket count"
ck_has "B4 the ledger is named as derived, the graph as ground truth" "GRAPH is ground truth"

sl dispatch --agent backlog-writer --kind regen --subject backlog.md \
   --files packages/alpha/x.py
ck_exit "B6b a dispatch is recorded" 0 "$RC"
sl resume
ck_has "B6b resume reports work in flight that owns no worktree" "open dispatches : 1"
sl check-wave T-001 T-002
ck_exit "B6b a wave member overlapping IN-FLIGHT work is vetoed" 1 "$RC"
ck_has "B6b the in-flight agent is named" "backlog-writer"
sl dispatch --close backlog-writer
ck_exit "B6b closing a dispatch" 0 "$RC"
sl check-wave T-001 T-002
ck_exit "B6b the wave passes once the dispatch is closed" 0 "$RC"
sl resume
ck_hasnt "B6b a closed dispatch drops off the resume report" "open dispatches"


# ===========================================================================
# B5 · H-10 — the `reads` field: read edges `scope` cannot express, and the
#             cross-ticket import CLOSURE rule (not the naive scope rule, which
#             was reproduced live, fired on every legitimate read, and withdrawn)
# ===========================================================================
section "B5 read edges + closure rule  [repo: rreads]"
mk_repo rreads

# No ticket graph at all: a clear refusal naming the file, never a traceback.
sl reads
ck_exit "B5 'reads' on a run with no ticket graph exits 3, not 1" 3 "$RC"
ck_has "B5 the no-graph message names what it looked for" "no ticket graph found"
ck_hasnt "B5 no traceback when the graph is absent" "Traceback"

cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true",
  "scope": ["**"], "depends_on": []},
 {"id": "T-010", "title": "contracts", "verify": "exit 1",
  "scope": ["packages/contracts/**"], "depends_on": ["T-000"]},
 {"id": "T-020", "title": "engine", "verify": "exit 1",
  "scope": ["apps/engine/src/**"], "depends_on": ["T-010"]},
 {"id": "T-030", "title": "report", "verify": "exit 1",
  "scope": ["apps/report/src/**"], "depends_on": ["T-000"]}
]}
EOF

# T-020 -> T-010 is a LEGAL cross-ticket read: T-010 is in T-020's closure.
# This is the exact shape whose false positive got the naive "imports must be
# inside scope" rule withdrawn during the live run.
cat > .swarm-loop/acceptance/test_reads.py <<'EOF'
import json


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-020")
def test_engine_consumes_the_contract():
    from packages.contracts import Offer
    from apps.engine.src.core import run
    assert run(Offer())


@pytest.mark.ticket("T-010")
def test_contract_is_self_contained():
    from packages.contracts import Offer
    assert Offer()


def test_nobody_marked_this_one():
    from packages.contracts import Offer
    assert Offer()
EOF

sl reads
ck_exit "B5 a legal cross-ticket read is NOT a violation" 0 "$RC"
ck_has "B5 the inbound read edge is derived from the frozen test's imports" \
       "T-020     reads T-010     packages.contracts"
ck_has "B5 the OUTBOUND obligation is stated for the owner's task packet" \
       "T-010     must satisfy T-020"
ck_hasnt "B5 the withdrawn 'imports must be in scope' rule does NOT fire" "ILLEGAL READ"
ck_hasnt "B5 a ticket importing what it owns itself is not a read edge" \
       "T-010     reads"
ck_has "B5 an unmarked test is counted and grades nothing" \
       "carry no @pytest.mark.ticket marker"
ck_hasnt "B5 stdlib imports are not reported as unowned product modules" "UNOWNED"

# Now the H-10 instance itself: T-030's test imports a module T-020 owns, and
# T-020 is NOT in T-030's closure. T-030 is graded before the module exists.
cat >> .swarm-loop/acceptance/test_reads.py <<'EOF'


@pytest.mark.ticket("T-030")
def test_report_reads_the_engine():
    from apps.engine.src.core import run
    assert run
EOF
sl reads
ck_exit "B5 an out-of-closure cross-ticket import exits 1" 1 "$RC"
ck_has "B5 the illegal read names ticket, module, owner and file:line" \
       "ILLEGAL READ: T-030 imports apps.engine.src.core"
ck_has "B5 it says the owner is not in the importing ticket's closure" \
       "is NOT in T-030's dependency closure"
sl reads --no-fail
ck_exit "B5 --no-fail reports the violation without failing" 0 "$RC"
sl reads --closed T-020
ck_exit "B5 a violation clears once the owning ticket is declared closed" 0 "$RC"

# freeze must REFUSE, and refuse before writing anything.
gq add -A; gq commit -qm "graph + reads"
sl freeze
ck_exit "B5 freeze REFUSES over an illegal cross-ticket read" 1 "$RC"
ck_has "B5 the freeze refusal explains the tests can never pass" \
       "freeze REFUSED"
ck_nofile "B5 the refused freeze wrote no manifest" .swarm-loop/manifest.json
ck "B5 goals.json is still unfrozen after the refusal" "False" \
   "$(jget .swarm-loop/goals.json 'd["frozen"]')"
sl freeze --allow-read-violations
ck_exit "B5 --allow-read-violations gets past the gate" 0 "$RC"
ck_has "B5 the override is recorded in the freeze output" \
       "--allow-read-violations was passed"
ck_file "B5 the overridden freeze did write a manifest" .swarm-loop/manifest.json

# The unfixable shape: the edge that would legalise the read closes a cycle.
# This is the live instance (T-062 already depends on T-065) and it is why the
# check has to run at INTAKE — at dispatch time the answer is not actionable.
python3 -c '
import json
d = json.load(open("tickets.json"))
for t in d["tickets"]:
    if t["id"] == "T-020":
        t["depends_on"] = ["T-010", "T-030"]
json.dump(d, open("tickets.json", "w"), indent=2)'
sl reads
ck_exit "B5 the cycle-unfixable read is still a violation" 1 "$RC"
ck_has "B5 it says the fixing edge would close a CYCLE" \
       "would close a CYCLE"
ck_has "B5 it names the real remedy (re-mark or split), not 'add an edge'" \
       "re-mark the test or split the ticket"

# --write: additive optional key, idempotent, and every existing reader survives.
python3 -c '
import json
d = json.load(open("tickets.json"))
for t in d["tickets"]:
    if t["id"] == "T-020":
        t["depends_on"] = ["T-010"]
json.dump(d, open("tickets.json", "w"), indent=2)'
sl reads --write --no-fail
ck_exit "B5 reads --write succeeds" 0 "$RC"
ck "B5 --write records the derived read edge on the ticket" "[['T-010']]" \
   "$(jget tickets.json '[t.get("reads") for t in d["tickets"] if t["id"]=="T-020"]')"
ck "B5 --write leaves a ticket with no read edges alone" "[True]" \
   "$(jget tickets.json '["reads" not in t for t in d["tickets"] if t["id"]=="T-010"]')"
ck "B5 --write preserves every pre-existing field" "[True]" \
   "$(jget tickets.json '[all(k in t for k in ("id","title","verify","scope","depends_on")) for t in d["tickets"] if t["id"]=="T-020"]')"
sl reads --write --no-fail
ck "B5 --write is idempotent (second run changes nothing)" "[['T-010']]" \
   "$(jget tickets.json '[t.get("reads") for t in d["tickets"] if t["id"]=="T-020"]')"

# ===========================================================================
# B5b · the three diagnostics the read pass finds for free
# ===========================================================================
section "B5b shared scope / naming mismatch / unowned  [repo: rreads2]"
mk_repo rreads2
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-010", "title": "a", "verify": "exit 1",
  "scope": ["packages/shared/**"], "depends_on": ["T-000"]},
 {"id": "T-011", "title": "b", "verify": "exit 1",
  "scope": ["packages/shared/**"], "depends_on": ["T-000"]},
 {"id": "T-020", "title": "dashed", "verify": "exit 1",
  "scope": ["packages/store-agent/src/**"], "depends_on": ["T-000"]},
 {"id": "T-021", "title": "consumer", "verify": "exit 1",
  "scope": ["apps/consumer/**"], "depends_on": ["T-020"]}
]}
EOF
cat > .swarm-loop/acceptance/test_reads.py <<'EOF'
@pytest.mark.ticket("T-010")
def test_shared_module():
    from packages.shared.thing import go
    assert go


@pytest.mark.ticket("T-021")
def test_dashed_package():
    from packages.store_agent.src.hooks import Hook
    assert Hook


@pytest.mark.ticket("T-021")
def test_module_nobody_owns():
    from apps.orphan.src.thing import lost
    assert lost
EOF
sl reads --no-fail
ck_has "B5b a module inside two tickets' scopes is reported as SHARED WRITE OWNERSHIP" \
       "SHARED WRITE OWNERSHIP"
ck_has "B5b the co-owners are named" "claimed by T-010, T-011"
ck_hasnt "B5b shared ownership is NOT reported as a mutual read edge" \
       "T-010     reads T-011"
ck_has "B5b a hyphen scope vs an underscore import is caught" \
       "SCOPE/IMPORT NAMING MISMATCH"
ck_has "B5b the dashed edge is still derived, so the obligation is visible" \
       "T-021     reads T-020"
ck_has "B5b a product module no ticket owns is reported" "UNOWNED PRODUCT MODULES"
ck_has "B5b the unowned module is named" "apps.orphan.src.thing"

# A module-level import fails COLLECTION, so it is attributed to every marked
# test in the file — a wider blast radius than a function-level one, not smaller.
cat > .swarm-loop/acceptance/test_modlevel.py <<'EOF'
from packages.shared.thing import go


@pytest.mark.ticket("T-021")
def test_uses_module_level_import():
    assert go
EOF
sl reads --no-fail
ck_has "B5b a module-level product import becomes a read edge too" \
       "T-021     reads T-010"

# ===========================================================================
# B5c/B2 · a read edge INSIDE a wave is advisory, never a veto
# ===========================================================================
section "B5c read edges inside a wave  [repo: rreads3]"
mk_repo rreads3
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-010", "title": "port", "verify": "exit 1",
  "scope": ["packages/port/**"], "depends_on": ["T-000"]},
 {"id": "T-020", "title": "user", "verify": "exit 1",
  "scope": ["apps/user/**"], "depends_on": ["T-010"]}
]}
EOF
cat > .swarm-loop/acceptance/test_reads.py <<'EOF'
@pytest.mark.ticket("T-020")
def test_user_imports_the_port():
    from packages.port.api import call
    assert call
EOF
sl check-wave T-010 T-020
ck_exit "B5c disjoint WRITE sets still pass check-wave" 0 "$RC"
ck_has "B5c a read edge inside the wave is reported" "advisory (read edges"
ck_has "B5c it says the consumer cannot be graded until the owner merges" \
       "T-020 cannot be graded until T-010 merges"
ck_hasnt "B5c a read edge never vetoes a wave" "VETO"

# ===========================================================================
# B-def · the ticket graph is a PROJECT file: no shape may produce a traceback
# ===========================================================================
section "B-def defensive ticket-graph reading  [repo: rdefensive]"
mk_repo rdefensive
cat > tickets.json <<'EOF'
{"version": 1, "tickets": [
 {"id": "T-001", "title": "ok", "verify": "exit 1", "scope": ["packages/alpha/**"]},
 "a stray string",
 42,
 null,
 {"title": "no id at all", "scope": ["packages/zed/**"]},
 {"id": "T-002", "title": "string scope", "verify": "exit 1",
  "scope": "packages/beta/**", "depends_on": "T-001"}
]}
EOF
sl frontier
# exit 1, like a dangling dep or a cycle: an entry the graph silently dropped
# shrinks the frontier, and a shrunk frontier is indistinguishable from a
# finished one. What must NOT happen is the AttributeError traceback it used
# to raise from inside _graph_index.
ck_exit "B-def frontier flags a malformed graph instead of crashing on it" 1 "$RC"
ck_hasnt "B-def no traceback from a hand-edited graph" "Traceback"
ck_has "B-def the skipped non-object entries are reported, not swallowed" \
       "are not an object and have been SKIPPED"
ck_has "B-def a ticket with no id is reported as skipped" "have no 'id' and have been SKIPPED"
ck_has "B-def a depends_on given as a bare string is still an edge" \
       "T-002     waiting on T-001"
sl check-wave T-001 T-002
ck_exit "B-def a scope given as a bare STRING is one pattern, not one per character" 0 "$RC"
ck_has "B-def the bare-string scope is read whole" "owns(scope): packages/beta/**"
sl reads --no-fail
ck_exit "B-def reads survives the same graph" 0 "$RC"
ck_hasnt "B-def reads produces no traceback either" "Traceback"
sl resume
ck_exit "B-def resume survives it too (the B4 reconcile reads the same graph)" 0 "$RC"
ck_hasnt "B-def resume produces no traceback" "Traceback"

# ===========================================================================
# B4b · the rest of the structure the ledger is checked against
# ===========================================================================
section "B4b roots / depth / frontier re-derivation  [repo: rstruct]"
mk_repo rstruct
cat > tickets.json <<'EOF'
{"version": 1, "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "a", "verify": "exit 1", "scope": ["a/**"], "depends_on": ["T-000"]},
 {"id": "T-002", "title": "b", "verify": "exit 1", "scope": ["b/**"], "depends_on": ["T-000"]},
 {"id": "T-003", "title": "c", "verify": "exit 1", "scope": ["c/**"], "depends_on": ["T-001"]}
]}
EOF
cat > .swarm-loop/backlog.md <<'EOF'
# Backlog — the human view

Graph re-derived: 4 tickets, 3 edges, acyclic. Root T-000, max depth 2.

| ticket |
|---|
| T-000 |
| T-001 |
| T-002 |
| T-003 |
EOF
sl resume
ck_exit "B4b a ledger that agrees with the graph runs clean" 0 "$RC"
ck_hasnt "B4b an agreeing ledger reports NO drift" "DRIFT between"
ck_hasnt "B4b an agreeing ledger's stated max depth is accepted" "never states the graph's actual max-depth"

# Drift: the ledger loses a ticket, and the regeneration numbers come with it.
python3 -c '
import json
d = json.load(open("tickets.json"))
d["tickets"].append({"id": "T-004", "title": "d", "verify": "exit 1",
                     "scope": ["d/**"], "depends_on": ["T-003"]})
json.dump(d, open("tickets.json", "w"), indent=2)'
sl resume
ck_has "B4b a ticket added to the graph and not the ledger is DRIFT" \
       "in the graph, missing from the ledger: T-004"
ck_has "B4b the drift block re-derives the root set" "root(s) T-000"
ck_has "B4b the drift block re-derives max depth and the deepest ticket" \
       "max depth 3 (deepest: T-004)"
ck_has "B4b the drift block re-derives the initial frontier" \
       "initial frontier T-001, T-002"
ck_has "B4b the ledger's stale max-depth statement is caught" \
       "never states the graph's actual max-depth count (3)"

# A graph with several roots is structural, and reported whatever the ledger says.
python3 -c '
import json
d = json.load(open("tickets.json"))
for t in d["tickets"]:
    if t["id"] == "T-002":
        t["depends_on"] = []
json.dump(d, open("tickets.json", "w"), indent=2)'
sl resume
ck_has "B4b a multi-root graph is reported" "has 2 root ticket(s)"
ck_has "B4b the roots are named" "T-000, T-002"

# ===========================================================================
# BC — a run directory written by the PREVIOUS version must still load.
#
# This section exists because of a specific, live liability: a swarm-loop run
# was PAUSED, not terminated, while these fixes were being written. When it
# resumes it loads whatever the harness is at that moment, against state the
# old harness wrote. Every command has to keep working on those bytes.
#
# The pre-change shape is reproduced by DOWNGRADING a freshly frozen run rather
# than by pasting a fixture, so it cannot drift out of date: the only on-disk
# difference the whole changeset introduced is the optional `manifest["targets"]`
# map (state.json, goals.json, history.csv and the freeze-log record shape are
# byte-identical between the two versions — checked by freezing the same repo
# with each and diffing the key sets). Dropping that one key IS the old format.
#
# Verified against the real thing as well as this synthetic one: the paused
# run's own `.swarm-loop/` (16 protected files, 3 freeze-log records, a manifest
# with no `targets` key) authenticates clean under the new code.
# ===========================================================================
section "BC pre-change state still loads and runs  [repo: rbc]"
mk_repo rbc
sl freeze
ck_exit "BC freeze" 0 "$RC"
sl measure --cycle 0
ck_exit "BC measure cycle 0" 0 "$RC"

# --- downgrade to the pre-change on-disk shape
python3 -c '
import json
p = ".swarm-loop/manifest.json"
d = json.load(open(p))
d.pop("targets", None)          # the one key the new writer added
json.dump(d, open(p, "w"), indent=2)'
ck "BC the fixture really is in the old shape (no targets key)" "False" \
   "$(jget .swarm-loop/manifest.json "'targets' in d")"
# The old writer never wrote this key either; absent must mean "nothing owed",
# not "unknown" — a KeyError here would strand the paused run on its first
# analyze.
ck "BC pre-change state.json has no pending_remeasure key" "None" \
   "$(jget .swarm-loop/state.json "d.get('pending_remeasure')")"

# --- every read-side command must survive a manifest with no `targets`
sl verify
ck_exit "BC verify accepts a manifest with no targets key" 0 "$RC"
ck_has "BC verify still authenticates it against the freeze log" \
       "manifest reconciled against"
sl status
ck_exit "BC status" 0 "$RC"
sl analyze --cycle 0
ck_exit "BC analyze" 0 "$RC"
ck_has "BC analyze still states the integrity result" \
       "Harness integrity at analysis time"
sl resume
ck_exit "BC resume" 0 "$RC"
ck_has "BC resume reads the old manifest as frozen, not as a tamper" \
       "harness intact"
sl checkpoint --cycle 0
ck_exit "BC checkpoint" 0 "$RC"
sl record --cycle 0 --metric accept --value 44
ck_exit "BC record" 0 "$RC"
sl measure --cycle 1
ck_exit "BC measure on a later cycle" 0 "$RC"

# --- the freeze log has never been committed here (mk_repo does not commit
#     .swarm-loop/). That is the NORMAL state of a freshly frozen run, and it
#     must be a note, not a violation: making it fatal would refuse every run
#     between `freeze` and the baseline commit.
sl verify
ck_exit "BC an uncommitted freeze log is not a violation" 0 "$RC"
ck_has "BC ...it is reported as a note instead" "never been committed"

# --- the graph subcommands are all NEW. A pre-change run has no ticket graph
#     at all, so each must decline cleanly rather than crash: exit 3 (usage),
#     never exit 1 (which resume/analyze read as a real failure) and never a
#     traceback.
for c in frontier reads; do
  sl "$c"
  ck_exit "BC '$c' on a run with no ticket graph exits 3, not 1" 3 "$RC"
  ck_hasnt "BC '$c' does not traceback on a missing graph" "Traceback"
done
sl check-wave T-001 T-002
ck_exit "BC 'check-wave' on a run with no ticket graph exits 3, not 1" 3 "$RC"
ck_has "BC the no-graph message names what it looked for" "no ticket graph found"
sl red-check --ticket T-001
ck_exit "BC 'red-check' on a run with no ticket graph exits 3, not 1" 3 "$RC"
# `dispatch` is the one new subcommand with no graph dependency: it keeps its
# own append-only file, and an absent file means "nothing in flight".
sl dispatch --list
ck_exit "BC 'dispatch --list' works with no dispatch.jsonl at all" 0 "$RC"
ck_has "BC ...and says so rather than erroring" "nothing open"

# --- D2 on a pre-change manifest. `targets` is how an amendment detects a MOVED
#     goalpost exactly; a manifest written before that key existed has to fall
#     back to the conservative test (goals.json changed at all => re-measure
#     owed). The paused run's manifest has no `targets` key, so this fallback —
#     not the exact path — is the one that will actually run on it.
python3 -c '
import json
p = ".swarm-loop/goals.json"
d = json.load(open(p))
d["metrics"][0]["target"] = 60
json.dump(d, open(p, "w"), indent=2)'
sl freeze --amend "BC: move the accept target with no targets map to compare against"
ck_exit "BC --amend against a pre-change manifest" 0 "$RC"
ck "BC the conservative fallback still owes a re-measure" "[0]" \
   "$(jget .swarm-loop/state.json "d.get('pending_remeasure')")"
# The distinguishing assertion: this must be the FALLBACK, not the exact
# comparison. With no `targets` map the amendment cannot name which target
# moved, and it has to say so rather than quietly deciding nothing moved.
ck_has "BC the fallback names itself as a fallback, not an exact comparison" \
       "the previous manifest predates target recording"
sl analyze --cycle 1
ck_has "BC the pre-amendment rows are flagged, not scored" "PRE-AMENDMENT"
ck_has "BC all-targets-met is withheld" "RE-MEASURE OWED"
# Migration is FORWARD and additive: the amendment upgrades the manifest to
# carry `targets`, so the next amendment gets the exact comparison. Nothing is
# renamed and nothing new is required.
ck "BC the amendment upgrades the manifest in place" "True" \
   "$(jget .swarm-loop/manifest.json "'targets' in d")"

# The laundering path this closes: an owed re-measure is a claim about what was
# MEASURED, so the one thing that must not clear it is a hand-typed number.
# `record` has to warn and leave the debt standing; only `measure` may clear it.
sl record --cycle 0 --metric accept --value 99
ck_exit "BC record while a re-measure is owed" 0 "$RC"
ck_has "BC ...warns that a hand-entered row does not satisfy it" \
       "hand-entered row does NOT satisfy it"
ck "BC ...and leaves the debt standing" "[0]" \
   "$(jget .swarm-loop/state.json "d.get('pending_remeasure')")"
sl measure --cycle 0
ck_has "BC only a real re-measure clears it" "pending_remeasure cleared"
ck "BC ...and then the debt is gone" "None" \
   "$(jget .swarm-loop/state.json "d.get('pending_remeasure')")"

# ===========================================================================
# D1 · goals.json is INSIDE the manifest/freeze-log reconciliation
#
# A1 authenticated every protected file against the append-only freeze log
# EXCEPT goals.json — the one file that holds every target. So the P-2/H-31
# forgery still worked on the highest-value file in the run: edit a target,
# recompute that single digest into manifest.json, and 'verify' said "OK —
# frozen harness intact" forever. The exclusion was not gratuitous (freeze
# stamps frozen_at/amendments into goals.json AFTER the delta the log records,
# so the raw digests are genuinely incomparable); the fix is a canonical,
# stamp-free digest recorded on both sides, not the deletion of the skip.
# ===========================================================================
section "D1 a forged goals.json target is caught  [repo: rgoalforge]"
mk_repo rgoalforge
sl freeze
ck_exit "D1 control: freeze" 0 "$RC"
gq add -A; gq commit -qm harness
sl verify
ck_exit "D1 control: verify green on an untampered run" 0 "$RC"

# A legitimate, recorded target move must stay green — the check must not fire
# on the amendment mechanism it exists to make mandatory.
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = 90
json.dump(g, open(p, "w"), indent=2)'
sl freeze --amend "user approved lowering the acceptance target to 90"
ck_exit "D1 a RECORDED target move (freeze --amend) still succeeds" 0 "$RC"
sl verify
ck_exit "D1 ...and verify is green after it" 0 "$RC"
ck_hasnt "D1 no [FORGED] on a legitimately amended target" "[FORGED]"
# The LOW follow-on: goals.json is rewritten as the last act of every freeze, so
# its bytes are never in history at that instant. Naming it in the git-anchoring
# advisory fired on every freshly frozen and freshly amended run — a permanent
# false positive that teaches operators to skip the one line that is ever real.
ck_hasnt "D1 no false-positive git-anchoring advisory (goals.json is re-stamped by freeze)" \
         "not in git history yet"
cp .swarm-loop/goals.json "$TMP/rgoalforge-goals.amended"
cp .swarm-loop/manifest.json "$TMP/rgoalforge-manifest.amended"

# THE ATTACK: move a target and recompute exactly the one digest that covers it.
python3 - <<'PY'
import hashlib, json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = 40          # today's reading becomes the goal
json.dump(g, open(p, "w"), indent=2)
m = json.load(open(".swarm-loop/manifest.json"))
m["files"][p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
sl verify
ck_exit "D1 a moved target + recomputed manifest digest is CAUGHT (used to verify clean)" 1 "$RC"
ck_has "D1 the forgery is named as such" "[FORGED]"
ck_has "D1 ...and it names goals.json, the file that holds every target" ".swarm-loop/goals.json"
sl measure --cycle 0
ck_exit "D1 measure refuses to score against forged goals" 1 "$RC"

# Forging the manifest's own canonical digest as well must not help: the
# reconciliation is against the append-only log, and the value compared is
# derived from the bytes on disk, not from a field the attacker just wrote.
python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
m = json.load(open(".swarm-loop/manifest.json"))
m["goals_canon"] = swarmloop._goals_canon_file(".swarm-loop/goals.json")
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
sl verify
ck_exit "D1 forging the manifest's canonical digest too does not launder it" 1 "$RC"
ck_has "D1 ...still reported as a forgery" "[FORGED]"

cp "$TMP/rgoalforge-goals.amended" .swarm-loop/goals.json
cp "$TMP/rgoalforge-manifest.amended" .swarm-loop/manifest.json
sl verify
ck_exit "D1 control: green again once the real goals are put back" 0 "$RC"

# ===========================================================================
# D2 · two freezes inside ONE wall-clock second must not brick the run
#
# frozen_at is "%Y-%m-%dT%H:%M:%S" — one-second resolution — and the replay
# stopped at the FIRST record carrying the manifest's timestamp. A freeze
# immediately followed by an amend (any scripted repair) therefore replayed the
# log to the wrong record, so every digest the second freeze wrote was absent
# from the expected map and verify reported [FORGED] over a clean tree. There
# was no command that could clear it.
# ===========================================================================
section "D2 a same-second amendment does not brick the run  [repo: rsamesec]"
D2_STATE=no
D2_TRY=0
while [ "$D2_TRY" -lt 5 ]; do
  D2_TRY=$((D2_TRY + 1))
  mk_repo rsamesec
  # Land just after a second boundary so both freezes get a full second.
  python3 -c 'import time; time.sleep(1.02 - (time.time() % 1.0))'
  sl freeze --skip-smoke
  [ "$RC" -eq 0 ] || break
  echo "def test_widened(): assert True" >> .swarm-loop/acceptance/test_feature.py
  sl freeze --skip-smoke --amend "user approved widening the suite, seconds later"
  [ "$RC" -eq 0 ] || break
  D2_STATE=$(python3 -c '
import json
r = [json.loads(l) for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()]
print("natural" if len(r) >= 2 and r[-1]["frozen_at"] == r[-2]["frozen_at"] else "no")')
  [ "$D2_STATE" = "natural" ] && break
done
if [ "$D2_STATE" = "no" ]; then
  # The race did not land this time. The state under test is "two records share
  # frozen_at", so construct exactly that rather than skipping the case: the
  # bytes are identical to what the race produces.
  python3 - <<'PY'
import json
p = ".swarm-loop/freeze-log.jsonl"
recs = [json.loads(l) for l in open(p) if l.strip()]
recs[-1]["frozen_at"] = recs[-2]["frozen_at"]
with open(p, "w") as f:
    for r in recs:
        f.write(json.dumps(r, sort_keys=True) + "\n")
m = json.load(open(".swarm-loop/manifest.json"))
m["frozen_at"] = recs[-1]["frozen_at"]
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
g = json.load(open(".swarm-loop/goals.json"))
g["frozen_at"] = recs[-1]["frozen_at"]
json.dump(g, open(".swarm-loop/goals.json", "w"), indent=2)
import hashlib
m["files"][".swarm-loop/goals.json"] = hashlib.sha256(
    open(".swarm-loop/goals.json", "rb").read()).hexdigest()
m["goals_canon"] = m.get("goals_canon")
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
  D2_STATE=synthetic
fi
ck "D2 two freeze records really do share one frozen_at" "yes" \
   "$(python3 -c '
import json
r = [json.loads(l) for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()]
print("yes" if len(r) >= 2 and r[-1]["frozen_at"] == r[-2]["frozen_at"] else "no")')"
echo "  (same-second collision reached: $D2_STATE, attempt $D2_TRY)"
sl verify
ck_exit "D2 verify is GREEN on a same-second amendment (was a permanent false [FORGED])" 0 "$RC"
ck_hasnt "D2 no forgery is alleged against a clean tree" "[FORGED]"
# The disambiguator: identity is the record, not the clock. Both keys are
# optional additions — a manifest without them still resolves by amendment.
ck "D2 the manifest records the log position the timestamp cannot supply" "True" \
   "$(jget .swarm-loop/manifest.json "isinstance(d.get('log_seq'), int)")"
ck "D2 ...and it points at the LAST of the two records, not the first" "True" \
   "$(python3 -c '
import json
r = [json.loads(l) for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()]
m = json.load(open(".swarm-loop/manifest.json"))
print(m.get("log_seq") == len(r) - 1 or m.get("amendments") == r[-1].get("amendment"))')"
sl measure --cycle 0
ck_exit "D2 the run is not bricked — measure still works" 0 "$RC"

# ===========================================================================
# D3 · --protect-path may not drop the mandatory protected paths
#
# `if args.protect_path:` REPLACED the list wholesale, silently dropping
# .swarm-loop/goals.json. The frozen targets were then freely editable for the
# whole run while verify/measure/analyze went on asserting the harness was
# intact — the end state of a successful forgery, reached with no forgery, and
# neither init nor freeze said a word.
# ===========================================================================
section "D3 --protect-path cannot drop goals.json  [repo: rprotdrop]"
mk_repo rprotdrop
sl init --max-cycles 4 --force --protect-path src
ck_exit "D3 init --protect-path src" 0 "$RC"
ck "D3 goals.json is unioned back into protected_paths (was: silently dropped)" "True" \
   "$(jget .swarm-loop/state.json "'.swarm-loop/goals.json' in d['protected_paths']")"
ck "D3 the acceptance suite is too" "True" \
   "$(jget .swarm-loop/state.json "'.swarm-loop/acceptance' in d['protected_paths']")"
ck "D3 the caller's own path is still honoured" "True" \
   "$(jget .swarm-loop/state.json "'src' in d['protected_paths']")"
ck_has "D3 init SAYS it added them rather than doing it silently" \
       "--protect-path did not list"
sl freeze
ck_exit "D3 freeze succeeds with the mandatory paths present" 0 "$RC"
ck_has "D3 goals.json really is under protection" ".swarm-loop/goals.json"

# The only remaining route in is a hand-edited state.json, which IS the tamper.
set_state protected_paths '["src", ".swarm-loop/acceptance"]'
rm -f .swarm-loop/manifest.json .swarm-loop/freeze-log.jsonl
python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p)); g["frozen"], g["frozen_at"] = False, None
json.dump(g, open(p, "w"), indent=2)'
sl freeze
ck_exit "D3 freeze REFUSES when goals.json is absent from protected_paths" 1 "$RC"
ck_has "D3 the refusal names the missing mandatory path" \
       ".swarm-loop/goals.json missing from protected_paths"
ck_nofile "D3 the refused freeze wrote no manifest" .swarm-loop/manifest.json

# ===========================================================================
# D4 · the B5 read gate is ADVISORY on an already-frozen project
#
# It refused `freeze --amend` on state an earlier version wrote, with an escape
# flag documented nowhere. Every remedy it proposes (re-mark the test, split the
# ticket) IS an amendment, so refusing the amend command refuses the only door
# out of the state it complains about. It stays BLOCKING at the first freeze,
# where the goals are still malleable and the fix is cheap.
# ===========================================================================
section "D4 the read gate is advisory once frozen  [repo: rreadamend]"
mk_repo rreadamend
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true",
  "scope": ["**"], "depends_on": []},
 {"id": "T-020", "title": "engine", "verify": "exit 1",
  "scope": ["apps/engine/src/**"], "depends_on": ["T-000"]},
 {"id": "T-030", "title": "report", "verify": "exit 1",
  "scope": ["apps/report/src/**"], "depends_on": ["T-000"]}
]}
EOF
cat > .swarm-loop/acceptance/test_reads.py <<'EOF'
import pytest


@pytest.mark.ticket("T-030")
def test_report_reads_the_engine():
    from apps.engine.src.core import run
    assert run
EOF
# FIRST freeze: still blocking. This half must not regress.
sl freeze
ck_exit "D4 the FIRST freeze still REFUSES over an illegal cross-ticket read" 1 "$RC"
ck_has "D4 ...and says so" "freeze REFUSED"
ck_nofile "D4 the refused first freeze wrote no manifest" .swarm-loop/manifest.json
sl freeze --allow-read-violations
ck_exit "D4 --allow-read-violations still gets the first freeze through" 0 "$RC"
gq add -A; gq commit -qm harness

# Now the live blocker: an ALREADY-FROZEN run, amended WITHOUT the override.
echo "def test_widened(): assert True" >> .swarm-loop/acceptance/test_feature.py
sl freeze --amend "user approved widening the acceptance suite"
ck_exit "D4 freeze --amend on an already-frozen run SUCCEEDS (was exit 1, unrecoverable)" 0 "$RC"
ck_hasnt "D4 the amend is not refused by the read gate" "freeze REFUSED"
ck_has "D4 the finding is still reported, loudly" "ADVISORY, NOT A REFUSAL"
ck_has "D4 ...and names the offending test and line" "test_reads.py"
ck_has "D4 ...and the ticket that cannot legally read it" "T-030 imports apps.engine.src.core"
ck "D4 the amendment really was recorded" "1" \
   "$(jget .swarm-loop/goals.json "d.get('amendments')")"
sl verify
ck_exit "D4 verify is green after the advisory amend" 0 "$RC"

# ===========================================================================
# P1 · check-wave PASSED the exact wave it exists to catch
#
# A member that declares no ownership (`scope` absent, `[]`, or `null`) was
# treated by the overlap loops as owning NOTHING, so a MIXED wave — one ticket
# declaring a scope, one declaring none — passed clean and was dispatched in
# parallel. The refusal only ever fired when EVERY member declared nothing.
# The command printed the correct rule ("treat it as owning everything until it
# says otherwise") and contradicted it on the very next line.
# ===========================================================================
section "P1 an undeclared wave member owns EVERYTHING  [repo: rundecl]"
mk_repo rundecl
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-A",  "title": "declares a scope",       "verify": "exit 1",
  "scope": ["src/**"],     "depends_on": []},
 {"id": "T-N",  "title": "no scope key at all",    "verify": "exit 1",
                           "depends_on": []},
 {"id": "T-E",  "title": "scope is an empty list", "verify": "exit 1",
  "scope": [],             "depends_on": []},
 {"id": "T-Z",  "title": "scope is null",          "verify": "exit 1",
  "scope": null,           "depends_on": []},
 {"id": "T-B",  "title": "one file inside T-A",    "verify": "exit 1",
  "scope": ["src/a/b.py"], "depends_on": []},
 {"id": "T-D1", "title": "disjoint a",             "verify": "exit 1",
  "scope": ["src/a/**"],   "depends_on": []},
 {"id": "T-D2", "title": "disjoint b",             "verify": "exit 1",
  "scope": ["src/b/**"],   "depends_on": []},
 {"id": "T-O",  "title": "a per-file owns map",    "verify": "exit 1",
  "owns": ["docs/x.md"],   "depends_on": []}
]}
EOF

# --- the three shapes of "declares nothing", each mixed with a real scope
sl check-wave T-A T-N
ck_exit "P1 a MIXED wave (one declared, one not) is VETOED — it used to exit 0" 1 "$RC"
ck_has "P1 the offending pair is named" "T-A <-> T-N"
ck_has "P1 ...via the declared member's own pattern" "via  src/**"
ck_has "P1 the rule the command prints is the rule it now enforces" \
       "treat it as owning everything until it says otherwise"
ck_has "P1 the undeclared member is still SHOWN as undeclared, not as owning **" \
       "owns(scope): (NOTHING DECLARED)"

sl check-wave T-A T-E
ck_exit "P1 'scope': [] means DECLARES nothing, not OWNS nothing" 1 "$RC"
ck_has "P1 ...and is vetoed against the declared member" "T-A <-> T-E"

sl check-wave T-A T-Z
ck_exit "P1 'scope': null means DECLARES nothing, not OWNS nothing" 1 "$RC"
ck_has "P1 ...and is vetoed against the declared member" "T-A <-> T-Z"

# --- the undeclared member is not special-cased by position in the wave
sl check-wave T-D1 T-N T-D2
ck_exit "P1 three members, only the MIDDLE one undeclared, is VETOED" 1 "$RC"
ck_has "P1 ...against the member listed before it" "T-D1 <-> T-N"
ck_has "P1 ...and against the member listed after it" "T-N <-> T-D2"

# --- the controls named in the finding: none of these behaviours may move
sl check-wave T-N T-Z
ck_exit "P1 control: a wave where EVERY member declares nothing is still REFUSED" 1 "$RC"
ck_has "P1 control: ...with the refusal it always had" \
       "REFUSED — no ticket in this wave declares any ownership"
ck_hasnt "P1 control: ...and it is a refusal, not a veto" "VETO:"

sl check-wave T-A T-B
ck_exit "P1 control: a real overlap is still VETOED" 1 "$RC"
ck_has "P1 control: ...naming the pair" "T-A <-> T-B"

sl check-wave T-D1 T-D2
ck_exit "P1 control: a genuinely disjoint wave still passes" 0 "$RC"
ck_has "P1 control: ...with the OK it always had" "check-wave: OK"

# A ticket that declares a per-file `owns` map HAS declared ownership, even with
# no `scope` key — the substitution must key off the resolved list, not `scope`.
sl check-wave T-O T-D1
ck_exit "P1 a per-file 'owns' map is a declaration and is NOT treated as undeclared" 0 "$RC"
ck_has "P1 ...and it is read from 'owns', not from 'scope'" "owns(owns): docs/x.md"

# ===========================================================================
# P2 · red-check scored a gate that ran ZERO tests as "a real gate"
#
# _collected_count read what pytest COLLECTED, and pytest prints the collected
# and deselected counts on the SAME line: `pytest -m ticket_T010` written
# before that marker exists prints "collected 120 items / 120 deselected /
# 0 selected", exits non-zero, and was scored "red (120 test(s) collected) — a
# real gate". Its fallback regex was unanchored and matched `error` inside
# `errors`, so tsc / ruff / eslint / make handed it a fabricated test count off
# a run that collected nothing at all.
#
# The second half is the exit code, which IS the contract: `if vacuous:` alone
# meant WEAK, NO GATE and SKIPPED all exited 0 — and the incident named in
# red-check's own docstring (T-011/T-012 "reported `no tests ran`") is WEAK.
# ===========================================================================
section "P2 red-check counts what RAN, not what pytest listed  [repo: rredcheck]"
mk_repo rredcheck

# --- the parser itself, against the shapes real tools actually print
cc_() {  # cc_ <name> <expected> <tool output>
  local got
  got=$(python3 - "$3" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
print(swarmloop._collected_count(sys.argv[1]))
PY
)
  ck "$1" "$2" "$got"
}
cc_ "P2 pytest: a normal run counts its collected tests" "12" \
    "collected 12 items

12 passed"
cc_ "P2 pytest: 120 collected / 120 DESELECTED / 0 selected is ZERO tests (was 120)" "0" \
    "collected 120 items / 120 deselected / 0 selected"
cc_ "P2 pytest: a partial deselection counts only what was selected (was 120)" "2" \
    "collected 120 items / 118 deselected / 2 selected

2 failed"
cc_ "P2 pytest: with no 'selected' line, collected minus deselected" "2" \
    "collected 120 items / 118 deselected

2 failed"
cc_ "P2 pytest: zero collected is still zero" "0" \
    "collected 0 items

no tests ran"
cc_ "P2 tsc: 'Found 3 errors in 2 files.' is not 3 tests (was 3)" "None" \
    "Found 3 errors in 2 files."
cc_ "P2 ruff: 'Found 12 errors.' is not 12 tests (was 12)" "None" \
    "Found 12 errors."
cc_ "P2 eslint: '7 problems (7 errors, 0 warnings)' is not 7 tests (was 7)" "None" \
    "7 problems (7 errors, 0 warnings)"
cc_ "P2 make: '*** [test] Error 2' is not 2 tests" "None" \
    "make: *** [test] Error 2"
cc_ "P2 an import failure still reports no count at all" "None" \
    "ModuleNotFoundError: No module named 'x'"
cc_ "P2 pytest's own summary line is still read when nothing was collected-logged" "1" \
    "===== 1 failed in 0.20s ====="

# --- and end to end, through the command whose verdict it drives
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-100", "title": "marker gate written before the marker exists",
  "verify": "echo 'collected 120 items / 120 deselected / 0 selected'; exit 1",
  "scope": ["a/**"], "depends_on": []},
 {"id": "T-101", "title": "a type checker, not a test run",
  "verify": "echo 'Found 3 errors in 2 files.'; exit 2",
  "scope": ["b/**"], "depends_on": []},
 {"id": "T-102", "title": "no tests ran (the docstring's own incident)",
  "verify": "echo 'collected 0 items'; echo 'no tests ran'; exit 5",
  "scope": ["c/**"], "depends_on": []},
 {"id": "T-103", "title": "declares no verify command at all",
  "scope": ["d/**"], "depends_on": []},
 {"id": "T-104", "title": "a genuinely red gate",
  "verify": "echo 'collected 3 items'; echo '3 failed'; exit 1",
  "scope": ["e/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm "red-check graph"

sl red-check --ticket T-100
ck_exit "P2 a gate that DESELECTED every test is not a real gate" 1 "$RC"
ck_has "P2 ...it is WEAK" "WEAK"
ck_hasnt "P2 ...NOT 'red (120 test(s) collected) — a real gate'" "a real gate"
ck_hasnt "P2 ...and the deselected count is never reported as a test count" "120 test(s)"

sl red-check --ticket T-101
ck_exit "P2 a compiler's error count is not a test count" 1 "$RC"
ck_hasnt "P2 ...tsc's 'Found 3 errors' does not fabricate a real gate" "a real gate"
ck_has "P2 ...it is WEAK, which is the honest answer" "WEAK"

sl red-check --ticket T-102
ck_exit "P2 'no tests ran' now FAILS the gate it motivated (used to exit 0)" 1 "$RC"
ck_has "P2 ...classified WEAK" "WEAK"

sl red-check --ticket T-103
ck_exit "P2 a ticket with NO verify command FAILS the gate (used to exit 0)" 1 "$RC"
ck_has "P2 ...classified NO GATE" "NO GATE"

sl red-check --ticket T-104
ck_exit "P2 control: a genuinely red gate still PASSES red-check" 0 "$RC"
ck_has "P2 control: ...and is still called a real gate" "a real gate"
ck_has "P2 control: ...counting what was SELECTED" "3 test(s) selected"

sl red-check --ticket T-100,T-101,T-102,T-103,T-104
ck_exit "P2 a mixed set fails on the weak members, not only on a vacuous one" 1 "$RC"
ck_has "P2 the summary counts every bucket, SKIPPED included" "SKIPPED"

# SKIPPED — a worktree that could not be provisioned — used to land in NO bucket
# at all: uncounted in the summary and unable to affect the exit code, so a
# ticket that was never checked read exactly like one that had passed.
python3 - <<'PY'
import json
d = json.load(open("tickets.json"))
d["tickets"].append({"id": "T-9" + "x" * 400, "title": "worktree cannot be provisioned",
                     "verify": "exit 1", "scope": ["f/**"], "depends_on": []})
json.dump(d, open("tickets.json", "w"), indent=2)
PY
LONGID="T-9$(python3 -c 'print("x" * 400)')"
sl red-check --ticket "$LONGID"
ck_exit "P2 a ticket whose worktree could not be provisioned FAILS the gate" 1 "$RC"
ck_has "P2 ...and says the gate was NEVER CHECKED" "NEVER CHECKED"
ck_has "P2 ...and it is counted in the summary (it landed in no bucket at all)" "1 SKIPPED"

# ===========================================================================
# P4 · ONE appended log record forged the whole frozen harness
#
# Replay RESETS the protected map on any record marked `action: "initial"`
# (_replay_freeze_log, _replay_goals_canon) and nothing validated that a run
# has only one. The git anchor could not catch it: _log_git_check only required
# the committed log to be a PREFIX of the disk log, and appending preserves a
# prefix. So: gut a frozen acceptance test, move a goal 100 -> 0, append ONE
# `initial` record with the same frozen_at and the recomputed digests, point
# manifest.json at it — and `verify` said "OK — frozen harness intact", `status`
# said "goals_frozen": true, `resume` said "harness intact".
#
# Two checks close it: one `initial` per frozen_at, and the anchor `initial`
# must be a MEMBER of the git-committed copy, not merely compatible with it.
# ===========================================================================
section "P4 an appended 'initial' record cannot forge the harness  [repo: rforge]"

# The forgery itself, in the shape it was reproduced: append one record, then
# reconcile manifest.json to it so every cheap byte comparison agrees.
forge_initial() {  # forge_initial [<frozen_at override; default: the same one>]
  python3 - "${1:-}" <<'PY'
import hashlib, json, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
m = json.load(open(".swarm-loop/manifest.json"))
fa = sys.argv[1] or m["frozen_at"]
files = dict((r, hashlib.sha256(open(r, "rb").read()).hexdigest()) for r in m["files"])
canon = swarmloop._goals_canon_file(".swarm-loop/goals.json")
rec = {"action": "initial", "amendment": 0, "reason": None, "prev_frozen_at": None,
       "frozen_at": fa, "seq": 99, "goals_canon": canon, "modified": [], "removed": [],
       "added": [{"path": r, "new": h} for r, h in sorted(files.items())]}
open(".swarm-loop/freeze-log.jsonl", "a").write(json.dumps(rec, sort_keys=True) + "\n")
m["files"] = files
m["goals_canon"] = canon
m["log_seq"] = 99
m["frozen_at"] = fa
g = json.load(open(".swarm-loop/goals.json"))
m["targets"] = dict((x["id"], x.get("target")) for x in g.get("metrics", []))
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
}
gut_the_harness() {   # the tamper the forgery exists to launder
  cat > .swarm-loop/acceptance/test_feature.py <<'EOF'
def test_feature():
    pass
EOF
  python3 -c '
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["target"] = 0
json.dump(g, open(p, "w"), indent=2)'
}

mk_repo rforge
sl freeze
ck_exit "P4 control: freeze" 0 "$RC"
gq add -A; gq commit -qm harness
sl verify
ck_exit "P4 control: a normal single-'initial' run verifies GREEN" 0 "$RC"
ck_has "P4 control: ...reconciled against the append-only log" "manifest reconciled against"

gut_the_harness
sl verify
ck_exit "P4 control: the raw tamper is caught before any laundering" 1 "$RC"
ck_has "P4 control: ...as a modification of the frozen bytes" "[MODIFIED]"

forge_initial
sl verify
ck_exit "P4 ONE appended 'initial' record does NOT launder it (verify used to say OK)" 1 "$RC"
ck_has "P4 the second anchor under one timestamp is named" "share the freeze timestamp"
ck_has "P4 ...and the anchor is not in the git-COMMITTED log" "is NOT in the copy committed at"
ck_hasnt "P4 verify no longer calls the forged harness intact" "OK — frozen harness intact"
sl measure --cycle 0
ck_exit "P4 measure refuses to score against the forged harness" 1 "$RC"
sl resume
ck_hasnt "P4 resume no longer reports 'harness intact'" "harness intact"

# --- the same forgery with a NEW timestamp: the duplicate check cannot see it,
#     so this is the half the git anchor has to carry.
section "P4 an appended anchor under a NEW timestamp  [repo: rforge2]"
mk_repo rforge2
sl freeze
gq add -A; gq commit -qm harness
gut_the_harness
forge_initial "2099-01-01T00:00:00"
sl verify
ck_exit "P4 an appended 'initial' with a DIFFERENT frozen_at is caught too" 1 "$RC"
ck_has "P4 ...by MEMBERSHIP in the committed log, not by continuation of it" \
       "APPENDED afterwards"
ck_hasnt "P4 ...and it is not laundered into an OK" "OK — frozen harness intact"

# --- appended on top of a LEGITIMATE amendment: the amend must stay green and
#     the forgery on top of it must not.
section "P4 an anchor appended after a real --amend  [repo: rforge3]"
mk_repo rforge3
sl freeze
gq add -A; gq commit -qm harness
echo "def test_widened(): assert True" >> .swarm-loop/acceptance/test_feature.py
sl freeze --amend "user approved widening the acceptance suite"
ck_exit "P4 control: a legitimate --amend still succeeds" 0 "$RC"
gq add -A; gq commit -qm "amended harness"
sl verify
ck_exit "P4 control: ...and verify is GREEN after it" 0 "$RC"
ck_hasnt "P4 control: an amendment is not a forgery" "[FORGED]"
gut_the_harness
forge_initial "2099-02-02T00:00:00"
sl verify
ck_exit "P4 an 'initial' appended AFTER a legitimate --amend record is caught" 1 "$RC"
ck_has "P4 ...named as an appended anchor" "APPENDED afterwards"

# --- backward compatibility. Neither check may turn a legitimate run red.
section "P4 BC: uncommitted and pre-change freeze logs  [repo: rforge4]"
mk_repo rforge4
sl freeze
# mk_repo does not commit .swarm-loop/: this is the NORMAL state of every run
# between `freeze` and the baseline commit, and it must stay a note.
sl verify
ck_exit "P4 BC: an uncommitted freeze log still verifies green" 0 "$RC"
ck_has "P4 BC: ...reported as a note" "never been committed"
ck_hasnt "P4 BC: ...never as an accusation" "[FORGED]"
# The duplicate-anchor refusal is independent of git, so it still fires here.
gut_the_harness
forge_initial
sl verify
ck_exit "P4 the duplicate-anchor refusal needs no commit to anchor against" 1 "$RC"
ck_has "P4 ...naming the shared timestamp" "share the freeze timestamp"

mk_repo rforge5
sl freeze
gq add -A; gq commit -qm harness
# A run frozen before `action` / `seq` / `goals_canon` existed has no anchor to
# demand and no sequence to match. It must still verify green — "I cannot check
# this axis" is a note, never a forgery claim.
python3 - <<'PY'
import json
p = ".swarm-loop/freeze-log.jsonl"
recs = [json.loads(l) for l in open(p) if l.strip()]
for r in recs:
    r.pop("action", None)
    r.pop("seq", None)
    r.pop("goals_canon", None)
open(p, "w").write("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
m = json.load(open(".swarm-loop/manifest.json"))
m.pop("log_seq", None)
m.pop("goals_canon", None)
json.dump(m, open(".swarm-loop/manifest.json", "w"), indent=2)
PY
gq add -A; gq commit -qm "pre-change freeze-log shape"
ck "P4 BC: the fixture really is in the old shape (no action key)" "False" \
   "$(python3 -c '
import json
r = [json.loads(l) for l in open(".swarm-loop/freeze-log.jsonl") if l.strip()]
print(any("action" in x for x in r))')"
sl verify
ck_exit "P4 BC: a log with no 'action'/'seq'/'goals_canon' still verifies green" 0 "$RC"
ck_hasnt "P4 BC: ...and is never accused of forging an anchor" "[FORGED]"
sl resume
ck_exit "P4 BC: resume runs on the pre-change log shape" 0 "$RC"
ck_has "P4 BC: ...and still reads it as intact" "harness intact"

# ===========================================================================
# retire — mechanical worktree teardown
#
# The defect these cover: teardown was PROSE. merge-protocol step 10 had the
# right command, the right refusal semantics and the right escalation path, and
# nothing executed it — thirteen worktrees, ~14.7 GB measured, six of them
# finished work, while the run reported nothing amiss. So two things are pinned
# here: that `retire` retires exactly the trees whose work LANDED, and that
# `checkpoint` says so out loud when it has not been run.
#
# The safety assertions are the load-bearing ones. `git worktree remove` without
# --force is the only teardown that refuses a tree holding uncommitted work;
# every case below that proves a refusal is really proving that an agent's
# unsaved work survived a mechanical loop.
# ===========================================================================
section "retire: landed / refused / kept classification  [repo: rtire]"
mk_repo rtire
WTR="$TMP/rtire-worktrees"

mk_wt() {  # mk_wt <id> — a task worktree carrying one commit on task/<id>
  gq worktree add -q "$WTR/$1" -b "task/$1" main
  ( cd "$WTR/$1" && echo "work $1" > "$1.txt" && git add -A && git commit -qm "$1 work" ) \
    >/dev/null 2>&1
  return 0
}

mk_wt T-01     # lands on main, left clean               -> RETIRED
mk_wt T-02     # lands on main, left DIRTY               -> REFUSED
mk_wt T-03     # never merged anywhere                   -> kept
mk_wt T-04     # merged into an integration branch ONLY  -> kept
gq merge --no-edit task/T-01
gq merge --no-edit task/T-02
echo "uncommitted agent work" > "$WTR/T-02/scratch.txt"
gq branch integration/9001-b2 main
gq checkout integration/9001-b2
gq merge --no-edit task/T-04
gq checkout main
# A landed integration branch whose tree is already gone MUST be deleted; an
# unlanded one MUST NOT be. Without these two, every branch in this repo is
# protected by a live worktree and the ancestry test in _landed_branches is
# never actually exercised — a mutant that deleted every task/ and integration/
# branch it found stayed green.
gq branch integration/9002-b1 task/T-01     # landed (T-01 is on main), no worktree
gq branch task/T-05 task/T-03               # never landed, never had a worktree

ck_nofile "retire runs with NO dispatch ledger at all (the live run has none)" \
          ".swarm-loop/dispatch.jsonl"

# --- --dry-run mutates NOTHING -------------------------------------------
BEFORE_WT="$(git worktree list | sort)"
BEFORE_BR="$(git branch --format='%(refname:short)' | sort)"
BEFORE_ST="$(sha_of .swarm-loop/state.json)"
sl retire --dry-run --idle-seconds 0
ck_exit "retire --dry-run exits 0" 0 "$RC"
ck_has "--dry-run announces itself" "DRY RUN"
ck_has "--dry-run names the tree it WOULD retire" "WOULD RETIRE  task/T-01"
ck_has "--dry-run names the branches it WOULD delete" "branches that WOULD be deleted"
ck "--dry-run removed no worktree" "$BEFORE_WT" "$(git worktree list | sort)"
ck "--dry-run deleted no branch" "$BEFORE_BR" "$(git branch --format='%(refname:short)' | sort)"
ck "--dry-run wrote nothing to state.json" "$BEFORE_ST" "$(sha_of .swarm-loop/state.json)"
ck_file "--dry-run left the retirable tree on disk" "$WTR/T-01/T-01.txt"

# --- the idle guard is ON by default -------------------------------------
# Fix agents re-entered an adjudicated ticket's worktree on the live run and
# re-verification ran at the new tips. A freshly touched tree is never retired
# by default, however landed its branch looks.
sl retire
ck_exit "retire under the default idle guard exits 0 (nothing removed)" 0 "$RC"
ck_has "the idle guard is named, with its threshold" "idle guard: 900s"
ck_has "...and says how to release it once agents are collected" "--idle-seconds 0"
ck_file "the idle guard left the landed tree standing" "$WTR/T-01/T-01.txt"

# --- the real thing -------------------------------------------------------
sl retire --idle-seconds 0
ck_exit "retire exits 1 when a tree REFUSES" 1 "$RC"
ck_has "a landed CLEAN worktree is RETIRED" "RETIRED       task/T-01"
ck_nofile "...and its directory is really gone" "$WTR/T-01/T-01.txt"
ck_has "a landed DIRTY worktree REFUSES" "REFUSED       task/T-02"
ck_has "...quoting git's own refusal rather than swallowing it" \
       "contains modified or untracked files"
ck_file "...and the agent's uncommitted work is INTACT (never forced)" "$WTR/T-02/scratch.txt"
ck_has "a refusal is reported as a FINDING" "A REFUSAL IS A FINDING, NOT AN OBSTACLE"
ck_has "...with the escalate command spelled out" "escalate --kind harness --subject"
ck_has "an unmerged task branch is left alone" "kept          task/T-03"
ck_has "...with a reason that says it did not land" "task/T-03 has NOT landed on main"
ck_file "...and its diff survives for the retry packet" "$WTR/T-03/T-03.txt"
ck_has "merged into an INTEGRATION branch is NOT landed" "kept          task/T-04"
ck_file "...so a rejected/integration-only tree survives too" "$WTR/T-04/T-04.txt"
ck_file "the PRIMARY checkout is never touched" "src/app.py"
ck "no retire verdict line ever names the primary checkout" "0" \
   "$(awk '/RETIRED|REFUSED|WOULD RETIRE|  kept /{n=split($NF,a,"/"); if (a[n]=="rtire") c++}
           END{print c+0}' "$OUT")"
ck "git still lists the primary checkout" "1" \
   "$(git worktree list --porcelain | awk '/^worktree /{n=split($2,a,"/"); if (a[n]=="rtire") c++}
      END{print c+0}')"

# --- branch bookkeeping ---------------------------------------------------
ck "the landed branch of a retired tree is deleted" "0" \
   "$(git branch --list 'task/T-01' | wc -l | tr -d ' ')"
ck "an unmerged branch is KEPT (its diff is the retry packet)" "1" \
   "$(git branch --list 'task/T-03' | wc -l | tr -d ' ')"
ck "an integration-only branch is KEPT" "1" \
   "$(git branch --list 'task/T-04' | wc -l | tr -d ' ')"
ck "a landed branch still checked out somewhere is NOT deleted" "1" \
   "$(git branch --list 'task/T-02' | wc -l | tr -d ' ')"
ck "a LANDED integration branch with no worktree IS deleted" "0" \
   "$(git branch --list 'integration/9002-b1' | wc -l | tr -d ' ')"
ck "an UNLANDED integration branch is kept" "1" \
   "$(git branch --list 'integration/9001-b2' | wc -l | tr -d ' ')"
ck "an unlanded task branch with no worktree is kept" "1" \
   "$(git branch --list 'task/T-05' | wc -l | tr -d ' ')"

# --- --json ---------------------------------------------------------------
sl retire --idle-seconds 0 --json
ck_exit "retire --json exits 1 while a refusal stands" 1 "$RC"
ck "--json reports the refusal machine-readably" "task/T-02" \
   "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
print(",".join(x["branch"] for x in d["refused"]))' "$OUT")"
ck "--json reports the kept trees and why" "task/T-03,task/T-04" \
   "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
print(",".join(sorted(x["branch"] for x in d["kept"])))' "$OUT")"
ck "--json carries the worktree-root size" "True" \
   "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
print(isinstance(d["root_size_kb"], int))' "$OUT")"

# --- clearing the refusal by hand finishes the job -------------------------
rm -f "$WTR/T-02/scratch.txt"
sl retire --idle-seconds 0
ck_exit "retire exits 0 once the refusal is cleared" 0 "$RC"
ck_has "the once-dirty tree is now RETIRED" "RETIRED       task/T-02"
ck_nofile "...and its directory is gone" "$WTR/T-02/T-02.txt"
ck "...and its landed branch is deleted now nothing holds it" "0" \
   "$(git branch --list 'task/T-02' | wc -l | tr -d ' ')"

# --- retire never reaches for a force spelling ----------------------------
# Behavioural proof is above (the dirty tree survived). This is the structural
# one: no call inside the retire path may pass --force, `move`, or an rm.
ck "no call in the retire path passes a force/move/rm argument" "0" \
   "$(python3 - <<'PY'
import ast, inspect, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
BAD = {"--force", "-f", "move", "rmtree", "rm", "-rf"}
n = 0
for fn in (swarmloop._worktree_remove, swarmloop.cmd_retire,
           swarmloop._landed_branches, swarmloop._retire_scan):
    for node in ast.walk(ast.parse(inspect.getsource(fn))):
        if not isinstance(node, ast.Call):
            continue
        lits = [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        for sub_ in ast.walk(node):
            if isinstance(sub_, (ast.List, ast.Tuple)):
                lits += [e.value for e in sub_.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if BAD & set(lits):
            n += 1
print(n)
PY
)"

# ===========================================================================
section "retire: the guards that keep a tree standing  [repo: rtguard]"
mk_repo rtguard
WTG="$TMP/rtguard-worktrees"

# (i) a DETACHED worktree has no branch to test against main, so nothing proves
#     its work landed. red-check makes exactly this kind of tree.
gq worktree add -q --detach "$WTG/scratch" main
sl retire --idle-seconds 0
ck_exit "retire exits 0 with only a detached tree" 0 "$RC"
ck_has "a detached worktree is kept, never retired" "detached HEAD"
ck_file "...and it is still on disk" "$WTG/scratch/src/app.py"
gq worktree remove "$WTG/scratch"

# (ii) a LOCKED worktree: someone claimed it deliberately.
gq worktree add -q "$WTG/T-10" -b task/T-10 main
gq worktree lock "$WTG/T-10"
sl retire --idle-seconds 0
ck_exit "retire exits 0 with a locked landed tree" 0 "$RC"
ck_has "a LOCKED worktree is kept" "LOCKED"
ck_file "...and survives" "$WTG/T-10/src/app.py"
gq worktree unlock "$WTG/T-10"

# (iii) an OPEN dispatch is the strongest live-agent signal there is: it says so
#       in words instead of being inferred from a timestamp.
sl dispatch --agent fixer-1 --kind ticket --subject "T-10 rerun after adjudication"
ck_exit "record an open dispatch on T-10" 0 "$RC"
sl retire --idle-seconds 0
ck_exit "retire exits 0 while a dispatch is open" 0 "$RC"
ck_has "an OPEN dispatch keeps its ticket's worktree" "an OPEN dispatch still owns this tree"
ck_has "...and names the agent" "fixer-1"
ck_file "...and the tree survives" "$WTG/T-10/src/app.py"
sl dispatch --close fixer-1
sl retire --idle-seconds 0
ck_exit "retire exits 0 after the dispatch is closed" 0 "$RC"
ck_has "closing the dispatch releases the tree" "RETIRED       task/T-10"
ck_nofile "...and it is reclaimed" "$WTG/T-10/src/app.py"

# (iv) the PRIMARY checkout, even sitting on a landed task/ branch — which is
#      what makes it pass the ownership union — is excluded STRUCTURALLY: git
#      lists the main working tree first, and retire skips index 0. It is where
#      the frozen metric commands run and the one tree whose loss ends the run.
gq checkout -b task/PRIMARY-LOCAL
sl retire --idle-seconds 0
ck_exit "retire exits 0 with the primary checkout on a landed task/ branch" 0 "$RC"
ck_hasnt "the primary checkout is never even a candidate" "REFUSED       task/"
ck_hasnt "...and is never retired" "RETIRED       task/"
ck_has "...retire simply reports it owns no worktrees" "nothing to retire"
ck_file "...and the primary's files are untouched" "src/app.py"
ck "...and the branch it is standing on is never deleted" "1" \
   "$(git branch --list 'task/PRIMARY-LOCAL' | wc -l | tr -d ' ')"
gq checkout main

# ===========================================================================
# The half that matters most. The teardown guidance already existed and was
# simply never executed, and skipping it left NO trace anywhere the run could
# see. checkpoint runs at every cycle boundary, so an accumulating worktree root
# now shows up in the run's own output instead of only on the disk.
# ===========================================================================
section "checkpoint: the worktree teardown tripwire  [repo: rtcp]"
mk_repo rtcp
gq add -A; gq commit -qm harness
sl checkpoint --cycle 0
ck_exit "checkpoint with no worktrees exits 0" 0 "$RC"
ck_hasnt "checkpoint says NOTHING about teardown when nothing is stale" \
         "WORKTREE TEARDOWN OVERDUE"

WTC="$TMP/rtcp-worktrees"
gq worktree add -q "$WTC/T-20" -b task/T-20 main
( cd "$WTC/T-20" && echo x > x.txt && git add -A && git commit -qm w ) >/dev/null 2>&1
gq merge --no-edit task/T-20
echo "# cycle 1" >> src/app.py; gq add -A; gq commit -qm "cycle 1 work"
sl checkpoint --cycle 1
ck_exit "checkpoint STILL exits 0 with a stale worktree (advisory, never fatal)" 0 "$RC"
ck_has "checkpoint reports the overdue teardown" "WORKTREE TEARDOWN OVERDUE: 1 worktree(s)"
ck_has "...names the branch still on disk" "task/T-20"
ck_has "...reports the worktree-root size" "disk          :"
ck_has "...and points at the command that fixes it" "swarmloop.py retire"
ck_has "...and still prints the stall count it is actually for" "stall count:"

# A tree whose work has NOT landed is not overdue for anything.
sl retire --idle-seconds 0
ck_exit "retire reclaims it" 0 "$RC"
gq worktree add -q "$WTC/T-21" -b task/T-21 main
( cd "$WTC/T-21" && echo y > y.txt && git add -A && git commit -qm w ) >/dev/null 2>&1
echo "# cycle 2" >> src/app.py; gq add -A; gq commit -qm "cycle 2 work"
sl checkpoint --cycle 2
ck_exit "checkpoint exits 0" 0 "$RC"
ck_hasnt "an UNLANDED worktree is never called overdue teardown" "WORKTREE TEARDOWN OVERDUE"

# The tripwire may never be the thing that crashes a checkpoint.
set_state worktree_root '"../nowhere-at-all"'
echo "# cycle 3" >> src/app.py; gq add -A; gq commit -qm "cycle 3 work"
sl checkpoint --cycle 3
ck_exit "checkpoint survives a worktree_root that does not exist" 0 "$RC"
ck_hasnt "...without a traceback" "Traceback"

# ===========================================================================
section "retire: backward compatibility with a pre-teardown .swarm-loop  [repo: rtbc]"
mk_repo rtbc
python3 - <<'PY'
import json
p = ".swarm-loop/state.json"
s = json.load(open(p))
for k in ("worktree_root", "worktree_root_size_kb", "worktree_root_size_at",
          "last_retire_at", "base_branch"):
    s.pop(k, None)
json.dump(s, open(p, "w"), indent=2)
PY
ck "BC: the fixture really has no worktree_root key" "False" \
   "$(jget .swarm-loop/state.json "'worktree_root' in d")"
gq add -A; gq commit -qm bc
sl retire --dry-run
ck_exit "BC: retire runs on a state.json with none of the new keys" 0 "$RC"
ck_has "BC: it falls back to the legacy in-tree worktree root" ".swarm-loop/worktrees"
sl checkpoint --cycle 0
ck_exit "BC: checkpoint runs on the pre-teardown state shape" 0 "$RC"
ck_hasnt "BC: no traceback out of the tripwire" "Traceback"
sl status
ck_exit "BC: status is unchanged by the new optional keys" 0 "$RC"


# ===========================================================================
# (n) THE OTHER STALL — a run whose code changes every cycle and whose goal
#     never moves. The fingerprint kill switch cannot see this and never
#     could: it asks "did the code change", not "did the goal move", so any
#     loop that writes a line of product code per cycle resets it forever.
#     Live shape this reproduces: one metric pinned at 13.33 for seven
#     consecutive cycles at stall 0/3 with 95 cycles of budget left.
# ===========================================================================
section "(n) goal-progress kill switch  [repo: gstall]"
mk_repo gstall
sl freeze
ck_exit "(n) freeze the pinned harness" 0 "$RC"
gq add -A; gq commit -qm harness

# score.txt / lint.txt never move. src/app.py moves every single cycle, which
# is exactly what keeps the codebase fingerprint "changed" forever.
gcycle() {  # gcycle <n> — one product-changing, goal-pinning cycle
  echo "# cycle $1 product work" >> src/app.py
  gq add -A; gq commit -qm "cycle $1 product work"
  sl measure --cycle "$1"
  gq add -A; gq commit -qm "cycle $1 bookkeeping"
  sl checkpoint --cycle "$1"
}
gstall_n() { jget .swarm-loop/state.json "d['goal_stall_count']"; }
cstall_n() { jget .swarm-loop/state.json "d['stall_count']"; }

gcycle 0
ck_exit "(n) cycle 0 baseline checkpoint" 0 "$RC"
ck_has "(n) a first-ever reading is movement, never a stall" "first-ever reading"
ck "(n) goal stall is 0 after the baseline" "0" "$(gstall_n)"

gcycle 1
ck_exit "(n) cycle 1 checkpoint is not yet terminal" 0 "$RC"
ck_has "(n) the pinned metric is named" "no improvement in"
ck "(n) goal stall 1 after one pinned cycle" "1" "$(gstall_n)"
ck "(n) the CODEBASE stall is still 0 — the code changed" "0" "$(cstall_n)"

gcycle 2
ck_exit "(n) cycle 2 checkpoint is not yet terminal" 0 "$RC"
ck "(n) goal stall 2" "2" "$(gstall_n)"
ck "(n) codebase stall still 0" "0" "$(cstall_n)"

gcycle 3
ck_exit "(n) the goal-progress kill switch FIRES at the limit" 2 "$RC"
ck "(n) goal stall reached the limit" "3" "$(gstall_n)"
ck_has "(n) it says which switch fired" "no graded metric has improved"
ck_has "(n) it distinguishes itself from the codebase switch" "This is the OTHER stall"
ck_has "(n) it points at the analysis that names the pinned goal" "analysis/cycle-3.md"
ck "(n) the codebase stall NEVER left 0 — it structurally cannot see this" "0" "$(cstall_n)"
ck_hasnt "(n) the codebase kill switch did not fire" "no codebase changes for"

sl resume
ck_exit "(n) resume runs against the fired switch" 0 "$RC"
ck_has "(n) resume reports it as terminal" "kill switch already fired (goal progress"
ck_has "(n) resume shows both counters" "goal-progress"
sl status
ck_exit "(n) status runs" 0 "$RC"
ck_has "(n) status exposes the goal-progress counter" "stall_goal_progress"
ck_has "(n) status KEEPS the original stall key — an addition, not a rename" "\"stall\":"

# ===========================================================================
# (n2) The switch must be capable of NOT firing. A run making real progress
#      may never be terminated by it, and ANY single metric improving is
#      enough to reset — a net-zero cycle is not a stall.
# ===========================================================================
section "(n2) goal progress resets on ANY improvement  [repo: gmove]"
mk_repo gmove
sl freeze
gq add -A; gq commit -qm harness
gmove_cycle() {  # <n> <accept-score>   (lint stays pinned at 3 throughout)
  echo "$2" > score.txt
  echo "# c$1" >> src/app.py
  gq add -A; gq commit -qm "c$1"
  sl measure --cycle "$1"
  gq add -A; gq commit -qm "b$1"
  sl checkpoint --cycle "$1"
}
gmove_cycle 0 40
ck_exit "(n2) baseline" 0 "$RC"
gmove_cycle 1 50
ck_exit "(n2) cycle 1" 0 "$RC"
ck_has "(n2) the improving metric is named" "error fell for accept"
gmove_cycle 2 60
ck_exit "(n2) cycle 2" 0 "$RC"
gmove_cycle 3 70
ck_exit "(n2) cycle 3" 0 "$RC"
gmove_cycle 4 80
ck_exit "(n2) five cycles of real progress never trip the switch" 0 "$RC"
ck "(n2) goal stall is still 0 with one metric pinned the whole time" "0" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"
ck_hasnt "(n2) and no kill switch text anywhere" "KILL SWITCH"

# ===========================================================================
# (n3) An UNMEASURED cycle holds the counter where it is — neither bumped nor
#      cleared. A broken metric command is a defect analyze already exits 1
#      over; it may not also spend the run's stall budget, and it may not
#      silently refund it.
# ===========================================================================
section "(n3) an unmeasured cycle HOLDS the goal counter  [repo: ghold]"
mk_repo ghold
sl freeze
gq add -A; gq commit -qm harness
hcycle() {  # <n>  — pinned cycle, metric commands intact
  echo 40 > score.txt; echo 3 > lint.txt
  echo "# c$1" >> src/app.py
  gq add -A; gq commit -qm "c$1"
  sl measure --cycle "$1"
  gq add -A; gq commit -qm "b$1"
  sl checkpoint --cycle "$1"
}
hcycle 0
hcycle 1
hcycle 2
ck "(n3) two pinned cycles put the counter at 2" "2" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"

# both metric commands now fail: nothing is recorded for this cycle at all
rm -f score.txt lint.txt
echo "# c3" >> src/app.py
gq add -A; gq commit -qm c3
sl measure --cycle 3
ck_exit "(n3) measure fails when every metric command does" 1 "$RC"
gq add -A; gq commit -qm b3
sl checkpoint --cycle 3
ck_exit "(n3) checkpoint of an unmeasured cycle is not terminal" 0 "$RC"
ck_has "(n3) it says the cycle is unknown, not flat" "unknown"
ck "(n3) the counter HELD at 2 — not bumped by a broken measuring stick" "2" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"

# restore the measuring stick: the counter resumes from 2, it was not refunded
echo 40 > score.txt; echo 3 > lint.txt
echo "# c4" >> src/app.py
gq add -A; gq commit -qm c4
sl measure --cycle 4
gq add -A; gq commit -qm b4
sl checkpoint --cycle 4
ck_exit "(n3) the switch fires once real measurement resumes" 2 "$RC"
ck "(n3) the counter was not refunded by the gap either" "3" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"

# a replay is not a new cycle, for this counter as for the other one
G_BEFORE="$(jget .swarm-loop/state.json "d['goal_stall_count']")"
sl checkpoint --cycle 4
sl checkpoint --cycle 4
ck "(n3) three replays of cycle 4 do not move the goal counter" "$G_BEFORE" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"

# ===========================================================================
# (o) check-branch COUNTS COMMITS. Every question the harness asked about a
#     branch was ancestry-shaped, and ancestry cannot count: a branch with
#     zero commits is an ancestor of main, merges clean, and reads MERGED.
#     Live: two lanes provisioned, never dispatched, 0 commits, both passing.
# ===========================================================================
section "(o) check-branch counts commits  [repo: bcount]"
mk_repo bcount
sl freeze
gq add -A; gq commit -qm harness

gq branch task/T-empty main
git merge-base --is-ancestor task/T-empty main >/dev/null 2>&1; ANC=$?
ck "(o) ancestry ALONE still calls the empty lane merged" "0" "$ANC"
sl check-branch --branch task/T-empty --base main
ck_exit "(o) an EMPTY lane is REFUSED, not reported OK" 1 "$RC"
ck_has "(o) the refusal states the finding" "NO COMMITS of its own"
ck_has "(o) and names the command that answers it" "rev-list --count"
ck_has "(o) and says why ancestry could not see it" "reads MERGED"
ck_has "(o) and gives both readings of a zero" "never committed"
ck_hasnt "(o) an empty lane is never reported OK" "check-branch: OK"

# stale base, DISJOINT files: the live shape — merges clean, base long gone
gq branch task/T-stale main
gq checkout -q main
echo "moved once" > src/other.py
gq add -A; gq commit -qm "main moves 1"
echo "moved twice" >> src/other.py
gq add -A; gq commit -qm "main moves 2"
gq checkout -q task/T-stale
echo "# worker" >> src/app.py
gq add -A; gq commit -qm "worker work"
gq checkout -q main
sl check-branch --branch task/T-stale --base main
ck_exit "(o) a stale base is a WARNING, never a veto" 0 "$RC"
ck_has "(o) the drift is reported with its commit count" "STALE BASE"
ck_has "(o) the branch still passes" "check-branch: OK"
ck_has "(o) a non-intersecting drift says so" "None of them intersect"

# stale base OVERLAPPING the branch's own changes
gq branch task/T-over "$(git merge-base main task/T-stale)"
gq checkout -q task/T-over
echo "# worker touches the same file main moved" >> src/other.py
gq add -A; gq commit -qm "worker work on src/other.py"
gq checkout -q main
sl check-branch --branch task/T-over --base main
ck_exit "(o) an overlapping stale base is still not a veto" 0 "$RC"
ck_has "(o) the intersecting file is listed by name" "~ src/other.py"
ck_has "(o) mergeability is explicitly NOT the answer" "textual mergeability"

# CONTROL: real work on a current base must not be blocked or warned about
gq checkout -q -b task/T-good main
echo "# good work" >> src/app.py
gq add -A; gq commit -qm "good work"
gq checkout -q main
sl check-branch --branch task/T-good --base main
ck_exit "(o) CONTROL: real work on a current base passes" 0 "$RC"
ck_hasnt "(o) CONTROL: no false empty-lane veto" "NO COMMITS"
ck_hasnt "(o) CONTROL: no false stale-base warning" "STALE BASE"
ck_has "(o) CONTROL: the count is reported either way" "commit(s) ahead"

# ===========================================================================
# (p) check-wave asks the reciprocal question BEFORE dispatch: how far has
#     the base moved since an existing member branch forked from it, and does
#     that drift land inside what the ticket owns. Live: seven lanes were
#     dispatched onto worktrees 26-27 commits behind main and every one of
#     them merged clean. Nothing here vetoes — pre-dispatch, an empty lane is
#     the normal state, and refusing it would block the loop's own
#     provisioning step.
# ===========================================================================
section "(p) check-wave fork accounting  [repo: wfork]"
mk_repo wfork
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-101", "title": "alpha", "verify": "true",
  "scope": ["src/alpha/**"], "depends_on": [], "parallel_safe": true},
 {"id": "T-102", "title": "beta", "verify": "true",
  "scope": ["src/beta/**"], "depends_on": [], "parallel_safe": true}
]}
EOF
gq add -A; gq commit -qm graph
gq branch task/T-101 main
mkdir -p src/alpha
echo "main moved inside T-101's ownership" > src/alpha/mod.py
gq add -A; gq commit -qm "main moves"
sl check-wave T-101 T-102 --base main
ck_exit "(p) fork accounting never vetoes" 0 "$RC"
ck_has "(p) an existing member branch is counted" "task/T-101:"
ck_has "(p) a zero-commit lane is named as such" "EMPTY lane"
ck_has "(p) the stale base is reported" "STALE BASE"
ck_has "(p) drift inside the ticket's OWN ownership is listed" "~ src/alpha/mod.py"
ck_has "(p) a member with no branch yet is accounted for" "no branch yet"
ck_has "(p) the base sha the wave will fork from is pinned" "base main @"
ck_has "(p) the wave still passes" "check-wave: OK"

# ===========================================================================
# (q) The goal counter must work on a state.json written before it existed —
#     an in-flight run picks the switch up with no migration.
# ===========================================================================
section "(q) goal stall on a pre-counter state.json  [repo: gbc]"
mk_repo gbc
python3 - <<'PY'
import json
p = ".swarm-loop/state.json"
s = json.load(open(p))
s.pop("goal_stall_count", None)
s.pop("goal_stall_limit", None)
json.dump(s, open(p, "w"), indent=2)
PY
ck "(q) the fixture really has neither key" "False False" \
   "$(jget .swarm-loop/state.json "'%s %s' % ('goal_stall_count' in d, 'goal_stall_limit' in d)")"
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
ck_exit "(q) measure" 0 "$RC"
sl checkpoint --cycle 0
ck_exit "(q) checkpoint runs on the pre-counter state shape" 0 "$RC"
ck_hasnt "(q) no traceback" "Traceback"
ck_has "(q) the limit falls back to stall_limit" "goal-progress stall: 0/3"
ck "(q) and the counter key is created" "0" \
   "$(jget .swarm-loop/state.json "d['goal_stall_count']")"
sl resume
ck_exit "(q) resume runs on it" 0 "$RC"
ck_hasnt "(q) resume does not call a fresh run terminal" "kill switch already fired"
sl status
ck_exit "(q) status runs on it" 0 "$RC"

# ===========================================================================
# (r) P1 / OPEN §3 — the self-improvement loop is REPORTED, at both the places
#     the orchestrator already reads. SKILL.md has told every run to append to
#     .swarm-loop/learnings.md each epoch and to distil it into the skill's
#     LEARNED.md at Termination since the beginning; nothing ever checked, and
#     LEARNED.md has zero entries to show for it. This does not enforce the
#     append — it makes the omission visible. Read-only by design: the harness
#     writes nothing outside the run's own repo.
# ===========================================================================
section "(r) self-improvement visibility  [repo: selfimp]"
mk_repo selfimp
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
sl checkpoint --cycle 0
ck_exit "(r) checkpoint still exits 0 with the new line" 0 "$RC"
ck_has "(r) checkpoint reports the self-improvement loop" "self-improvement:"
ck_has "(r) an absent per-run learnings file is called out" "learnings.md: ABSENT"
ck_has "(r) the skill's cross-run memory is named too" "LEARNED.md"
ck_hasnt "(r) no traceback from the reporter" "Traceback"

sl resume
ck_exit "(r) resume still exits 0" 0 "$RC"
ck_has "(r) resume carries the same block" "self-improvement:"

# once the run actually keeps a learnings file, it is counted, not scolded
mkdir -p .swarm-loop
printf -- '- cycle 0: packets lacked the ownership map\n- cycle 1: wave width too wide\n' \
  > .swarm-loop/learnings.md
sl checkpoint --cycle 0
ck_exit "(r) checkpoint replay with a learnings file" 0 "$RC"
ck_has "(r) a populated learnings file is COUNTED, not reported absent" "learnings.md: 2 non-empty line(s)"
ck_hasnt "(r) and is no longer called absent" "learnings.md: ABSENT"

# the reporter must survive a skill it cannot see (script run from a copy)
cp "$SL" ./swarmloop-copy.py
python3 ./swarmloop-copy.py checkpoint --cycle 0 > "$OUT" 2>&1; RC=$?
ck_exit "(r) a relocated script does not crash the reporter" 0 "$RC"
ck_hasnt "(r) still no traceback when LEARNED.md is not where it looked" "Traceback"
ck_has "(r) it says plainly that it cannot see the skill memory" "LEARNED.md"


# ===========================================================================
# (s) findings -> tickets. Two live defects, one command. Five checkers
#     returned six HIGH defects; the MEDIUMs became tickets and the HIGHs
#     reached NO repo artifact at all — the severity filter was in effect
#     inverted. And the formatter that minted the survivors did
#     `location.split('/')[-1]`, so four of eight tickets pointed at a file
#     that does not contain the defect.
# ===========================================================================
section "(s) findings record + mint  [repo: findg]"
mk_repo findg
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-129", "title": "prior", "verify": "true", "scope": ["src/**"], "depends_on": []}
]}
EOF
cat > findings.json <<'EOF'
[
 {"severity":"HIGH","location":"apps/trust/src/ledger/__init__.py:175","defect":"ledger replays a foreign principal","reproduction":"pytest e2e/test_trust.py::test_identity_leak"},
 {"severity":"HIGH","location":"checkout/domain.py:47-48","defect":"authorize_discount bypasses approval"},
 {"severity":"MEDIUM","location":"fixtures/generator/__init__.py:175","defect":"generator seeds a shared RNG","reproduction":"pytest -k generator_seed"},
 {"severity":"LOW","location":"docs/runbook.md","defect":"runbook names a dead flag"}
]
EOF
sl findings --file findings.json --min-severity MEDIUM
ck_exit "(s) findings runs" 0 "$RC"
# Assert against the GRAPH, not stdout: the below-threshold listing prints the
# same "SEVERITY  location" shape as the minted listing, so a stdout match here
# stays green while HIGHs are being dropped — which is the live defect itself.
ck "(s) both HIGH findings became TICKETS — the band that vanished live" "2" \
   "$(jget tickets.json "len([t for t in d['tickets'] if t.get('severity') == 'HIGH'])")"
ck "(s) and the HIGH ticket carries its full location" "apps/trust/src/ledger/__init__.py:175" \
   "$(jget tickets.json "sorted(t['location'] for t in d['tickets'] if t.get('severity') == 'HIGH')[0]")"
ck_has "(s) every severity band is reconciled out loud" "EVERY severity band is accounted for"
ck_has "(s) 3 minted + 1 below threshold = 4 recorded" "3 minted + 1 below threshold + 0 already ticketed = 4 recorded"
ck_has "(s) the sub-threshold finding is recorded, not discarded" "not ticketed"

# THE formatter defect: the directory must survive
ck "(s) location is carried VERBATIM, never basenamed" "fixtures/generator/__init__.py:175" \
   "$(jget tickets.json "[t['location'] for t in d['tickets'] if t.get('finding_id') and 'generator' in t['location']][0]")"
ck "(s) scope is the FULL path, not the basename" "fixtures/generator/__init__.py" \
   "$(jget tickets.json "[t['scope'][0] for t in d['tickets'] if t.get('finding_id') and 'generator' in t['location']][0]")"
ck "(s) reproduction is carried — the field the old formatter never read" "pytest -k generator_seed" \
   "$(jget tickets.json "[t.get('reproduction') for t in d['tickets'] if t.get('finding_id') and 'generator' in t['location']][0]")"
ck "(s) a finding with no reproduction simply omits it" "False" \
   "$(jget tickets.json "any('reproduction' in t for t in d['tickets'] if 'domain.py' in str(t.get('location')))")"
ck "(s) the graph's own siblings survive the write" "['version', 'project']" \
   "$(jget tickets.json "sorted([k for k in d if k != 'tickets'], key=lambda k: ['version','project'].index(k))")"
ck "(s) every recorded finding is on disk at EVERY severity" "4" \
   "$(grep -c . .swarm-loop/findings.jsonl)"
ck "(s) including the LOW one that got no ticket" "1" \
   "$(grep -c '\"severity\": \"LOW\"' .swarm-loop/findings.jsonl)"

# the placeholder gate must be refused, not silently dispatchable
gq add -A; gq commit -qm findings
MINTED="$(jget tickets.json "[t['id'] for t in d['tickets'] if t.get('finding_id')][0]")"
sl red-check --ticket "$MINTED"
ck_exit "(s) red-check REFUSES a minted ticket's placeholder gate" 1 "$RC"

# idempotence: the same report twice must not double the graph
sl findings --file findings.json --min-severity MEDIUM
ck_exit "(s) re-running the same report is safe" 0 "$RC"
ck_has "(s) it says the findings already carry tickets" "already carry a ticket"
ck "(s) and mints nothing new" "4" \
   "$(jget tickets.json "len(d['tickets'])")"

# an unrecognised severity is the exact shape of a silently-dropped HIGH
BEFORE="$(grep -c . .swarm-loop/findings.jsonl)"
echo '[{"severity":"URGENT","location":"a.py:1","defect":"x"}]' > bad.json
sl findings --file bad.json
ck_exit "(s) an unrecognised severity is REFUSED" 1 "$RC"
ck_has "(s) the refusal names the offending value" "'URGENT' is not one of"
ck_has "(s) and says nothing was written" "nothing has been written"
ck "(s) and NOTHING was in fact written" "$BEFORE" "$(grep -c . .swarm-loop/findings.jsonl)"

# a finding with no defect text cannot become an actionable ticket
echo '[{"severity":"HIGH","location":"a.py:1","defect":"  "}]' > nodefect.json
sl findings --file nodefect.json
ck_exit "(s) a finding with no defect text is refused" 1 "$RC"
ck_has "(s) and says why" "cannot become a ticket anyone can act on"

# --no-tickets still records every severity
sl findings --file findings.json --no-tickets
ck_exit "(s) --no-tickets exits 0" 0 "$RC"
ck_has "(s) --no-tickets records without minting" "Every severity is on disk"


# ===========================================================================
# (t) check-wave and reads must REFUSE a graph that does not parse as one.
#     frontier has always validated and exited 1; these two unpacked `dupes`
#     and threw it away. _graph_index keeps the LAST entry for a repeated id,
#     so check-wave was answering "no two members can own the same file" — the
#     one safety property it exists for — from whichever definition happened to
#     come second.
# ===========================================================================
section "(t) graph validation in check-wave and reads  [repo: gdup]"
mk_repo gdup
cat > tickets.json <<'EOF'
{"tickets": [
 {"id": "T-001", "title": "first", "verify": "true", "scope": ["src/a/**"], "depends_on": []},
 {"id": "T-001", "title": "second, same id, DIFFERENT ownership", "verify": "true",
  "scope": ["src/b/**"], "depends_on": []},
 {"id": "T-002", "title": "other", "verify": "true", "scope": ["src/c/**"], "depends_on": []}
]}
EOF
sl frontier
ck_exit "(t) frontier refuses a duplicate ticket id (it always did)" 1 "$RC"
sl check-wave T-001 T-002
ck_exit "(t) check-wave now refuses the SAME graph" 1 "$RC"
ck_has "(t) the refusal names the duplicate id" "duplicate ticket id: T-001"
ck_has "(t) and says what would have been computed over it" "ownership overlap"
# The other half of the severity split: skipped junk entries are ADVISORY here.
# frontier refuses on them because its whole output is the ticket set; check-wave
# and reads are asked about NAMED tickets and answer those correctly, so refusing
# would be a false block on a graph this harness deliberately survives (B-def).
ck_hasnt "(t) it must NOT report the wave OK" "check-wave: OK"
sl reads
ck_exit "(t) reads refuses it too — it gates freeze" 1 "$RC"
ck_has "(t) reads says what it gates" "gates 'freeze' on"

# a dangling dependency is the other half of _graph_validate
cat > tickets.json <<'EOF'
{"tickets": [
 {"id": "T-001", "title": "a", "verify": "true", "scope": ["src/a/**"], "depends_on": []},
 {"id": "T-002", "title": "b", "verify": "true", "scope": ["src/b/**"], "depends_on": ["T-999"]}
]}
EOF
sl check-wave T-001 T-002
ck_exit "(t) check-wave refuses a dangling dependency" 1 "$RC"
ck_has "(t) naming the dep that can never close" "T-999"

# CONTROL — the whole point is that a VALID graph is untouched. A false refusal
# here blocks the loop's own dispatch step, which is strictly worse than the gap.
cat > tickets.json <<'EOF'
{"tickets": [
 {"id": "T-001", "title": "a", "verify": "true", "scope": ["src/a/**"], "depends_on": []},
 {"id": "T-002", "title": "b", "verify": "true", "scope": ["src/b/**"], "depends_on": ["T-001"]}
]}
EOF
sl check-wave T-001 T-002
ck_exit "(t) CONTROL: a valid graph still passes check-wave" 0 "$RC"
ck_has "(t) CONTROL: and is still reported OK" "check-wave: OK"
sl reads
ck_exit "(t) CONTROL: a valid graph still passes reads" 0 "$RC"
sl frontier
ck_exit "(t) CONTROL: and frontier" 0 "$RC"


# ===========================================================================
# (u) Ownership overlap: brace expansion and case. check-wave's ONE safety
#     property is that two concurrent workers never own the same file, and it
#     answered "no overlap" for two pairs that collide on disk.
# ===========================================================================
section "(u) ownership overlap gaps  [repo: ovl]"
mk_repo ovl
python3 - "$SL" > "$OUT" 2>&1 <<'PY'
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("sl", sys.argv[1])
sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)
must_overlap = [("src/{a,b}/**","src/a/**"), ("src/{a,b}/**","src/b/core.py"),
                ("pkg/{x,y}/m.py","pkg/x/m.py"), ("SRC/**","src/**"),
                ("Src/App.py","src/app.py")]
must_not    = [("src/a/**","src/b/**"), ("pkg/x/m.py","pkg/y/m.py"),
               ("a/**","b/**"), ("src/{a,b}/**","src/{c,d}/**")]
bad  = ["%s vs %s should OVERLAP" % (a,b) for a,b in must_overlap if not sl._patterns_overlap(a,b)]
bad += ["%s vs %s must NOT overlap" % (a,b) for a,b in must_not if sl._patterns_overlap(a,b)]
# Bounded AND conservative. Timing alone left the `{` in _has_glob — the
# backstop for exactly this overflow path — with no coverage: reverting that
# one character kept the whole suite green.
t = time.time()
overflow = sl._patterns_overlap("{a,b}"*12 + "/**", "zzz/**")
if time.time() - t > 5:
    bad.append("brace expansion is not bounded")
if not overflow:
    bad.append("a brace too large to expand must fall back to OVERLAP, not to no-overlap")
if len(sl._expand_braces("{a,b}"*12 + "/**")) != 1:
    bad.append("expansion past MAX_BRACE_ALTS must return the pattern unexpanded")
for b in bad:
    print("  BAD: " + b)
print("OVERLAP_BAD=%d" % len(bad))
PY
ck_has "(u) every brace/case/bounding expectation holds" "OVERLAP_BAD=0"

sl_out=$(python3 - "$SL" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sl", sys.argv[1])
sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)
print(int(sl._patterns_overlap("src/{a,b}/**","src/a/**")), end="")
print(int(sl._patterns_overlap("SRC/**","src/**")), end="")
print(int(sl._patterns_overlap("src/a/**","src/b/**")), end="")
print(int(sl._scope_match("src/{a,b}/**","src/b/core.py")), end="")
print(int(sl._scope_match("SRC/**","src/app.py")), end="")
print(int(sl._scope_match("src/a/**","src/b/x.py")))
PY
)
ck "(u) brace/case overlap and scope-match, 6 answers" "110110" "$sl_out"

# and end-to-end: check-wave must VETO a brace-vs-plain collision
cat > tickets.json <<'EOF'
{"tickets":[
 {"id":"T-001","title":"ab","verify":"true","scope":["src/{a,b}/**"],"depends_on":[]},
 {"id":"T-002","title":"a","verify":"true","scope":["src/a/**"],"depends_on":[]},
 {"id":"T-003","title":"upper","verify":"true","scope":["SRC/**"],"depends_on":[]},
 {"id":"T-004","title":"lower","verify":"true","scope":["src/**"],"depends_on":[]},
 {"id":"T-005","title":"c","verify":"true","scope":["src/c/**"],"depends_on":[]}]}
EOF
sl check-wave T-001 T-002
ck_exit "(u) check-wave VETOES src/{a,b}/** against src/a/**" 1 "$RC"
ck_has "(u) naming the intersecting pair" "T-001 <-> T-002"
sl check-wave T-003 T-004
ck_exit "(u) check-wave VETOES SRC/** against src/**" 1 "$RC"
sl check-wave T-002 T-005
ck_exit "(u) CONTROL: genuinely disjoint globs still pass" 0 "$RC"

# ===========================================================================
# (v) `reads` blind spots — it GATES freeze, so a missed edge means a ticket
#     is graded against a moving target with no warning.
# ===========================================================================
section "(v) reads scanner blind spots  [repo: rdscan]"
mk_repo rdscan
mkdir -p packages/alpha packages/beta
echo "x = 1" > packages/alpha/__init__.py
echo "y = 1" > packages/beta/__init__.py
cat > tickets.json <<'EOF'
{"tickets":[
 {"id":"T-001","title":"alpha","verify":"true","scope":["packages/alpha/**"],"depends_on":["T-002"]},
 {"id":"T-002","title":"beta","verify":"true","scope":["packages/beta/**"],"depends_on":[]}]}
EOF
# mk_repo ships its own unmarked test_feature.py; drop it so the counts below
# are about THIS section's files and nothing else.
rm -f .swarm-loop/acceptance/test_feature.py
cat > .swarm-loop/acceptance/test_guarded.py <<'PYEOF2'
import pytest, sys
try:
    from packages.beta import y            # guarded module-level import
except ImportError:
    y = None

@pytest.mark.ticket("T-001")
def test_a(): assert True

if sys.version_info >= (3, 8):
    @pytest.mark.ticket("T-001")
    def test_nested():                     # a test defined inside an `if`
        assert True
PYEOF2
cat > .swarm-loop/acceptance/test_pytestmark.py <<'PYEOF3'
import pytest
pytestmark = pytest.mark.ticket("T-001")
from packages.beta import y
def test_b(): assert True
PYEOF3
gq add -A; gq commit -qm scan
sl reads --json
ck_exit "(v) reads runs" 0 "$RC"
ck "(v) a test nested in an if-block is SCANNED (was invisible)" "3" \
   "$(python3 -c "import json;print(json.load(open('$OUT'))['tests_scanned'])" 2>/dev/null || echo ERR)"
ck "(v) pytestmark marks the file, so nothing reads as unmarked" "0" \
   "$(python3 -c "import json;print(json.load(open('$OUT'))['unmarked_tests'])" 2>/dev/null || echo ERR)"
ck "(v) the guarded import IS a read edge T-001 -> T-002" "['packages.beta']" \
   "$(python3 -c "import json;print(json.load(open('$OUT'))['reads'].get('T-001',{}).get('T-002'))" 2>/dev/null || echo ERR)"
ck "(v) conftest.py is scanned as a test file" "True" \
   "$(python3 -c "
import re,sys
sys.path.insert(0,'$SL_DIR')
import importlib.util
spec=importlib.util.spec_from_file_location('sl','$SL'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(bool(m.READ_TEST_RE.match('conftest.py')))")"

# ===========================================================================
# (w) goal-setting.md: "Zero frozen tests is not a row in a table; it stops
#     the freeze." It never did.
# ===========================================================================
section "(w) zero-coverage tickets stop the freeze  [repo: zcov]"
mk_repo zcov
mkdir -p packages/alpha packages/beta
echo "x = 1" > packages/alpha/__init__.py
echo "y = 1" > packages/beta/__init__.py
cat > tickets.json <<'EOF'
{"tickets":[
 {"id":"T-001","title":"alpha","verify":"true","scope":["packages/alpha/**"],"depends_on":[]},
 {"id":"T-002","title":"beta","verify":"true","scope":["packages/beta/**"],"depends_on":[]}]}
EOF
rm -f .swarm-loop/acceptance/test_feature.py
printf 'import pytest\n@pytest.mark.ticket("T-001")\ndef test_a(): assert True\n' \
  > .swarm-loop/acceptance/test_a.py
gq add -A; gq commit -qm zc
sl freeze
ck_exit "(w) freeze REFUSES a ticket with zero frozen tests" 1 "$RC"
ck_has "(w) the uncovered ticket is named" "T-002"
ck_has "(w) and the count is stated" "TICKET(S) CARRY NO FROZEN TEST AT ALL"
ck "(w) goals were NOT frozen by the refusal" "False" \
   "$(jget .swarm-loop/goals.json "d['frozen']")"

sl freeze --allow-read-violations
ck_exit "(w) --allow-read-violations is the documented override" 0 "$RC"
ck_has "(w) which says so rather than passing silently" "ADVISORY only"

# CONTROL: full coverage must freeze cleanly
mk_repo zcov2
mkdir -p packages/alpha packages/beta
echo "x = 1" > packages/alpha/__init__.py
echo "y = 1" > packages/beta/__init__.py
cat > tickets.json <<'EOF'
{"tickets":[
 {"id":"T-001","title":"alpha","verify":"true","scope":["packages/alpha/**"],"depends_on":[]},
 {"id":"T-002","title":"beta","verify":"true","scope":["packages/beta/**"],"depends_on":[]}]}
EOF
rm -f .swarm-loop/acceptance/test_feature.py
printf 'import pytest\n@pytest.mark.ticket("T-001")\ndef test_a(): assert True\n' > .swarm-loop/acceptance/test_a.py
printf 'import pytest\n@pytest.mark.ticket("T-002")\ndef test_b(): assert True\n' > .swarm-loop/acceptance/test_b.py
gq add -A; gq commit -qm zc2
sl freeze
ck_exit "(w) CONTROL: every ticket covered freezes cleanly" 0 "$RC"
ck_hasnt "(w) CONTROL: no coverage complaint" "CARRY NO FROZEN TEST"

# CONTROL: a project with NO ticket graph is a silent no-op
mk_repo zcov3
gq add -A; gq commit -qm zc3
sl freeze
ck_exit "(w) CONTROL: no ticket graph, freeze is unaffected" 0 "$RC"
ck_hasnt "(w) CONTROL: and says nothing about coverage" "CARRY NO FROZEN TEST"


# ===========================================================================
# (x) Verdicts bound to the SHA they judged, and freeze refusing a metric that
#     destroys its own evidence. Both were rules the docs stated and nothing
#     enforced.
# ===========================================================================
section "(x) dispatch --branch pins the tree a verdict is about  [repo: dsha]"
mk_repo dsha
# Commit the ledger on main FIRST. mk_repo leaves .swarm-loop/ untracked, and a
# later `add -A` on the branch would commit it THERE — so checking main back out
# deletes dispatch.jsonl from the working tree and the close finds no prior
# record. That is a fixture trap, not a defect, and it cost three red cases.
gq add -A; gq commit -qm ledger
gq branch task/T-010
sl dispatch --agent v1 --kind verifier --subject T-010 --branch task/T-010
ck_exit "(x) dispatch records the branch it judges" 0 "$RC"
ck_has "(x) and prints the tip it is bound to" "judging task/T-010@"
SHA1="$(git rev-parse task/T-010)"
# the branch moves under the agent — the fix->re-verify loop merge-protocol.md
# describes and nothing re-pinned
gq checkout -q task/T-010
echo "# a fix landed while the verifier was out" >> src/app.py
gq add src/app.py; gq commit -qm fix          # ONLY the product file
gq checkout -q main
sl dispatch --close v1 --branch task/T-010
ck_exit "(x) closing it exits 0" 0 "$RC"
ck_has "(x) a branch that MOVED under the agent is reported, not accepted" "MOVED since dispatch"
ck_has "(x) and says why that verdict is void" "is not a green"
ck "(x) the close record carries the NEW sha, not the dispatch-time one" "$(git rev-parse task/T-010)" \
   "$(python3 -c "
import json
recs=[json.loads(l) for l in open('.swarm-loop/dispatch.jsonl') if l.strip()]
print([r for r in recs if r.get('action')=='close'][-1].get('sha'))")"
ck "(x) and the open record kept the ORIGINAL sha" "$SHA1" \
   "$(python3 -c "
import json
recs=[json.loads(l) for l in open('.swarm-loop/dispatch.jsonl') if l.strip()]
print([r for r in recs if r.get('action')=='open'][-1].get('sha'))")"

# an UNMOVED branch must not cry wolf
gq branch task/T-011
sl dispatch --agent v2 --kind verifier --subject T-011 --branch task/T-011
sl dispatch --close v2 --branch task/T-011
ck_has "(x) CONTROL: an unmoved branch says so plainly" "unmoved since dispatch"
ck_hasnt "(x) CONTROL: and does not warn" "MOVED since dispatch"

section "(x2) freeze refuses a metric that discards its failure output  [repo: dnull]"
mk_repo dnull
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["command"] = "if cat score.txt >/dev/null 2>&1; then echo 100; else echo 0; fi"
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm dn
sl freeze
ck_exit "(x2) freeze REFUSES a metric redirecting to /dev/null" 1 "$RC"
ck_has "(x2) naming what it destroys" "discards its own failure output"
ck_has "(x2) and offering the fix" "reports/"
ck "(x2) goals were NOT frozen" "False" "$(jget .swarm-loop/goals.json "d['frozen']")"

# every spelling a shell actually uses
for spell in '2>/dev/null' '&>/dev/null' '> /dev/null'; do
  python3 - "$spell" <<'PY'
import json, sys
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["command"] = "cat score.txt %s; echo 100" % sys.argv[1]
json.dump(g, open(p, "w"), indent=2)
PY
  sl freeze
  ck_exit "(x2) refused for spelling $spell" 1 "$RC"
done

# CONTROL: redirecting to a FILE keeps the evidence and must freeze
mk_repo dnull2
mkdir -p .swarm-loop/reports
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"][0]["command"] = "cat score.txt > .swarm-loop/reports/accept-last.log 2>&1; cat score.txt"
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm dn2
sl freeze
ck_exit "(x2) CONTROL: redirecting to a file still freezes" 0 "$RC"
ck_hasnt "(x2) CONTROL: no complaint" "discards its own failure output"


# ===========================================================================
# W48 · the scheduler was INVERTED because closure never entered it
#
# Measured on a live run: `frontier` printed `108 ticket(s) · 0 closed · 27
# READY · 81 blocked` and ranked `T-000 depth 0 unblocks 81 "Monorepo skeleton"`
# first — a ticket that had shipped long before — while the five tickets worth
# dispatching read `blocked`. No ticket field is read for status anywhere in
# swarmloop.py; closure enters ONLY through these flags, and the flags were
# documented NOWHERE, so an orchestrator following SKILL.md literally could
# only ever get `0 closed`. Four defects, one section:
#   (1) `0 closed` printed as if it were a measurement, with no red path.
#   (2) a zero-commit provisioned lane read as CLOSED (false-closed).
#   (3) one branch name closed two tickets (false-closed).
#   (4) closure derived from git is erased by `retire`.
# ===========================================================================
section "W48 frontier closure derivation  [repo: rclose]"
mk_repo rclose
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true",
  "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "alpha", "verify": "false",
  "scope": ["a/**"], "depends_on": ["T-000"]},
 {"id": "T-002", "title": "beta", "verify": "false",
  "scope": ["b/**"], "depends_on": ["T-001"]},
 {"id": "T-003", "title": "gamma", "verify": "false",
  "scope": ["c/**"], "depends_on": ["T-000"]},
 {"id": "T-010", "title": "delta", "verify": "false",
  "scope": ["d/**"], "depends_on": ["T-000"]},
 {"id": "T-011", "title": "epsilon", "verify": "false",
  "scope": ["e/**"], "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
# T-000 lands for real.
gq checkout -b task/T-000
mkdir -p scaffold; echo x > scaffold/x; gq add -A; gq commit -qm "T-000 scaffold"
gq checkout main; gq merge --no-ff -m "merge T-000" task/T-000
# A landed branch whose NAME mentions a second ticket that was never built.
gq checkout -b task/T-010-supersedes-T-011
mkdir -p d; echo x > d/x; gq add -A; gq commit -qm "T-010"
gq checkout main; gq merge --no-ff -m "merge T-010" task/T-010-supersedes-T-011
# T-003: a lane provisioned and never dispatched. `git branch task/T-003 main`
# is trivially `--merged main`, and Phase 3 calls this "the normal state".
gq branch task/T-003 main

# --- (1) the no-source warning, and the count it is about --------------------
sl frontier
ck_exit "W48 frontier with no closure source still exits 0" 0 "$RC"
ck_has "W48 '0 closed' with no source is called out, not printed as a measurement" \
       "NO CLOSURE SOURCE WAS SUPPLIED"
ck_has "W48 the warning names every flag that would fix it" \
       "--closed T-001,T-002  ·  --closed-from-merged  ·  --closed-from-ledger"
ck_has "W48 the warning says the ready set is the graph's ROOTS, not a queue" \
       "the graph's ROOTS rather than a work queue"
# EVIDENCE, not heuristic: a task/* branch that LANDED is proof this is not a
# first wave. A greenfield cycle-1 run has none and must not see this line.
ck_has "W48 landed task branches are cited as evidence the run is past wave 1" \
       "EVIDENCE that this run is NOT at its first wave"
# ...and the numbers themselves are untouched. The live build is running against
# this file; a changed count under it is the defect class, not the fix.
ck_has "W48 UNCHANGED: the count line is what it always was" \
       "6 ticket(s)  ·  0 closed  ·  1 READY  ·  5 blocked"
ck_has "W48 UNCHANGED: with no closure the shipped root still ranks first" \
       "T-000     depth 0   unblocks 5"

# --- (1b) supplying a source SILENCES the loud line ---------------------------
sl frontier --closed T-000
ck_exit "W48 frontier --closed exits 0" 0 "$RC"
ck_hasnt "W48 the loud line is ABSENT once a closure source is supplied" \
         "NO CLOSURE SOURCE WAS SUPPLIED"
ck_hasnt "W48 ...and the evidence line goes with it" \
         "EVIDENCE that this run is NOT at its first wave"
ck_has "W48 supplied closure actually moves the number" \
       "1 closed  ·  4 READY  ·  1 blocked"

# --- (2) + (3) the two false-CLOSED defects in --closed-from-merged -----------
sl frontier --closed-from-merged --json
ck_exit "W48 --closed-from-merged --json exits 0" 0 "$RC"
ck "W48 a zero-commit provisioned lane is NOT closed; a two-id branch closes ONE" \
   "T-000,T-010" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48 the un-closed provisioned lane is REPORTED, not silently dropped" \
       "UNDETERMINED task/T-003 (T-003)"
# W48c REPLACES the old assertion here, which pinned the message "the lane has
# ZERO commits of its own. Provisioned, not built." That message was a FACT the
# command could not establish: `tip == base_sha` is equally true of a branch
# merged --ff-only (git's default when base has not moved), and W48c-2 shows it
# printed verbatim about a lane whose work is demonstrably IN main. The
# assertion was pinning a false statement, so it moves to what is actually
# determined — that the two shapes are indistinguishable and the lane stays open.
ck_has "W48 ...saying what could NOT be determined, never asserting 'not built'" \
       "Nothing here can tell those apart, so it stays OPEN"
ck_hasnt "W48 the un-establishable reason is gone for good" \
         "Provisioned, not built."
ck_has "W48 the two-ticket branch name says which id it closed" \
       "task/T-010-supersedes-T-011 closes T-010 ONLY"
ck_has "W48 the falsely-closed lane is back in the ready set" "T-003     depth 1"
ck_has "W48 the never-built second id is back in the ready set" "T-011     depth 1"

# --- (4) closure that `retire` cannot erase ----------------------------------
sl dispatch --agent w-000 --kind ticket --subject T-000 --branch task/T-000
sl dispatch --close w-000 --ticket T-000 --verdict accepted --note "verifier green"
ck_exit "W48 dispatch --close --ticket --verdict is accepted" 0 "$RC"
ck_has "W48 the close says the record outlives the branch" "ledger: T-000 -> accepted"
sl dispatch --agent w-001 --kind ticket --subject T-001 --branch task/T-000
sl dispatch --close w-001 --ticket T-001 --verdict rejected --note "gate was vacuous"
sl frontier --closed-from-ledger --json
ck "W48 --closed-from-ledger closes the ACCEPTED ticket and only that one" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-ledger
ck_has "W48 the REJECTED verdict is reported as still open, never as closed" \
       "carry a REJECTED verdict and stay OPEN: T-001"
ck_hasnt "W48 a ledger source silences the no-source warning too" \
         "NO CLOSURE SOURCE WAS SUPPLIED"

# `retire` deletes every landed task branch (_landed_branches). That is what
# makes git-derived closure NON-MONOTONIC: the work stays in main, the signal
# does not. Run the real command, not a hand-rolled `git branch -D`.
sl retire
ck_exit "W48 retire runs" 0 "$RC"
ck_has "W48 retire deleted the landed branch this closure was derived from" \
       "branches deleted (landed on main): task/T-000"
sl frontier --closed-from-merged --json
ck "W48 AFTER retire, git-derived closure has FORGOTTEN the landed work" \
   "" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48 a source that was supplied and found nothing says so differently" \
       "a closure source WAS supplied"
ck_hasnt "W48 ...and is NOT the no-source alarm" "NO CLOSURE SOURCE WAS SUPPLIED"
sl frontier --closed-from-ledger --json
ck "W48 AFTER retire, the ledger verdict SURVIVES the branch's deletion" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"

# --- the new dispatch flags refuse to be recorded where they mean nothing -----
sl dispatch --agent w-x --kind ticket --subject T-002 --ticket T-002
ck_exit "W48 --ticket on an OPEN dispatch is refused at intake (exit 3)" 3 "$RC"
sl dispatch --agent w-y --kind ticket --subject T-002 --verdict accepted
ck_exit "W48 --verdict on an OPEN dispatch is refused at intake (exit 3)" 3 "$RC"
sl dispatch --agent w-002 --kind ticket --subject T-002
sl dispatch --close w-002 --ticket T-002
ck_has "W48 --ticket with NO --verdict is loudly inert, not silently inert" \
       "recorded with NO --verdict, so it closes NOTHING"
sl dispatch --agent w-003 --kind ticket --subject T-003
sl dispatch --close w-003 --verdict accepted
ck_has "W48 --verdict naming no ticket is loudly inert too" \
       "names no ticket (--ticket T-nnn)"
sl frontier --closed-from-ledger --json
ck "W48 neither half alone closes anything" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"

# --- a genuinely first-wave run must NOT see the evidence line ----------------
# This is the false-positive the warning could have had: cycle 1 legitimately
# has zero closed and legitimately ranks the scaffold ticket first.
section "W48b a first-wave run  [repo: rclose2]"
mk_repo rclose2
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"],
  "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
sl frontier
ck_has "W48b the no-source line still fires on a first-wave run" \
       "NO CLOSURE SOURCE WAS SUPPLIED"
ck_hasnt "W48b but nothing claims it is past wave 1 — no landed branch exists" \
         "EVIDENCE that this run is NOT at its first wave"
ck_has "W48b the quiet case says CONSISTENT WITH, and admits what it cannot see" \
       "CONSISTENT with a first wave, and proof of nothing"
# A git query that FAILED must not read as "nothing landed" — that is the same
# silent-wrong the banner exists to kill, one level down.
sl frontier --base no-such-base
ck_has "W48b a --base git cannot resolve is reported, not read as 'nothing landed'" \
       "COULD NOT LOOK:"
ck_hasnt "W48b ...and it does NOT then claim the run is consistent with a first wave" \
         "CONSISTENT with a first wave, and proof of nothing"


# ===========================================================================
# W48c · the two false-CLOSED guards W48 shipped were both wrong
#
# W48 claimed `--closed-from-merged` "can only REDUCE what is reported closed".
# It cannot. Measured, all four with real output:
#
#  (1) the zero-commit guard `tip == base_sha` fires ONLY while --base has not
#      moved. In a swarm main advances continuously, so it essentially never
#      fired: two provisioned lanes plus one merged ticket read `3 closed` and
#      the frontier reported the whole project finished. W48's own case passed
#      only because its fixture provisions task/T-003 AFTER the merges — the one
#      construction order where the predicate holds. W48c-1 builds the ORDER A
#      SWARM ACTUALLY PRODUCES.
#  (2) the same predicate is true of `git merge --ff-only` — git's default when
#      main has not moved — so genuinely landed work printed "the lane has ZERO
#      commits of its own. Provisioned, not built." about a branch whose files
#      are in main. A reason that was never established, in the FALSE direction.
#  (3) the banner's evidence line called two provisioned lanes "LANDED on main"
#      on a greenfield wave 1 where nothing had ever been built.
#  (4) two surviving mutants: the branch-name anchoring had NO covering
#      assertion (the commit message called it the fix), and last-verdict-wins
#      had no case giving one ticket two verdicts.
#
# `rev-list --count base..branch == 0` and `tip == base_sha` are BOTH true of a
# provisioned lane and of a properly merged one; neither discriminates. Closure
# is therefore driven by POSITIVE evidence only — a ledger-pinned tip the branch
# has moved past, or a tip off base's first-parent chain — and everything else
# is UNDETERMINED and stays OPEN.
# ===========================================================================
section "W48c-1 provisioned BEFORE the merges — the order a swarm produces  [repo: rclose3]"
mk_repo rclose3
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"],
  "depends_on": ["T-000"]},
 {"id": "T-003", "title": "gamma", "verify": "false", "scope": ["c/**"],
  "depends_on": ["T-000"]},
 {"id": "T-004", "title": "zeta", "verify": "false", "scope": ["z/**"],
  "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
# Lanes are provisioned FIRST — Phase 3 calls this "the normal state" — and THEN
# main advances, which is what makes `tip == base_sha` stop holding.
gq branch task/T-003 main
gq branch task/T-004 main
gq checkout -b task/T-000
mkdir -p scaffold; echo x > scaffold/x; gq add -A; gq commit -qm "T-000 scaffold"
gq checkout main; gq merge --no-ff -m "merge T-000" task/T-000
sl frontier --closed-from-merged --json
ck_exit "W48c-1 --closed-from-merged --json exits 0" 0 "$RC"
ck "W48c-1 two zero-commit lanes stay OPEN once main has moved past them" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48c-1 the first lane is named, not silently closed" \
       "UNDETERMINED task/T-003 (T-003)"
ck_has "W48c-1 ...and so is the second" "UNDETERMINED task/T-004 (T-004)"
ck_has "W48c-1 the ticket that really landed says WHICH evidence closed it" \
       "closed on first-parent evidence"
ck_has "W48c-1 both un-closed lanes are back in the ready set" "T-003     depth 1"
ck_has "W48c-1 ...both of them" "T-004     depth 1"
ck_hasnt "W48c-1 nothing asserts a lane was 'provisioned, not built'" \
         "Provisioned, not built."

section "W48c-2 a FAST-FORWARD merge is not an unbuilt lane  [repo: rclose4]"
mk_repo rclose4
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"],
  "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
gq branch task/T-000 main
# The dispatch record pins the tip BEFORE the lane has committed anything. That
# pin is the only thing that can later prove the lane moved.
sl dispatch --agent w-000 --kind ticket --subject T-000 --branch task/T-000
gq checkout task/T-000
mkdir -p scaffold; echo x > scaffold/x; gq add -A; gq commit -qm "T-000 real work"
gq checkout main
gq merge --ff-only task/T-000
ck "W48c-2 FIXTURE: the merge really was a fast-forward (tips are identical)" \
   "same" "$([ "$(git rev-parse main)" = "$(git rev-parse task/T-000)" ] && echo same)"
ck_file "W48c-2 FIXTURE: the work really is in main" scaffold/x
# --- with the ledger moved aside: git alone genuinely cannot tell ------------
mv .swarm-loop/dispatch.jsonl .swarm-loop/dispatch.jsonl.off
sl frontier --closed-from-merged --json
ck "W48c-2 with no ledger the lane is NOT closed — false-OPEN is the safe way to be wrong" \
   "" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_hasnt "W48c-2 landed work is NEVER called 'Provisioned, not built'" \
         "Provisioned, not built."
ck_has "W48c-2 the note names fast-forward as the shape it cannot rule out" \
       "of a lane merged FAST-FORWARD"
ck_has "W48c-2 ...and says what to record to settle it" \
       "--verdict accepted."
sl frontier
ck_hasnt "W48c-2 the banner does NOT call this consistent with a first wave" \
         "CONSISTENT with a first wave, and proof of nothing"
ck_has "W48c-2 it reports the lane it could not classify instead" \
       "UNDETERMINED: 1 task/* branch(es) are merged into main"
# --- with the ledger back: the pinned tip settles it ------------------------
mv .swarm-loop/dispatch.jsonl.off .swarm-loop/dispatch.jsonl
sl frontier --closed-from-merged --json
ck "W48c-2 the ledger-pinned tip PROVES the lane committed, so it closes" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48c-2 ...and says the ledger is what proved it" \
       "the dispatch ledger pinned task/T-000@"
ck_has "W48c-2 ...naming the move it measured" "its tip has since moved to"

section "W48c-3 greenfield wave 1 is never called 'landed'  [repo: rclose5]"
mk_repo rclose5
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"],
  "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
# Two lanes provisioned, NOTHING ever built, and main takes one ordinary commit.
gq branch task/T-000 main
gq branch task/T-001 main
echo note > NOTES.md; gq add -A; gq commit -qm "an ordinary commit on main"
sl frontier
ck_hasnt "W48c-3 zero branches landed, so nothing claims any did" \
         "have a task/* branch LANDED on main"
ck_has "W48c-3 the no-source alarm still fires" "NO CLOSURE SOURCE WAS SUPPLIED"
ck_has "W48c-3 the two lanes are reported as unclassifiable, which is what they are" \
       "UNDETERMINED: 2 task/* branch(es) are merged into main"
sl frontier --closed-from-merged --json
ck "W48c-3 and neither one closes a ticket" \
   "" "$(jget "$OUT" '",".join(d["closed"])')"

section "W48c-4 the ticket id is ANCHORED at the front of the branch name  [repo: rclose6]"
mk_repo rclose6
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-000", "title": "scaffold", "verify": "true", "scope": ["**"], "depends_on": []},
 {"id": "T-030", "title": "thirty", "verify": "false", "scope": ["c/**"],
  "depends_on": ["T-000"]},
 {"id": "T-040", "title": "forty", "verify": "false", "scope": ["d/**"],
  "depends_on": ["T-000"]}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
# A branch that really landed and really closes its ticket — the control.
gq checkout -b task/T-000
mkdir -p scaffold; echo x > scaffold/x; gq add -A; gq commit -qm "T-000"
gq checkout main; gq merge --no-ff -m "merge T-000" task/T-000
# Two shapes that must close NOTHING. Both carry real landed commits, so the
# only thing standing between them and a false close is the anchoring: an
# unanchored scan finds T-030 in "T-030x" and T-040 in "sub/T-040".
gq checkout -b task/T-030x main
mkdir -p c; echo x > c/x; gq add -A; gq commit -qm "T-030x"
gq checkout main; gq merge --no-ff -m "merge T-030x" task/T-030x
gq checkout -b task/sub/T-040 main
mkdir -p d; echo x > d/x; gq add -A; gq commit -qm "sub/T-040"
gq checkout main; gq merge --no-ff -m "merge sub/T-040" task/sub/T-040
sl frontier --closed-from-merged --json
ck "W48c-4 a trailing suffix and a nested path close NOTHING; the plain one closes" \
   "T-000" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48c-4 'T-030x' is reported, not read as T-030" \
       "SKIPPED task/T-030x — no T-nnn ticket id at the front"
ck_has "W48c-4 'sub/T-040' is reported, not read as T-040" \
       "SKIPPED task/sub/T-040 — no T-nnn ticket id at the front"
ck_has "W48c-4 both un-closed tickets are still in the ready set" "T-030     depth 1"
ck_has "W48c-4 ...both of them" "T-040     depth 1"

section "W48c-5 one ticket, two verdicts: the LAST one wins  [repo: rclose7]"
mk_repo rclose7
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"], "depends_on": []},
 {"id": "T-002", "title": "beta", "verify": "false", "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
# T-001: accepted in one cycle, then REJECTED by a later audit -> open again.
sl dispatch --agent a1 --kind ticket --subject T-001
sl dispatch --close a1 --ticket T-001 --verdict accepted --note "cycle 1 green"
sl dispatch --agent a2 --kind auditor --subject T-001
sl dispatch --close a2 --ticket T-001 --verdict rejected --note "audit: the gate was vacuous"
# T-002: rejected first, then FIXED and accepted -> closed. The other direction,
# so an implementation that simply prefers `rejected` cannot pass this pair.
sl dispatch --agent b1 --kind ticket --subject T-002
sl dispatch --close b1 --ticket T-002 --verdict rejected --note "first attempt red"
sl dispatch --agent b2 --kind ticket --subject T-002
sl dispatch --close b2 --ticket T-002 --verdict accepted --note "fixed and re-verified"
sl frontier --closed-from-ledger --json
ck_exit "W48c-5 --closed-from-ledger exits 0" 0 "$RC"
ck "W48c-5 accepted-then-rejected reopens; rejected-then-accepted closes" \
   "T-002" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-ledger
ck_has "W48c-5 the re-opened ticket is named as still OPEN" \
       "carry a REJECTED verdict and stay OPEN: T-001"
ck_has "W48c-5 the re-opened ticket is back in the ready set" "T-001     depth 0"
# L3 — the ledger is a trust boundary and the READ side is where that is said.
ck_has "W48c-5 closure from the ledger admits it re-verifies nothing" \
       "closed on an OPERATOR-RECORDED verdict, not on anything re-verified here"
ck_has "W48c-5 ...and names the consequence exactly" \
       "a record naming a branch that never existed closes its ticket exactly like a real one"
# A hand-appended malformed line is COUNTED, never silently dropped.
echo 'this is not json' >> .swarm-loop/dispatch.jsonl
printf '\n' >> .swarm-loop/dispatch.jsonl
echo '{"action": "close", "verdict": "accepted", "tickets": ["T-001"]' >> .swarm-loop/dispatch.jsonl
sl frontier --closed-from-ledger
ck_has "W48c-5 unparseable ledger lines are counted and reported" \
       "2 unparseable line(s) in dispatch.jsonl were skipped"
sl frontier --closed-from-ledger --json
ck "W48c-5 ...and a truncated record does NOT close the ticket it names" \
   "T-002" "$(jget "$OUT" '",".join(d["closed"])')"

section "W48c-6 a merged-then-REVERTED branch: the known limit, stated  [repo: rclose8]"
mk_repo rclose8
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
gq checkout -b task/T-001
mkdir -p a; echo x > a/x; gq add -A; gq commit -qm "T-001 work"
gq checkout main; gq merge --no-ff -m "merge T-001" task/T-001
gq revert -m 1 --no-edit HEAD
ck_nofile "W48c-6 FIXTURE: the revert really removed the work from main" a/x
ck "W48c-6 FIXTURE: git still lists the branch as merged (reachability survives)" \
   "task/T-001" "$(git branch --merged main --list 'task/*' | tr -d ' *+')"
sl frontier --closed-from-merged --json
# NOT a fix — a PINNED LIMIT. `git branch --merged` answers a reachability
# question, and a revert changes presence without changing reachability. W48's
# claim that this guard "can only under-report" was false; this case makes the
# real behaviour visible so it cannot be claimed away again.
ck "W48c-6 KNOWN LIMIT: a reverted ticket still reads CLOSED here" \
   "T-001" "$(jget "$OUT" '",".join(d["closed"])')"
sl frontier --closed-from-merged
ck_has "W48c-6 ...and the limit is STATED wherever anything closes" \
       "this is REACHABILITY, not presence"
ck_has "W48c-6 ...naming revert as the case, and the ledger as the way out" \
       "Only a REJECTED ledger verdict reopens one."

section "W48c-7 the unknown-id note names its real source  [repo: rclose9]"
mk_repo rclose9
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-001", "title": "alpha", "verify": "false", "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm "ticket graph"
sl frontier --closed T-001,T-999
ck_exit "W48c-7 frontier with an unknown closed id still exits 0" 0 "$RC"
# L2: this note used to carry `or "--closed"` as a fallback that could not run —
# `closed` is non-empty only when a source ran, and every source records itself
# BEFORE contributing. The fallback is gone; this is the note's live path.
ck_has "W48c-7 the note names the source that supplied the unknown id" \
       "NOTE: closure (--closed) named ids that are not in the graph: T-999"
# ...and the source it names is the one that really supplied it, not a constant.
# The dead `or "--closed"` arm meant a hard-coded name would have read correctly
# on the only path the suite exercised.
sl dispatch --agent w-x --kind ticket --subject T-998
sl dispatch --close w-x --ticket T-998 --verdict accepted
sl frontier --closed-from-ledger
ck_has "W48c-7 a DIFFERENT source is named as itself, not as '--closed'" \
       "NOTE: closure (--closed-from-ledger) named ids that are not in the graph: T-998"

# --- and --help must not go on teaching the predicate that was removed --------
# SKILL.md and `--help` both documented the guard as skipping "a branch whose
# tip IS --base", which is a verbatim restatement of a check that essentially
# never fired and that mis-classified every fast-forward merge. A doc that
# teaches an orchestrator to trust it is the defect wearing a third face.
# COLUMNS is load-bearing: argparse wraps help to the terminal width, and a
# ck_hasnt against a string the formatter has split across two lines passes
# vacuously — it would "prove" the removal of text that was still there.
COLUMNS=400 sl frontier --help
ck_exit "W48c-7 frontier --help exits 0" 0 "$RC"
ck_has "W48c-7 CONTROL: the help really was captured, unwrapped" \
       "also treat as closed a ticket whose task/* branch is merged into --base"
ck_hasnt "W48c-7 --help no longer teaches the tip-IS-base rule" \
         "a branch whose tip IS --base"
ck_hasnt "W48c-7 ...nor its justification" "zero commits is a provisioned lane"
ck_has "W48c-7 --help states the rule that is actually implemented" \
       "POSITIVELY shown to have carried work"
ck_has "W48c-7 --help says being --merged proves nothing by itself" \
       "Being --merged proves nothing on its own"
ck_has "W48c-7 --help names last-verdict-wins for the ledger" \
       "LAST verdict per ticket wins"


# ===========================================================================
# (y) push-gate — the Phase 7 gate that no epoch could ever satisfy.
#
# D-1: the gate's last condition was "everything .swarm-loop/ holds is
# committed", evaluated inside Phase 7 — while Phase 7's own four commands write
# into .swarm-loop/ (measure appends history.csv, analyze writes
# analysis/cycle-N.json, checkpoint rewrites state.json). The condition was false
# the instant the sequence finished, in every epoch of every run, forever, and
# the failure was SILENT: an orchestrator that never pushes prints nothing about
# not pushing, so the run's only offsite copy was withheld by a gate nobody could
# satisfy. The prose now fires the push at the END of Phase 8; this battery pins
# the mechanical half, including the two rungs the command deliberately REFUSES
# to score.
# ===========================================================================
section "(y) push-gate: the gate no epoch could satisfy  [repo: pgate]"
mk_repo pgate
sl freeze
gq add -A; gq commit -qm freeze
sl measure --cycle 1 --timeout 60
ck_exit "(y) cycle 1 measures" 0 "$RC"
sl analyze --cycle 1
sl checkpoint --cycle 1
ck_exit "(y) cycle 1 checkpoints" 0 "$RC"

sl push-gate --cycle 1
ck_exit "(y) D-1 REPRODUCED: the gate refuses straight after Phase 7's commands" 1 "$RC"
ck_has "(y) naming the rung that cannot be met inside Phase 7" "swarm-committed"
ck_has "(y) and the file measure just appended" ".swarm-loop/history.csv"
ck_has "(y) and the file analyze just wrote" "analysis/cycle-1.json"
ck_has "(y) verdict is unambiguous" "push-gate: DO NOT PUSH"
# Pin the per-rung STATUS, not just the prose beside it. Asserting on the detail
# text alone is green whichever verdict the rung reached — the same defect this
# file catalogues, one layer down.
ck_has "(y) the committed rung is marked FAIL" \
       'FAIL          everything `.swarm-loop/` holds is committed'
ck_has "(y) and the rungs Phase 7 CAN satisfy are marked PASS" \
       'PASS          `verify` passed this epoch'
ck_has "(y) including the analysis rung" 'PASS          `analyze` did not exit 1'
ck_has "(y) and the tracked-file rung" "PASS          this epoch's tracked-file count"

# The same command, same cycle, after Phase 8's ledger commit. This is the whole
# claim of the reorder: the condition is satisfiable HERE and nowhere earlier.
gq add .swarm-loop; gq commit -qm "epoch 1 ledger"
sl push-gate --cycle 1
ck_exit "(y) and CLEARS after Phase 8's ledger commit — same cycle, same gate" 0 "$RC"
ck_has "(y) saying only the mechanical conditions are met" "the mechanical conditions are MET"
ck_has "(y) exit 0 is explicitly not permission" "This is not permission to push"
# The two conditions no command can answer are printed on the PASS path too. A
# gate that scores five of seven while LOOKING like seven is the defect class
# this whole file exists to catch.
ck_has "(y) the unscorable integration condition is printed on the PASS path" \
       "OPERATOR      this epoch's integrations landed green"
ck_has "(y) so is the unscorable findings condition" \
       "OPERATOR      no rung-2 verifier finding invalidates"
ck_has "(y) and the count of what WAS scored is stated" \
       "5 of the gate's conditions are mechanical"

# A cycle that never happened must not pass vacuously.
sl push-gate --cycle 2
ck_exit "(y) a cycle nobody measured cannot push" 1 "$RC"
ck_has "(y) naming the missing analysis" "analysis/cycle-2.json does not exist"
ck_has "(y) and the missing checkpoint" "has no entry in state.json head_history"
ck_has "(y) the analysis rung is marked FAIL, not merely mentioned" \
       'FAIL          `analyze` did not exit 1'
ck_has "(y) and so is the tracked-file rung" \
       "FAIL          this epoch's tracked-file count"

# Rung 2 is async: an outstanding verifier is an unmet condition, not a pass.
sl dispatch --agent V-1 --kind verifier --subject "batch e1-b1"
gq add .swarm-loop; gq commit -qm dispatched
sl push-gate --cycle 1
ck_exit "(y) an outstanding rung-2 verifier blocks the push" 1 "$RC"
ck_has "(y) naming the verifier that has not returned" "V-1"
ck_has "(y) as the rung it is" "rung2-returned"

# The counterpart, and it is the load-bearing half: dispatch is ROLLING, so an
# open TICKET dispatch is the normal state of a wide swarm. Blocking on every
# open dispatch would rebuild the unsatisfiable gate in a new place.
sl dispatch --close V-1
sl dispatch --agent W-9 --kind ticket --subject T-900
gq add .swarm-loop; gq commit -qm "rolling dispatch"
sl push-gate --cycle 1
ck_exit "(y) an open TICKET dispatch does NOT block — dispatch is rolling" 0 "$RC"
ck_has "(y) it is reported as context instead of a veto" "not a push blocker"

# A violated harness voids the scores, so there is no number to have read.
echo "def test_injected(): assert True" >> .swarm-loop/acceptance/test_feature.py
sl push-gate --cycle 1
ck_exit "(y) a violated harness blocks the push" 1 "$RC"
ck_has "(y) named as an integrity violation, not a bookkeeping miss" "harness VIOLATED"
ck_has "(y) with the verify rung marked FAIL, not merely annotated" \
       'FAIL          `verify` passed this epoch'
write_acceptance_test
sl verify
ck_exit "(y) control: verify is green again once the frozen bytes are back" 0 "$RC"

# A stale metric: 'analyze' exits 1, the table is not a verdict on the cycle,
# and the epoch has no values for a push to be carrying.
gq rm -q score.txt
gq commit -qm "break the measuring stick"
sl measure --cycle 2 --timeout 60
ck_exit "(y) measure exits 1 when a metric command fails" 1 "$RC"
sl analyze --cycle 2
ck_exit "(y) analyze exits 1 on a stale metric" 1 "$RC"
sl checkpoint --cycle 2
gq add .swarm-loop; gq commit -qm "epoch 2 ledger"
sl push-gate --cycle 2
ck_exit "(y) a stale metric blocks the push" 1 "$RC"
ck_has "(y) naming the metric with no row this cycle" "no measurement at cycle 2 for: accept"
ck_has "(y) carried by the analysis rung's own FAIL" 'FAIL          `analyze` did not exit 1'

# --push-remote never: the honest answer to "may I push?" is no, and the text
# has to say that is a configuration rather than a failed condition.
set_state push_remote '"never"'
sl push-gate --cycle 2
ck_exit "(y) --push-remote never answers no" 1 "$RC"
ck_has "(y) and says it is configuration, not a failure" "NOT APPLICABLE"
ck_hasnt "(y) scoring nothing at all in that mode" "OPERATOR"

# ===========================================================================
# (y2) The tracked-file rung, against checkpoint's OWN arithmetic — the two
#      sites must not disagree about what "sharp" means.
# ===========================================================================
section "(y2) push-gate: the tracked-file drop rung  [repo: pgdrop]"
mk_repo pgdrop
sl freeze
gq add -A; gq commit -qm freeze
sl measure --cycle 1 --timeout 60
sl analyze --cycle 1
sl checkpoint --cycle 1
mkdir -p bulk
i=1
while [ "$i" -le 60 ]; do echo "x" > "bulk/f$i.txt"; i=$((i + 1)); done
gq add -A; gq commit -qm bulk
sl measure --cycle 2 --timeout 60
sl analyze --cycle 2
sl checkpoint --cycle 2
gq add .swarm-loop; gq commit -qm "epoch 2 ledger"
sl push-gate --cycle 2
ck_exit "(y2) control: a tree that GREW pushes fine" 0 "$RC"
ck_has "(y2) with the rung marked PASS" "PASS          this epoch's tracked-file count"
ck_has "(y2) and both numbers printed" "tracked files"

gq rm -r -q bulk
gq commit -qm "index wipe shape"
sl measure --cycle 3 --timeout 60
sl analyze --cycle 3
sl checkpoint --cycle 3
ck_has "(y2) checkpoint's own tripwire fires on the drop" "TRACKED FILE COUNT DROPPED SHARPLY"
gq add .swarm-loop; gq commit -qm "epoch 3 ledger"
sl push-gate --cycle 3
ck_exit "(y2) and the push gate REFUSES on the same arithmetic" 1 "$RC"
ck_has "(y2) naming the rung" "tracked-files"
ck_has "(y2) marked FAIL, not merely reported" "FAIL          this epoch's tracked-file count"
ck_has "(y2) citing checkpoint's own threshold, not a second opinion" "max(25, 10%)"

# ===========================================================================
# (z) conflicts — pairwise TEXTUAL conflict between task branches, answered by
#     git rather than by declared globs.
#
# D-2: `merge-tree` appeared four times in this skill and all four were prose.
# check-wave's veto reasons about the ownership globs a ticket DECLARED and can
# only ever be as right as the declarations; nothing anywhere compared two
# branches' actual content. This is deliberately NOT a pre-dispatch check —
# at check-wave time every lane holds zero commits, so a tidy pairwise verdict
# there would be a green about work that does not exist yet.
# ===========================================================================
section "(z) conflicts: pairwise merge-tree verdicts  [repo: cflct]"
mk_repo cflct
printf 'A = 1\nB = 2\nC = 3\n' > shared.py
gq add -A; gq commit -qm shared

gq checkout -q -b task/T-101 main
printf 'A = 111\nB = 2\nC = 3\n' > shared.py
gq commit -qam T101
gq checkout -q main
gq checkout -q -b task/T-102 main
printf 'A = 999\nB = 2\nC = 3\n' > shared.py
gq commit -qam T102
gq checkout -q main
gq checkout -q -b task/T-103 main
printf 'A = 1\nB = 2\nC = 333\n' > shared.py
gq commit -qam T103
gq checkout -q main
gq checkout -q -b task/T-104 main
echo "x = 1" > only_mine.py
gq add -A; gq commit -qm T104
gq checkout -q main
gq branch task/T-105 main          # provisioned, never dispatched

sl conflicts T-101,T-102 T-103 T-104 T-105 T-999
ck_exit "(z) a conflicting pair takes the batch to exit 1" 1 "$RC"
ck_has "(z) the conflicting PAIR is named" "task/T-101 <-> task/T-102"
ck_has "(z) with the path git actually conflicted on" "! shared.py"
ck_has "(z) clean-but-same-file is ADVISORY, never a veto" "ADVISORY (merges clean)"
ck_has "(z) naming the file both sides touched" "task/T-101 <-> task/T-103   via  shared.py"
ck_has "(z) a genuinely disjoint pair reads CLEAN" "CLEAN     task/T-101 <-> task/T-104"
ck_has "(z) an EMPTY lane is named, never dropped" "task/T-105: 0 commits ahead"
ck_has "(z) a branch that does not exist is named too" "T-999: no such branch"
ck_has "(z) and the base-staleness blind spot is stated" "BLIND SPOT"
ck_hasnt "(z) a VETOed pair is not ALSO reported clean" "CLEAN     task/T-101 <-> task/T-102"
ck_hasnt "(z) nor advisory" "task/T-101 <-> task/T-102   via"

# A wholly clean batch: the blind spot is printed here TOO, which is the point of
# printing it — a clean pairwise answer is never permission to land.
sl conflicts T-103 T-104
ck_exit "(z) two disjoint branches exit 0" 0 "$RC"
ck_hasnt "(z) with no veto" "VETO:"
ck_has "(z) a bare ticket id resolves task/<id>" "CLEAN     task/T-103 <-> task/T-104"
ck_has "(z) the blind spot prints on the all-clean path as well" "BLIND SPOT"

# Base staleness is the half merge-tree structurally cannot see.
echo "main moved" > src/app.py
gq add -A; gq commit -qm "main moves"
sl conflicts T-103 T-104
ck_exit "(z) a stale base is not a veto" 0 "$RC"
ck_has "(z) but it IS named, per branch" "STALE BASE  task/T-103"
ck_has "(z) with the commit count behind base" "1 commit(s) behind main"

# No merge base at all: not comparable, and it says so rather than guessing.
gq checkout --orphan task/T-106
gq reset -q
echo "z = 1" > z.py
gq add z.py; gq commit -qm orphan
gq checkout -qf main
sl conflicts T-103 T-106
ck_exit "(z) a branch with no merge base is not comparable" 1 "$RC"
ck_has "(z) and is reported, not silently dropped" "git could not count commits against main"

sl conflicts T-101 T-102 --json
ck_exit "(z) --json keeps the exit code" 1 "$RC"
ck_has "(z) --json carries the verdict" '"verdict": "veto"'
ck_has "(z) --json carries the blind spot too" '"blind_spot"'

sl conflicts T-103 --base nope
ck_exit "(z) an unresolvable base is a refusal, not a clean answer" 1 "$RC"
ck_has "(z) named as such" "does not resolve"

# The documented merge-tree mapping is WRONG in a way that manufactures a
# confident false VETO, and this was measured before the command was written:
# `git merge-tree` exits 1 both for a real conflict AND for a ref it cannot
# resolve, with stdout EMPTY. Read as "exit 1 == conflicts", a typo'd branch
# name comes back as a conflict naming zero files.
: > "$OUT"
python3 - > "$OUT" 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
r = os.getcwd()
print("badref=%s"   % swarmloop._merge_tree_pair(r, "task/T-101", "nosuchref")[0])
print("nomergebase=%s" % swarmloop._merge_tree_pair(r, "task/T-101", "task/T-106")[0])
print("conflict=%s" % swarmloop._merge_tree_pair(r, "task/T-101", "task/T-102")[0])
print("clean=%s"    % swarmloop._merge_tree_pair(r, "task/T-101", "task/T-104")[0])
PY
ck_has "(z) a ref merge-tree cannot resolve is UNKNOWN, never a conflict" "badref=unknown"
ck_has "(z) unrelated histories are UNKNOWN, never clean" "nomergebase=unknown"
ck_has "(z) a real conflict is still reported as one" "conflict=conflict"
ck_has "(z) and a real clean merge as clean" "clean=clean"

# A pair git could not judge must never be folded into the clean list — the
# prototype's clean set was "everything not vetoed and not advisory", which
# printed each UNKNOWN pair a second time as CLEAN.
: > "$OUT"
python3 - > "$OUT" 2>&1 <<'PY'
import argparse, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
swarmloop._merge_tree_pair = lambda root, a, b: ("unknown", ["stubbed: git refused"])
try:
    swarmloop.cmd_conflicts(argparse.Namespace(
        branches=["T-103", "T-104"], base="main", as_json=False))
except SystemExit as e:
    print("exit=%d" % e.code)
PY
ck_has "(z) a pair git could not judge prints UNKNOWN" "UNKNOWN   task/T-103 <-> task/T-104"
ck_hasnt "(z) and is NEVER also listed as clean" "CLEAN     task/T-103 <-> task/T-104"
ck_has "(z) an unanswered pair is exit 1, not a green" "exit=1"

# The other way a CLEAN can be manufactured: 'no shared files' is what an EMPTY
# file set looks like, so a failed `git diff` coerced to set() prints CLEAN about
# a pair nothing was compared for.
: > "$OUT"
python3 - > "$OUT" 2>&1 <<'PY'
import argparse, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
swarmloop._branch_files = lambda root, base, ref: None
try:
    swarmloop.cmd_conflicts(argparse.Namespace(
        branches=["T-103", "T-104"], base="main", as_json=False))
except SystemExit as e:
    print("exit=%d" % e.code)
PY
ck_has "(z) a branch whose file list git refuses is UNANSWERABLE" \
       "git could not list the files it changed"
ck_hasnt "(z) never a CLEAN built from an empty file set" \
       "CLEAN     task/T-103 <-> task/T-104"
ck_has "(z) and that is exit 1" "exit=1"


# ===========================================================================
# (y3) push-gate — an UNANSWERABLE rung is NOT a pass.
#
# The gate shipped printing, one line apart, "1 mechanical condition(s) this
# command could NOT answer ... they are not passes." and "the mechanical
# conditions are MET", at exit 0 — so `push-gate --cycle N && git push` spent an
# unread rung as a passed one. That is the defect class the command was written
# to remove, reappearing inside it. Both directions are pinned here, because a
# refusal that fires on everything is not an improvement: the SAME fixture
# clears at exit 0 before the rung is broken and refuses at exit 1 after.
# ===========================================================================
section "(y3) push-gate: an unanswered rung is not a pass  [repo: pgunans]"
mk_repo pgunans
sl freeze
gq add -A; gq commit -qm freeze
sl measure --cycle 1 --timeout 60
sl analyze --cycle 1
sl checkpoint --cycle 1
echo "cycle 2 product change" >> src/app.py
gq add -A; gq commit -qm c2
sl measure --cycle 2 --timeout 60
sl analyze --cycle 2
sl checkpoint --cycle 2
gq add .swarm-loop; gq commit -qm "epoch 2 ledger"

# POSITIVE CONTROL, first and on the same fixture: a genuinely met gate clears.
sl push-gate --cycle 2
ck_exit "(y3) control: a genuinely met gate still exits 0" 0 "$RC"
ck_has "(y3) control: and says so" "the mechanical conditions are MET"
ck_has "(y3) control: with all five mechanical rungs answered here" \
       "5 of the gate's conditions are mechanical and were scored here"
sl push-gate --cycle 2 --json
ck_exit "(y3) control: --json agrees on the exit code" 0 "$RC"
ck_has "(y3) control: --json verdict is clear" '"verdict": "clear"'
ck_has "(y3) control: --json counts five mechanical rungs" '"mechanical_conditions": 5'
ck_has "(y3) control: all five of them answered" '"mechanical_answered": 5'
ck_has "(y3) control: and nothing left unanswered" '"unanswered": []'

# Now break exactly ONE rung's ability to answer, and nothing else: a state.json
# that predates the tracked_files key — a run resumed across a skill upgrade.
python3 - <<'PY'
import json
p = ".swarm-loop/state.json"
s = json.load(open(p))
for h in s.get("head_history", []):
    h.pop("tracked_files", None)
json.dump(s, open(p, "w"), indent=2)
PY
gq add .swarm-loop; gq commit -qm "state.json predating tracked_files"
sl push-gate --cycle 2
ck_exit "(y3) an UNANSWERABLE rung refuses the push, exactly like a FAIL" 1 "$RC"
ck_has "(y3) the rung is marked UNANSWERABLE, not PASS" \
       "UNANSWERABLE  this epoch's tracked-file count"
ck_has "(y3) and the verdict line says DO NOT PUSH" "push-gate: DO NOT PUSH"
ck_has "(y3) naming it as UNANSWERED rather than as a failure" "condition(s) UNANSWERED"
ck_has "(y3) and naming WHICH rung went unanswered" "UNANSWERED: tracked-files"
ck_has "(y3) in the gate's own words" "an unanswered rung is not a met one"
# The whole defect in one assertion: these two sentences appeared together.
ck_hasnt "(y3) and it must NOT also report the conditions met" \
         "the mechanical conditions are MET"
# The header count is a property of the GATE and must not shrink to the subset
# it managed to read — "4 of the gate's conditions are mechanical" is how a
# partial answer looks complete.
ck_has "(y3) the mechanical rung COUNT does not degrade to the answered subset" \
       "5 of the gate's conditions are mechanical"
ck_has "(y3) the header states the split instead" "4 answered here, 1 UNANSWERABLE"
# The operator checklist is still printed — an unanswerable rung must not also
# swallow the two conditions no command can score.
ck_has "(y3) the operator conditions are still printed on the refusal path" \
       "OPERATOR      this epoch's integrations landed green"

sl push-gate --cycle 2 --json
ck_exit "(y3) --json refuses on the same rung" 1 "$RC"
ck_has "(y3) --json verdict is 'unanswerable', never 'clear'" '"verdict": "unanswerable"'
ck_hasnt "(y3) --json must not call it clear" '"verdict": "clear"'
ck_has "(y3) --json still counts five mechanical rungs" '"mechanical_conditions": 5'
ck_has "(y3) --json says only four were answered" '"mechanical_answered": 4'
ck_has "(y3) --json names the unanswered rung for a machine reader" '"tracked-files"'

# ===========================================================================
# (y4) push-gate — the analysis rung's integrity check had no red path.
#
# It read `harness_integrity == "VIOLATED"`, and nothing can ever write that:
# `analyze` is the only writer of analysis/cycle-N.json and it routes through
# `_integrity_gate`, which die()s on VIOLATED before the file is saved. A new
# refusal with no reachable red path, added by the change whose stated purpose
# was removing gates with no red path. It now reads POSITIVELY — anything that
# is not "intact" is not a pass — which is reachable, and a file predating the
# key is UNANSWERABLE rather than a silent pass.
# ===========================================================================
section "(y4) push-gate: the analysis integrity rung, positively  [repo: pgunans]"
cd "$TMP/pgunans" || exit 1
python3 - <<'PY'
import json
p = ".swarm-loop/state.json"
s = json.load(open(p))
for i, h in enumerate(s.get("head_history", [])):
    h["tracked_files"] = 40 + i     # restore the key the (y3) block stripped
json.dump(s, open(p, "w"), indent=2)
PY
gq add .swarm-loop; gq commit -qm "state.json with tracked_files back"

# CONTROL first: the untouched analysis passes, and says on what grounds.
sl push-gate --cycle 2
ck_exit "(y4) control: an intact analysis clears the gate" 0 "$RC"
ck_has "(y4) control: and the rung says WHY it passed" "records harness_integrity=intact"

# The reachable red path the old '== VIOLATED' comparison could not see.
python3 - <<'PY'
import json
p = ".swarm-loop/analysis/cycle-2.json"
a = json.load(open(p)); a["harness_integrity"] = "tampered"
json.dump(a, open(p, "w"), indent=2)
PY
gq add .swarm-loop; gq commit -qm "analysis records a non-intact integrity"
sl push-gate --cycle 2
ck_exit "(y4) an analysis whose integrity is not 'intact' refuses the push" 1 "$RC"
ck_has "(y4) the analysis rung is the one marked FAIL" \
       'FAIL          `analyze` did not exit 1'
ck_has "(y4) quoting the value it actually found" "harness_integrity='tampered'"
ck_has "(y4) and saying what the rung requires" "not 'intact'"
ck_hasnt "(y4) and the gate does not report the conditions met" \
         "the mechanical conditions are MET"

# The sibling hand-edit guard on this same rung, which had no case either.
python3 - <<'PY2'
import json
p = ".swarm-loop/analysis/cycle-2.json"
a = json.load(open(p)); a["cycle"] = 99
json.dump(a, open(p, "w"), indent=2)
PY2
gq add .swarm-loop; gq commit -qm "analysis recording someone else's cycle"
sl push-gate --cycle 2
ck_exit "(y4) an analysis recording another cycle refuses" 1 "$RC"
ck_has "(y4) naming both numbers" "records cycle 99, not 2"
python3 - <<'PY2'
import json
p = ".swarm-loop/analysis/cycle-2.json"
a = json.load(open(p)); a["cycle"] = 2
json.dump(a, open(p, "w"), indent=2)
PY2
gq add .swarm-loop; gq commit -qm "analysis cycle restored"

# The historical value still refuses — the old branch's intent is preserved.
python3 - <<'PY'
import json
p = ".swarm-loop/analysis/cycle-2.json"
a = json.load(open(p)); a["harness_integrity"] = "VIOLATED"
json.dump(a, open(p, "w"), indent=2)
PY
gq add .swarm-loop; gq commit -qm "analysis records VIOLATED"
sl push-gate --cycle 2
ck_exit "(y4) a VIOLATED integrity still refuses" 1 "$RC"
ck_has "(y4) named" "harness_integrity='VIOLATED'"

# A file written before the key existed: not a pass and not a failure. It must
# not read as a pass, which is what '== VIOLATED' did with an absent key.
python3 - <<'PY'
import json
p = ".swarm-loop/analysis/cycle-2.json"
a = json.load(open(p)); a.pop("harness_integrity", None)
json.dump(a, open(p, "w"), indent=2)
PY
gq add .swarm-loop; gq commit -qm "analysis predating harness_integrity"
sl push-gate --cycle 2
ck_exit "(y4) an analysis predating the key is UNANSWERABLE, not a pass" 1 "$RC"
ck_has "(y4) marked as such on the rung" 'UNANSWERABLE  `analyze` did not exit 1'
ck_has "(y4) explaining what is missing" "carries no harness_integrity"
ck_hasnt "(y4) and it is not laundered into a met gate" \
         "the mechanical conditions are MET"

# ===========================================================================
# (y5) push-gate — the fail path the reorder left unsatisfiable.
#
# F7: the gate moved to the END of Phase 8, AFTER the epoch report is written
# and committed, but its fail-path instruction still read "name the failing item
# in the epoch report" with nothing said about committing that note. Obeying it
# re-dirtied .swarm-loop/ and flipped the gate's LAST rung to FAIL on the re-run
# — a refusal the operator created by following the instruction. This is that
# sequence, mechanically: the middle step is the reproduction and the last step
# is the fix SKILL.md and the command's own refusal text now both prescribe.
# ===========================================================================
section "(y5) push-gate: recording a refusal must not create one  [repo: pgnote]"
mk_repo pgnote
sl freeze
gq add -A; gq commit -qm freeze
sl measure --cycle 1 --timeout 60
sl analyze --cycle 1
sl checkpoint --cycle 1
echo "# cycle 1" > .swarm-loop/reports/cycle-1.md
sl dispatch --agent V-7 --kind verifier --subject "batch e1-b1"
gq add .swarm-loop; gq commit -qm "epoch 1: report + ledger"

sl push-gate --cycle 1
ck_exit "(y5) the gate refuses on the outstanding verifier" 1 "$RC"
ck_has "(y5) that is the rung that failed" "rung2-returned"
ck_has "(y5) and the ledger rung is clean at this point" \
       'PASS          everything `.swarm-loop/` holds is committed'
ck_has "(y5) the refusal text tells you to commit the note it asks for" \
       "COMMIT that amendment"

# Obey the instruction as it USED to read — write the note, commit nothing.
echo "- push declined: rung2-returned (V-7 outstanding)" >> .swarm-loop/reports/cycle-1.md
sl push-gate --cycle 1
ck_exit "(y5) still refusing" 1 "$RC"
ck_has "(y5) REPRODUCED: an uncommitted note flips the LAST rung to FAIL too" \
       'FAIL          everything `.swarm-loop/` holds is committed'
ck_has "(y5) naming the report the operator was told to write" "reports/cycle-1.md"

# Obey it as it reads now.
gq add .swarm-loop; gq commit -qm "cycle 1: push declined — rung2-returned"
sl push-gate --cycle 1
ck_exit "(y5) and the real rung still refuses, as it should" 1 "$RC"
ck_has "(y5) still the outstanding verifier" "rung2-returned"
ck_has "(y5) but the ledger rung is clean again — no self-inflicted second failure" \
       'PASS          everything `.swarm-loop/` holds is committed'
ck_hasnt "(y5) and nothing invents a ledger failure" \
         'FAIL          everything `.swarm-loop/` holds is committed'

# ===========================================================================
# (z2) conflicts — zero branches, and one branch counted twice.
#
# ZERO branches is the maximal form of "a shorter list read as a clean list",
# which this command's own comment names as its whole failure mode: `conflicts
# ""` / `,` / `"  "` — an unset `"$IDS"` in `conflicts "$IDS" && git merge ...` —
# printed "0 comparable branch(es) ... nothing to compare" and exited 0.
# Separately, dedupe was on the raw ARGUMENT string, so the two spellings this
# command advertises for one branch both entered the live set and the branch was
# compared to ITSELF, printing an ADVISORY that a branch disagrees with itself.
# ===========================================================================
section "(z2) conflicts: zero branches, and a branch named twice  [repo: cflct2]"
mk_repo cflct2
printf 'A = 1\nB = 2\nC = 3\n' > shared.py
gq add -A; gq commit -qm shared
gq checkout -q -b task/T-103 main
printf 'A = 1\nB = 2\nC = 333\n' > shared.py
gq commit -qam T103
gq checkout -q main
gq checkout -q -b task/T-104 main
echo "x = 1" > only_mine.py
gq add -A; gq commit -qm T104
gq checkout -q main

# POSITIVE CONTROL first: a real argument list is still answered, so the new
# refusal is not one that fires on everything.
sl conflicts T-103 T-104
ck_exit "(z2) control: a real two-branch batch still answers, exit 0" 0 "$RC"
ck_has "(z2) control: with the pair judged" "CLEAN     task/T-103 <-> task/T-104"

sl conflicts ""
ck_exit "(z2) an EMPTY argument refuses — nothing compared is not a clean batch" 1 "$RC"
ck_has "(z2) saying exactly what resolved to nothing" "resolved to ZERO branch names"
ck_hasnt "(z2) and never prints a comparable-branch count for it" "comparable branch(es)"
sl conflicts ","
ck_exit "(z2) a comma-only argument refuses too" 1 "$RC"
ck_has "(z2) same reason" "resolved to ZERO branch names"
sl conflicts "   "
ck_exit "(z2) whitespace-only refuses too" 1 "$RC"
ck_has "(z2) same reason" "resolved to ZERO branch names"

# And the line is drawn at ZERO, not at "few": one real branch cannot conflict
# with a peer, so it stays exit 0 — but the text refuses to be read as a clean
# verdict about a comparison that never happened.
sl conflicts T-104
ck_exit "(z2) ONE real branch is still exit 0 — nothing can conflict with a peer" 0 "$RC"
ck_has "(z2) but it says no pair was judged" "no pair was formed, so no pair was judged"

# One branch, two spellings, both advertised by --help.
sl conflicts T-103 task/T-103 T-104
ck_exit "(z2) the two spellings of one branch do not manufacture a third" 0 "$RC"
ck_has "(z2) the branch count is the number of BRANCHES, not of arguments" \
       "conflicts: 2 comparable branch(es)"
ck_hasnt "(z2) and no branch is compared to itself" "task/T-103 <-> task/T-103"
ck_hasnt "(z2) so no ADVISORY is raised about a branch disagreeing with itself" \
         "ADVISORY (merges clean)"
n=$(grep -c -F "CLEAN     task/T-103 <-> task/T-104" "$OUT")
ck "(z2) the one genuine pair is judged exactly once" "1" "$n"

# --json must carry the branches the human-readable path is tested to name, or a
# machine reader can be blinded to exactly the ones that made the exit code 1.
sl conflicts T-103 T-104 T-999 --json
ck_exit "(z2) --json refuses on an unresolvable branch" 1 "$RC"
ck_has "(z2) --json names it in the unanswerable array, not just in the exit code" \
       '"branch": "T-999"'
ck_has "(z2) with the reason" "no such branch"
ck_has "(z2) and the pair it COULD judge is still there" '"verdict": "CLEAN"'

# ===========================================================================
# (doc) The prose and the code must not contradict each other.
#
# The last pass at this reorder amended three sites and missed two, and a doc
# claim that outlived its code is what F3/F6 were: merge-protocol.md documented
# an ownership-glob comparison `conflicts` has never performed, and SKILL.md
# still told an orchestrator mid-integration that the push happens in Phase 7.
# Nothing executes prose, so nothing was red. These cases are the grep the last
# pass did not run.
# ===========================================================================
section "(doc) SKILL.md and references must not contradict the code"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SKILL_ROOT" || exit 1

: > "$OUT"; cat "$SKILL_ROOT/SKILL.md" >> "$OUT"
ck_hasnt "(doc) Phase 6's body no longer puts the push in Phase 7" \
         "any push all happen in Phase 7"
ck_has "(doc) it says where the push actually fires" \
       "the push fires at the END of Phase 8"
ck_hasnt "(doc) nor does Phase 2 step 4 still say 'the push in Phase 7'" \
         "the push in Phase 7 ships a"
ck_has "(doc) the fail path tells you to COMMIT the amendment it asks for" \
       "commit that amendment"
ck_has "(doc) because the report was already committed when the gate ran" \
       "written **and committed** before the gate answered"
ck_has "(doc) and the terminal path covers a FINAL epoch that declined to push" \
       "that refusal is the last thing this run records"
ck_has "(doc) SKILL.md names the conflicts command at the step that runs it" \
       "swarmloop.py conflicts <ids> --base main"

: > "$OUT"; cat "$SKILL_ROOT/references/merge-protocol.md" >> "$OUT"
ck_hasnt "(doc) merge-protocol no longer claims a comparison the code never makes" \
         "compares the files a branch **touched** against the globs its ticket **declared**"
ck_has "(doc) it states what ADVISORY actually reads" "loads no ticket graph"
ck_has "(doc) and hands the declared-glob half to the operator" \
       "check the named file against what each ticket **declared** it owns"
ck_has "(doc) the zero-branch refusal is documented where the command is" \
       "resolves to **zero** branch names"

: > "$OUT"; cat "$SKILL_ROOT/scripts/swarmloop.py" >> "$OUT"
ck_hasnt "(doc) and the docstring carries no copy of the retired claim" \
         "ACTUAL touched files against check-wave's DECLARED globs"
ck_has "(doc) it says plainly what ADVISORY is" \
       "It is NOT a comparison against"
# (y) DISTIL — the self-improvement loop's write half
#
# The loop was declared in SKILL.md from version one and nothing ever closed it:
# LEARNED.md carried ZERO entries across every run, because the append was a
# paragraph. `distil` makes it mechanical, and the whole risk of that is a writer
# that cannot refuse — this skill's signature defect class. So the cases below
# are mostly refusals, each paired with a control that must still pass.
#
# EVERY distil call here runs an ISOLATED COPY of the skill (`sli`), never the
# installed one. LEARNED.md is machine-wide memory shared by every run on this
# machine; a suite that wrote to it would be the exact blast radius the two-tier
# split exists to avoid. The last case re-hashes the real file to prove it.
# ===========================================================================
ISO="$TMP/iso-skill"
LM="$ISO/LEARNED.md"
REAL_LEARNED="$SCRIPT_DIR/../LEARNED.md"
REAL_LEARNED_SHA="$(sha_of "$REAL_LEARNED")"

mk_iso() {   # a pristine isolated skill: the real script, the real LEARNED.md
  # ...with the ENTRY REGION EMPTIED. The preamble, the rules it states about
  # itself and the '## Entries' marker are the installed file's own bytes,
  # because those are what is under test. The entries are not: that file is
  # machine-wide memory any run on this machine may append to at any moment, and
  # a suite whose expected counts move when someone else distils goes red for a
  # reason that is not a defect. (It did: the first real entries this file ever
  # held arrived mid-session as '## ' headings.)
  rm -rf "$ISO"
  mkdir -p "$ISO/scripts"
  cp "$SL" "$ISO/scripts/swarmloop.py" || return 1
  python3 - "$REAL_LEARNED" "$LM" <<'PY' || return 1
import sys
src, dst = sys.argv[1], sys.argv[2]
head, sep, _ = open(src).read().partition("## Entries")
assert sep, "the installed LEARNED.md has lost its '## Entries' marker"
open(dst, "w").write(head + sep + "\n")
PY
  return 0
}

sli() {   # swarmloop, run from the isolated skill copy
  python3 "$ISO/scripts/swarmloop.py" "$@" > "$OUT" 2>&1
  RC=$?
  return 0
}

fill_learned() {   # fill_learned <n> — append n well-formed entries to the copy
  python3 - "$LM" "$1" <<'PY'
import sys
p, n = sys.argv[1], int(sys.argv[2])
with open(p, "a") as f:
    for i in range(n):
        f.write("\n- **Bound metric command %d to a timeout a hung runner cannot outlive** - a "
                "runner that hangs forever reads as a metric that never improves\n" % i)
PY
}

n_entries() {   # entries counted exactly the way the harness counts them
  python3 - "$LM" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
ents, marked = swarmloop._learned_entries(open(sys.argv[1]).read())
print(len(ents) if marked else "UNMARKED")
PY
}

has_in() {   # has_in <file> <substring> -> yes/no, whitespace-collapsed
  # Entries wrap at ~94 columns, so a line-oriented grep misses any phrase the
  # wrapper split. Collapsing whitespace compares the entry as it was written.
  python3 - "$1" "$2" <<'PY'
import sys
print("yes" if sys.argv[2] in " ".join(open(sys.argv[1]).read().split()) else "no")
PY
}

prov() {   # prov <lesson> — make <lesson> part of what THIS run recorded
  # Tier 2 promotes what tier 1 wrote down. Every --to-skill case below therefore
  # has to put its lesson in the run file first, including the cases that assert
  # a refusal: a case whose refusal comes from the provenance gate proves nothing
  # about the rule it claims to be testing.
  mkdir -p .swarm-loop
  printf -- '- (cycle 0) **%s** - recorded during the run\n' "$1" >> .swarm-loop/learnings.md
}

# A lesson that must SURVIVE every rule: imperative, specific, motivated, and
# carrying the harness's own vocabulary, which a lazier matcher would reject.
GOOD_L="Commit the run state directory at every epoch boundary, never only at freeze"
GOOD_B="a resumed run found no state at all and re-dispatched every in-flight ticket into a second set of worktrees"

section "(y) distil tier 1 — the run's own file  [repo: distil1]"
mk_repo distil1
mk_iso
ck_file "(y) the isolated skill copy has a LEARNED.md" "$LM"
ck "(y) and it starts with zero entries" "0" "$(n_entries)"

# Every entry SHAPE this file has actually carried must count as one entry. The
# reader knew '- ' bullets and '### ' headings; the first entries LEARNED.md ever
# held were written by hand as '## ' headings with prose bodies, and the reader
# scored eight of them as ZERO — `checkpoint` reported "EMPTY (0 entries) —
# Termination is supposed to distil" over a file that had just been filled. A
# count that is wrong about the file it exists to measure is the defect class
# this whole command is against, so the three shapes are pinned here.
python3 - "$LM" <<'PY'
import sys
open(sys.argv[1], "a").write("""
- **Cap wave width at three when the batch shares build outputs** - a bullet, the
  shape `distil` writes, wrapped across two lines

### A third-level heading entry

## A second-level heading entry, the shape the first real run wrote

Its body is prose across several paragraphs, and none of it is a fourth entry.

APPLY: nothing here opens an entry either.
""")
PY
ck "(y) bullet + '###' + '##' all count as ONE entry each" "3" "$(n_entries)"
# ...and the count of the INSTALLED file agrees with an independent count of its
# openers, whatever shapes a curator happens to have used in it today.
ck "(y) the reader's count of the real LEARNED.md matches its opener count" \
   "$(python3 -c "
import re,sys
b=open('$REAL_LEARNED').read().split('## Entries',1)[-1]
print(len(re.findall(r'^(?:- |### |## )', b, re.M)))")" \
   "$(python3 -c "
import os,sys
sys.path.insert(0, os.environ['SL_DIR']); import swarmloop
e,_=swarmloop._learned_entries(open('$REAL_LEARNED').read()); print(len(e))")"
mk_iso

sli distil --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y) tier 1 appends without a flag — the safe tier is the default" 0 "$RC"
ck_has "(y) and says which file it landed in" ".swarm-loop/learnings.md"
ck_file "(y) the run file now exists" ".swarm-loop/learnings.md"
ck "(y) the lesson is IN the run file" "yes" "$(has_in .swarm-loop/learnings.md "$GOOD_L")"
ck "(y) the motivating failure went in with it" "yes" "$(has_in .swarm-loop/learnings.md "$GOOD_B")"
ck "(y) tier 1 wrote NOTHING to the skill's memory" "0" "$(n_entries)"

sli distil --lesson "$GOOD_L"
ck_exit "(y) --because is REQUIRED, not optional prose" 3 "$RC"
ck_has "(y) and the refusal says why a lesson needs its failure" "cannot be judged for applicability"
ck "(y) the run file did not grow on the refusal" "1" \
   "$(grep -c -F -- "(cycle 0)" .swarm-loop/learnings.md)"

# `--cycle N` overrides the cycle read out of state.json — the only way an
# orchestrator distilling a lesson about an EARLIER epoch can file it under the
# epoch it came from rather than the one it is standing in.
sli distil --cycle 7 --lesson "Probe a quiet worker with a counter that only grows when work lands" \
  --because "a hung worker and a working one are indistinguishable from the orchestrator, and one wave spent an epoch on nothing"
ck_exit "(y) --cycle N is accepted" 0 "$RC"
ck "(y) ...and the entry is filed under the cycle it names, not state.json's" "1" \
   "$(grep -c -F -- "(cycle 7)" .swarm-loop/learnings.md)"

# The run file's ~30 cap is a WARNING, never a refusal: it is one repo's file,
# curated by hand every epoch, and refusing mid-run loses the lesson itself.
#
# This CONTROL must read the output of a SUCCESSFUL under-cap append. It used to
# sit after the exit-3 `--because` usage error above, which never reaches the
# append path at all — so it asserted that a usage error prints no soft-cap
# warning, the threshold had no red path anywhere in the suite, and firing the
# warning unconditionally left every case green.
ck_hasnt "(y) CONTROL: under the soft cap it says nothing about curating" "over SKILL.md Phase 8's"
ck "(y) CONTROL: ...over an append that really happened, well under the cap" "2" \
   "$(grep -c '^- (cycle ' .swarm-loop/learnings.md)"
python3 - <<'PY'
with open(".swarm-loop/learnings.md", "a") as f:
    for i in range(31):
        f.write("- (cycle 0) **Filler entry %d** - filler\n" % i)
PY
sli distil --lesson "Split any ticket whose verify command names more than one module" \
  --because "a two-module ticket came back half done and neither half could be verified alone"
ck_exit "(y) over the soft cap it still APPENDS" 0 "$RC"
ck_has "(y) ...and warns to curate at this epoch boundary" "over SKILL.md Phase 8's"

section "(y2) distil --to-skill — provenance and the happy path  [repo: distil2]"
mk_repo distil2
mk_iso
LM_SHA0="$(sha_of "$LM")"

# Distillation compresses what the run recorded. With an empty run file there is
# nothing to compress and the "lesson" is being invented at Termination, which is
# how a memory file fills with plausible sentences nobody learned.
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y2) --to-skill REFUSES when the run recorded nothing" 1 "$RC"
ck_has "(y2) ...naming what there is nothing to distil from" "nothing to distil FROM"
ck "(y2) and LEARNED.md is byte-identical" "$LM_SHA0" "$(sha_of "$LM")"

sli distil --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y2) tier 1 satisfies the provenance gate in one command" 0 "$RC"
cp "$LM" "$TMP/distil-learned.before"
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y2) CONTROL: a durable, imperative, motivated lesson IS accepted" 0 "$RC"
ck_has "(y2) ...reported against the cap" "1/40 entries"
ck "(y2) the entry is really in LEARNED.md" "1" "$(n_entries)"
ck "(y2) the lesson landed verbatim" "yes" "$(has_in "$LM" "$GOOD_L")"
ck "(y2) and so did the failure that motivated it" "yes" "$(has_in "$LM" "$GOOD_B")"

# Append-only IN EFFECT, not by promise: everything that was in the file is
# still there, in order, as a byte prefix. A rewrite-the-whole-file
# implementation could pass every other case here and fail this one.
ck "(y2) the previous bytes are a strict PREFIX of the new file" "yes" \
   "$(python3 -c "
a=open('$TMP/distil-learned.before','rb').read(); b=open('$LM','rb').read()
print('yes' if b.startswith(a) and len(b)>len(a) else 'NO')")"

# The reporter and the writer must agree — the whole point of resolving
# LEARNED.md in one place. checkpoint is where the orchestrator reads it.
sl freeze
gq add -A; gq commit -qm harness
sli checkpoint --cycle 0
ck_has "(y2) checkpoint now reports the entry distil just wrote" "LEARNED.md: 1 entr(ies)"
ck_hasnt "(y2) ...and no longer calls the file EMPTY" "EMPTY (0 entries)"

sli distil --list
ck_exit "(y2) --list reports both tiers and writes nothing" 0 "$RC"
ck_has "(y2) --list names the run file" ".swarm-loop/learnings.md"
ck_has "(y2) --list names the skill memory" "iso-skill/LEARNED.md"   # normpath'd

LM_SHA1="$(sha_of "$LM")"
prov "Rebase an integration branch onto the base immediately before merging it"
sli distil --to-skill --dry-run \
  --lesson "Rebase an integration branch onto the base immediately before merging it" \
  --because "two integration branches landed the same file twice because neither had been rebased"
ck_exit "(y2) --dry-run validates and exits 0" 0 "$RC"
ck_has "(y2) --dry-run says it wrote nothing" "nothing written"
ck "(y2) --dry-run really wrote nothing" "$LM_SHA1" "$(sha_of "$LM")"

section "(y3) distil --to-skill REFUSES a project-specific entry  [repo: distil3]"
mk_repo distil3
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"     # satisfy provenance
LM_SHA="$(sha_of "$LM")"

# Each of these is a signal that cannot be true of a durable orchestration
# lesson. The refusal names the token, because "rejected" without the word is
# not something the agent that wrote it can act on.
prov "Pin the ProxyShop renderer before dispatching a wave that touches it"
sli distil --to-skill --lesson "Pin the ProxyShop renderer before dispatching a wave that touches it" \
  --because "the renderer moved under three agents at once and each rebased onto a different tree"
ck_exit "(y3) a product / service name is refused" 1 "$RC"
ck_has "(y3) ...naming the offending token" "'ProxyShop'"
ck_has "(y3) ...and saying how to fix it" "Name the ROLE it played"

prov "Reuse the venv at /Users/dev/app/.venv instead of building one per worktree"
sli distil --to-skill --lesson "Reuse the venv at /Users/dev/app/.venv instead of building one per worktree" \
  --because "each worktree rebuilt a two-minute environment and the wave took longer to provision than to run"
ck_exit "(y3) a machine path is refused" 1 "$RC"
ck_has "(y3) ...named as a path" "a machine or repo path"

prov "Push the run branch to the mirror at labs.example.com before the final report"
sli distil --to-skill --lesson "Push the run branch to the mirror at labs.example.com before the final report" \
  --because "the only offsite copy of a nine-hour run lived on one laptop until the last push"
ck_exit "(y3) a host or URL is refused" 1 "$RC"
ck_has "(y3) ...named as a host" "a URL, host or remote"

prov "Schedule T-036 ahead of anything that imports the registry it creates"
sli distil --to-skill --lesson "Schedule T-036 ahead of anything that imports the registry it creates" \
  --because "the dependent tickets all failed red-check because the enabler had not landed yet"
ck_exit "(y3) a ticket identifier is refused" 1 "$RC"
ck_has "(y3) ...named as an identifier" "a ticket or defect identifier"

ck "(y3) not one refusal wrote a byte" "$LM_SHA" "$(sha_of "$LM")"

section "(y4) distil --to-skill REFUSES a malformed entry  [repo: distil4]"
mk_repo distil4
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"     # satisfy provenance
LM_SHA="$(sha_of "$LM")"

prov "the merge queue was slower than expected all cycle"
sli distil --to-skill --lesson "the merge queue was slower than expected all cycle" \
  --because "waves four and five each waited on a review that nobody had been assigned to do"
ck_exit "(y4) a report-shaped opening is refused" 1 "$RC"
ck_has "(y4) ...named as non-imperative" "that is a report about what happened"

prov "Capping wave width at three keeps merge review survivable"
sli distil --to-skill --lesson "Capping wave width at three keeps merge review survivable" \
  --because "wave five collided on shared build outputs and two merges had to be redone by hand"
ck_exit "(y4) a participle opening is refused" 1 "$RC"
ck_has "(y4) ...named as a participle" "a participle, not an imperative"

prov "Cap wave width at three or fewer tickets when the batch shares build outputs"
sli distil --to-skill --lesson "Cap wave width at three or fewer tickets when the batch shares build outputs" \
  --because "it was slow"
ck_exit "(y4) a stub motivation is refused" 1 "$RC"
ck_has "(y4) ...named as too short to judge" "it is a stub"

prov "Cap it"
sli distil --to-skill --lesson "Cap it" --because "$GOOD_B"
ck_exit "(y4) a stub lesson is refused" 1 "$RC"

ck "(y4) not one malformed entry wrote a byte" "$LM_SHA" "$(sha_of "$LM")"

# CONTROL: the near-miss. Each of the four refusals above must be a property of
# the entry, not of a validator that refuses everything it is shown.
prov "Cap wave width at three or fewer tickets when the batch shares build outputs"
sli distil --to-skill --lesson "Cap wave width at three or fewer tickets when the batch shares build outputs" \
  --because "wave five collided on shared build outputs and two merges had to be redone by hand"
ck_exit "(y4) CONTROL: the corrected entry is accepted" 0 "$RC"
ck "(y4) CONTROL: and it landed" "1" "$(n_entries)"

# Curation is a rule LEARNED.md states about itself: merge duplicates, do not
# add one. The check reads whole entry BLOCKS, so a lesson that only wrapped
# differently is still recognised as the same lesson.
prov "Cap wave width at three or fewer tickets when the batch shares build outputs"
sli distil --to-skill --lesson "Cap wave width at three or fewer tickets when the batch shares build outputs" \
  --because "an entirely different failure text, so only the lesson itself collides with what is there"
ck_exit "(y4) a duplicate lesson is refused" 1 "$RC"
ck_has "(y4) ...naming the curation rule it would break" "merge duplicates, not to add one"
ck "(y4) and the file still holds exactly one" "1" "$(n_entries)"

# The same rule against a lesson LONG enough that the writer wraps it across
# lines. A duplicate check that kept only each entry's OPENING line sees a third
# of this one and lets the second copy in — which is what the block folding in
# _learned_entries is for, and the only case that can tell.
LONG_L="Rebase every integration branch onto the base branch immediately before merging it and never after"
prov "$LONG_L"
sli distil --to-skill --lesson "$LONG_L" \
  --because "two integration branches landed the same file twice because neither had been rebased first"
ck_exit "(y4) a lesson long enough to WRAP is accepted once" 0 "$RC"
ck "(y4) ...and is stored across more than one line" "yes" \
   "$(python3 -c "
import sys
t = open('$LM').read()
i = t.index('Rebase every integration branch')
print('yes' if '\n' in t[i:i+len('$LONG_L')] else 'NO')")"
prov "$LONG_L"
sli distil --to-skill --lesson "$LONG_L" \
  --because "a different failure text entirely, so only the wrapped lesson itself collides"
ck_exit "(y4) the wrapped duplicate is refused too" 1 "$RC"
ck "(y4) ...and did not land" "2" "$(n_entries)"

section "(y5) distil --to-skill enforces the ~40 cap LEARNED.md states  [repo: distil5]"
mk_repo distil5
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"     # satisfy provenance
fill_learned 39
ck "(y5) the copy now holds 39 entries" "39" "$(n_entries)"

prov "Kill a wave that has produced no commit by its second checkpoint"
sli distil --to-skill --lesson "Kill a wave that has produced no commit by its second checkpoint" \
  --because "a hung worker and a working one look identical from the orchestrator and the run spent an epoch on nothing"
ck_exit "(y5) the append that FILLS the file still succeeds" 0 "$RC"
ck_has "(y5) ...and shouts at the agent whose append filled it" "The next one will be REFUSED"
ck_has "(y5) ...telling it to stop and tell the user" "STOP AND TELL THE USER"
ck "(y5) 40 entries now" "40" "$(n_entries)"

LM_SHA="$(sha_of "$LM")"
prov "Run the frozen metric commands from the primary checkout only"
sli distil --to-skill --lesson "Run the frozen metric commands from the primary checkout only" \
  --because "a worktree copy of the harness measured a tree that had never been merged and the verdict was fiction"
ck_exit "(y5) the 41st entry is REFUSED" 1 "$RC"
ck_has "(y5) ...with the capacity message, not a trim" "IS FULL"
ck_has "(y5) ...saying the skill has a backlog" "means the SKILL has"
ck_has "(y5) ...and handing the refused lesson back for the report" "proposed skill updates"
ck "(y5) NOTHING was trimmed to make room" "40" "$(n_entries)"
ck "(y5) the file is byte-identical after the refusal" "$LM_SHA" "$(sha_of "$LM")"

section "(y6) distil --to-skill will not write a file it cannot read  [repo: distil6]"
mk_repo distil6
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"     # satisfy provenance

# The '## Entries' marker is where every count this command makes is measured.
# Without it the whole file counts, preamble rule bullets included, so the
# capacity gate would be answering about the wrong thing.
python3 - "$LM" <<'PY'
import sys
p = sys.argv[1]
open(p, "w").write(open(p).read().replace("## Entries", "## Notes"))
PY
LM_SHA="$(sha_of "$LM")"
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y6) a LEARNED.md with no entries marker is refused" 1 "$RC"
ck_has "(y6) ...naming the missing section" "has no '## Entries' section"
ck "(y6) and it is left untouched" "$LM_SHA" "$(sha_of "$LM")"
sli distil --list
ck_has "(y6) the reporter says its own count cannot be trusted either" \
       "REFUSES until the marker is restored"

# A held lock means a process is mid-write on memory shared by every run on this
# machine. `_state_lock` proceeds after its timeout because refusing to
# checkpoint is worse than the race it prevents; here the opposite holds — the
# loser of the race is at Termination and one re-run costs nothing.
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"
: > "$LM.lock"
LM_SHA="$(sha_of "$LM")"
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y6) a held lock REFUSES rather than racing" 1 "$RC"
ck_has "(y6) ...saying nothing was written" "NOTHING was written here"
ck "(y6) and nothing was" "$LM_SHA" "$(sha_of "$LM")"
rm -f "$LM.lock"
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y6) CONTROL: with the lock released the same entry lands" 0 "$RC"

# The compare-and-swap. The capacity count and the duplicate check are computed
# against bytes read a moment earlier; if the file moved under them — a second
# run's distil, an operator's editor saving mid-flight — both answers are stale
# and the append would write past a gate that no longer holds. Driven through
# the module because no black-box ordering interleaves a foreign writer
# deterministically.
mk_iso
LM_SHA="$(sha_of "$LM")"
CAS_OUT=$(python3 - "$LM" 2>/dev/null <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
p = sys.argv[1]
stale = open(p, "rb").read() + b"\n- **an entry this caller never saw** - x\n"
try:
    swarmloop._dl_append(p, stale, b"\n- **should never land** - y\n")
    print("APPENDED")
except SystemExit:
    print("REFUSED")
PY
)
ck "(y6) an append whose read is stale is REFUSED" "REFUSED" "$CAS_OUT"
ck "(y6) ...and the file is untouched" "$LM_SHA" "$(sha_of "$LM")"

# CONTROL for the CAS: handed the bytes it actually read, the same call appends.
python3 - "$LM" >/dev/null 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
p = sys.argv[1]
swarmloop._dl_append(p, open(p, "rb").read(),
                     b"\n- **Retire a worktree only once its branch is an ancestor of the "
                     b"base** - merged into an integration branch is not landed\n")
PY
ck "(y6) CONTROL: with the bytes it read, the append lands" "1" "$(n_entries)"

# ---------------------------------------------------------------------------
# (y7) THE FALSE-REFUSAL DIRECTION
#
# Every case above this point asks "does the gate refuse what it should?". None
# asked "does it ACCEPT what it should?", and measured against ground truth the
# answer was no: the validator refused 13 of the 20 entries in this skill's own
# hand-curated archive and 6 of the 8 the first real run ever wrote — including
# "Never ..." and "Always ...", the two commonest shapes a rule takes, with the
# message "that is a report about what happened", which is false of both.
#
# A gate that refuses the correct form does not teach the form. It teaches
# writing around the gate, and SKILL.md tells the agent every refusal "is the
# gate working, not an obstacle to route around" — so the agent that hits a
# false refusal mangles a correct lesson rather than reporting the bug. Each
# case below is one canonical form that MUST be accepted, and each goes red if
# its matcher regresses.
# ---------------------------------------------------------------------------
section "(y7) distil --to-skill must ACCEPT the correct forms  [repo: distil7]"
mk_repo distil7
mk_iso
ACC_B="a run ended with an unverifiable claim because nobody had measured the thing the claim was about"

accept_() {   # accept_ <name> <lesson> — must validate; --dry-run writes nothing
  prov "$2"
  sli distil --to-skill --dry-run --lesson "$2" --because "$ACC_B"
  ck_exit "$1" 0 "$RC"
}
refuse_() {   # refuse_ <name> <lesson> <fragment the refusal must name>
  prov "$2"
  sli distil --to-skill --dry-run --lesson "$2" --because "$ACC_B"
  ck_exit "$1 — refused" 1 "$RC"
  ck_has "$1 — for the stated reason" "$3"
}

# F1: the imperative forms. "Never do X" IS the verb a future run should obey.
accept_ "(y7) 'Never ...' is an imperative, not a report" \
        "Never merge an integration branch that has not been rebased onto the base first"
accept_ "(y7) 'Always ...' likewise" \
        "Always re-measure a metric from the primary checkout before believing a worktree's number"
# ...and both are still imperatives when the rule NAMES the past failure inside
# itself, which is exactly where the report test and the imperative form collide.
accept_ "(y7) 'Never ...' even when the rule mentions what went wrong" \
        "Never dispatch a lane against a base that was rewritten after its packet was written"
accept_ "(y7) 'Always ...' even when the rule mentions what went wrong" \
        "Always re-pin a branch tip that was measured while its worktree was still dirty"
accept_ "(y7) 'When X, do Y' — a conditional imperative" \
        "When a worker goes quiet, probe it with a counter that only grows as real work lands"
accept_ "(y7) 'If X, do Y' likewise" \
        "If a graded metric has not moved for two measured cycles, kill the wave rather than extend it"
accept_ "(y7) 'Every X must ...' — a deontic rule" \
        "Every ticket must carry a machine-checkable goal frozen before it is dispatched"
accept_ "(y7) 'One X per Y, and never ...' — a quantifier rule" \
        "One agent per file scope, and never two agents mutating one worktree at any depth"
accept_ "(y7) a standing claim opening with an article is a rule, not a story" \
        "A shared scratchpad path is not lane isolation and inherits none of a worktree's guarantees"

# ...and the refusals that direction must NOT cost. A story is still a story.
refuse_ "(y7) a past-tense story opening with an article" \
        "the merge queue was slower than every estimate we made all cycle long" \
        "narrates in the past tense"
refuse_ "(y7) a clause that never arrives at an instruction" \
        "When the merge queue was slow the whole wave sat behind one unassigned review" \
        "never reaches an instruction"
refuse_ "(y7) a participle opening still reads as a description" \
        "Capping wave width at three keeps merge review survivable across a long epoch" \
        "a participle, not an imperative"

# F6: ordinary technical vocabulary is not project trivia, and a refusal that
# names the WRONG rule sends the author to fix something that is not there.
accept_ "(y7) an encoding is not a ticket identifier" \
        "Decode every captured log as UTF-8 with replacement before matching a pattern against it"
accept_ "(y7) a digest algorithm is not a ticket identifier" \
        "Compare build artifacts by SHA-256 digest rather than by mtime when deciding staleness"
accept_ "(y7) a script filename is not a host" \
        "Run the whole gate with tests/run.sh before declaring any phase of the work complete"
accept_ "(y7) the platform the loop runs on is not a product fact" \
        "Give the orchestrator a macOS-independent progress probe so the same loop runs anywhere"
# CONTROL: the exception lists are exceptions, not an opt-out of the rule.
refuse_ "(y7) CONTROL: a real ticket key is still refused" \
        "Schedule T-036 ahead of anything that imports the registry that ticket creates" \
        "a ticket or defect identifier"
refuse_ "(y7) CONTROL: a real host is still refused" \
        "Push the run branch to the mirror at labs.example.com before writing the final report" \
        "a URL, host or remote"
refuse_ "(y7) CONTROL: a real product name is still refused" \
        "Pin the ProxyShop renderer before dispatching any wave that touches it" \
        "interior capital"

# F7: the -ed/-ing allowlist exists ONLY to prevent false refusals, and had no
# case of its own. Two classes live in it: imperative verbs spelled like
# participles, and pronouns that are not verbs at all.
accept_ "(y7) an imperative verb spelled like a participle is accepted" \
        "Seed every worktree with the run state directory before the first dispatch reaches it"
accept_ "(y7) ...and so is a pronoun that merely ends in -ing" \
        "Nothing calls this is a defect only when a frozen test already passes because of it"

# F7: the over-length ceiling. The archived corpus contains an 1138-character
# --because the ceiling exists to catch, and nothing exercised it.
prov "Bound every metric command with a timeout a hung runner cannot outlive"
sli distil --to-skill --dry-run \
  --lesson "Bound every metric command with a timeout a hung runner cannot outlive" \
  --because "$(python3 -c "print('a runner that hangs forever reads as a metric that never improves, and ' * 14)")"
ck_exit "(y7) a --because over the ceiling is refused" 1 "$RC"
ck_has "(y7) ...told it belongs in the final report" "belongs in the final report"
sli distil --to-skill --dry-run \
  --lesson "Bound every metric command with a timeout a hung runner cannot outlive" \
  --because "$(python3 -c "print('a' * 899)")"
ck_exit "(y7) CONTROL: one character under the ceiling is accepted" 0 "$RC"

# F2: the provenance gate. It counted non-blank lines in the run file and never
# compared the promoted lesson to a byte of it — so a run file holding the single
# character 'x' licensed any sentence at all, which is the one thing the gate
# exists to stop. It now measures the overlap it claims to.
section "(y8) distil --to-skill provenance is a COMPARISON  [repo: distil8]"
mk_repo distil8
mk_iso
mkdir -p .swarm-loop
printf 'x\n' > .swarm-loop/learnings.md
LM_SHA="$(sha_of "$LM")"
sli distil --to-skill --lesson "Cap wave width at three tickets whenever the batch shares any build output" \
  --because "an entirely fabricated failure that appears nowhere in the run file this gate distils from"
ck_exit "(y8) a lesson the run file never mentions is REFUSED" 1 "$RC"
ck_has "(y8) ...saying its words appear nowhere in the run" "nowhere in what the run recorded"
ck_has "(y8) ...and naming one of them" "'wave'"
ck "(y8) and nothing was written" "$LM_SHA" "$(sha_of "$LM")"
# CONTROL: the same lesson, once the run has actually recorded it.
prov "Cap wave width at three tickets whenever the batch shares any build output"
sli distil --to-skill --lesson "Cap wave width at three tickets whenever the batch shares any build output" \
  --because "two lanes in one wave collided on a shared build output and both merges had to be redone"
ck_exit "(y8) CONTROL: recorded in the run first, the same lesson lands" 0 "$RC"
ck "(y8) ...and it really landed" "1" "$(n_entries)"
# CONTROL: distillation may GENERALISE. Half the distinctive words is the bar,
# not verbatim recall, or the gate would forbid the compression it is named for.
sli distil --to-skill \
  --lesson "Cap wave width whenever a batch shares any build output between its tickets" \
  --because "a generalisation of what the run recorded is still a distillation of it, not an invention"
ck_exit "(y8) CONTROL: a reworded distillation of a recorded lesson is accepted" 0 "$RC"

# F4/F5/F7/F8: the writer's mechanics, each of which had no red path.
section "(y9) the writer's mechanics  [repo: distil9]"
mk_repo distil9
mk_iso

# APPEND-ONLY, tested as append-only. The prefix assertion in (y2) catches a
# rewrite that LOSES content; it does not catch a read-modify-write that
# reproduces the same bytes, and a truncate-and-rewrite passed it. What
# O_APPEND actually buys is that a concurrent appender's bytes are still there
# afterwards — so this interleaves one, between the compare-and-swap's read and
# the write, and asserts it survived.
CONC_OUT=$(python3 - "$LM" 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
p = sys.argv[1]
FOREIGN = (b"\n- **Retire a worktree only once its branch is an ancestor of the base** - "
           b"a second process appended this between the read and the write\n")
real_open, fired = open, []

class _Racer(object):
    """Appends FOREIGN after the CAS has read the file and before it writes."""
    def __init__(self, f, path):
        self.f, self.path = f, path
    def read(self, *a):
        data = self.f.read(*a)
        if not fired:
            fired.append(1)
            with real_open(self.path, "ab") as g:
                g.write(FOREIGN)
        return data
    def __getattr__(self, n):
        return getattr(self.f, n)

def hooked(path, mode="r", *a, **k):
    f = real_open(path, mode, *a, **k)
    return _Racer(f, path) if mode == "rb" else f

seen = real_open(p, "rb").read()
swarmloop.open = hooked
try:
    swarmloop._dl_append(p, seen, b"\n- **Cap wave width at three when a batch shares build "
                                  b"outputs** - the entry this call was asked to write\n")
finally:
    swarmloop.open = real_open
now = real_open(p, "rb").read()
print("foreign=%s ours=%s prefix=%s"
      % (b"a second process appended this" in now,
         b"the entry this call was asked to write" in now,
         now.startswith(seen)))
PY
)
ck "(y9) a concurrent appender's bytes SURVIVE the append" \
   "foreign=True ours=True prefix=True" "$CONC_OUT"

# The trailing-newline branch: a file whose last byte is not \n must not have
# the new entry welded onto the end of the old one.
mk_iso
python3 - "$LM" <<'PY'
import sys
p = sys.argv[1]
open(p, "a").write("- **Kill a wave that produced no commit by its second checkpoint** - no newline")
PY
python3 - "$LM" >/dev/null 2>&1 <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
p = sys.argv[1]
swarmloop._dl_append(p, open(p, "rb").read(),
                     b"- **Rebase an integration branch immediately before merging it** - x\n")
PY
ck "(y9) an append onto a file with no trailing newline does not weld two entries" "2" \
   "$(n_entries)"

# The STALE-LOCK BREAKER — the only escape from a machine-wide wedge. This lock
# REFUSES after its timeout rather than proceeding, so an orphaned lockfile left
# by a SIGKILLed run blocks `distil --to-skill` for every future run on this
# machine until the staleness check clears it. (y6) proves the refusal; nothing
# proved the recovery, and disabling the breaker was invisible to the whole suite.
mk_iso
sli distil --lesson "$GOOD_L" --because "$GOOD_B"
python3 - "$LM.lock" <<'PY'
import os, sys, time
p = sys.argv[1]
open(p, "w").write("999999\n")          # an orphan: the pid is long gone
old = time.time() - 100000              # ...and far older than the 120s ceiling
os.utime(p, (old, old))
PY
ck_file "(y9) an orphaned lockfile is in place" "$LM.lock"
sli distil --to-skill --lesson "$GOOD_L" --because "$GOOD_B"
ck_exit "(y9) a STALE lock is broken and the append proceeds" 0 "$RC"
ck "(y9) ...and the entry really landed" "1" "$(n_entries)"
ck_nofile "(y9) ...and the orphan lockfile is gone" "$LM.lock"

# Normalisation: \x0b and \x0c were DROPPED rather than replaced, silently
# welding the words on either side of them together.
ck "(y9) a vertical tab becomes a space, it does not weld two words together" \
   "cap wave width" \
   "$(python3 -c "
import os, sys
sys.path.insert(0, os.environ['SL_DIR']); import swarmloop
print(swarmloop._dl_norm('cap\x0bwave\x0cwidth'))")"

# The suite's own blast radius. Every case above ran an isolated copy; if any one
# of them ever reaches the INSTALLED skill's memory instead, this goes red.
ck "(y) THE SUITE NEVER TOUCHED THE INSTALLED LEARNED.md" \
   "$REAL_LEARNED_SHA" "$(sha_of "$REAL_LEARNED")"



# ===========================================================================
# reachable — the wiring check as a command instead of a habit
#
# The defect these cover: two commits recovered from a killed agent, both
# claiming progress in prose. Commit A's function had ZERO call sites, was in
# neither __all__, and carried docstrings asserting a behaviour that existed
# nowhere — 0% effective, with the suite GREEN over a live HIGH-severity defect.
# Commit B's function was wired at two independent points and its claim held.
# The messages did not distinguish them; a call-site grep did, in ten seconds.
#
# So the fixture below is that incident, in miniature: one wired symbol, one
# with no call sites, one reached only by its own tests, one reached only by
# other dead code, one reached only through getattr. What is pinned is that the
# five come back as five DIFFERENT verdicts — a checker that could not tell
# commit A from commit B is the thing being replaced, and a checker that called
# everything suspicious would be worse than none.
# ===========================================================================
section "reachable: the five wiring verdicts  [repo: rwire]"
mk_repo rwire

verdict_of() {  # verdict_of <symbol> -> the verdict on that symbol's header line
  awk -v s="$1" '$1==s && NF>1 {$1=""; sub(/^ +/,""); print; exit}' "$OUT"
}

mkdir -p pkg tests node_modules/vendored docs
cat > pkg/__init__.py <<'EOF'
from .core import wired_ref

__all__ = ["wired_ref"]
EOF
cat > pkg/core.py <<'EOF'
def wired_ref(x):
    return x + 1


def scoped_ref(x):
    """Commit A: a docstring asserting a behaviour that exists nowhere."""
    return x - 1


def suite_ref(x):
    return x * 2


def accept_ref(x):
    return x


def dead_ref(x):
    return x


def orphan(x):
    return dead_ref(x)


def dyn_ref(x):
    return x


def loop_ref(n):
    if n <= 0:
        return 0
    return loop_ref(n - 1)


class Widget:
    def paint(self):
        return wired_ref(1)


# Appended AFTER Widget on purpose: every assertion above pins a line number in
# this file, so this fixture may only ever grow at the bottom.
def register(fn):
    return fn


@register
def registered_only(x):
    """Nothing references this name. The decorator above it is the wiring."""
    return x


@staticmethod
def inert_only(x):
    """Decorated too — but @staticmethod registers with nothing at all."""
    return x
EOF
cat > pkg/cli.py <<'EOF'
from . import core
from .core import Widget, wired_ref


def dyn():
    return getattr(core, "dyn_ref")(1)


def main():
    Widget().paint()
    return wired_ref(1) + dyn()


if __name__ == "__main__":
    main()
EOF
cat > tests/test_pkg.py <<'EOF'
from pkg.core import suite_ref, loop_ref


def test_a():
    assert suite_ref(2) == 4


def test_b():
    assert loop_ref(3) == 0
EOF
# NOT named test_*: the only thing making this a test file is that it lives
# under the protected acceptance directory, which is what this pins.
cat > .swarm-loop/acceptance/helpers.py <<'EOF'
from pkg.core import accept_ref


def use():
    return accept_ref(1)
EOF
cat > node_modules/vendored/copy.py <<'EOF'
def scoped_ref(x):
    return x
EOF
echo "notes" > docs/readme.md

# --- the headline case: defined, and nothing reaches it -------------------
sl reachable --symbol scoped_ref
ck_exit "a symbol with a definition and no callers exits 1" 1 "$RC"
ck "...and the verdict is the unmissable one" "NO CALL SITES" "$(verdict_of scoped_ref)"
ck_has "...naming where it IS defined, with the line" "defined    pkg/core.py:5  (function)"
ck_has "...and saying plainly that nothing references it" \
       "callers    NONE — zero references outside its own body"
ck_has "...and that it is on no public surface" "exported   NO — in no __all__"
ck_has "...with the diagnosis tying it to the recovered commit" \
       "This is the recovered commit's exact shape"

# --- the counterpart: a genuinely wired symbol comes back clean -----------
# Without this the checker above is satisfied by one that flags everything.
sl reachable --symbol wired_ref
ck_exit "a wired symbol exits 0" 0 "$RC"
ck "...and reads WIRED" "WIRED" "$(verdict_of wired_ref)"
ck_has "...with its product call sites enumerated" "callers    2 (2 product, 0 test)"
# This pins a call held by a METHOD BODY — the holder qualname is the method,
# not the module. It was labelled 'an ATTRIBUTE call' for a while, which it is
# not: `wired_ref(1)` there is a bare ast.Name. The attribute form has its own
# repo below, because nothing in THIS fixture exercised it and deleting the whole
# ast.Attribute arm of the scanner left the section green.
ck_has "...one of which is held by a METHOD, not by the module scope" \
       "pkg/core.py:38  in pkg.core.Widget.paint"
ck_has "...and both export routes named" "package __init__ (pkg/__init__.py:1)"
ck_has "...including __all__" "__all__ (pkg/__init__.py:3)"

# --- called only by its own tests: green suite, dead product path ---------
sl reachable --symbol suite_ref
ck_exit "a symbol whose only callers are tests exits 1" 1 "$RC"
ck "...and is not confused with WIRED" "TESTS ONLY" "$(verdict_of suite_ref)"
ck_has "...with the split spelled out" "callers    1 (0 product, 1 test)"
ck_has "...and the caller marked as a test" "tests/test_pkg.py:5  in tests.test_pkg.test_a  [TEST]"

# --- a caller under the frozen acceptance directory is a TEST caller ------
# helpers.py is not named test_*; only its location makes it one.
sl reachable --symbol accept_ref
ck "a caller under .swarm-loop/acceptance counts as a test caller" \
   "TESTS ONLY" "$(verdict_of accept_ref)"
ck_has "...and is shown as such" ".swarm-loop/acceptance/helpers.py:5  in"

# --- 'called from somewhere' is NOT 'reachable from the entry point' -----
# The same symbol, the same tree, two different questions. If these two ever
# return the same answer the reachability layer has stopped existing.
sl reachable --symbol dead_ref
ck_exit "dead_ref has a product caller, so the caller layer alone passes it" 0 "$RC"
ck "...reading WIRED with no --from" "WIRED" "$(verdict_of dead_ref)"
ck_has "...and the report says so out loud rather than implying reachability" \
       "NO --from WAS GIVEN, so nothing was checked for reachability"
# The limits block is an if/else and only its positive arm was ever asserted, so
# forcing the branch either way left the suite green while a report printed both
# halves at once. Both directions are pinned now, on both arms.
ck_hasnt "...and NOT the arm that only applies when --from IS given" \
         "--from reachability is a NAME-KEYED forward closure"
sl reachable --symbol dead_ref --from pkg/cli.py
ck_exit "...but with a real entry point named it exits 1" 1 "$RC"
ck "...because its only caller is itself unreached" "UNREACHED" "$(verdict_of dead_ref)"
ck_has "...and the reachable line says NO against the entry point" \
       "reachable  NO from pkg/cli.py"
ck_has "...and the limits block switches to the --from arm" \
       "--from reachability is a NAME-KEYED forward closure"
ck_hasnt "...and does NOT also claim no --from was given, three lines below a verdict that used one" \
         "NO --from WAS GIVEN"
sl reachable --symbol wired_ref --from pkg/cli.py
ck_exit "the same entry point passes the symbol that IS on the path" 0 "$RC"
ck_has "...with reachable YES" "reachable  YES from pkg/cli.py"

# --- an entry point may also be named as a bare symbol -------------------
sl reachable --symbol dead_ref,wired_ref --from main
ck_exit "--from accepts a bare symbol name as the entry scope" 1 "$RC"
ck "...and grades the unreached one" "UNREACHED" "$(verdict_of dead_ref)"
ck "...and the reached one" "WIRED" "$(verdict_of wired_ref)"

# --- getattr: the verdict is NOT upgraded on a string literal -------------
# Erring toward 'reachable' here would produce exactly the false OK this
# command exists to prevent, so the hint is printed and the verdict stands.
sl reachable --symbol dyn_ref
ck_exit "a symbol reached only through getattr still exits 1" 1 "$RC"
ck "...the verdict is not upgraded by the string literal" \
   "NO CALL SITES" "$(verdict_of dyn_ref)"
ck_has "...but the string IS reported so the human can settle it" \
       "the bare name appears as a STRING at pkg/cli.py:6"
ck_has "...and the report says the verdict was not upgraded on it" \
       "The verdict above is NOT upgraded on this."

# --- recursion is not a call site ----------------------------------------
# Positive counterpart first: the scanner DOES see loop_ref's test caller.
sl reachable --symbol loop_ref
ck "the scanner sees loop_ref's one real caller" "TESTS ONLY" "$(verdict_of loop_ref)"
sl reachable --symbol loop_ref --path pkg
ck_exit "with the tests out of scope, a self-call is not a caller" 1 "$RC"
ck "...so recursion alone reads as no call sites" "NO CALL SITES" "$(verdict_of loop_ref)"
ck_has "...and --path really narrowed the scan" "3 python file(s) under pkg"

# --- a symbol that is not there at all ------------------------------------
sl reachable --symbol never_written
ck_exit "a claimed symbol that does not exist exits 1" 1 "$RC"
ck "...with its own verdict, not 'no callers'" "UNDEFINED" "$(verdict_of never_written)"
ck_has "...and says where it looked" "defined    NOWHERE in the"

# --- vendored copies are not scanned unless asked for ---------------------
sl reachable --symbol scoped_ref
ck_hasnt "an unqualified scan skips node_modules/" "node_modules"
ck_hasnt "...so the vendored duplicate does not make it AMBIGUOUS" \
         "AMBIGUOUS  2 definitions"
sl reachable --symbol scoped_ref --path node_modules/vendored
ck_has "...but a --path that names it IS honoured (the file is really there)" \
       "defined    node_modules/vendored/copy.py:1"

# --- everything at once: the summary line ---------------------------------
# One symbol per verdict class, so the ORDER and the LABELS of the summary line
# are pinned as well as the counts. A class that never appears here is a class
# the summary could silently stop printing.
sl reachable --symbol wired_ref,registered_only,scoped_ref,suite_ref,dead_ref,never_written \
   --from pkg/cli.py
ck_exit "a mixed run exits 1 when any symbol is not wired" 1 "$RC"
ck_has "the summary counts every verdict class" \
       "summary: 1 WIRED · 1 DECORATOR-REGISTERED · 1 NO CALL SITES · 1 TESTS ONLY · 1 UNREACHED · 1 UNDEFINED"

# --- a decorator is also a REFERENCE held by the enclosing scope ----------
# `@register` is the only mention of `register` anywhere in the tree. If the
# scanner stops visiting decorator EXPRESSIONS, every decorator a codebase
# implements itself reads as dead code — the opposite error to the one below.
sl reachable --symbol register --path pkg
ck_exit "a function used only AS a decorator is wired" 0 "$RC"
ck "...and reads WIRED" "WIRED" "$(verdict_of register)"
ck_has "...counting the decorator line as the call site" "callers    1 (1 product, 0 test)"
ck_has "...held by the scope the decorator evaluates in" "pkg/core.py:47  in pkg.core"

# --- and the symbol it decorates is NOT the recovered commit's shape ------
sl reachable --symbol registered_only,inert_only --path pkg
ck_exit "a registering decorator is no hard finding; an inert one still is" 1 "$RC"
ck "the decorated symbol gets the decorator verdict" \
   "DECORATOR-REGISTERED" "$(verdict_of registered_only)"
ck "...and @staticmethod, which registers nothing, does not" \
   "NO CALL SITES" "$(verdict_of inert_only)"

# --- the limits ship WITH the answer, including a clean one ---------------
# The whole value of this command is that its OK can be trusted; a blind spot
# the reader has to already know about is not a stated limit.
sl reachable --symbol wired_ref
ck_exit "an all-clean report still exits 0" 0 "$RC"
ck_has "...and still prints what it cannot see" \
       "WHAT THIS CANNOT SEE — a clean report is not proof of wiring"
ck_has "...naming the dynamic-dispatch blind spot" \
       "getattr(), a registry dict, a decorator table or a plugin loader"
ck_has "...naming the name-matching blind spot and its DIRECTION" \
       "it over-counts"
ck_has "...and that it is Python-only" "Python only."

# --- --no-fail reports without failing ------------------------------------
sl reachable --symbol scoped_ref --no-fail
ck_exit "--no-fail exits 0" 0 "$RC"
ck "...while the verdict is unchanged" "NO CALL SITES" "$(verdict_of scoped_ref)"

# --- --json ---------------------------------------------------------------
sl reachable --symbol scoped_ref,wired_ref --from pkg/cli.py --json
ck_exit "--json exits 1 the same way" 1 "$RC"
ck "json: the verdict is a field" "NO CALL SITES" \
   "$(jget "$OUT" "d['symbols'][0]['verdict']")"
ck "json: the definition is located" "pkg/core.py:5" \
   "$(jget "$OUT" "d['symbols'][0]['definitions'][0]['file'] + ':' + str(d['symbols'][0]['definitions'][0]['line'])")"
ck "json: product call sites are counted separately" "2" \
   "$(jget "$OUT" "d['symbols'][1]['product_call_sites']")"
ck "json: not_wired names exactly the failing symbol" "['scoped_ref']" \
   "$(jget "$OUT" "d['not_wired']")"
ck "json: the limits travel with the machine-readable answer too" "True" \
   "$(jget "$OUT" "any('cannot see' in x.lower() for x in d['limits'])")"

# --- an entry point that resolves to nothing must be LOUD -----------------
# Otherwise every symbol comes back UNREACHED from an empty graph and the
# operator reads a typo as a finding.
sl reachable --symbol wired_ref --from no_such_entry
ck_exit "an unresolvable entry point exits 1" 1 "$RC"
ck_has "...and is named" "ENTRY POINT DID NOT RESOLVE: no_such_entry"
ck_has "...and the resulting UNREACHED is called an artefact, not a finding" \
       "reporting about an EMPTY graph"

# --- a scan that found no Python at all may not be read as evidence -------
sl reachable --symbol wired_ref --path docs
ck_exit "scanning a directory with no .py exits 1 rather than reporting clean" 1 "$RC"
ck_has "...and refuses to be read as evidence about another language" \
       "NO PYTHON FILES WERE SCANNED"

# --- usage --------------------------------------------------------------
sl reachable
ck_exit "reachable with no --symbol is a usage error (exit 3, never terminal)" 3 "$RC"

# --- a second definition of the same name is declared AMBIGUOUS ----------
# Name-keyed matching reads generously here, which is the false-OK direction,
# so it must be said rather than absorbed.
cat > pkg/alt.py <<'EOF'
def wired_ref(y):
    return y
EOF
sl reachable --symbol wired_ref
ck_has "two definitions of one name are flagged AMBIGUOUS" "AMBIGUOUS  2 definitions share this name"
ck_has "...and both definitions are listed" "defined    pkg/alt.py:1"

# --- a file that does not parse is a hole in the ANSWER, and says so -----
printf 'def oops(\n' > pkg/broken.py
sl reachable --symbol wired_ref
ck_has "an unparsable file is reported next to the verdicts" "COULD NOT PARSE pkg/broken.py"
ck_has "...as a blind spot in this answer, not a footnote" \
       "its call sites are invisible to this answer"
ck "...and the rest of the scan still answers" "WIRED" "$(verdict_of wired_ref)"
ck_hasnt "...with no traceback escaping the parse failure" "Traceback"


# ===========================================================================
section "reachable: a decorator IS wiring, and the report names which  [repo: rdec]"
# Measured, read-only, against a live FastAPI project: 24.5% of its public
# top-level functions came back not-wired, and 20 of the 31 'NO CALL SITES' —
# 'defined, and NOTHING references it' — were @router.get/@router.post handlers,
# i.e. the application's entire HTTP surface, i.e. the most-wired code in the
# repo. The scanner was already holding n.decorator_list and throwing it away.
# Decorator registration is not an edge case in Python: it is how routes, CLI
# commands, event handlers, plugins and fixtures are wired.
mk_repo rdec

mkdir -p app
cat > app/routes.py <<'EOF'
from fastapi import APIRouter

router = APIRouter()
app = router


@router.post("/auctions", status_code=201)
def create_auction(body):
    """Registered by the decorator above, and by nothing else at all."""
    return {"ok": True}


@lru_cache
def memoised_and_dead(x):
    """Decorated too — and @lru_cache registers it with nothing at all."""
    return x


@app.cache
def cached_by_app(x):
    """@app.cache is NOT @functools.cache; only the WHOLE label may match."""
    return x


def helper(x):
    return x


@router.get("/things")
def read_things():
    return helper(1)
EOF
cat > app/main.py <<'EOF'
from fastapi import FastAPI

from .routes import router

app = FastAPI()
app.include_router(router)
EOF

# --- the headline: a live route handler is not the recovered commit -------
sl reachable --symbol create_auction --path app --from app/main.py
ck "a decorator-registered handler gets its own verdict" \
   "DECORATOR-REGISTERED" "$(verdict_of create_auction)"
ck_exit "...and does NOT fail the run: the commonest wiring idiom in Python cannot be a hard failure" \
        0 "$RC"
ck_has "...the decorator is named, with the line it sits on" \
       "decorated  @router.post at app/routes.py:7"
ck_has "...and what that means is spelled out" \
       "a route/registry/plugin table may be its only call site"
ck_has "...including the hop that was NOT taken" \
       "This scanner does NOT follow a decorator into the table on the other side"
ck_hasnt "...and the incident's own text is NOT put on live wired code" \
         "defined, and NOTHING references it"
ck_has "...while the diagnosis still refuses to call it an all-clear" \
       "confirm BY EYE that the table it registers into is itself imported and reached"
ck_has "...and the limits block says the hop is the reader's" \
       "a DECORATOR on the definition is reported and NOT followed"

# --- the counterpart: a decorator that registers with NOTHING -------------
# Without this the softening above is satisfied by softening everything, and
# the zero-caller defect walks straight through wearing @staticmethod.
sl reachable --symbol memoised_and_dead --path app
ck_exit "a decorator that only WRAPS leaves the finding intact (exit 1)" 1 "$RC"
ck "...and the verdict is unchanged" "NO CALL SITES" "$(verdict_of memoised_and_dead)"
ck_has "...the decorator is still reported" "decorated  @lru_cache at app/routes.py:13"
ck_has "...and said to register nothing" \
       "every one of these WRAPS IN PLACE and registers nothing"
ck_has "...so the incident's text DOES appear here" \
       "This is the recovered commit's exact shape"

# --- the inert list matches the WHOLE label, never the last component -----
sl reachable --symbol cached_by_app --path app
ck "@app.cache is not @functools.cache" \
   "DECORATOR-REGISTERED" "$(verdict_of cached_by_app)"
ck_has "...and is reported by its full dotted label" \
       "decorated  @app.cache at app/routes.py:19"

# --- ordinary accounting is untouched by the decorator layer --------------
sl reachable --symbol helper --path app
ck_exit "a plain function called from a decorated body is still just WIRED" 0 "$RC"
ck "...with no softening applied" "WIRED" "$(verdict_of helper)"
ck_has "...and the caller is the decorated function's body" \
       "app/routes.py:31  in app.routes.read_things"

# --- the exit code separates 'a hop I did not take' from 'a finding' ------
sl reachable --symbol create_auction,read_things --path app
ck_exit "a run of nothing but decorator-registered symbols exits 0" 0 "$RC"
ck_has "...and says how many there were" \
       "summary: 0 WIRED · 2 DECORATOR-REGISTERED · 0 NO CALL SITES"
sl reachable --symbol create_auction,memoised_and_dead --path app
ck_exit "...but one real finding alongside them still exits 1" 1 "$RC"

# --- --json keeps the two questions apart --------------------------------
sl reachable --symbol create_auction,helper --path app --json
ck_exit "json: a decorator-registered symbol alone exits 0" 0 "$RC"
ck "json: it is still NOT claimed wired" "['create_auction']" \
   "$(jget "$OUT" "d['not_wired']")"
ck "json: ...but it is not what the exit code is about" "[]" \
   "$(jget "$OUT" "d['findings']")"
ck "json: the decorator travels with the symbol" "@router.post" \
   "$(jget "$OUT" "d['symbols'][0]['decorators'][0]['label']")"
ck "json: and is marked as a possible registration" "True" \
   "$(jget "$OUT" "d['symbols'][0]['registering_decorator']")"

# --- a decorator expression this scanner cannot read is not called inert --
# PEP 614 spellings (@x[0], @lambda...) are 3.9+, so the label renderer is
# exercised directly rather than through a fixture the suite may not parse.
ck "a dotted decorator renders to its callee" "@router.post" \
   "$(python3 -c 'import ast, os, sys; sys.path.insert(0, os.environ["SL_DIR"]); import swarmloop as S; print(S._reach_dec_label(ast.parse("router.post(\"/x\", status_code=201)").body[0].value))')"
ck "...and one that is not a dotted name is OPAQUE, not silently inert" "@<expression>" \
   "$(python3 -c 'import ast, os, sys; sys.path.insert(0, os.environ["SL_DIR"]); import swarmloop as S; print(S._reach_dec_label(ast.parse("TABLE[0]").body[0].value))')"
ck "...and an opaque decorator counts as a POSSIBLE registration" "False" \
   "$(python3 -c 'import os, sys; sys.path.insert(0, os.environ["SL_DIR"]); import swarmloop as S; print(S.REACH_DEC_OPAQUE[1:] in S.REACH_INERT_DECORATORS)')"


# ===========================================================================
section "reachable: call-site forms, classifiers and --path  [repo: rform]"
# Every case in this block corresponds to a mutation that changed real,
# demonstrated behaviour and left the whole suite green. `mod.f()` and `self.f()`
# are the dominant way a symbol is called in real Python and had NO red path at
# all: deleting the ast.Attribute arm flipped a wired symbol to NO CALL SITES
# with 70/70 still passing.
mk_repo rform

mkdir -p pkg tests
cat > pkg/core.py <<'EOF'
REGISTRY = {}


def attr_called(x):
    """Called ONLY as core.attr_called(...) — the dominant form in real Python."""
    return x


def self_called(x):
    return x


def outer(n):
    """Its ONLY reference is from the function nested inside its own body."""
    def inner():
        return outer(n - 1)
    return inner


def helper_called(x):
    return x


def fixture_called(x):
    return x


def conftest_called(x):
    return x


def suffix_called(x):
    return x


def many_callers(x):
    return x


class Holder:
    def go(self):
        return self.self_called(1)


__all__ = ["attr_called", "REGISTRY"]


# Appended after __all__ on purpose — every assertion above pins a line in this
# file, so it may only grow at the bottom.
def alias_called(x):
    return x


def probe_only(x):
    return x
EOF
cat > pkg/cli.py <<'EOF'
from . import core


def main():
    core.attr_called(1)
    return core.Holder().go()


class Boot:
    SEEDED = core.helper_called(1)


if __name__ == "__main__":
    main()
EOF
cat > pkg/uses.py <<'EOF'
from pkg.core import many_callers


def u01(): return many_callers(1)
def u02(): return many_callers(2)
def u03(): return many_callers(3)
def u04(): return many_callers(4)
def u05(): return many_callers(5)
def u06(): return many_callers(6)
def u07(): return many_callers(7)
def u08(): return many_callers(8)
def u09(): return many_callers(9)
def u10(): return many_callers(10)
EOF
# Three files that are tests for three DIFFERENT reasons. Each of the classifier's
# arms gets exactly one caller that no other arm would catch.
cat > pkg/conftest.py <<'EOF'
from pkg.core import conftest_called


def fixture():
    return conftest_called(1)
EOF
cat > pkg/thing_test.py <<'EOF'
from pkg.core import suffix_called


def check():
    return suffix_called(1)
EOF
cat > tests/helpers.py <<'EOF'
from pkg.core import fixture_called


def use():
    return fixture_called(1)
EOF
# The two shapes an ast pass cannot see by default: a symbol consumed under a
# LOCAL RENAME, and a symbol called from Python source that lives in a STRING.
cat > pkg/alias_use.py <<'EOF'
from pkg.core import alias_called as _alias

PROBE = """
from pkg import core
core.probe_only()
"""


def go():
    """Prose mentioning probe_only(1) and alias_called(2) — documentation."""
    return _alias(1)


RESULT = go()
EOF
echo "not python" > notes.txt

# --- mod.f() : the form the suite could not see -------------------------
sl reachable --symbol attr_called --path pkg
ck_exit "a symbol called only as mod.f() is wired" 0 "$RC"
ck "...and reads WIRED, not NO CALL SITES" "WIRED" "$(verdict_of attr_called)"
ck_has "...with the attribute call site located" "pkg/cli.py:5  in pkg.cli.main"
ck_has "...and counted as product" "callers    1 (1 product, 0 test)"

# --- self.f() : the same arm, through a method --------------------------
sl reachable --symbol self_called --path pkg
ck "a symbol called only as self.f() is wired too" "WIRED" "$(verdict_of self_called)"
ck_has "...held by the method that calls it" "pkg/core.py:42  in pkg.core.Holder.go"

# --- a helper nested INSIDE the symbol is not an external caller ---------
# The equality half of this rule was pinned by the recursion cases; the
# startswith half was not, and it fails in the false-OK direction.
sl reachable --symbol outer --path pkg
ck_exit "a function referenced only from its own nested inner() exits 1" 1 "$RC"
ck "...because a helper nested inside it is still its own body" \
   "NO CALL SITES" "$(verdict_of outer)"

# --- the __all__ branch records an EXPORT, not a dynamic hint -----------
sl reachable --symbol attr_called --path pkg
ck_has "a name inside __all__ is an export" "exported   __all__ (pkg/core.py:45)"
# Anchored on the per-symbol line, because the LIMITS block legitimately uses
# the same wording to describe the hint in general.
ck_hasnt "...and NOT a string literal a getattr might resolve" \
         "dynamic    the bare name appears as a STRING at"

# --- a module-level assignment IS a definition ---------------------------
# Without this rule a constant reads UNDEFINED, whose diagnosis asserts 'the
# commit does not contain what its message claims' — flatly false.
sl reachable --symbol REGISTRY --path pkg
ck "a module-level assignment is defined, not UNDEFINED" \
   "NO CALL SITES" "$(verdict_of REGISTRY)"
ck_has "...and is located, with its kind" "defined    pkg/core.py:1  (assignment)"

# --- the listing caps; the COUNT on the header line does not -------------
sl reachable --symbol many_callers --path pkg
ck_has "the caller COUNT is exact even past the listing cap" \
       "callers    10 (10 product, 0 test)"
ck_has "...and the elision says how many were not printed" "... and 2 more"
ck_has "...having printed exactly the cap" "pkg/uses.py:11  in pkg.uses.u08"
ck_hasnt "...and nothing past it" "pkg/uses.py:12  in pkg.uses.u09"
ck_has "the symbols a file IMPORTS are reported" "imported   pkg/uses.py:1"

# --- each arm of the test classifier gets its own caller ----------------
sl reachable --symbol fixture_called --path pkg --path tests
ck_exit "a caller under tests/ that is NOT named test_* still counts as a test" 1 "$RC"
ck "...so the symbol is TESTS ONLY, not WIRED" "TESTS ONLY" "$(verdict_of fixture_called)"
ck_has "...and is marked" "tests/helpers.py:5  in tests.helpers.use  [TEST]"
sl reachable --symbol conftest_called --path pkg
ck "a conftest.py caller is a test caller" "TESTS ONLY" "$(verdict_of conftest_called)"
sl reachable --symbol suffix_called --path pkg
ck "a *_test.py caller is a test caller" "TESTS ONLY" "$(verdict_of suffix_called)"
sl reachable --symbol attr_called --path pkg
ck_has "...and the limits block says the classifier goes by PATH alone" \
       "a caller counts as a TEST by its PATH alone"

# --- reaching a scope reaches the CLASS BODIES inside it ----------------
# A class body executes when its enclosing scope does; a nested function does
# not. helper_called is referenced ONLY from a class body in the entry module.
sl reachable --symbol helper_called --path pkg --from pkg/cli.py
ck_exit "a symbol referenced only from a class body in the entry module is reached" 0 "$RC"
ck "...and reads WIRED" "WIRED" "$(verdict_of helper_called)"
ck_has "...with the class body named as the holder" "pkg/cli.py:10  in pkg.cli.Boot"

# --- the other two documented --from spellings actually work ------------
sl reachable --symbol helper_called --path pkg --from pkg.cli
ck_exit "--from accepts a DOTTED MODULE" 0 "$RC"
ck_has "...and resolves it rather than reporting it unresolved" "reachable  YES from pkg.cli"
sl reachable --symbol attr_called,helper_called --path pkg --from pkg/cli.py:main
ck_exit "--from accepts module:symbol, seeding ONE scope" 1 "$RC"
ck_hasnt "...and does not report the spelling as unresolved" "ENTRY POINT DID NOT RESOLVE"
ck "...reaching what that scope reaches" "WIRED" "$(verdict_of attr_called)"
ck "...and not what it does not" "UNREACHED" "$(verdict_of helper_called)"

# --- a typo'd --from fails the run even when every symbol is wired ------
# Otherwise the clause is dead: an empty seed set already forces UNREACHED on
# everything, so the only live path is 'some entries resolved, one did not'.
sl reachable --symbol attr_called --path pkg --from pkg/cli.py --from nope_typo
ck_exit "one unresolvable entry point fails a run whose symbols are all WIRED" 1 "$RC"
ck "...the symbol really is wired" "WIRED" "$(verdict_of attr_called)"
ck_has "...and the typo is what failed it" "ENTRY POINT DID NOT RESOLVE: nope_typo"

# --- a --path that names nothing is NOT silently dropped -----------------
# It used to be, while the header line went on asserting it had been scanned —
# a confident NO CALL SITES on wired code, from a typo.
sl reachable --symbol attr_called --path pkg
ck_exit "a --path that resolves is quiet about it" 0 "$RC"
ck_hasnt "...saying nothing about unresolved paths" "--path DID NOT RESOLVE"
sl reachable --symbol attr_called --path pkg --path does_not_exist_typo
ck_exit "a --path naming nothing fails the run" 1 "$RC"
ck_has "...and is named" "--path DID NOT RESOLVE: does_not_exist_typo"
ck_has "...and says what that cost the answer" "NOTHING under it was scanned"
ck "...while the symbols that WERE scanned are still graded" \
   "WIRED" "$(verdict_of attr_called)"
sl reachable --symbol attr_called --path pkg --path notes.txt
ck_exit "a --path naming a file this scan cannot read fails too" 1 "$RC"
ck_has "...and names it" "--path DID NOT RESOLVE: notes.txt"
sl reachable --symbol attr_called --path pkg/core.py --path pkg/cli.py --no-fail
ck_hasnt "...but a .py file named directly resolves" "--path DID NOT RESOLVE"
ck_has "...and only those two files were scanned" "against 2 python file(s)"
sl reachable --symbol attr_called --path pkg --path does_not_exist_typo --json
ck "json: the unresolved path is a field of its own" "['does_not_exist_typo']" \
   "$(jget "$OUT" "d['paths_unresolved']")"

# --- a symbol consumed under a LOCAL RENAME is still consumed ------------
# `from m import foo as _foo` then `_foo()` used to count as nothing, because
# refs are keyed by the identifier at the reference site. Measured on a live
# repo: one symbol's ONLY consumption path was that idiom, in four packages, and
# it read NO CALL SITES — the verdict that says 'NOTHING references it'.
sl reachable --symbol alias_called --path pkg
ck_exit "a symbol called only under an import alias is wired" 0 "$RC"
ck "...and reads WIRED, not NO CALL SITES" "WIRED" "$(verdict_of alias_called)"
ck_has "...counted as a product caller" "callers    1 (1 product, 0 test)"
ck_has "...with the rename shown, so the reader can follow it" \
       "pkg/alias_use.py:11  in pkg.alias_use.go  [as _alias]"
ck_has "...and the limits say a rename any OTHER way is not followed" \
       "references are matched by the identifier AT THE REFERENCE SITE"
# The caller LIST and the reachability CLOSURE are two different consumers of
# that edge. Resolving the alias for one and not the other buys a symbol that is
# WIRED with no --from and UNREACHED with one, for no reason a reader can see.
sl reachable --symbol alias_called --path pkg --from pkg/alias_use.py
ck_exit "the closure follows an alias too, not only the caller list" 0 "$RC"
ck "...so a symbol reached only under a local rename is WIRED" \
   "WIRED" "$(verdict_of alias_called)"
ck_has "...with the reachability line saying YES" "reachable  YES from pkg/alias_use.py"

# --- Python source inside a STRING is text, and the report says where ----
# The limits block promised the string hint covered this; it only ever matched a
# string whose WHOLE value was the name, so the subprocess-probe case — the one
# it cites as measured — produced no hint at all.
sl reachable --symbol probe_only --path pkg
ck_exit "a symbol called only from source inside a string is still a finding" 1 "$RC"
ck "...the verdict is NOT upgraded on the string" "NO CALL SITES" "$(verdict_of probe_only)"
ck_has "...but the string is located, call-shaped" \
       "embedded   the name appears CALL-SHAPED inside a longer string at pkg/alias_use.py:3"
ck_has "...and says why a call can hide there" \
       "is text to an ast pass, so a real call site can hide there"
ck_hasnt "...while a name merely MENTIONED in a docstring raises nothing" \
         "pkg/alias_use.py:10"

# --- the tool FAILING must not be spelled like the tool FINDING ---------
sl reachable --symbol ,, --path pkg
ck_exit "an all-empty --symbol is a usage error (3), not a wiring finding (1)" 3 "$RC"

# ===========================================================================
# V1 · the whole scoring apparatus could be driven to target by DESELECTION
#
# Measured end to end before any of this existed. Honest red baseline, then a
# worker adds a marker filter to pytest.ini — NOT a protected path, not in any
# test diff — and with src/ still EMPTY:
#
#   freeze   -> ok (parsed 2, target 0, error 2)     honest red baseline
#   measure  -> value=2 error=2                      "2 failed in 0.03s"
#   [pytest.ini gains a marker filter; NOTHING is built]
#   verify   -> OK - frozen harness intact           exit 0
#   measure  -> value=0 error=0 OK                   "2 deselected in 0.00s"
#   analyze  -> Targets met 1/1, ALL TARGETS MET     build->audit transition
#
# Every number in that chain is well-formed. The single token that separates the
# two worlds is `deselected`, and no code in swarmloop.py read it — while
# SKILL.md:278 had stated the rule ("record PASSED / SKIPPED / DESELECTED beside
# every binary gate reading") since before the incident.
# ===========================================================================
section "V1 the deselection parser  [repo: rdesel1]"
mk_repo rdesel1

sf_() {  # sf_ <name> <expected "sel desel pytest"> <tool output>
  local got
  got=$(python3 - "$3" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
print("%s %s %s" % swarmloop._selection_facts(sys.argv[1]))
PY
)
  ck "$1" "$2" "$got"
}
sn_() {  # sn_ <name> <expected note> <tool output>
  local got
  got=$(python3 - "$3" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
print(swarmloop._selection_note(*swarmloop._selection_facts(sys.argv[1])))
PY
)
  ck "$1" "$2" "$got"
}

sf_ "V1 the honest red baseline: 2 selected, nothing thrown away" "2 None True" \
    "2 failed in 0.03s"
sf_ "V1 the SAME metric after a marker filter: nothing ran, 2 DESELECTED" "None 2 True" \
    "2 deselected in 0.00s"
sf_ "V1 a partial deselection keeps both numbers" "1 2 True" \
    "1 failed, 2 deselected in 0.03s"
sf_ "V1 pytest's verbose shape" "0 120 True" \
    "collected 120 items / 120 deselected / 0 selected"
sf_ "V1 'no tests ran' is pytest-shaped and selects nothing" "None None True" \
    "no tests ran in 0.01s"
# The false-positive guard, and it is the reason `error` is not in the shape
# regex: these three are the exact strings _collected_count was hardened against
# reading as a test count. A zero-selection REFUSAL that fired on them would
# refuse every lint/typecheck metric in existence.
sf_ "V1 tsc is not pytest-shaped, so it can never be 'selected ZERO'" "None None False" \
    "Found 3 errors in 2 files."
sf_ "V1 ruff is not pytest-shaped either" "None None False" \
    "Found 12 errors."
sf_ "V1 eslint is not pytest-shaped either" "None None False" \
    "7 problems (7 errors, 0 warnings)"
sf_ "V1 a bare number from a wc-style metric is not pytest-shaped" "None None False" \
    "42"
sn_ "V1 the note names DESELECTED in caps, because that is the finding" \
    "? test(s) selected, 2 DESELECTED" "2 deselected in 0.00s"
sn_ "V1 the note is silent when there is nothing to say" "" "42"
sn_ "V1 a pytest run that selected nothing says so" "ZERO tests selected" \
    "no tests ran in 0.01s"

# --- and end to end, through the three commands that produce the number ------
section "V1 freeze REFUSES a metric scored by selection  [repo: rdesel2]"
mk_repo rdesel2
cat > run_tests.sh <<'EOF'
#!/bin/sh
if [ -f DESELECT ]; then echo "2 deselected in 0.00s"; echo 0; exit 5; fi
echo "2 failed in 0.03s"; echo 2
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "failing", "name": "failing tests", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm baseline

sl freeze
ck_exit "V1 CONTROL: the honest red baseline freezes" 0 "$RC"
ck_has "V1 CONTROL: ...and the selection is recorded beside the number" "[2 test(s) selected]"
ck_hasnt "V1 CONTROL: ...with no refusal" "decided by TEST SELECTION"

# Now the deselecting world, at a FIRST freeze — the last moment the command is
# editable text rather than frozen bytes needing a user-approved amendment.
mk_repo rdesel3
cat > run_tests.sh <<'EOF'
#!/bin/sh
echo "2 deselected in 0.00s"; echo 0
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "failing", "name": "failing tests", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm desel
sl freeze
ck_exit "V1 freeze REFUSES a metric whose smoke run deselects its own tests" 1 "$RC"
ck_has "V1 ...naming the metric" "failing: ? test(s) selected, 2 DESELECTED"
ck_has "V1 ...showing the deselect line itself" "2 deselected in 0.00s"
ck_has "V1 ...and naming the sanctioned way forward" "--allow-deselection"
ck_has "V1 ...the goals stay unfrozen" "goals remain unfrozen"
ck_nofile "V1 ...and no manifest is written" ".swarm-loop/manifest.json"
ck "V1 ...and goals.json is still unfrozen on disk" "False" \
   "$(jget .swarm-loop/goals.json "d['frozen']")"

# The escape hatch: a project whose deselection is a deliberate convention.
sl freeze --allow-deselection
ck_exit "V1 --allow-deselection freezes anyway" 0 "$RC"
ck_has "V1 ...and says out loud what it accepted, and on whose authority" \
       "1 metric(s) whose score depends on test SELECTION accepted (--allow-deselection passed)"
ck_file "V1 ...and the manifest is written" ".swarm-loop/manifest.json"

# THE D4 RULE, and it is the assertion that keeps this gate off a live run. A
# harness frozen by an earlier version carries a deselecting metric this gate
# never inspected, and every remedy for it — editing the frozen command — IS an
# amendment. A refusal on an already-frozen harness would refuse the only door
# out of the state it is complaining about.
mk_repo rdesel3b
cat > run_tests.sh <<'EOF'
#!/bin/sh
if [ -f DESELECT ]; then echo "2 deselected in 0.00s"; echo 0; exit 5; fi
echo "2 failed in 0.03s"; echo 2
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "failing", "name": "failing tests", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm honest
sl freeze
ck_exit "V1 the run froze honestly, before any filter existed" 0 "$RC"
gq add -A; gq commit -qm frozen
touch DESELECT
sl freeze --amend "the user approved widening the target"
ck_exit "V1 an ALREADY-FROZEN harness is never wedged by this gate — --amend proceeds" 0 "$RC"
ck_has "V1 ...the finding is still reported loudly, by name" \
       "failing: ? test(s) selected, 2 DESELECTED"
ck_has "V1 ...and says why it cannot be a refusal here" "the remedy is itself an amendment"

# A metric that runs pytest and pytest runs NOTHING is the same defect wearing a
# different number: whatever the value is, it was not observed.
mk_repo rdesel4
cat > run_tests.sh <<'EOF'
#!/bin/sh
echo "no tests ran in 0.01s"; echo 0
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "failing", "name": "failing tests", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm empty
sl freeze
ck_exit "V1 freeze REFUSES a metric whose pytest selects zero tests" 1 "$RC"
ck_has "V1 ...saying pytest ran nothing" "pytest ran nothing"

# CONTROL, and it is the one that keeps this gate off every lint metric in
# existence: a non-pytest command that legitimately prints zero must freeze.
mk_repo rdesel5
cat > run_tests.sh <<'EOF'
#!/bin/sh
echo "Found 3 errors in 2 files."
echo 3
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "types", "name": "type errors", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm tsc
sl freeze
ck_exit "V1 CONTROL: a tsc-shaped metric is untouched by the selection gate" 0 "$RC"
ck_hasnt "V1 CONTROL: ...not refused" "decided by TEST SELECTION"
ck_hasnt "V1 CONTROL: ...and not accused of selecting zero tests" "ZERO tests selected"

# --- measure: the same fact, WARN-ONLY, permanently -------------------------
section "V1 measure reports deselection and never stops the run  [repo: rdesel6]"
mk_repo rdesel6
cat > run_tests.sh <<'EOF'
#!/bin/sh
if [ -f DESELECT ]; then echo "2 deselected in 0.00s"; echo 0; exit 5; fi
echo "2 failed in 0.03s"; echo 2
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "failing", "name": "failing tests", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
gq add -A; gq commit -qm m0
sl freeze
ck_exit "V1 freeze the honest baseline" 0 "$RC"
gq add -A; gq commit -qm frozen
sl measure --cycle 0
ck_exit "V1 cycle 0 measures the honest red" 0 "$RC"
ck_has "V1 ...recording the selection beside the value" "[2 test(s) selected]"
ck_hasnt "V1 ...with no warning" "DESELECTED"

# Nothing is built. The filter arrives instead.
touch DESELECT
sl measure --cycle 1
ck_exit "V1 measure NEVER exits nonzero over deselection — a run must not wedge" 0 "$RC"
ck_has "V1 ...but the value now carries its selection" "2 DESELECTED"
ck_has "V1 ...and a warning names what moved the number" \
       "this number is partly a statement about a filter"
ck_has "V1 ...the zero is still recorded, because measure records what it measured" "value=0"

# ===========================================================================
# V2 · red-check certified gates that prove nothing, and nothing invoked it
#
# Three shapes were measured being scored "a real gate":
#   DESELECTION  verify = `pytest -q -m 'not slow' tests`; the ticket's own
#                grading test is slow-marked so it NEVER runs. The "1 selected"
#                came from an unrelated broken test — and when a later ticket
#                fixed THAT test the gate went green with the defect fully live.
#   SWALLOWED    `pytest -q tests > /dev/null 2>&1 || echo '1 failed'; exit 1`
#                — the count is read off the wrapper's own echo.
#   FORCED       `pytest tests/test_scaffold.py; exit 1` — 3 passing scaffold
#                tests and a red manufactured by the wrapper.
#
# And red-check was invoked by NOTHING: `grep -c cmd_red_check` returned 2, the
# def and the set_defaults. From the live run's findings.jsonl:120, three tickets
# were reported CLOSED TWICE with their defects live; red-check would have caught
# all three, and nobody ran it until cycle 10.
# ===========================================================================
section "V2 red-check asks whether the TICKET'S OWN tests ran  [repo: rprobe]"
mk_repo rprobe

# A stand-in pytest. `python3 -m pytest` puts the cwd first on sys.path, so this
# package shadows any real pytest for every probe run inside this repo — which
# is what lets the suite keep its "no pytest, no pip, no network" constraint
# while still driving the collect-only probe end to end.
mkdir -p pytest
: > pytest/__init__.py
cat > pytest/__main__.py <<'EOF'
import os
import sys

args = [a for a in sys.argv[1:] if not a.startswith("-")]
mode = open("FAKE_MODE").read().strip() if os.path.exists("FAKE_MODE") else "ok"
if "--collect-only" in sys.argv:
    if mode == "desel":
        print("2 deselected in 0.01s")
        sys.exit(5)
    for a in args:
        print(a)
    print("%d tests collected in 0.01s" % len(args))
    sys.exit(0)
print("1 failed in 0.02s")
sys.exit(1)
EOF
cat > .swarm-loop/acceptance/test_marked.py <<'EOF'
import pytest


@pytest.mark.ticket("T-1")
def test_one():
    assert False


@pytest.mark.ticket("T-2")
def test_two():
    assert False


@pytest.mark.ticket("T-3")
def test_three():
    assert False
EOF
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "red for a reason unrelated to this ticket",
  "verify": "echo '1 failed in 0.02s'; exit 1", "scope": ["a/**"], "depends_on": []},
 {"id": "T-2", "title": "swallows its own output",
  "verify": "python3 -m pytest -q t > /dev/null 2>&1 || echo '1 failed'; exit 1",
  "scope": ["b/**"], "depends_on": []},
 {"id": "T-3", "title": "a non-zero exit manufactured over a green run",
  "verify": "echo '3 passed in 0.01s'; exit 1", "scope": ["c/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm probe

ck "V2 the node-id scanner finds the ticket's own frozen test" \
   ".swarm-loop/acceptance/test_marked.py::test_one" \
   "$(python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
state = json.load(open(".swarm-loop/state.json"))
nodes, unparsed = swarmloop._ticket_test_nodes(os.getcwd(), state, "T-1")
print(",".join(nodes))
PY
)"
ck "V2 ...and reports NO unparsed file when every frozen file parses" "[]" \
   "$(python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
state = json.load(open(".swarm-loop/state.json"))
print(swarmloop._ticket_test_nodes(os.getcwd(), state, "T-1")[1])
PY
)"

# NOTE, because this pair was measured NON-DISCRIMINATING on its own and the
# note is the only thing that stops it being re-read as proof: this fixture's
# stand-in pytest prints the requested node ids back verbatim in "ok" mode,
# which is byte-identical in SHAPE to real pytest's `ERROR: not found: <nodeid>`
# echo. So these two lines passed identically over tests that were collected and
# over tests that could not be collected at all. What discriminates them is
# section V4, whose stand-in emits the real error shapes and whose assertions
# are the ones that go red if the echo is counted again. Keep both.
sl red-check --ticket T-1
ck_exit "V2 CONTROL: a gate whose own tests DO get collected is still a real gate" 0 "$RC"
ck_has "V2 CONTROL: ...and says so" "frozen test(s) survive collection here"
ck_hasnt "V2 CONTROL: ...with no deselection finding" "DESELECTED"

echo desel > FAKE_MODE
gq add -A; gq commit -qm desel
sl red-check --ticket T-1
ck_exit "V2 on the DISPATCH path a deselection finding is EXIT-CODE-NEUTRAL" 0 "$RC"
ck_has "V2 ...it is still said out loud" \
       "Whatever went red above, it was not this ticket's grading tests"
ck_has "V2 ...and the ticket is still counted red there" "1 real"

sl red-check --close --ticket T-1
ck_exit "V2 --close REFUSES the gate whose own tests were deselected" 1 "$RC"
ck_has "V2 ...bucketed as DESELECTED" "1 DESELECTED"
ck_has "V2 ...and the incident is named" "went GREEN with the defect fully live"

echo ok > FAKE_MODE
gq add -A; gq commit -qm ok
sl red-check --ticket T-2
ck_exit "V2 SWALLOWED is advisory on the dispatch path too" 0 "$RC"
ck_has "V2 ...DEVNULL_RE now reads ticket verify strings, not only metric commands" \
       "discards its own output"
ck_has "V2 ...and names the SEGMENT whose output went away, not just the redirect" \
       "SWALLOWED — \`python3 -m pytest -q t > /dev/null 2>&1\`"
sl red-check --close --ticket T-2
ck_exit "V2 --close REFUSES a verify that throws its own output away" 1 "$RC"
ck_has "V2 ...bucketed as SWALLOWED" "1 SWALLOWED"

sl red-check --ticket T-3
ck_exit "V2 FORCED is advisory in BOTH modes (a linter's red looks identical)" 0 "$RC"
ck_has "V2 ...and says the exit code did not come from the test run" \
       "this non-zero exit did not come from the test run"
sl red-check --close --ticket T-3
ck_exit "V2 ...--close does not refuse it either, and the limit is stated" 0 "$RC"
ck_has "V2 ...ADVISORY ONLY, in the output, where it can be read" "ADVISORY ONLY"

# ===========================================================================
# V3 · the stamp, and the close-time gate that reads it
#
# `dispatch --close` is append-only bookkeeping about an AGENT: it never reads
# the ticket's verify, cannot fail, and records a close even for a ref that was
# never open. check-branch took no ticket id, so it could not know which gate it
# was judging. Nothing prevented a ticket being marked done on a gate that
# cannot go red.
# ===========================================================================
section "V3 red-check stamps a durable verdict  [repo: rstamp]"
mk_repo rstamp
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "an honest red gate",
  "verify": "echo 'collected 3 items'; echo '3 failed'; exit 1",
  "scope": ["a/**"], "depends_on": []},
 {"id": "T-2", "title": "a gate that is already green",
  "verify": "true", "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm stamp
ck_nofile "V3 no ledger before the first red-check" ".swarm-loop/redcheck.jsonl"
sl red-check --ticket T-1
ck_exit "V3 a red gate passes" 0 "$RC"
ck_file "V3 ...and the verdict outlives the terminal it was printed in" \
        ".swarm-loop/redcheck.jsonl"
ck "V3 ...stamped as red" "red" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"
ck "V3 ...against the base SHA, not the ref name (main can move)" "40" \
   "$(python3 -c 'import json;print(len(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["base_sha"]))')"
sl red-check --ticket T-2
ck_exit "V3 a VACUOUS gate still fails" 1 "$RC"
ck "V3 ...and a refused run stamps too — the finding must outlive the refusal" "vacuous" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"

section "V3 check-branch --ticket, the D4 shape  [repo: rstamp2]"
mk_repo rstamp2
gq add -A; gq commit -qm state
BASE_SHA=$(git rev-parse HEAD)
gq checkout -b task/T-5
echo work > src/t5.py
gq add -A; gq commit -qm t5
gq checkout main

sl check-branch --branch task/T-5 --ticket T-5
ck_exit "V3 no ledger at all: ADVISORY, never a veto — a run that predates it must not wedge" 0 "$RC"
ck_has "V3 ...and it prints the command that would establish the stamp" "red-check --close --ticket T-5"

python3 - "$BASE_SHA" <<'PY'
import json, sys
rec = {"ticket": "T-5", "verdict": "red", "base": "main", "base_sha": sys.argv[1],
       "cmd": "pytest", "mode": "pre-dispatch", "timestamp": "2026-01-01T00:00:00"}
open(".swarm-loop/redcheck.jsonl", "a").write(json.dumps(rec, sort_keys=True) + "\n")
PY
sl check-branch --branch task/T-5 --ticket T-5
ck_exit "V3 a red stamp at an ancestor of the merge parent clears the branch" 0 "$RC"
ck_has "V3 ...saying which SHA it was red at" "was RED at"

sl check-branch --branch task/T-5 --ticket T-9
ck_exit "V3 an unstamped, UNDATEABLE ticket is advisory — 'unknown' must keep a tree" 0 "$RC"
ck_has "V3 ...and says why it is not refusing" "predates the ledger is not refused"

# Dispatched AFTER the ledger's first stamp: blocking from the first stamp on.
sl dispatch --agent a-t9 --kind ticket --subject "T-9 thing" --branch task/T-9
sl check-branch --branch task/T-5 --ticket T-9
ck_exit "V3 a ticket dispatched AFTER the first stamp and carrying none is REFUSED" 1 "$RC"
ck_has "V3 ...naming the ledger it is missing from" "Nothing is stamped for T-9 at all"

python3 - <<'PY'
import json
p = ".swarm-loop/dispatch.jsonl"
out = []
for line in open(p):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if r.get("dispatched_at"):
        r["dispatched_at"] = "2020-01-01T00:00:00"
    out.append(json.dumps(r, sort_keys=True))
open(p, "w").write("\n".join(out) + "\n")
PY
sl check-branch --branch task/T-5 --ticket T-9
ck_exit "V3 ...but the SAME ticket dispatched before the ledger began is advisory" 0 "$RC"
ck_has "V3 ...saying so with both timestamps" "before this ledger's first stamp"

python3 - "$BASE_SHA" <<'PY'
import json, sys
rec = {"ticket": "T-7", "verdict": "weak", "base": "main", "base_sha": sys.argv[1],
       "cmd": "pytest", "mode": "pre-dispatch", "timestamp": "2026-01-02T00:00:00"}
open(".swarm-loop/redcheck.jsonl", "a").write(json.dumps(rec, sort_keys=True) + "\n")
PY
sl check-branch --branch task/T-5 --ticket T-7
ck_exit "V3 a ticket whose only stamp is WEAK is refused — a stamp is not a pass" 1 "$RC"
ck_has "V3 ...and the refusal shows what IS stamped" "What IS stamped for it"

# A red stamp against a base the branch never forked from is a red about a tree
# this branch has nothing to do with. LATER_SHA is a real commit that main
# acquired AFTER task/T-5 was cut, so it is genuinely not an ancestor of the
# fork — as opposed to a sha git cannot resolve at all, which is the UNKNOWN
# case below and must not veto.
echo later > src/later.py
gq add -A; gq commit -qm later
LATER_SHA=$(git rev-parse HEAD)
python3 - "$LATER_SHA" <<'PY'
import json, sys
rec = {"ticket": "T-6", "verdict": "red", "base": "main",
       "base_sha": sys.argv[1], "cmd": "pytest", "mode": "pre-dispatch",
       "timestamp": "2026-01-03T00:00:00"}
open(".swarm-loop/redcheck.jsonl", "a").write(json.dumps(rec, sort_keys=True) + "\n")
PY
sl check-branch --branch task/T-5 --ticket T-6
ck_exit "V3 a red stamped at a base that is NOT an ancestor does not clear the branch" 1 "$RC"
ck_has "V3 ...and the reason is spelled out" "a tree this branch never forked from"

# ...but a base sha git CANNOT PLACE is not evidence of anything. _is_ancestor's
# own contract is that unknown keeps a tree, and a rewritten or pruned history
# must not be read as "this gate was never red".
python3 - <<'PY'
import json
rec = {"ticket": "T-8", "verdict": "red", "base": "main",
       "base_sha": "0" * 40, "cmd": "pytest", "mode": "pre-dispatch",
       "timestamp": "2026-01-04T00:00:00"}
open(".swarm-loop/redcheck.jsonl", "a").write(json.dumps(rec, sort_keys=True) + "\n")
PY
sl check-branch --branch task/T-5 --ticket T-8
ck_exit "V3 a red stamp whose base sha git cannot resolve is ADVISORY, not a veto" 0 "$RC"
ck_has "V3 ...saying git could not place it" "git cannot place relative to"

# THE BACKWARD-COMPATIBILITY ASSERTION. Every existing check-branch invocation
# in SKILL.md, in merge-protocol.md and in every packet omits --ticket, and must
# keep working verbatim — including on a repo that HAS a populated ledger.
sl check-branch --branch task/T-5
ck_exit "V3 check-branch WITHOUT --ticket is unchanged, ledger or no ledger" 0 "$RC"
ck_hasnt "V3 ...and says nothing about red-check at all" "red-check"
ck_has "V3 ...still ending in its own verdict line" "check-branch: OK"



# ===========================================================================
# V4 · the DESELECTION discriminator counted pytest's ECHO OF THE QUESTION
#
# `seen()` accepted any collect-only line ending in "/<file>::<test>" — and that
# is EXACTLY the shape of pytest's own failure lines. Measured, pytest 6.2.4,
# `--collect-only -q`, exit 4:
#     ERROR: not found: /abs/.../.swarm-loop/acceptance/test_x.py::test_one
#     ERROR: file or directory not found: .swarm-loop/acceptance/test_x.py::test_one
# So a node id that DOES NOT EXIST scored hits == len(nodes), the probe answered
# ('ok', 'all N frozen test(s) survive collection here'), red-check printed
# "a real gate" and stamped `red`, and check-branch --ticket accepted it. The
# whole deselection fix rested on a discriminator that an error message quoting
# the question back could satisfy. The collect-only RETURN CODE was available on
# every one of those runs and was never read.
#
# The V2 fixture's stand-in pytest could not see this: in "ok" mode it prints the
# requested node ids back verbatim, which is byte-identical to the not-found
# echo. So this fixture's stand-in emits the REAL error shapes.
# ===========================================================================
section "V4 the probe read pytest's error echo as evidence  [repo: rprobe2]"
mk_repo rprobe2

# A stand-in pytest that can produce every measured collect-only shape. The
# modes are named for what REAL pytest does, and the exit codes are the ones it
# actually returns — 4 for an unresolvable node id (including the ImportError
# case: naming a node id inside a module that fails to import is a USAGE error,
# not a collection interrupt), 5 for "ran and collected nothing".
mkdir -p pytest
: > pytest/__init__.py
cat > pytest/__main__.py <<'EOF'
import os
import sys

args = [a for a in sys.argv[1:] if not a.startswith("-")]
mode = open("FAKE_MODE").read().strip() if os.path.exists("FAKE_MODE") else "ok"
if "--collect-only" in sys.argv:
    if mode == "notfound":
        for a in args:
            print("ERROR: not found: %s" % os.path.join(os.getcwd(), a))
            print("(no name '%s' in any of [<Module x.py>])" % os.path.join(os.getcwd(), a))
        sys.exit(4)
    if mode == "importerr":
        for a in args:
            print("ERROR: not found: %s" % os.path.join(os.getcwd(), a))
        print("==================================== ERRORS ====================================")
        print("______________ ERROR collecting .swarm-loop/acceptance/test_marked.py __________")
        print("ImportError while importing test module 'test_marked.py'.")
        print("E   ModuleNotFoundError: No module named 'src'")
        print("no tests collected, 1 error in 0.03s")
        sys.exit(4)
    if mode == "param":
        for a in args:
            print("%s[1]" % a)
            print("%s[2]" % a)
        print("%d tests collected in 0.01s" % (2 * len(args)))
        sys.exit(0)
    if mode == "listed-rc4":
        # Every requested node id listed as a real collected item, AND a
        # non-zero exit. Real pytest's own error lines are filtered by
        # _probe_noise, so this shape is what tests the OTHER guard -- the one
        # that reads the collect-only return code -- independently of it.
        for a in args:
            print(a)
        print("%d tests collected in 0.01s" % len(args))
        sys.exit(4)
    if mode == "silent":
        sys.exit(5)
    if mode == "desel":
        print("no tests collected (2 deselected) in 0.00s")
        sys.exit(5)
    for a in args:
        print(a)
    print("%d tests collected in 0.01s" % len(args))
    sys.exit(0)
print("1 failed in 0.02s")
sys.exit(1)
EOF
cat > .swarm-loop/acceptance/test_marked.py <<'EOF'
import pytest


@pytest.mark.ticket("T-1")
def test_one():
    assert False
EOF
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "a red that may or may not be this ticket's",
  "verify": "echo '1 failed in 0.02s'; exit 1", "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm probe2

# THE POSITIVE COUNTERPART FIRST, so the negatives below are not "everything
# refuses". Real collection, real exit 0.
echo ok > FAKE_MODE
gq add -A; gq commit -qm ok
sl red-check --close --ticket T-1
ck_exit "V4 CONTROL: tests that really are collected still close clean" 0 "$RC"
ck_has "V4 CONTROL: ...and the affirmative sentence is earned" \
       "frozen test(s) survive collection here"

# THE DEFECT. Every node id appears in the output — inside an ERROR line.
echo notfound > FAKE_MODE
gq add -A; gq commit -qm notfound
sl red-check --ticket T-1
ck_hasnt "V4 an ERROR line quoting the node id is NOT evidence of collection" \
         "frozen test(s) survive collection here"
ck_has "V4 ...it is reported as unresolvable, with the exit code that said so" \
       "could not resolve ANY of this ticket's"
ck_has "V4 ...and names the consequence for the red" \
       "it was not this ticket's test"
sl red-check --close --ticket T-1
ck_exit "V4 --close REFUSES a red whose grading test is not in the tree" 1 "$RC"
ck "V4 ...and the stamp says so, so check-branch cannot be told otherwise" "deselected" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"

# A COLLECTION ERROR IS A DIFFERENT FACT AND MUST NOT REFUSE. The throwaway
# worktree has no venv, so ImportError on a third-party dependency is the
# DEFAULT for any project that has dependencies; refusing here would refuse
# every ticket in every such project.
echo importerr > FAKE_MODE
gq add -A; gq commit -qm importerr
sl red-check --close --ticket T-1
ck_exit "V4 a COLLECTION ERROR is advisory even under --close (no venv is the default)" 0 "$RC"
ck_has "V4 ...and says the red is unverified rather than disproved" \
       "this red is simply unverified, not disproved"
ck_hasnt "V4 ...and still never claims the tests survived" \
         "frozen test(s) survive collection here"

# PARAMETRIZATION. pytest expands `::test_one` to `::test_one[1]`, which does not
# equal and does not end with "/test_one" — so the discriminator scored hits == 0
# on one of the commonest test shapes and reported "a rootdir/path-shape
# difference, not a finding". Both halves of that sentence were wrong.
echo param > FAKE_MODE
gq add -A; gq commit -qm param
sl red-check --close --ticket T-1
ck_exit "V4 a PARAMETRIZED grading test is collected, not a phantom deselection" 0 "$RC"
ck_has "V4 ...and is counted as surviving collection" \
       "frozen test(s) survive collection here"
ck_hasnt "V4 ...with no rootdir story invented to explain it" \
         "rootdir/path-shape difference"

# TWO INDEPENDENT GUARDS, and this case is what keeps them independent. Filtering
# pytest's error lines and requiring exit 0 both close the echo defect on the
# measured shape, so either one alone makes the other look redundant -- and a
# guard that no case distinguishes is a guard the next edit deletes. Here every
# node id is listed as a genuinely collected item and pytest still exits
# non-zero: the line filter cannot see anything wrong, and only the return code
# can. `ok` is an affirmative claim; a runner that refused the run has not
# earned it.
echo listed-rc4 > FAKE_MODE
gq add -A; gq commit -qm listedrc4
sl red-check --close --ticket T-1
ck_hasnt "V4 every node id LISTED but pytest exited non-zero is not 'ok'" \
         "frozen test(s) survive collection here"
ck_has "V4 ...it says the collect run did not complete cleanly" \
       "did not complete cleanly"
ck_hasnt "V4 ...and it is NOT rendered as the nonsense 'only N of N'" \
         "only 1 of 1"
ck_exit "V4 ...and an unexplained non-zero collect is advisory, not a refusal" 0 "$RC"
ck "V4 ...the stamp records it, so the merge gate can see it" "unconfirmed" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1]).get("selection"))')"

# THE STAMP CARRIES THE PROBE'S ANSWER. Without it, a `red` stamped over a gate
# whose grading tests could not be collected at all is byte-identical IN THE
# LEDGER to a `red` whose grading tests were observed running — and the merge
# gate reads the ledger, not the terminal that printed the difference.
echo importerr > FAKE_MODE
gq add -A; gq commit -qm importerr2
# The branch is built BEFORE red-check runs: red-check leaves an UNCOMMITTED
# .swarm-loop/redcheck.jsonl, and `git add -A` after it carries orchestrator
# state onto the branch, which check-branch vetoes for an entirely different rule.
gq checkout -b task/T-1 -q
echo w > a.txt; gq add -A; gq commit -qm t1
gq checkout main -q
sl red-check --close --ticket T-1
ck "V4 the stamp records what the PROBE said, not only the verdict" "uncollectable" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1]).get("selection"))')"
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V4 ...and an unverified red still CLEARS the branch (never a veto)" 0 "$RC"
ck_has "V4 ...but the merge gate says the red was never confirmed" \
       "the probe did NOT positively observe"
# The CONTROL needs its own lane: the gate reports the last `red` stamp whose
# base is an ancestor of THIS branch's fork point, and task/T-1 forked before
# the ok-mode run, so it would keep reporting the uncollectable stamp above.
echo ok > FAKE_MODE
gq add -A; gq commit -qm ok2
gq checkout -b task/T-1b -q
echo w2 > b.txt; gq add -A; gq commit -qm t1b
gq checkout main -q
sl red-check --close --ticket T-1
ck "V4 CONTROL: a probe that DID observe the tests stamps selection ok" "ok" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1]).get("selection"))')"
sl check-branch --branch task/T-1b --ticket T-1
ck_exit "V4 CONTROL: ...and that branch clears too" 0 "$RC"
ck_hasnt "V4 CONTROL: ...and the merge gate adds no caveat to it" \
         "the probe did NOT positively observe"

# The `empty` verdict used to be unreachable: it was gated on PYTEST_SHAPE_RE,
# which deliberately does not match "no tests collected". Keyed off exit 5 it is
# reachable under real pytest (measured: a conftest `pytest_collection_modifyitems`
# that empties items exits 5 and prints nothing under -q).
echo silent > FAKE_MODE
gq add -A; gq commit -qm silent
sl red-check --close --ticket T-1
ck_exit "V4 pytest exiting 5 with no output at all REFUSES under --close" 1 "$RC"
ck_has "V4 ...and the 'collected NONE' verdict is finally reachable" \
       "the probe collected NONE of this ticket's"

# And the plain deselection path, on this fixture, still lands where it did.
echo desel > FAKE_MODE
gq add -A; gq commit -qm desel2
sl red-check --close --ticket T-1
ck_exit "V4 CONTROL: a genuine deselection still refuses under --close" 1 "$RC"
ck_has "V4 CONTROL: ...named as a DESELECTION, not as a missing test" \
       "were DESELECTED by this project's own pytest configuration"


# ===========================================================================
# V5 · DEVNULL was matched against the WHOLE verify string
#
# `command -v jq >/dev/null 2>&1 || exit 3; pytest -q tests/test_t1.py` scored
# SWALLOWED — a gate whose test output is entirely visible, refused because a
# routine shell idiom appears somewhere else in the command. Under --close that
# is exit 1 AND a `swallowed` stamp, which check-branch --ticket then reads as a
# permanent veto; the remedy check-branch prints (re-run red-check --close)
# re-runs the same string and re-stamps `swallowed`. A false refusal on the
# loop's own merge step that the printed remedy cannot clear.
# ===========================================================================
section "V5 the /dev/null match is anchored to the test run  [repo: rdevnull]"
mk_repo rdevnull

ck "V5 the anchoring table, in one shot (1 = SWALLOWED)" \
   "1 0 0 1 0 1 0 0 1 0 1 0" \
   "$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop as s
cases = [
    # the shape the check exists for: the test runner output itself goes away
    "pytest -q tests > /dev/null 2>&1 || echo '1 failed'; exit 1",
    # the measured FALSE POSITIVE: a precondition probe, tests fully visible
    "command -v jq >/dev/null 2>&1 || exit 3; pytest -q tests/test_t1.py",
    # the other measured false positive: a quiet install, tests fully visible
    "npm ci >/dev/null 2>&1 && npx jest tests/t1.test.ts",
    # a runner behind a name this code cannot know, with nothing else visible
    "./gate.sh >/dev/null 2>&1; echo '1 failed'; exit 1",
    # ...but the same unknown script is NOT flagged when a runner IS visible
    "./setup.sh >/dev/null; pytest -q tests",
    # a wrapper script whose NAME says it runs tests
    "./scripts/verify.sh check > /dev/null 2>&1; echo '1 failed'; exit 1",
    # no redirect at all
    "echo '1 failed in 0.02s'; exit 1",
    # a redirect to a FILE is the remedy the message recommends, never a finding
    "pytest -q tests > .swarm-loop/reports/t1.log 2>&1; exit 1",
    "go test ./... > /dev/null 2>&1; echo '1 failed'; exit 1",
    "mkdir -p out 2>/dev/null; pytest -q tests",
    # QUOTED SEPARATORS, both directions. A regex split on ; && || | cuts inside
    # quotes, and the false NEGATIVE it produces hides the exact shape the check
    # exists for: the redirect lands on a fragment with no runner while the
    # runner lands on a fragment with no redirect.
    'pytest -q -k "a;b" > /dev/null 2>&1; echo "1 failed"; exit 1',
    "echo 'a|b' >/dev/null; pytest -q tests",
]
print(" ".join("1" if s._swallowing_redirect(c) else "0" for c in cases))
PY
)"

# END TO END, because a helper's unit table is not the gate. The verify string
# below is the verifier's measured false positive verbatim.
mkdir -p pytest
: > pytest/__init__.py
cat > pytest/__main__.py <<'EOF'
import sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if "--collect-only" in sys.argv:
    for a in args:
        print(a)
    print("%d tests collected in 0.01s" % len(args))
    sys.exit(0)
print("1 failed in 0.02s")
sys.exit(1)
EOF
cat > .swarm-loop/acceptance/test_marked.py <<'EOF'
import pytest


@pytest.mark.ticket("T-1")
def test_one():
    assert False


@pytest.mark.ticket("T-2")
def test_two():
    assert False
EOF
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "a precondition probe, and fully visible tests",
  "verify": "command -v git >/dev/null 2>&1 || exit 3; echo 'collected 4 items'; echo '4 failed in 0.2s'; exit 1",
  "scope": ["a/**"], "depends_on": []},
 {"id": "T-2", "title": "the runner's own output is thrown away",
  "verify": "python3 -m pytest -q tests > /dev/null 2>&1 || echo '4 failed in 0.2s'; exit 1",
  "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm devnull
BASE_DN=$(git rev-parse HEAD)

# The branch is built BEFORE red-check runs: red-check writes an UNCOMMITTED
# .swarm-loop/redcheck.jsonl, and `git add -A` after it would carry orchestrator
# state onto the branch, which check-branch vetoes for a different rule entirely.
gq checkout -b task/T-1 -q
echo w > a.txt; gq add -A; gq commit -qm t1
gq checkout main -q

sl red-check --close --ticket T-1
ck_exit "V5 a precondition probe redirecting to /dev/null is NOT a swallowed gate" 0 "$RC"
ck_hasnt "V5 ...and nothing is said about swallowing" "discards its own output"
ck "V5 ...so the stamp is a plain red, which the merge gate accepts" "red" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V5 ...and the branch is not vetoed at merge by a shell idiom" 0 "$RC"

# THE PAIRED POSITIVE. Identical fixture, and the redirect moved onto the runner.
sl red-check --close --ticket T-2
ck_exit "V5 CONTROL: the runner's own output going to /dev/null still REFUSES" 1 "$RC"
ck_has "V5 CONTROL: ...and the message names the SEGMENT, not just the redirect" \
       "SWALLOWED — \`python3 -m pytest -q tests > /dev/null 2>&1\`"
ck_has "V5 CONTROL: ...bucketed" "1 SWALLOWED"


# ===========================================================================
# V6 · the pre-dispatch path printed `red` while STAMPING a refusal
#
# red-check with no --close printed `red`, counted the ticket in the `red`
# bucket and exited 0 — while writing verdict `swallowed`/`deselected` into
# redcheck.jsonl. SKILL.md justified --close by saying the false positive can
# only fire at merge; it fired at merge from a stamp written at DISPATCH, with
# no exit-code signal and a summary line ("1 real") that contradicted the file
# the merge gate reads.
# ===========================================================================
section "V6 the dispatch path says what it stamped  [repo: rpredisp]"
mk_repo rpredisp
mkdir -p pytest
: > pytest/__init__.py
cat > pytest/__main__.py <<'EOF'
import sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if "--collect-only" in sys.argv:
    for a in args:
        print(a)
    print("%d tests collected in 0.01s" % len(args))
    sys.exit(0)
print("1 failed in 0.02s")
sys.exit(1)
EOF
cat > .swarm-loop/acceptance/test_marked.py <<'EOF'
import pytest


@pytest.mark.ticket("T-1")
def test_one():
    assert False


@pytest.mark.ticket("T-2")
def test_two():
    assert False
EOF
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "the runner's output is thrown away",
  "verify": "python3 -m pytest -q tests > /dev/null 2>&1 || echo '4 failed in 0.2s'; exit 1",
  "scope": ["a/**"], "depends_on": []},
 {"id": "T-2", "title": "an honest red gate",
  "verify": "echo 'collected 3 items'; echo '3 failed'; exit 1",
  "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm predisp

sl red-check --ticket T-1
ck_exit "V6 the dispatch path still never refuses (a false positive must not stop a build)" 0 "$RC"
ck_has "V6 ...but it no longer prints \`red\` and stamps something else silently" \
       "but STAMPED \`swallowed\`"
ck_has "V6 ...naming the command that will refuse the branch later" \
       "will REFUSE at merge"
ck_has "V6 ...and the summary reconciles the two" \
       "of those verdict(s) are NOT \`red\`"
ck_has "V6 ...and says re-running alone cannot clear it" \
       "re-stamps the same verdict"
ck "V6 ...the stamp itself stays honest" "swallowed" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"

sl red-check --ticket T-2
ck_exit "V6 CONTROL: an honest red gate passes the dispatch path" 0 "$RC"
ck_hasnt "V6 CONTROL: ...with no merge-time warning attached to it" "will REFUSE at merge"
ck_hasnt "V6 CONTROL: ...and no reconciliation line" "of those verdict(s) are NOT"


# ===========================================================================
# V7 · the merge gate's evidence store was FAIL-OPEN on truncation
#
# `: > .swarm-loop/redcheck.jsonl` turned a live VETO into exit 0 for the whole
# run: an empty file parsed to {}, _redcheck_gate took its "no ledger yet"
# branch, and printed `no .swarm-loop/redcheck.jsonl exists` — about a file it
# had just opened. redcheck.jsonl is ALSO not in git-guard's LEDGER_NAMES, so
# the redirect that does this is not blocked the way it is for state.json /
# goals.json / manifest.json (that half is a NEEDS, recorded in SKILL.md).
# ===========================================================================
section "V7 an emptied ledger is a finding, not a clean slate  [repo: rledger]"
mk_repo rledger
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "a gate that is already green", "verify": "true",
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm ledger
gq checkout -b task/T-1 -q
echo w > a.txt; gq add -A; gq commit -qm t1
gq checkout main -q

sl red-check --ticket T-1
ck_exit "V7 a vacuous gate stamps and refuses" 1 "$RC"
sl dispatch --agent a-t1 --kind ticket --subject "T-1 thing" --branch task/T-1
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V7 ...so the branch is vetoed at merge" 1 "$RC"

: > .swarm-loop/redcheck.jsonl
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V7 TRUNCATING the ledger does NOT clear the veto" 1 "$RC"
ck_has "V7 ...and the advisory no longer claims the file is absent" \
       "EXISTS but holds no usable verdict"
ck_hasnt "V7 ...specifically, it does not say 'no redcheck.jsonl exists'" \
         "so nothing here can say whether"

echo 'not json at all' > .swarm-loop/redcheck.jsonl
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V7 a ledger of unparseable lines is refused the same way" 1 "$RC"

# THE POSITIVE COUNTERPART: absent is still absent, and still advisory. The D4
# rule — a run that has never stamped must not be wedged by a gate it predates —
# is what this whole change is not allowed to break.
rm -f .swarm-loop/redcheck.jsonl
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V7 CONTROL: an ABSENT ledger is still an advisory, never a veto" 0 "$RC"
ck_has "V7 CONTROL: ...and says the file does not exist" "exists, so nothing"


# ===========================================================================
# V8 · check-branch --ticket never validated the id against the graph
#
# cmd_check_branch did not load the ticket graph at all, so an unknown id had no
# stamps and no dispatch record, `when` came back None, and the gate took its
# "predates the ledger" advisory branch and exited 0. A one-character typo
# silently disarmed the merge gate while printing a reassuring line, and nothing
# distinguished it from a genuine resumed-run advisory.
# ===========================================================================
section "V8 a mistyped --ticket cannot disarm the merge gate  [repo: rtypo]"
mk_repo rtypo
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "a gate that is already green", "verify": "true",
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm typo
gq checkout -b task/T-1 -q
echo w > a.txt; gq add -A; gq commit -qm t1
gq checkout main -q
sl red-check --ticket T-1
ck_exit "V8 the vacuous gate stamps" 1 "$RC"
sl dispatch --agent a-t1 --kind ticket --subject "T-1 thing" --branch task/T-1
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V8 CONTROL: the REAL id is judged, and vetoed" 1 "$RC"

sl check-branch --branch task/T-1 --ticket T-l
ck_exit "V8 a one-character typo is REFUSED, not waved through as 'predates the ledger'" 1 "$RC"
ck_has "V8 ...naming the graph it is not in" "is not a ticket in"
ck_has "V8 ...and saying what the typo would otherwise have done" "DISARMING the merge gate"
ck_hasnt "V8 ...and it is NOT reported as a ticket the ledger predates" \
         "predates the ledger is not refused"

sl check-branch --branch task/T-1 --ticket t-1
ck_has "V8 ...a case-only typo gets the near-miss suggestion" "Did you mean: T-1"

# Where there is no graph the id CANNOT be validated, and saying so out loud is
# the difference between "checked" and "not checkable". Never a veto: refusing
# every --ticket on a repo with no graph would wedge exactly the resumed runs the
# D4 shape exists to protect.
# A graph that is not readable JSON must not take the merge gate down with it:
# check-branch's three real vetoes are statements about the DIFF and hold
# whatever the graph says, so this validation is not allowed to turn a working
# gate into a hard error on a repo with a broken tickets.json.
echo 'this is not json' > tickets.json
gq add -A; gq commit -qm brokengraph
sl check-branch --branch task/T-1 --ticket T-1
ck_exit "V8 an UNREADABLE graph degrades to an advisory, it does not wedge the gate" 1 "$RC"
ck_has "V8 ...saying the id could not be validated against it" "could not be validated"
ck_has "V8 ...and the red-check gate still ran, and still vetoed on its own evidence" \
       "has no red-check verdict of \`red\`"

rm -f tickets.json
gq add -A; gq commit -qm nograph
sl check-branch --branch task/T-1 --ticket T-zzz
ck_has "V8 with NO graph the id is unvalidatable, and the output says so" \
       "could NOT be validated"
ck_exit "V8 ...but that is an advisory, not a veto" 0 "$RC"


# ===========================================================================
# V9 · a frozen acceptance file that would not PARSE was reported as absent
#
# _ticket_test_nodes swallowed OSError/SyntaxError/ValueError per file and
# continued, so a ticket whose only grading test lived in the unparseable file
# was reported as "this ticket carries no frozen test at all — freeze's
# acceptance-coverage gate owns that finding, not this one". Neither half is
# supportable: the test may be right there, and the coverage gate counts marks
# with the SAME scanner, so it will not own the finding either.
# ===========================================================================
section "V9 an unparseable frozen test is a stated unknown  [repo: rparse]"
mk_repo rparse
cat > .swarm-loop/acceptance/test_marked.py <<'EOF'
import pytest


@pytest.mark.ticket("T-1")
def test_one(:
    assert False
EOF
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-1", "title": "an honest red gate",
  "verify": "echo 'collected 3 items'; echo '3 failed'; exit 1",
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm parse
ck "V9 the scanner RETURNS the file it could not parse instead of dropping it" "1" \
   "$(python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SL_DIR"])
import swarmloop
state = json.load(open(".swarm-loop/state.json"))
print(len(swarmloop._ticket_test_nodes(os.getcwd(), state, "T-1")[1]))
PY
)"
sl red-check --close --ticket T-1
ck_has "V9 ...and red-check says the question is unanswered, not answered no" \
       "could not be PARSED"
ck_hasnt "V9 ...it no longer asserts the ticket has no frozen test" \
         "carries no frozen test at all"
ck_exit "V9 ...and an unknown still never refuses" 0 "$RC"


# ===========================================================================
# V10 · the DESELECTION signal reached no durable artifact
#
# measure's new warning went to stdout and nowhere else: history.csv gained no
# column, analysis/cycle-N.json gained no key, and a later reader could not tell
# a deselected green from a real one. Replayed on this branch before these
# columns existed — honest freeze, then a marker filter added to pytest.ini with
# src/ UNTOUCHED — `grep -ic desel` over history.csv, analysis/cycle-1.json and
# analysis/cycle-1.md returned 0 / 0 / 0 while analyze printed ALL TARGETS MET.
#
# freeze's teeth are INTAKE-ONLY and always will be: the attack adds the filter
# AFTER the freeze, which the intake gate by construction never sees. What CAN
# see it is the comparison across cycles, which is what these columns are for.
# ===========================================================================
section "V10 selection facts reach disk, and analyze reads them  [repo: rdesel]"
mk_repo rdesel
cat > run_tests.sh <<'EOF'
#!/bin/sh
if [ -f FILTER ]; then
  echo "2 deselected in 0.00s"
  echo 0
elif [ -f DONE ]; then
  echo "2 passed in 0.03s"
  echo 0
else
  echo "2 failed in 0.03s"
  echo 2
fi
EOF
chmod +x run_tests.sh
python3 - <<'PY'
import json
p = ".swarm-loop/goals.json"
g = json.load(open(p))
g["metrics"] = [{"id": "accept", "name": "acceptance", "command": "./run_tests.sh",
                 "target": 0, "direction": "minimize"}]
json.dump(g, open(p, "w"), indent=2)
PY
sl freeze
ck_exit "V10 the honest red baseline freezes" 0 "$RC"
gq add -A; gq commit -qm harness

sl measure --cycle 0
ck_exit "V10 cycle 0 measures the honest baseline" 0 "$RC"
ck "V10 ...and history.csv records what the run SELECTED" "2" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["selected"])')"
ck "V10 ...and that it deselected NOTHING — 0, not blank, so a later cycle has a baseline" "0" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["deselected"])')"

touch FILTER
sl verify
ck_exit "V10 the frozen harness is INTACT — the filter is not a frozen byte" 0 "$RC"
sl measure --cycle 1
ck_exit "V10 cycle 1 measures 0 errors, exactly as the incident did" 0 "$RC"
ck "V10 ...and the DESELECTION is now on disk, not only on stdout" "2" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["deselected"])')"

sl analyze --cycle 0
ck "V10 CONTROL: a PYTEST metric that deselected nothing reports 0, not unknown" "0" \
   "$(jget .swarm-loop/analysis/cycle-0.json 'd["metrics"][0]["latest_deselected"]')"
sl analyze --cycle 1
ck_exit "V10 analyze does NOT signal all-targets-met over a post-freeze filter (exit 2 withheld)" 0 "$RC"
# Asserted against the TERMINAL banner's own wording. The withholding notice
# deliberately quotes the phrase ("ALL TARGETS MET is withheld while this
# stands"), so a bare grep for it cannot tell the two apart -- which is the
# "two code paths print the same shape" trap this whole battery is about.
ck_hasnt "V10 ...the terminal banner does not fire" "**ALL TARGETS MET — act-dependent"
ck_has "V10 ...and the reason is stated in the report, next to the integrity line" \
       "TEST SELECTION CHANGED AFTER THE FREEZE"
ck "V10 ...and analysis/cycle-1.json carries it for a reader who was not there" "False" \
   "$(jget .swarm-loop/analysis/cycle-1.json 'd["all_targets_met"]')"
ck "V10 ...naming the metric" "accept" \
   "$(jget .swarm-loop/analysis/cycle-1.json 'd["selection_regressions"][0]["metric"]')"
ck_has "V10 ...and the markdown says where the filter can hide" "conftest.py"

# THE POSITIVE COUNTERPART. Same harness, no filter: all-targets-met still fires,
# so the new condition is not a blanket refusal.
rm -f FILTER
touch DONE
sl measure --cycle 2
ck_exit "V10 CONTROL: the work lands and the suite goes green, with no filter" 0 "$RC"
ck "V10 CONTROL: ...selecting the same 2 tests it always did" "2" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["selected"])')"
sl analyze --cycle 2
ck_exit "V10 CONTROL: with the target met and no selection change, ALL TARGETS MET still fires" 2 "$RC"
ck_has "V10 CONTROL: ...and says so" "**ALL TARGETS MET — act-dependent"
ck_hasnt "V10 CONTROL: ...with no selection complaint" "TEST SELECTION CHANGED AFTER THE FREEZE"

# A NON-PYTEST metric must never be dragged into this: its output has no test
# count at all, so both columns stay BLANK and there is nothing to regress from.
section "V10 a non-pytest metric records no selection facts  [repo: rdesel2]"
mk_repo rdesel2
sl freeze
gq add -A; gq commit -qm harness
sl measure --cycle 0
ck "V10 a cat-style non-pytest metric writes an EMPTY selected column, never 0" "" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["selected"])')"
ck "V10 ...and an EMPTY deselected column" "" \
   "$(python3 -c '
import csv
r = list(csv.DictReader(open(".swarm-loop/history.csv")))
print(r[-1]["deselected"])')"
sl analyze --cycle 0
ck "V10 ...so it can never be reported as a selection regression" "0" \
   "$(jget .swarm-loop/analysis/cycle-0.json 'len(d["selection_regressions"])')"
# THE DISTINCTION ITSELF, asserted rather than inferred. `len(regressions) == 0`
# is true whether a blank column reads as UNKNOWN or as zero, so it cannot tell
# them apart -- and reading blank as zero is what would give every non-pytest
# metric a fabricated deselect baseline of 0 for the first pytest-shaped row to
# regress from. analyze must SAY unknown.
ck "V10 ...and analyze reports its selection as UNKNOWN, never as zero" "None" \
   "$(jget .swarm-loop/analysis/cycle-0.json 'd["metrics"][0]["latest_deselected"]')"
ck "V10 ...for both columns" "None" \
   "$(jget .swarm-loop/analysis/cycle-0.json 'd["metrics"][0]["latest_selected"]')"

# BACKWARD COMPATIBILITY: a history.csv written by a version with no selection
# columns must still analyze, and must read as "unknown", not as zero.
python3 - <<'PY'
import csv
rows = list(csv.reader(open(".swarm-loop/history.csv")))
rows = [r[:6] for r in rows]
with open(".swarm-loop/history.csv", "w", newline="") as f:
    csv.writer(f).writerows(rows)
PY
sl analyze --cycle 0
ck_exit "V10 a 6-column history.csv from an older version still analyzes" 0 "$RC"
ck "V10 ...and its rows carry no selection claim either way" "None" \
   "$(jget .swarm-loop/analysis/cycle-0.json 'd["metrics"][0]["latest_deselected"]')"


# ===========================================================================
# V11 · `bootstrap: true` — the graph's root ticket cannot go red, by design
#
# build-docs emits `"bootstrap": false` on every ticket and true on T-0 only.
# T-0 is the sole root — repo skeleton, test harness, lint/CI config, marker
# registration — and its declared outcome IS "pipeline green on an empty suite".
# Run red-check against it in a clean worktree with nothing implemented and it
# exits 0 (VACUOUS) or non-zero with nothing selected (WEAK): either way
# red-check exits 1 on the ONE TICKET THAT MUST DISPATCH FIRST. A false refusal
# on the loop's own dispatch step is strictly worse than the gap it closes.
#
# The exemption is exactly one thing wide — exempt from MUST-GO-RED, from
# nothing else — and it is read from the DATA only. There is deliberately no
# title heuristic: the live run's root is titled "Monorepo skeleton with green
# empty verify pipeline", and keying on that string is how a check ends up
# answering a question nobody asked.
# ===========================================================================
section "V11 the bootstrap ticket is exempt from must-go-red  [repo: rboot]"
mk_repo rboot
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton with a green empty pipeline", "bootstrap": true,
  "verify": "true", "scope": ["a/**"], "depends_on": []},
 {"id": "T-9", "title": "an ordinary ticket with the IDENTICAL gate", "bootstrap": false,
  "verify": "true", "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm boot

sl red-check --ticket T-0
ck_exit "V11 a bootstrap ticket whose gate is green on an empty tree PASSES" 0 "$RC"
ck_has "V11 ...and says why, in its own words" "that IS the outcome"
ck_has "V11 ...bucketed as BOOTSTRAP, not counted as a real red" "1 BOOTSTRAP"
ck_has "V11 ...and states how narrow the exemption is" "no gate at all is still NO GATE"
ck "V11 ...stamped as bootstrap, so the ledger says what happened" "bootstrap" \
   "$(python3 -c 'import json;print(json.loads(open(".swarm-loop/redcheck.jsonl").read().splitlines()[-1])["verdict"])')"

# THE PAIR THAT PROVES THE EXEMPTION IS DOING THE WORK. Identical verify string,
# identical output, identical exit code — and no `bootstrap: true`.
sl red-check --ticket T-9
ck_exit "V11 CONTROL: the IDENTICAL gate on a non-bootstrap ticket still FAILS" 1 "$RC"
ck_has "V11 CONTROL: ...as VACUOUS" "VACUOUS"
ck_hasnt "V11 CONTROL: ...with no bootstrap bucket" "BOOTSTRAP"

# ...and it is not inferable from the title, which is where a heuristic would
# have keyed. T-9's title says nothing about skeletons; T-0's says everything.
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "Monorepo skeleton with green empty verify pipeline",
  "verify": "true", "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm titleonly
sl red-check --ticket T-0
ck_exit "V11 a bootstrap-SOUNDING title with no bootstrap field is NOT exempt" 1 "$RC"
ck_hasnt "V11 ...the title is never read for this" "BOOTSTRAP"

# NO GATE is still NO GATE. The exemption removes a ticket from vacuous/weak at
# the point of classification, so a bootstrap ticket with no verify never
# reaches it.
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton", "bootstrap": true,
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm nogate
sl red-check --ticket T-0
ck_exit "V11 a bootstrap ticket with NO verify command still FAILS" 1 "$RC"
ck_has "V11 ...as NO GATE" "NO GATE"

# The contract is "true on T-0 only". A second one is a mistake or someone
# widening the exemption to get a wave moving.
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton", "bootstrap": true, "verify": "true",
  "scope": ["a/**"], "depends_on": []},
 {"id": "T-1", "title": "also claiming to be the root", "bootstrap": true, "verify": "true",
  "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm twoboot
sl red-check --ticket T-0
ck_exit "V11 TWO bootstrap tickets is a refused graph, not a wider exemption" 3 "$RC"
ck_has "V11 ...and the refusal names both" "T-0, T-1"
ck_has "V11 ...and says what widening it silently would mean" \
       "ships on a gate that cannot fail"

# The graph is JSON written by another tool. `"bootstrap": "false"` is a NON-EMPTY
# STRING, so plain truthiness would read the graph's own word for "no" as yes.
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton", "bootstrap": "false", "verify": "true",
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm strfalse
sl red-check --ticket T-0
ck_exit "V11 the STRING \"false\" is not truthy here — the ticket is not exempt" 1 "$RC"
ck_has "V11 ...it is VACUOUS like any other green-on-empty gate" "VACUOUS"

cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton", "bootstrap": "maybe", "verify": "true",
  "scope": ["a/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm strmaybe
sl red-check --ticket T-0
ck_exit "V11 an unrecognised bootstrap value is REFUSED, never defaulted" 3 "$RC"
ck_has "V11 ...saying a default either way is a silent exemption or a silent refusal" \
       "silent exemption or a silent refusal"

# The narrow exemption covers the WEAK shape too — a pipeline that exits non-zero
# because it has nothing to run yet is the same pre-build fact as one that exits 0.
cat > tickets.json <<'EOF'
{"version": 1, "project": "e2e", "tickets": [
 {"id": "T-0", "title": "skeleton", "bootstrap": true,
  "verify": "echo 'no tests ran'; exit 5", "scope": ["a/**"], "depends_on": []},
 {"id": "T-9", "title": "ordinary", "bootstrap": false,
  "verify": "echo 'no tests ran'; exit 5", "scope": ["b/**"], "depends_on": []}
]}
EOF
gq add -A; gq commit -qm weakboot
sl red-check --ticket T-0
ck_exit "V11 a bootstrap pipeline that selects nothing is exempt too" 0 "$RC"
ck_has "V11 ...named as the pre-build shape it is" "nothing to run yet"
sl red-check --ticket T-9
ck_exit "V11 CONTROL: the IDENTICAL no-tests-ran gate without the field is still WEAK" 1 "$RC"
ck_has "V11 CONTROL: ...as WEAK" "NO tests were"


# ===========================================================================
cd / 2>/dev/null || true
ELAPSED=$(( $(date +%s) - START_TS ))
echo
echo "=========================================================="
echo "runtime: ${ELAPSED}s"
if [ "${SWARMLOOP_TESTS_KEEP:-}" = "1" ]; then
  echo "SWARMLOOP_TESTS_KEEP=1 — temp workspace kept at $TMP"
fi
echo "TEST_HARNESS: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && exit 0
exit 1
