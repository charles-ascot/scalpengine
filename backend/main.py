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
import hmac
import logging
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

from fastapi import FastAPI, Header, Query, HTTPException, Depends
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

app = FastAPI(title="CHIMERA Scalping Engine", version="1.2.0")

# ── CORS ──
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://scalpengine.thync.online")
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


@app.on_event("startup")
def _resume_after_cold_start():
    """Restart the scan loop if the instance was recycled while running.

    Cloud Run scales to zero; without this the engine comes back STOPPED even
    though a valid Betfair session was restored from persistence.
    """
    try:
        if engine._should_resume and engine.is_authenticated:
            engine.start()
            logger.info("Engine auto-resumed after cold start")
        elif engine._should_resume:
            logger.warning("Resume wanted but no valid Betfair session — login required")
    except Exception as e:
        logger.error(f"Auto-resume failed: {e}")
    finally:
        engine._should_resume = False


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

class ManualOrderRequest(BaseModel):
    side: str              # BACK / LAY
    price: float
    size: float
    reason: str = ""

class LadderProfileRequest(BaseModel):
    profile: str           # VERY_DEFENSIVE / DEFENSIVE / BALANCED


# ── Auth dependency ──
#
# Disabled by default so existing deployments keep working unchanged. Set
# REQUIRE_AUTH=true on Cloud Run to close the API to anonymous callers — do
# this before going live, since the kill switch and risk config are mutable.
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "false").lower() == "true"

# Operator key, mounted from Secret Manager (scalpengine-ops-api-key). Lets
# tooling operate the API without a browser session or a Betfair password.
# Unset means no operator key exists; it never falls back to a default.
OPS_API_KEY = os.environ.get("OPS_API_KEY", "")


def _valid_key(key: str) -> bool:
    if OPS_API_KEY and hmac.compare_digest(key.encode(), OPS_API_KEY.encode()):
        return True
    return engine.validate_api_key(key)


def require_auth(
    x_api_key: str = Header(None),
    x_session_token: str = Header(None),
    api_key: str = Query(None),
):
    """Accept either a browser session token or a machine API key."""
    if not REQUIRE_AUTH:
        return None
    if x_session_token and engine.validate_ui_session(x_session_token):
        return x_session_token
    key = x_api_key or api_key
    if key and _valid_key(key):
        return key
    raise HTTPException(status_code=401, detail="Authentication required")


def require_api_key(x_api_key: str = Header(None), api_key: str = Query(None)):
    """Machine-only gate. Always enforced, regardless of REQUIRE_AUTH."""
    key = x_api_key or api_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not _valid_key(key):
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
        "require_auth": REQUIRE_AUTH,
        "dry_run": engine.dry_run,
        "active_trades": len([
            t for t in engine.trades.values()
            if t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")
        ]),
        "active_alerts": len(engine.active_alerts),
    }

@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    return engine.get_state()


# ── Auth ──

@app.post("/api/login")
def login(req: LoginRequest):
    success, error = engine.login(req.username, req.password)
    if success:
        return {
            "status": "ok",
            "balance": engine.balance,
            "session_token": engine.issue_ui_session(),
        }
    return JSONResponse(
        status_code=401,
        content={"status": "error", "message": f"Login failed: {error}"},
    )

@app.post("/api/logout", dependencies=[Depends(require_auth)])
def logout(x_session_token: str = Header(None)):
    if x_session_token:
        engine.revoke_ui_session(x_session_token)
    engine.logout()
    return {"status": "ok"}


# ── Engine Controls ──

@app.post("/api/engine/start", dependencies=[Depends(require_auth)])
def start_engine():
    if not engine.is_authenticated:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Not authenticated"})
    engine.start()
    return {"status": engine.status}

@app.post("/api/engine/stop", dependencies=[Depends(require_auth)])
def stop_engine():
    engine.stop()
    return {"status": engine.status}

@app.post("/api/engine/dry-run", dependencies=[Depends(require_auth)])
def toggle_dry_run():
    engine.dry_run = not engine.dry_run
    return {"dry_run": engine.dry_run}

