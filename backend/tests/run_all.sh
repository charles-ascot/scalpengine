#!/usr/bin/env bash
# Runs the backend test suite. Betfair is stubbed throughout — no network,
# no credentials, no real bets.
set -uo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
fail=0

echo "── auth: REQUIRE_AUTH=false (default, backward compatible) ──"
REQUIRE_AUTH=false "$PY" test_auth.py 2>/dev/null | grep -E "PASS|FAIL|passed" || fail=1

echo
echo "── auth: REQUIRE_AUTH=true (hardened) ──"
REQUIRE_AUTH=true "$PY" test_auth.py 2>/dev/null | grep -E "PASS|FAIL|passed" || fail=1

echo
echo "── persistence + portfolio ──"
"$PY" test_persistence.py 2>/dev/null | grep -E "PASS|FAIL|passed" || fail=1

echo
echo "── stale trade purge ──"
"$PY" test_purge.py 2>/dev/null | grep -E "PASS|FAIL|passed" || fail=1

echo
[ "$fail" -eq 0 ] && echo "ALL SUITES PASSED" || echo "SUITE FAILURES"
exit "$fail"
