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
ck "the self-healed file has the canonical header" "cycle,metric_id,value,error,timestamp,source" \
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
cp "$TMP/compat-all.txt" "$OUT"
ck_hasnt "(q) not one traceback across every command" "Traceback"

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
