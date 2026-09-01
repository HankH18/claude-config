#!/usr/bin/env bash
# run.sh — the one-command regression gate for the swarm-loop skill.
#
#   ./tests/run.sh              run everything
#   ./tests/run.sh --guard      only the git-guard battery
#   ./tests/run.sh --harness    only the swarmloop.py end-to-end suite
#   ./tests/run.sh -v           verbose (list every passing guard case too)
#
# Exit code IS the contract:  0 = every case passed, nonzero = something
# regressed. Run this before any edit to SKILL.md, scripts/ or hooks/.
#
# Everything is located RELATIVE to this file, so the suite still works after
# the skill is moved, cloned, or installed under a different home directory.
# Nothing outside a temp dir the suites create for themselves is ever touched.

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SUITE_DIR/.." && pwd)"

RUN_GUARD=1
RUN_HARNESS=1
VERBOSE=""
for arg in "$@"; do
  case "$arg" in
    --guard)    RUN_HARNESS=0 ;;
    --harness)  RUN_GUARD=0 ;;
    -v|--verbose) VERBOSE="-v" ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "run.sh: unknown argument '$arg' (try --help)" >&2
      exit 3 ;;
  esac
done

# --------------------------------------------------------------- preflight
missing=0
for f in "$SKILL_DIR/hooks/git-guard.py" "$SKILL_DIR/scripts/swarmloop.py"; do
  if [ ! -f "$f" ]; then
    echo "run.sh: MISSING thing-under-test: $f" >&2
    missing=1
  fi
done
[ "$RUN_GUARD" -eq 1 ] && [ ! -f "$SUITE_DIR/test_git_guard.py" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_git_guard.py" >&2; missing=1; }
[ "$RUN_HARNESS" -eq 1 ] && [ ! -f "$SUITE_DIR/test_harness.sh" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_harness.sh" >&2; missing=1; }
for c in python3 git; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "run.sh: required command not found: $c" >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || exit 3

TMP_OUT="$(mktemp -t swarmloop-run-sh)" || exit 3
trap 'rm -f "$TMP_OUT"' EXIT

started=$(date +%s)
echo "swarm-loop regression suite"
echo "  skill under test: $SKILL_DIR"
echo "  python:           $(python3 --version 2>&1)   git: $(git --version 2>&1)"
echo ""

GUARD_RC=0;   GUARD_LINE="(skipped)"
HARNESS_RC=0; HARNESS_LINE="(skipped)"

# ------------------------------------------------------------ guard battery
if [ "$RUN_GUARD" -eq 1 ]; then
  echo "=============================================================="
  echo "  git-guard battery   (hooks/git-guard.py)"
  echo "=============================================================="
  python3 "$SUITE_DIR/test_git_guard.py" $VERBOSE 2>&1 | tee "$TMP_OUT"
  GUARD_RC=${PIPESTATUS[0]}
  GUARD_LINE=$(grep -m1 '^TEST_GIT_GUARD:' "$TMP_OUT")
  [ -n "$GUARD_LINE" ] || GUARD_LINE="TEST_GIT_GUARD: NO SUMMARY LINE (suite died early)"
  echo ""
fi

# ---------------------------------------------------------- harness battery
if [ "$RUN_HARNESS" -eq 1 ]; then
  echo "=============================================================="
  echo "  harness end-to-end suite   (scripts/swarmloop.py)"
  echo "=============================================================="
  bash "$SUITE_DIR/test_harness.sh" 2>&1 | tee "$TMP_OUT"
  HARNESS_RC=${PIPESTATUS[0]}
  HARNESS_LINE=$(grep -m1 '^TEST_HARNESS:' "$TMP_OUT")
  [ -n "$HARNESS_LINE" ] || HARNESS_LINE="TEST_HARNESS: NO SUMMARY LINE (suite died early)"
  echo ""
fi

# ------------------------------------------------------------------ summary
elapsed=$(( $(date +%s) - started ))
echo "=============================================================="
echo "  SUMMARY   (${elapsed}s)"
echo "=============================================================="
printf '  %-14s %s\n' "git-guard"  "$GUARD_LINE"
printf '  %-14s %s\n' "harness"    "$HARNESS_LINE"
echo ""

if [ "$GUARD_RC" -eq 0 ] && [ "$HARNESS_RC" -eq 0 ]; then
  echo "  ALL GREEN — the skill is safe to edit against."
  exit 0
fi

echo "  REGRESSION — read the FAIL blocks above."
echo "  A failing case is a finding about the SKILL, not a licence to edit the test."
echo "  (guard exit=$GUARD_RC  harness exit=$HARNESS_RC)"
exit 1
