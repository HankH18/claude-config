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