@app.post("/api/engine/point-value", dependencies=[Depends(require_auth)])
def set_point_value(req: PointValueRequest):
    if req.value < 0.5 or req.value > 100:
        raise HTTPException(status_code=400, detail="Point value must be 0.5–100")
    engine.point_value = round(req.value, 2)
    engine._save_state()
    return {"point_value": engine.point_value}

@app.post("/api/engine/countries", dependencies=[Depends(require_auth)])
def set_countries(req: CountriesRequest):
    valid = {"GB", "IE", "ZA", "FR"}
    filtered = [c for c in req.countries if c in valid]
    if not filtered:
        raise HTTPException(status_code=400, detail="At least one valid country required")
    engine.countries = filtered
    engine._save_state()
    return {"countries": engine.countries}

@app.post("/api/engine/process-window", dependencies=[Depends(require_auth)])
def set_process_window(req: ProcessWindowRequest):
    if req.minutes < 5 or req.minutes > 240:
        raise HTTPException(status_code=400, detail="Window must be 5–240 minutes")
    engine.process_window = req.minutes
    engine._save_state()
    return {"process_window": engine.process_window}

@app.post("/api/engine/ladder-profile", dependencies=[Depends(require_auth)])
def set_ladder_profile(req: LadderProfileRequest):
    if req.profile not in ("VERY_DEFENSIVE", "DEFENSIVE", "BALANCED"):
        raise HTTPException(status_code=400, detail="Invalid profile")
    engine.ladder_profile = req.profile
    engine._save_state()
    return {"ladder_profile": engine.ladder_profile}


# ── Markets ──

@app.get("/api/markets", dependencies=[Depends(require_auth)])
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

@app.get("/api/candidates", dependencies=[Depends(require_auth)])
def get_candidates():
    return {
        "candidates": engine.qualified_candidates[-50:],
        "count": len(engine.qualified_candidates),
    }


# ── Trades ──

def _trade_view(tid: str, t: dict) -> dict:
    """A trade plus its stage progress and live position, for the dashboard."""
    pos = engine.positions.get(tid) or {}
    plan = engine.trade_plans.get(tid) or {}
    return {
        **t,
        "position": {k: pos.get(k) for k in (
            "gross_back_stake", "avg_back_odds", "gross_lay_stake", "avg_lay_odds",
            "pnl_if_win", "pnl_if_lose", "max_open_loss")},
        "stages": [{k: st.get(k) for k in (
            "stage_no", "mode", "planned_stake", "executed", "actual_stake",
            "actual_price", "skipped", "last_block")} for st in plan.get("entry_stages", [])],
    }


