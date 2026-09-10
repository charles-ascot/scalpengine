#!/usr/bin/env bash
# Runs the backend test suite. Betfair is stubbed throughout — no network,
# no credentials, no real bets.
#
# Exits non-zero if any suite fails, crashes, or reports zero assertions, so a
# CI step can never pass on an empty run. On failure the suite's full output is
# printed, since CI has no other way to show the traceback.
set -uo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
fail=0

suite() {  # suite <label> [VAR=value ...] <file>
  local label="$1"; shift
  local out rc
  echo "── $label ──"
  out=$(env "$@" 2>&1); rc=$?
  echo "$out" | grep -E "^  (PASS|FAIL)  |passed, [0-9]+ failed$" || true
  if [ "$rc" -ne 0 ] || ! echo "$out" | grep -qE "^[1-9][0-9]* passed, 0 failed$"; then
    echo "!! $label FAILED (exit $rc). Last 40 lines:"
    echo "$out" | tail -40
    fail=1
  fi
  echo
}

suite "auth: REQUIRE_AUTH=false (default, backward compatible)" REQUIRE_AUTH=false "$PY" test_auth.py
suite "auth: REQUIRE_AUTH=true (hardened)"                      REQUIRE_AUTH=true  "$PY" test_auth.py
suite "persistence + portfolio"                                  "$PY" test_persistence.py
suite "stale trade purge"                                        "$PY" test_purge.py
suite "execution: orders, fills, simulation"                     "$PY" test_execution.py

[ "$fail" -eq 0 ] && echo "ALL SUITES PASSED" || echo "SUITE FAILURES"
exit "$fail"
