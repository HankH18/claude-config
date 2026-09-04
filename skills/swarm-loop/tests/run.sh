#!/usr/bin/env bash
# run.sh — the one-command regression gate for the swarm-loop skill.
#
#   ./tests/run.sh              run everything
#   ./tests/run.sh --guard      only the git-guard battery
#   ./tests/run.sh --harness    only the swarmloop.py end-to-end suite
#   ./tests/run.sh --contract   only the build-docs <-> swarm-loop contract test
#   ./tests/run.sh --delegation only the delegation-guard battery
#   ./tests/run.sh -v           verbose (list every passing case too)
#
# Selectors combine: `--guard --contract` runs those two. Naming none runs all
# four. (Before the contract battery existed, naming BOTH selectors ran
# NEITHER — each one switched the other off. That is fixed here.)
#
# Exit code IS the contract:  0 = every case passed, nonzero = something
# regressed. Run this before any edit to SKILL.md, scripts/ or hooks/.
#
# THE CONTRACT BATTERY IS CURRENTLY RED, ON PURPOSE — one open defect,
# `bootstrap`, routed as a separate fix to `red-check`. It is reported on its
# own SUMMARY line so it can never be mistaken for a regression in the other
# three, and it does not stop them running. Read the guard, harness and
# delegation lines to judge your own change; see the FAIL block for what
# `bootstrap` is waiting on.
#
# Everything is located RELATIVE to this file, so the suite still works after
# the skill is moved, cloned, or installed under a different home directory.
# Nothing outside a temp dir the suites create for themselves is ever touched.

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SUITE_DIR/.." && pwd)"

RUN_GUARD=0
RUN_HARNESS=0
RUN_CONTRACT=0
RUN_DELEGATION=0
SELECTED=0
VERBOSE=""
for arg in "$@"; do
  case "$arg" in
    --guard)    RUN_GUARD=1;    SELECTED=1 ;;
    --harness)  RUN_HARNESS=1;  SELECTED=1 ;;
    --contract) RUN_CONTRACT=1; SELECTED=1 ;;
    --delegation) RUN_DELEGATION=1; SELECTED=1 ;;
    -v|--verbose) VERBOSE="-v" ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "run.sh: unknown argument '$arg' (try --help)" >&2
      exit 3 ;;
  esac
done
if [ "$SELECTED" -eq 0 ]; then
  RUN_GUARD=1; RUN_HARNESS=1; RUN_CONTRACT=1; RUN_DELEGATION=1
fi

# --------------------------------------------------------------- preflight
missing=0
for f in "$SKILL_DIR/hooks/git-guard.py" "$SKILL_DIR/scripts/swarmloop.py" \
         "$SKILL_DIR/hooks/delegation-guard.py"; do
  if [ ! -f "$f" ]; then
    echo "run.sh: MISSING thing-under-test: $f" >&2
    missing=1
  fi
