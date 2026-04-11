"""
CHIMERA Scalping Engine — API Server
=======================================
FastAPI backend for Cloud Run (europe-west2).
Frontend served from Cloudflare Pages.

Mirrors the Lay Engine API pattern with scalping-specific endpoints:
  - /api/state, /api/login, /api/logout, /api/health
  - /api/engine/start, /api/engine/stop, /api/engine/dry-run
  - /api/candidates — scored candidates
  - /api/trades — active and historical trades
  - /api/trades/{id}/control — manual override
  - /api/trades/{id}/flatten — emergency flatten
  - /api/alerts — bookmaker trigger alerts
  - /api/alerts/{id}/confirm — confirm external bet
  - /api/risk — risk config and portfolio state
  - /api/markets — discovered markets
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

from fastapi import FastAPI, Header, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from engine import ScalpEngine

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chimera_scalp")

app = FastAPI(title="CHIMERA Scalping Engine", version="1.0.0")

# ── CORS ──
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://scalping.thync.online")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Engine singleton ──
engine = ScalpEngine()


# ── Request models ──

class LoginRequest(BaseModel):
    username: str
    password: str

class PointValueRequest(BaseModel):
    value: float

class CountriesRequest(BaseModel):
    countries: list[str]

class ProcessWindowRequest(BaseModel):
    minutes: float

class TradeControlRequest(BaseModel):
    mode: str              # AUTO / ASSISTED / MANUAL_LOCK
    reason: str = ""

class ConfirmBetRequest(BaseModel):
    alert_id: str
    stake: float
    odds: float
    bookmaker: str = ""
    bookmaker_reference: str = ""
    notes: str = ""

class RiskConfigUpdate(BaseModel):
    max_loss_per_trade: float = None
    daily_drawdown_cap: float = None
    bankroll: float = None
    kill_switch_enabled: bool = None
    flatten_only_mode: bool = None

class LadderProfileRequest(BaseModel):
    profile: str           # VERY_DEFENSIVE / DEFENSIVE / BALANCED


# ── Auth dependency ──

def require_api_key(x_api_key: str = Header(None), api_key: str = Query(None)):
    key = x_api_key or api_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not engine.validate_api_key(key):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Health & State ──

@app.get("/api/health")
def health():
    return {"status": "ok", "engine": engine.status}

@app.get("/api/keepalive")
def keepalive():
    return {
        "status": "ok",
        "engine": engine.status,
        "authenticated": engine.is_authenticated,
        "dry_run": engine.dry_run,
        "active_trades": len([
            t for t in engine.trades.values()
            if t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")
        ]),
        "active_alerts": len(engine.active_alerts),
    }

@app.get("/api/state")
def get_state():
    return engine.get_state()


# ── Auth ──

@app.post("/api/login")
def login(req: LoginRequest):
    success, error = engine.login(req.username, req.password)
    if success:
        return {"status": "ok", "balance": engine.balance}
    return JSONResponse(
        status_code=401,
        content={"status": "error", "message": f"Login failed: {error}"},
    )

@app.post("/api/logout")
def logout():
    engine.logout()
    return {"status": "ok"}


# ── Engine Controls ──

@app.post("/api/engine/start")
def start_engine():
    if not engine.is_authenticated:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Not authenticated"})
    engine.start()
    return {"status": engine.status}

@app.post("/api/engine/stop")
def stop_engine():
    engine.stop()
    return {"status": engine.status}

@app.post("/api/engine/dry-run")
def toggle_dry_run():
    engine.dry_run = not engine.dry_run
    return {"dry_run": engine.dry_run}

@app.post("/api/engine/point-value")
def set_point_value(req: PointValueRequest):
    if req.value < 0.5 or req.value > 100:
        raise HTTPException(status_code=400, detail="Point value must be 0.5–100")
    engine.point_value = round(req.value, 2)
    engine._save_state()
    return {"point_value": engine.point_value}

@app.post("/api/engine/countries")
def set_countries(req: CountriesRequest):
    valid = {"GB", "IE", "ZA", "FR"}
    filtered = [c for c in req.countries if c in valid]
    if not filtered:
        raise HTTPException(status_code=400, detail="At least one valid country required")
    engine.countries = filtered
    engine._save_state()
    return {"countries": engine.countries}

@app.post("/api/engine/process-window")
def set_process_window(req: ProcessWindowRequest):
    if req.minutes < 5 or req.minutes > 240:
        raise HTTPException(status_code=400, detail="Window must be 5–240 minutes")
    engine.process_window = req.minutes
    engine._save_state()
    return {"process_window": engine.process_window}

@app.post("/api/engine/ladder-profile")
def set_ladder_profile(req: LadderProfileRequest):
    if req.profile not in ("VERY_DEFENSIVE", "DEFENSIVE", "BALANCED"):
        raise HTTPException(status_code=400, detail="Invalid profile")
    engine.ladder_profile = req.profile
    engine._save_state()
    return {"ladder_profile": engine.ladder_profile}


# ── Markets ──

@app.get("/api/markets")
def get_markets():
    from datetime import datetime as dt, timezone as tz
    now = dt.now(tz.utc)
    upcoming = []
    for m in engine.markets:
        try:
            race_time = dt.fromisoformat(m.get("race_time", "").replace("Z", "+00:00"))
            minutes_to_off = (race_time - now).total_seconds() / 60
            upcoming.append({**m, "minutes_to_off": round(minutes_to_off, 1)})
        except (ValueError, KeyError):
            pass
    upcoming.sort(key=lambda x: x.get("race_time", ""))
    return {"markets": upcoming}


# ── Candidates ──

@app.get("/api/candidates")
def get_candidates():
    return {
        "candidates": engine.qualified_candidates[-50:],
        "count": len(engine.qualified_candidates),
    }


# ── Trades ──

@app.get("/api/trades")
def get_trades():
    active = {tid: t for tid, t in engine.trades.items()
              if t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")}
    return {"trades": active, "count": len(active)}

@app.get("/api/trades/all")
def get_all_trades():
    return {"trades": engine.trades, "count": len(engine.trades)}

@app.get("/api/trades/{trade_id}")
def get_trade(trade_id: str):
    trade = engine.trades.get(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return {
        "trade": trade,
        "plan": engine.trade_plans.get(trade_id),
        "position": engine.positions.get(trade_id),
    }

@app.post("/api/trades/{trade_id}/control")
def set_trade_control(trade_id: str, req: TradeControlRequest):
    ok, error = engine.set_trade_control(trade_id, req.mode, reason=req.reason)
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"trade_id": trade_id, "control_mode": req.mode}

@app.post("/api/trades/{trade_id}/flatten")
def flatten_trade(trade_id: str):
    ok, error = engine.flatten_trade(trade_id)
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"trade_id": trade_id, "state": "STOPPING_OUT"}


# ── Bookmaker Trigger Alerts ──

@app.get("/api/alerts")
def get_alerts():
    return {
        "active": engine.active_alerts,
        "count": len(engine.active_alerts),
    }

@app.get("/api/alerts/history")
def get_alert_history():
    return {
        "alerts": engine.alert_history[-100:],
        "count": len(engine.alert_history),
    }

@app.post("/api/alerts/{alert_id}/confirm")
def confirm_alert(alert_id: str, req: ConfirmBetRequest):
    ok, error = engine.confirm_external_bet({
        "alert_id": alert_id,
        "stake": req.stake,
        "odds": req.odds,
        "bookmaker": req.bookmaker,
        "bookmaker_reference": req.bookmaker_reference,
        "notes": req.notes,
    })
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"status": "confirmed", "alert_id": alert_id}

@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: str):
    for a in engine.active_alerts:
        if a.get("alert_id") == alert_id:
            a["status"] = "DISMISSED"
            engine.alert_history.append(a)
            engine.active_alerts = [x for x in engine.active_alerts if x.get("alert_id") != alert_id]
            engine._save_trades()
            return {"status": "dismissed"}
    raise HTTPException(status_code=404, detail="Alert not found")


# ── Risk ──

@app.get("/api/risk")
def get_risk():
    return {
        "config": engine.risk_config.to_dict(),
        "portfolio": engine.portfolio_state.to_dict(),
        "trade_gate": {
            "max_first_lay_only_loss": engine.trade_gate_config.max_first_lay_only_loss,
            "max_no_lay_loss": engine.trade_gate_config.max_no_lay_loss,
            "max_stop_loss": engine.trade_gate_config.max_stop_loss,
        },
    }

@app.post("/api/risk/config")
def update_risk_config(req: RiskConfigUpdate):
    if req.max_loss_per_trade is not None:
        engine.risk_config.max_loss_per_trade = req.max_loss_per_trade
    if req.daily_drawdown_cap is not None:
        engine.risk_config.daily_drawdown_cap = req.daily_drawdown_cap
    if req.bankroll is not None:
        engine.risk_config.bankroll = req.bankroll
    if req.kill_switch_enabled is not None:
        engine.risk_config.kill_switch_enabled = req.kill_switch_enabled
    if req.flatten_only_mode is not None:
        engine.risk_config.flatten_only_mode = req.flatten_only_mode
    engine._save_state()
    return {"config": engine.risk_config.to_dict()}

@app.post("/api/risk/kill-switch")
def toggle_kill_switch():
    engine.risk_config.kill_switch_enabled = not engine.risk_config.kill_switch_enabled
    engine._save_state()
    return {"kill_switch_enabled": engine.risk_config.kill_switch_enabled}


# ── Positions ──

@app.get("/api/positions")
def get_positions():
    return {"positions": engine.positions}


# ── Settlements ──

@app.get("/api/settlements")
def get_settlements():
    return {"settlements": engine.settlements[-100:]}


# ── Audit Trail ──

@app.get("/api/audit")
def get_audit():
    return {"transitions": engine.state_transitions[-100:]}


# ── Sessions ──

@app.get("/api/sessions")
def get_sessions():
    return {"sessions": engine.sessions[-50:]}