@app.get("/api/trades", dependencies=[Depends(require_auth)])
def get_trades():
    active = {tid: _trade_view(tid, t) for tid, t in engine.trades.items()
              if t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")}
    return {"trades": active, "count": len(active)}

@app.get("/api/trades/all", dependencies=[Depends(require_auth)])
def get_all_trades():
    return {"trades": {tid: _trade_view(tid, t) for tid, t in engine.trades.items()},
            "count": len(engine.trades)}

@app.get("/api/trades/{trade_id}", dependencies=[Depends(require_auth)])
def get_trade(trade_id: str):
    trade = engine.trades.get(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return {
        "trade": trade,
        "plan": engine.trade_plans.get(trade_id),
        "position": engine.positions.get(trade_id),
    }

@app.post("/api/trades/{trade_id}/control", dependencies=[Depends(require_auth)])
def set_trade_control(trade_id: str, req: TradeControlRequest):
    ok, error = engine.set_trade_control(trade_id, req.mode, reason=req.reason)
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"trade_id": trade_id, "control_mode": req.mode}

@app.post("/api/trades/{trade_id}/flatten", dependencies=[Depends(require_auth)])
def flatten_trade(trade_id: str):
    ok, error = engine.flatten_trade(trade_id)
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return {"trade_id": trade_id, "state": "STOPPING_OUT"}


# ── Bookmaker Trigger Alerts ──

@app.get("/api/alerts", dependencies=[Depends(require_auth)])
def get_alerts():
    return {
        "active": engine.active_alerts,
        "count": len(engine.active_alerts),
    }

@app.get("/api/alerts/history", dependencies=[Depends(require_auth)])
def get_alert_history():
    return {
        "alerts": engine.alert_history[-100:],
        "count": len(engine.alert_history),
    }

@app.post("/api/alerts/{alert_id}/confirm", dependencies=[Depends(require_auth)])
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

@app.post("/api/alerts/{alert_id}/dismiss", dependencies=[Depends(require_auth)])
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

@app.get("/api/risk", dependencies=[Depends(require_auth)])
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

@app.post("/api/risk/config", dependencies=[Depends(require_auth)])
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

@app.post("/api/risk/kill-switch", dependencies=[Depends(require_auth)])
def toggle_kill_switch():
    engine.risk_config.kill_switch_enabled = not engine.risk_config.kill_switch_enabled
    engine._save_state()
    return {"kill_switch_enabled": engine.risk_config.kill_switch_enabled}


# ── Positions ──

@app.get("/api/positions", dependencies=[Depends(require_auth)])
def get_positions():
    return {"positions": engine.positions}


# ── Settlements ──

@app.get("/api/settlements", dependencies=[Depends(require_auth)])
def get_settlements():
    return {"settlements": engine.settlements[-100:]}


# ── Audit Trail ──

@app.get("/api/audit", dependencies=[Depends(require_auth)])
def get_audit():
    return {"transitions": engine.state_transitions[-100:]}


# ── Sessions ──

@app.get("/api/sessions", dependencies=[Depends(require_auth)])
def get_sessions():
    return {"sessions": engine.sessions[-50:]}


# ── API Key Management ──

class ApiKeyRequest(BaseModel):
    label: str = ""


@app.post("/api/keys", dependencies=[Depends(require_auth)])
def create_api_key(req: ApiKeyRequest):
    """Mint a machine API key. The raw key is shown once, here."""
    return engine.generate_api_key(req.label)


@app.get("/api/keys", dependencies=[Depends(require_auth)])
def list_api_keys():
    """List key metadata. Never returns the key material itself."""
    return {
        "keys": [
            {
                "key_id": k["key_id"],
                "label": k["label"],
                "created_at": k["created_at"],
                "last_used": k.get("last_used"),
            }
            for k in engine.api_keys
        ]
    }


# ── Orders & Fills (B.14) ──

@app.get("/api/orders", dependencies=[Depends(require_auth)])
def list_orders(trade_id: str = Query(None)):
    orders = engine.order_manager.orders.values()
    if trade_id:
        orders = [o for o in orders if o.trade_id == trade_id]
    return {"orders": sorted((o.to_dict() for o in orders), key=lambda o: o["created_at"], reverse=True)}


@app.get("/api/fills", dependencies=[Depends(require_auth)])
def list_fills(trade_id: str = Query(None)):
    fills = engine.order_manager.fills
    if trade_id:
        fills = [f for f in fills if f.trade_id == trade_id]
    return {"fills": [f.to_dict() for f in reversed(fills)]}


@app.post("/api/trades/{trade_id}/manual-order", dependencies=[Depends(require_auth)])
def manual_order(trade_id: str, req: ManualOrderRequest):
    """Place a manual order against a trade (B.16).

    Dry run only until the execution stages that manage live orders exist:
    this is how the simulated venue is exercised against real prices. It
    requires a running engine, because only the scan loop reconciles orders.
    """
    if not engine.dry_run:
        return JSONResponse(status_code=409, content={
            "status": "error",
            "message": "Manual orders are dry-run only until live order management is built.",
        })
    if engine.status != "RUNNING":
        return JSONResponse(status_code=409, content={
            "status": "error",
            "message": "Start the engine first — only a running engine reconciles orders.",
        })
    side = req.side.upper()
    if side not in ("BACK", "LAY"):
        raise HTTPException(status_code=400, detail="side must be BACK or LAY")
    order, error = engine.submit_order(trade_id, side, req.price, req.size,
                                       source="MANUAL", user_id="operator")
    if order is None:
        raise HTTPException(status_code=404, detail=error)
    return {"status": "ok", "order": order.to_dict()}