done
[ "$RUN_GUARD" -eq 1 ] && [ ! -f "$SUITE_DIR/test_git_guard.py" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_git_guard.py" >&2; missing=1; }
[ "$RUN_HARNESS" -eq 1 ] && [ ! -f "$SUITE_DIR/test_harness.sh" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_harness.sh" >&2; missing=1; }
[ "$RUN_CONTRACT" -eq 1 ] && [ ! -f "$SUITE_DIR/test_contract.py" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_contract.py" >&2; missing=1; }
[ "$RUN_DELEGATION" -eq 1 ] && [ ! -f "$SUITE_DIR/test_delegation_guard.py" ] && {
  echo "run.sh: MISSING suite: $SUITE_DIR/test_delegation_guard.py" >&2; missing=1; }
# The contract battery has THREE things under test, and two of them are docs —
# one of which lives in the sibling skill. A missing leg is a missing gate, so
# it is preflighted like any other thing-under-test rather than discovered as a
# confusing in-suite failure.
if [ "$RUN_CONTRACT" -eq 1 ]; then
  for f in "$SKILL_DIR/../build-docs/references/templates.md" \
           "$SKILL_DIR/references/build-docs-intake.md"; do
    if [ ! -f "$f" ]; then
      echo "run.sh: MISSING contract leg: $f" >&2
      missing=1
    fi
  done
fi
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

GUARD_RC=0;      GUARD_LINE="(skipped)"
HARNESS_RC=0;    HARNESS_LINE="(skipped)"
CONTRACT_RC=0;   CONTRACT_LINE="(skipped)"
DELEGATION_RC=0; DELEGATION_LINE="(skipped)"

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

# --------------------------------------------------------- contract battery
if [ "$RUN_CONTRACT" -eq 1 ]; then
  echo "=============================================================="
  echo "  build-docs <-> swarm-loop ticket contract   (3-way)"
  echo "=============================================================="
  python3 "$SUITE_DIR/test_contract.py" $VERBOSE 2>&1 | tee "$TMP_OUT"
  CONTRACT_RC=${PIPESTATUS[0]}
  CONTRACT_LINE=$(grep -m1 '^TEST_CONTRACT:' "$TMP_OUT")
  [ -n "$CONTRACT_LINE" ] || CONTRACT_LINE="TEST_CONTRACT: NO SUMMARY LINE (suite died early)"
  echo ""
fi

# ------------------------------------------------------ delegation battery
if [ "$RUN_DELEGATION" -eq 1 ]; then
  echo "=============================================================="
  echo "  delegation-guard battery   (hooks/delegation-guard.py)"
  echo "=============================================================="
  python3 "$SUITE_DIR/test_delegation_guard.py" $VERBOSE 2>&1 | tee "$TMP_OUT"
  DELEGATION_RC=${PIPESTATUS[0]}
  DELEGATION_LINE=$(grep -m1 '^TEST_DELEGATION_GUARD:' "$TMP_OUT")
  [ -n "$DELEGATION_LINE" ] || DELEGATION_LINE="TEST_DELEGATION_GUARD: NO SUMMARY LINE (suite died early)"
  echo ""
fi

# ------------------------------------------------------------------ summary
elapsed=$(( $(date +%s) - started ))
echo "=============================================================="
echo "  SUMMARY   (${elapsed}s)"
echo "=============================================================="
printf '  %-14s %s\n' "git-guard"  "$GUARD_LINE"
printf '  %-14s %s\n' "harness"    "$HARNESS_LINE"
printf '  %-14s %s\n' "contract"   "$CONTRACT_LINE"
printf '  %-14s %s\n' "delegation" "$DELEGATION_LINE"
echo ""

if [ "$GUARD_RC" -eq 0 ] && [ "$HARNESS_RC" -eq 0 ] && [ "$CONTRACT_RC" -eq 0 ] \
   && [ "$DELEGATION_RC" -eq 0 ]; then
  # A skipped battery is RC=0 too — the initialisers above see to that — so the
  # RCs alone cannot separate "all three passed" from "one passed and two never
  # ran". Measured: `run.sh --guard --harness` printed ALL GREEN with `contract
  # (skipped)` on the SUMMARY line above it, which is the false all-clear this
  # suite exists to prevent. Exit stays 0 — a battery you did not select did not
  # regress — but only a full run may say the skill is safe to edit against.
  if [ "$RUN_GUARD" -eq 1 ] && [ "$RUN_HARNESS" -eq 1 ] && [ "$RUN_CONTRACT" -eq 1 ] \
     && [ "$RUN_DELEGATION" -eq 1 ]; then
    echo "  ALL GREEN — the skill is safe to edit against."
  else
    ran=""
    [ "$RUN_GUARD" -eq 1 ]      && ran="$ran guard"
    [ "$RUN_HARNESS" -eq 1 ]    && ran="$ran harness"
    [ "$RUN_CONTRACT" -eq 1 ]   && ran="$ran contract"
    [ "$RUN_DELEGATION" -eq 1 ] && ran="$ran delegation"
    echo "  PARTIAL GREEN — only$ran ran, and only$ran passed."
    echo "  A skipped battery is not a green one: this says NOTHING about the"
    echo "  ones you did not select, and it is not the gate. Run tests/run.sh"
    echo "  with no selector before declaring the skill safe to edit against."
  fi
  exit 0
fi

if [ "$GUARD_RC" -eq 0 ] && [ "$HARNESS_RC" -eq 0 ] && [ "$DELEGATION_RC" -eq 0 ]; then
  # Only the contract battery is red. It is red over a KNOWN, ROUTED defect in
  # the skill — not over your change — and saying so is the difference between
  # a gate someone acts on and a gate someone learns to ignore.
  #
  # A SKIPPED battery is not a green one. Claiming "guard and harness GREEN"
  # off RC=0 when neither ran is exactly the false all-clear this suite exists
  # to prevent, so the reassurance is gated on them having actually run.
  if [ "$RUN_GUARD" -eq 1 ] && [ "$RUN_HARNESS" -eq 1 ] && [ "$RUN_DELEGATION" -eq 1 ]; then
    echo "  guard, harness and delegation GREEN — your change did not regress any."
  else
    echo "  NOTE: not every non-contract battery ran — this says nothing about"
    echo "  the ones that did not."
  fi
  echo "  The contract battery is RED over the build-docs <-> swarm-loop drift"
  echo "  named in its FAIL block. That is a finding about the SKILL, and the fix"
  echo "  is in the CODE or the LEDGER — never in the test."
  echo "  (contract exit=$CONTRACT_RC)"
  exit 1
fi

echo "  REGRESSION — read the FAIL blocks above."
echo "  A failing case is a finding about the SKILL, not a licence to edit the test."
echo "  (guard exit=$GUARD_RC  harness exit=$HARNESS_RC  contract exit=$CONTRACT_RC"
echo "   delegation exit=$DELEGATION_RC)"
exit 1
