"""Stale-trade purge: dead races go, live exposure stays."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ.update({
    "STATE_FILE": f"{TMP}/s.json", "SESSIONS_FILE": f"{TMP}/se.json",
    "API_KEYS_FILE": f"{TMP}/k.json", "TRADES_FILE": f"{TMP}/t.json",
    "GCS_BUCKET": "", "BETFAIR_APP_KEY": "test", "DRY_RUN": "true",
})
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from engine import ScalpEngine

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}{'' if c else '  <- ' + d}")

now = datetime.now(timezone.utc)
past, future = now - timedelta(hours=5), now + timedelta(hours=2)
today = now.strftime("%Y-%m-%d")
yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

e = ScalpEngine()
e.trades = {
    "dead":    {"state": "PLANNED", "race_time": past.isoformat(),   "created_at": f"{yday}T17:06:00+00:00"},
    "upcoming":{"state": "PLANNED", "race_time": future.isoformat(), "created_at": f"{today}T09:00:00+00:00"},
    "exposed": {"state": "LIVE_EXPOSED", "race_time": past.isoformat(), "created_at": f"{yday}T17:06:00+00:00"},
    "oldclosed":{"state": "SETTLED", "race_time": past.isoformat(),  "created_at": f"{yday}T14:00:00+00:00"},
    "todayclosed":{"state":"SETTLED","race_time": past.isoformat(),  "created_at": f"{today}T10:00:00+00:00"},
    "nodate":  {"state": "PLANNED", "race_time": "",                 "created_at": f"{today}T09:00:00+00:00"},
}
e.trade_plans = {k: {} for k in e.trades}
e.positions   = {k: {} for k in e.trades}

n = e._purge_stale_trades()
kept = set(e.trades)

print("\n[purge]")
check("dead PLANNED race removed", "dead" not in kept)
check("old settled trade removed", "oldclosed" not in kept)
check("upcoming race kept", "upcoming" in kept)
check("LIVE_EXPOSED kept despite age", "exposed" in kept, "exposure must never be silently dropped")
check("today's settled trade kept", "todayclosed" in kept)
check("trade with no race_time kept", "nodate" in kept)
check("purge count correct", n == 2, str(n))
check("side tables pruned in step", set(e.trade_plans) == kept and set(e.positions) == kept)

print("\n[timestamp parsing]")
check("Z suffix", e._parse_dt("2026-09-09T12:51:00Z") is not None)
check("offset suffix", e._parse_dt("2026-09-09T12:51:00+00:00") is not None)
check("naive treated as UTC", e._parse_dt("2026-09-09T12:51:00").tzinfo is not None)
check("garbage is safe", e._parse_dt("not-a-date") is None)
check("empty is safe", e._parse_dt("") is None)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
