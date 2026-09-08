"""Session persistence across cold starts + portfolio recompute (Level-4 risk)."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ.update({
    "STATE_FILE": f"{TMP}/state.json", "SESSIONS_FILE": f"{TMP}/sessions.json",
    "API_KEYS_FILE": f"{TMP}/keys.json", "TRADES_FILE": f"{TMP}/trades.json",
    "GCS_BUCKET": "", "BETFAIR_APP_KEY": "test_key", "DRY_RUN": "true",
})
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from betfair_client import BetfairClient
BetfairClient.login = lambda self: (
    setattr(self, "session_token", "TOK") or
    setattr(self, "session_expiry", datetime.now(timezone.utc) + timedelta(hours=4)) or True)
BetfairClient.get_account_balance = lambda self: 500.0
BetfairClient.get_todays_win_markets = lambda self, countries=None: []

from engine import ScalpEngine
from risk_engine import check_portfolio_risk

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")

print("\n[cold-start session restore]")
e1 = ScalpEngine()
ok, _ = e1.login("u", "p")
check("login succeeds", ok)
e1.running = True
e1._save_state()

e2 = ScalpEngine()                      # simulates a Cloud Run cold start
check("session survives cold start", e2.is_authenticated, "client/token lost")
check("token matches", e2.client.session_token == "TOK")
check("resume flag set from persisted running=True", e2._should_resume is True)

print("\n[expired session is not restored]")
e1.client.session_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
e1._save_state()
e3 = ScalpEngine()
check("expired session rejected", not e3.is_authenticated)
check("no resume on expired session", e3._should_resume is False)

print("\n[password is never persisted]")
raw = open(os.environ["STATE_FILE"]).read()
check("state file contains no password", "p" not in __import__("json").loads(raw).get("session", {}))
check("state file has no 'password' key", "password" not in raw)

print("\n[portfolio recompute]")
e = ScalpEngine()
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
e.trades = {
    "t1": {"trade_id": "t1", "market_id": "m1", "state": "OPEN",    "created_at": f"{today}T10:00:00+00:00"},
    "t2": {"trade_id": "t2", "market_id": "m1", "state": "OPEN",    "created_at": f"{today}T10:05:00+00:00"},
    "t3": {"trade_id": "t3", "market_id": "m2", "state": "SETTLED", "created_at": f"{today}T09:00:00+00:00"},
}
e.positions = {
    "t1": {"max_open_loss": 120.0, "unrealized_pnl": -10.0, "realized_pnl": 0.0,   "hedge_completion_pct": 50.0},
    "t2": {"max_open_loss": 80.0,  "unrealized_pnl": 5.0,   "realized_pnl": 0.0,   "hedge_completion_pct": 100.0},
    "t3": {"max_open_loss": 0.0,   "unrealized_pnl": 0.0,   "realized_pnl": -45.0, "hedge_completion_pct": 100.0},
}
e._recompute_portfolio()
p = e.portfolio_state
check("open trades counted (settled excluded)", p.total_open_trades == 2, str(p.total_open_trades))
check("total exposure summed", p.total_exposure == 200.0, str(p.total_exposure))
check("unhedged only counts partial hedges", p.total_unhedged == 120.0, str(p.total_unhedged))
check("unrealised P&L netted", p.daily_unrealised_pnl == -5.0, str(p.daily_unrealised_pnl))
check("realised P&L from settled trade", p.daily_realised_pnl == -45.0, str(p.daily_realised_pnl))
check("exposure grouped by market", p.exposure_by_market == {"m1": 200.0}, str(p.exposure_by_market))
check("state API exposes real numbers", e.get_state()["portfolio"]["total_exposure"] == 200.0)

print("\n[Level-4 risk now actually fires]")
before = check_portfolio_risk(100.0, __import__("risk_engine").PortfolioState(), e.risk_config)
check("zeroed portfolio allows trade (the old always-on behaviour)", before.allowed)
e.portfolio_state.daily_realised_pnl = -600.0   # past the £500 daily cap
after = check_portfolio_risk(100.0, e.portfolio_state, e.risk_config)
check("drawdown breach now BLOCKS", not after.allowed, str(after.reasons))
check("block cites the cap", any("drawdown" in r.lower() for r in after.reasons), str(after.reasons))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
