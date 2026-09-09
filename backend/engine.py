"""
CHIMERA Scalping Engine — Core Engine
========================================
Event-driven scalping trade management engine.
Discovers candidates → scores → plans → stages entries → manages ladders → hedges.

Architecture mirrors the Lay Engine:
  - GCS state persistence (survives Cloud Run cold starts)
  - Background thread engine loop
  - Configurable polling interval
  - Session tracking
  - API key management

Key differences from Lay Engine:
  - BACK + LAY orders (not just LAY)
  - Staged entry (probe → confirm → late add)
  - Lay ladder management (3-rung front-loaded)
  - Bookmaker trigger detection (Part C)
  - 4-level risk engine
  - Mandatory scenario pre-check
  - Manual override controls (AUTO / ASSISTED / MANUAL_LOCK)
"""

import os
import json
import time
import secrets
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from betfair_client import BetfairClient
from fsu_client import FSUClient
from candidate import (
    CandidateScore, QualificationConfig, qualify_candidate,
    compute_favourite_persistence, compute_first_lay_hit_prob,
    compute_adverse_open_risk, compute_stop_out_survival,
    compute_liquidity_score, compute_bookmaker_consensus,
)
from trade_planner import (
    TradePlan, build_trade_plan, classify_open,
    qualifies_for_large_size,
)
from scenario_engine import (
    ScenarioEnvelope, TradeGateConfig, simulate_scenarios, passes_trade_gate,
)
from risk_engine import (
    RiskConfig, RiskDecision, PortfolioState, evaluate_risk,
)
from position import PositionSnapshot, Settlement
from bookmaker_trigger import (
    TriggerConfig, ExternalEntryAlert, ExternalBetConfirmation,
    evaluate_trigger,
)
from state_machines import (
    TradeState, ControlMode, OrderState, StateTransition,
    validate_trade_transition, validate_control_transition,
)

logger = logging.getLogger("scalp_engine")

# ── Configuration from environment ──
BETFAIR_APP_KEY = os.environ.get("BETFAIR_APP_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
PROCESS_WINDOW_MINUTES = int(os.environ.get("PROCESS_WINDOW_MINUTES", "120"))

# GCS persistence
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
_gcs_client = None

def _get_gcs_client():
    global _gcs_client
    if _gcs_client is None and GCS_BUCKET:
        from google.cloud import storage
        _gcs_client = storage.Client()
    return _gcs_client

def _gcs_write(blob_name: str, data: str):
    try:
        client = _get_gcs_client()
        if not client:
            return
        bucket = client.bucket(GCS_BUCKET)
        bucket.blob(blob_name).upload_from_string(data, content_type="application/json")
    except Exception as e:
        logger.warning(f"GCS write failed for {blob_name}: {e}")

def _gcs_read(blob_name: str) -> Optional[str]:
    try:
        client = _get_gcs_client()
        if not client:
            return None
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(blob_name)
        if not blob.exists():
            return None
        return blob.download_as_text()
    except Exception as e:
        logger.warning(f"GCS read failed for {blob_name}: {e}")
        return None

# State files
STATE_FILE = Path(os.environ.get("STATE_FILE", "/tmp/chimera_scalp_state.json"))
SESSIONS_FILE = Path(os.environ.get("SESSIONS_FILE", "/tmp/chimera_scalp_sessions.json"))
API_KEYS_FILE = Path(os.environ.get("API_KEYS_FILE", "/tmp/chimera_scalp_api_keys.json"))
TRADES_FILE = Path(os.environ.get("TRADES_FILE", "/tmp/chimera_scalp_trades.json"))


class ScalpEngine:
    """
    The scalping engine. Discovers candidates, scores them, builds trade plans,
    stages entries, manages lay ladders, and handles risk.
    """

    def __init__(self):
        self.client: Optional[BetfairClient] = None
        self.fsu_client: Optional[FSUClient] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None

        # ── Markets ──
        self.markets: list[dict] = []
        self.last_scan: Optional[str] = None
        self.status: str = "STOPPED"
        self.balance: Optional[float] = None
        self.errors: list[dict] = []
        self.day_started: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.dry_run: bool = DRY_RUN
        self.countries: list[str] = ["GB", "IE"]
        self.process_window: float = PROCESS_WINDOW_MINUTES

        # ── Candidates ──
        self.candidates: list[dict] = []          # Scored candidates
        self.qualified_candidates: list[dict] = [] # Passed qualification

        # ── Trades ──
        self.trades: dict = {}                     # trade_id → trade state dict
        self.trade_plans: dict = {}                # trade_id → TradePlan.to_dict()
        self.positions: dict = {}                  # trade_id → PositionSnapshot.to_dict()
        self.settlements: list[dict] = []

        # ── Bookmaker Triggers ──
        self.active_alerts: list[dict] = []        # Active trigger alerts
        self.alert_history: list[dict] = []        # All alerts (for analytics)
        self.confirmations: list[dict] = []        # Trader confirmations

        # ── Risk ──
        self.risk_config: RiskConfig = RiskConfig()
        self.trade_gate_config: TradeGateConfig = TradeGateConfig()
        self.qualification_config: QualificationConfig = QualificationConfig()
        self.trigger_config: TriggerConfig = TriggerConfig()
        self.portfolio_state: PortfolioState = PortfolioState()

        # ── Audit ──
        self.state_transitions: list[dict] = []    # Last 500 transitions

        # ── Configuration ──
        self.point_value: float = 1.0
        self.ladder_profile: str = "DEFENSIVE"
        self.default_exit_policy: str = "PRE_RACE_ONLY"

        # ── Session tracking (from lay engine) ──
        self.sessions: list[dict] = []
        self.current_session: Optional[dict] = None

        # ── API keys (from lay engine) ──
        self.api_keys: list[dict] = []

        # ── Browser sessions (issued on Betfair login) ──
        self.ui_sessions: dict = {}          # token → {created_at, expires_at}

        # ── Credentials ──
        self._username: Optional[str] = None
        self._password: Optional[str] = None
        self._should_resume: bool = False   # set by _load_state on cold start

        # ── Monitoring ──
        self.monitoring: dict = {}   # market_id → [snapshots]

        # Load state
        self._load_state()
        self._load_sessions()
        self._load_api_keys()
        self._load_trades()
        self._purge_stale_trades()

    # ──────────────────────────────────────────────
    #  STATE PERSISTENCE
    # ──────────────────────────────────────────────

    def _save_state(self):
        try:
            state = {
                "day_started": self.day_started,
                "dry_run": self.dry_run,
                "countries": self.countries,
                "process_window": self.process_window,
                "point_value": self.point_value,
                "ladder_profile": self.ladder_profile,
                "status": self.status,
                "balance": self.balance,
                "risk_config": self.risk_config.to_dict(),
                "errors": self.errors[-50:],
                "last_scan": self.last_scan,
                "session": self._session_snapshot(),
                "ui_sessions": self.ui_sessions,
                "running": self.running,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            state_json = json.dumps(state, default=str)
            STATE_FILE.write_text(state_json)
            _gcs_write("chimera_scalp_state.json", state_json)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")

    def _load_state(self):
        try:
            raw = _gcs_read("chimera_scalp_state.json")
            if raw:
                data = json.loads(raw)
            elif STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
            else:
                return

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("day_started") != today:
                logger.info("State from different day — starting fresh")
                return

            self.day_started = data["day_started"]
            self.dry_run = data.get("dry_run", DRY_RUN)
            self.countries = data.get("countries", ["GB", "IE"])
            self.process_window = data.get("process_window", PROCESS_WINDOW_MINUTES)
            self.point_value = data.get("point_value", 1.0)
            self.ladder_profile = data.get("ladder_profile", "DEFENSIVE")
            self.balance = data.get("balance")
            self.errors = data.get("errors", [])
            self.last_scan = data.get("last_scan")

            # Restore the Betfair session so a Cloud Run cold start does not
            # silently log the engine out mid-session.
            self.ui_sessions = data.get("ui_sessions") or {}
            self._prune_ui_sessions()

            if self._restore_session(data.get("session")):
                self._should_resume = bool(data.get("running"))

            logger.info("State restored from persistence")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def _save_trades(self):
        try:
            data = {
                "trades": self.trades,
                "trade_plans": self.trade_plans,
                "positions": self.positions,
                "settlements": self.settlements[-200:],
                "active_alerts": self.active_alerts[-50:],
                "alert_history": self.alert_history[-500:],
                "confirmations": self.confirmations[-200:],
                "state_transitions": self.state_transitions[-500:],
            }
            trades_json = json.dumps(data, default=str)
            TRADES_FILE.write_text(trades_json)
            _gcs_write("chimera_scalp_trades.json", trades_json)
        except Exception as e:
            logger.warning(f"Failed to save trades: {e}")

    def _load_trades(self):
        try:
            raw = _gcs_read("chimera_scalp_trades.json")
            if raw:
                data = json.loads(raw)
            elif TRADES_FILE.exists():
                data = json.loads(TRADES_FILE.read_text())
            else:
                return
            self.trades = data.get("trades", {})
            self.trade_plans = data.get("trade_plans", {})
            self.positions = data.get("positions", {})
            self.settlements = data.get("settlements", [])
            self.active_alerts = data.get("active_alerts", [])
            self.alert_history = data.get("alert_history", [])
            self.confirmations = data.get("confirmations", [])
            self.state_transitions = data.get("state_transitions", [])
            logger.info(f"Loaded {len(self.trades)} trades, {len(self.active_alerts)} active alerts")
        except Exception as e:
            logger.warning(f"Failed to load trades: {e}")

    def _save_sessions(self):
        try:
            sessions_json = json.dumps(self.sessions[-200:], default=str)
            SESSIONS_FILE.write_text(sessions_json)
            _gcs_write("chimera_scalp_sessions.json", sessions_json)
        except Exception as e:
            logger.warning(f"Failed to save sessions: {e}")

    def _load_sessions(self):
        try:
            raw = _gcs_read("chimera_scalp_sessions.json")
            if raw:
                self.sessions = json.loads(raw)
            elif SESSIONS_FILE.exists():
                self.sessions = json.loads(SESSIONS_FILE.read_text())
            logger.info(f"Loaded {len(self.sessions)} sessions")
        except Exception as e:
            logger.warning(f"Failed to load sessions: {e}")

    def _load_api_keys(self):
        try:
            raw = _gcs_read("chimera_scalp_api_keys.json")
            if raw:
                self.api_keys = json.loads(raw)
            elif API_KEYS_FILE.exists():
                self.api_keys = json.loads(API_KEYS_FILE.read_text())
            logger.info(f"Loaded {len(self.api_keys)} API keys")
        except Exception as e:
            logger.warning(f"Failed to load API keys: {e}")

    def _save_api_keys(self):
        try:
            keys_json = json.dumps(self.api_keys, default=str)
            API_KEYS_FILE.write_text(keys_json)
            _gcs_write("chimera_scalp_api_keys.json", keys_json)
        except Exception as e:
            logger.warning(f"Failed to save API keys: {e}")

    # ──────────────────────────────────────────────
    #  AUTH
    # ──────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        return self.client is not None and self.client.session_token is not None

    def login(self, username: str, password: str) -> tuple[bool, str]:
        self.client = BetfairClient(
            app_key=BETFAIR_APP_KEY, username=username, password=password,
        )
        if self.client.login():
            self.balance = self.client.get_account_balance()
            self._username = username
            self._password = password
            # Initial market fetch
            try:
                self.markets = self.client.get_todays_win_markets(countries=self.countries)
                logger.info(f"Login OK — {len(self.markets)} markets fetched")
            except Exception as e:
                logger.warning(f"Initial market fetch failed: {e}")
            return True, ""
        error = self.client.last_login_error or "unknown"
        self.client = None
        return False, error

    def logout(self):
        self.stop()
        self.client = None
        self._username = None
        self._password = None

    # ──────────────────────────────────────────────
    #  ENGINE LIFECYCLE
    # ──────────────────────────────────────────────

    def start(self):
        if not self.is_authenticated:
            raise RuntimeError("Not authenticated")
        if self.running:
            return
        self.running = True
        self.status = "STARTING"

        now = datetime.now(timezone.utc)
        session_id = f"scalp_{now.strftime('%Y%m%d_%H%M%S')}"
        self.current_session = {
            "session_id": session_id,
            "mode": "DRY_RUN" if self.dry_run else "LIVE",
            "date": now.strftime("%Y-%m-%d"),
            "start_time": now.isoformat(),
            "stop_time": None,
            "status": "RUNNING",
            "summary": {"trades_created": 0, "alerts_raised": 0},
        }
        self.sessions.append(self.current_session)
        self._save_sessions()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Scalping engine started")

    def stop(self):
        self.running = False
        self.status = "STOPPED"
        if self.current_session:
            self.current_session["stop_time"] = datetime.now(timezone.utc).isoformat()
            self.current_session["status"] = "COMPLETED"
            self._save_sessions()
            self.current_session = None
        self._save_state()
        self._save_trades()
        logger.info("Scalping engine stopped")

    def _run_loop(self):
        if not self.client.ensure_session():
            self.status = "AUTH_FAILED"
            self.running = False
            return

        self.balance = self.client.get_account_balance()
        self.status = "RUNNING"
        logger.info(f"Engine running (DRY_RUN={self.dry_run}, POLL={POLL_INTERVAL}s)")

        scan_count = 0
        while self.running:
            try:
                self._scan_and_process()
                scan_count += 1
                if scan_count % 5 == 0:
                    self._save_state()
                    self._save_trades()
            except Exception as e:
                logger.error(f"Engine loop error: {e}")
                self.errors.append({
                    "message": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            time.sleep(POLL_INTERVAL)

    # ──────────────────────────────────────────────
    #  CORE LOOP
    # ──────────────────────────────────────────────

    def _scan_and_process(self):
        """
        Main scan cycle:
        1. Fetch markets from Betfair
        2. Score candidates within window
        3. Evaluate bookmaker triggers
        4. Manage active trades (ladder updates, invalidation, stops)
        5. Expire stale alerts
        """
        now = datetime.now(timezone.utc)
        self.last_scan = now.isoformat()
        self._purge_stale_trades()
        self._recompute_portfolio()

        if not self.client.ensure_session():
            return

        # Refresh markets
        self.markets = self.client.get_todays_win_markets(countries=self.countries)

        logger.info(
            f"Scan: {len(self.markets)} markets, "
            f"{len(self.trades)} active trades, "
            f"{len(self.active_alerts)} active alerts"
        )

        # Process each market
        for market in self.markets:
            market_id = market["market_id"]

            try:
                race_time = datetime.fromisoformat(
                    market["race_time"].replace("Z", "+00:00")
                )
            except (ValueError, KeyError):
                continue

            minutes_to_race = (race_time - now).total_seconds() / 60

            if minutes_to_race < 0:
                continue  # Past

            if minutes_to_race <= self.process_window:
                self._evaluate_market(market, minutes_to_race)

        # Manage active trades
        self._manage_active_trades()

        # Expire old alerts
        self._expire_alerts()

    def _evaluate_market(self, market: dict, minutes_to_off: float):
        """Score runners and check for trigger opportunities."""
        market_id = market["market_id"]

        # Get live prices
        try:
            runners_with_prices, is_valid = self.client.get_market_prices(market_id)
        except Exception:
            return

        if not is_valid or not runners_with_prices:
            return

        # Merge runner names
        name_map = {
            r["selection_id"]: r["runner_name"]
            for r in market.get("runners", [])
        }
        for runner in runners_with_prices:
            if runner.selection_id in name_map:
                runner.runner_name = name_map[runner.selection_id]

        # Sort by lay odds (lowest = favourite)
        active = [r for r in runners_with_prices
                  if r.status == "ACTIVE" and r.best_available_to_lay is not None]
        active.sort(key=lambda r: r.best_available_to_lay)

        if len(active) < 2:
            return

        # Score top-2 candidates
        for rank, runner in enumerate(active[:2], 1):
            score = self._score_candidate(
                runner, market, rank, active, minutes_to_off,
            )
            if score is None:
                continue

            # Qualify
            qualified, reason = qualify_candidate(score, self.qualification_config)
            score.qualified = qualified
            score.qualification_reason = reason

            if qualified:
                self._handle_qualified_candidate(score, market, runner, minutes_to_off)

    def _score_candidate(self, runner, market, rank, all_runners, minutes_to_off) -> Optional[CandidateScore]:
        """Build a CandidateScore for a runner."""
        try:
            lay = runner.best_available_to_lay or 0
            back = runner.best_available_to_back or 0
            spread_ticks = round(lay - back, 2) if back > 0 else 10.0

            # Compute sub-scores
            fav_persistence = compute_favourite_persistence(
                bookmaker_rank=rank,
                bookmaker_trend_1h=0,     # TODO: from FSU-1X history
                bookmaker_trend_3h=0,
                bookmaker_dispersion=0.15,  # TODO: from FSU-1X
                runner_count=len(all_runners),
            )

            first_lay = compute_first_lay_hit_prob(
                current_lay_price=lay,
                lay_target_price=lay * 0.97,  # Target 3% contraction
                spread_ticks=spread_ticks,
                traded_volume=0,  # TODO: from depth data
                minutes_to_off=minutes_to_off,
            )

            adverse = compute_adverse_open_risk(
                bookmaker_best_price=lay,  # Placeholder
                projected_exchange_open=lay,
                bookmaker_dispersion=0.15,
                bookmaker_trend_1h=0,
            )

            stop_survival = compute_stop_out_survival(
                planned_stake=25.0,
                entry_odds=lay,
                stop_loss_ticks=15,
                first_lay_hit_prob=first_lay,
            )

            liquidity = compute_liquidity_score(
                traded_volume=0,
                back_depth=0,
                lay_depth=0,
                spread_ticks=spread_ticks,
                minutes_to_off=minutes_to_off,
            )

            bk_consensus = compute_bookmaker_consensus(
                bookmaker_best_price=lay,
                bookmaker_median_price=lay,
                bookmaker_dispersion=0.15,
                num_bookmakers_offering=8,
            )

            score = CandidateScore(
                race_id=market.get("race_id", market["market_id"]),
                market_id=market["market_id"],
                selection_id=runner.selection_id,
                horse_name=runner.runner_name,
                race_type="FLAT",  # TODO: from FSU-1Y
                meeting=market.get("venue", ""),
                off_time_utc=market.get("race_time", ""),
                runner_count=len(all_runners),
                bookmaker_best_price=lay,
                favourite_persistence_score=fav_persistence,
                first_lay_hit_prob=first_lay,
                adverse_open_risk=adverse,
                stop_out_cost_score=stop_survival,
                liquidity_score=liquidity,
                open_confirmation_score=0.5,  # Placeholder
                bookmaker_consensus_score=bk_consensus,
                projected_rank_near_off=rank,
            )
            score.compute_quality_score()
            return score

        except Exception as e:
            logger.debug(f"Scoring failed for {runner.runner_name}: {e}")
            return None

    def _handle_qualified_candidate(self, score, market, runner, minutes_to_off):
        """Handle a qualified candidate — build plan, check risk, create trade."""
        market_id = market["market_id"]
        trade_id = f"trd_{uuid.uuid4().hex[:12]}"

        # Check if we already have a trade on this selection in this market
        for tid, t in self.trades.items():
            if (t.get("market_id") == market_id
                    and t.get("selection_id") == runner.selection_id
                    and t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")):
                return  # Already trading this runner

        entry_odds = runner.best_available_to_lay or 2.0

        # Build trade plan
        plan = build_trade_plan(
            trade_id=trade_id,
            race_id=score.race_id,
            market_id=market_id,
            selection_id=runner.selection_id,
            horse_name=runner.runner_name,
            quality_score=score.trade_quality_score,
            first_lay_hit_prob=score.first_lay_hit_prob,
            adverse_open_risk=score.adverse_open_risk,
            stop_out_cost_score=score.stop_out_cost_score,
            entry_odds=entry_odds,
            ladder_profile=self.ladder_profile,
            point_value=self.point_value,
        )

        # Run scenario engine
        lay_rungs = [(r.target_odds, r.target_stake) for r in plan.lay_ladder]
        envelope = simulate_scenarios(
            back_stake=plan.total_intended_stake,
            back_odds=entry_odds,
            lay_rungs=lay_rungs,
            entry_source=plan.entry_source,
        )

        # Check trade gate
        gate_ok, gate_reason = passes_trade_gate(envelope, self.trade_gate_config)
        if not gate_ok:
            logger.info(f"Trade gate BLOCKED {runner.runner_name}: {gate_reason}")
            return

        # Check risk engine
        risk_decision = evaluate_risk(
            market_id=market_id,
            sport="horse_racing",
            planned_max_loss=abs(envelope.no_lay.min_pnl),
            planned_liability=plan.total_intended_stake * (entry_odds - 1),
            first_rung_only_pnl=envelope.first_lay_only.min_pnl,
            portfolio=self.portfolio_state,
            config=self.risk_config,
        )

        if not risk_decision.allowed:
            logger.info(f"Risk BLOCKED {runner.runner_name}: {risk_decision.reasons}")
            return

        # ── CREATE TRADE ──
        trade = {
            "trade_id": trade_id,
            "market_id": market_id,
            "selection_id": runner.selection_id,
            "horse_name": runner.runner_name,
            "venue": market.get("venue", ""),
            "race_time": market.get("race_time", ""),
            "state": TradeState.PLANNED.value,
            "control_mode": ControlMode.AUTO.value,
            "quality_score": score.trade_quality_score,
            "entry_odds": entry_odds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scenario_envelope": envelope.to_dict(),
            "risk_decision": risk_decision.to_dict(),
        }

        self.trades[trade_id] = trade
        self.trade_plans[trade_id] = plan.to_dict()
        self.positions[trade_id] = PositionSnapshot(trade_id=trade_id).to_dict()

        # Log transition
        self.state_transitions.append(StateTransition(
            entity_type="trade", entity_id=trade_id,
            from_state="NONE", to_state=TradeState.PLANNED.value,
            reason=f"Qualified: score={score.trade_quality_score:.4f}",
        ).to_dict())

        logger.info(
            f"TRADE CREATED: {trade_id} — {runner.runner_name} @ {market.get('venue')} "
            f"score={score.trade_quality_score:.4f} tier={plan.stake_tier}"
        )

        if self.current_session:
            self.current_session["summary"]["trades_created"] = (
                self.current_session["summary"].get("trades_created", 0) + 1
            )

    def _manage_active_trades(self):
        """Manage ladder placement, invalidation, stops for active trades."""
        # Phase 1: log management cycles. Full ladder automation in Phase 2.
        for trade_id, trade in list(self.trades.items()):
            state = trade.get("state")
            if state in ("SETTLED", "CANCELLED", "ERROR"):
                continue

            # TODO Phase 2: Execute staged entry, place lay rungs,
            # check invalidation, manage stops, flatten near off

    def _expire_alerts(self):
        """Remove expired bookmaker trigger alerts."""
        now = datetime.now(timezone.utc).isoformat()
        still_active = []
        for alert in self.active_alerts:
            if alert.get("expires_at", "") < now:
                alert["status"] = "EXPIRED"
                self.alert_history.append(alert)
            else:
                still_active.append(alert)
        self.active_alerts = still_active

    # ──────────────────────────────────────────────
    #  MANUAL OVERRIDE (A.13, B.16)
    # ──────────────────────────────────────────────

    def set_trade_control(
        self, trade_id: str, mode: str, user_id: str = "operator", reason: str = "",
    ) -> tuple[bool, str]:
        """Change trade control mode. Returns (success, error_msg)."""
        trade = self.trades.get(trade_id)
        if not trade:
            return False, "Trade not found"

        try:
            current = ControlMode(trade["control_mode"])
            target = ControlMode(mode)
        except ValueError:
            return False, f"Invalid control mode: {mode}"

        if not validate_control_transition(current, target):
            return False, f"Cannot transition from {current.value} to {target.value}"

        trade["control_mode"] = target.value
        self.state_transitions.append(StateTransition(
            entity_type="control", entity_id=trade_id,
            from_state=current.value, to_state=target.value,
            reason=reason, user_id=user_id,
        ).to_dict())

        self._save_trades()
        return True, ""

    def flatten_trade(self, trade_id: str, user_id: str = "operator") -> tuple[bool, str]:
        """Immediately flatten a trade — cancel all pending, close position."""
        trade = self.trades.get(trade_id)
        if not trade:
            return False, "Trade not found"

        trade["state"] = TradeState.STOPPING_OUT.value
        self.state_transitions.append(StateTransition(
            entity_type="trade", entity_id=trade_id,
            from_state=trade.get("state", "?"),
            to_state=TradeState.STOPPING_OUT.value,
            reason="Manual flatten", user_id=user_id,
        ).to_dict())

        # TODO Phase 2: Cancel outstanding orders, place closing orders
        self._save_trades()
        return True, ""

    def confirm_external_bet(self, confirmation: dict) -> tuple[bool, str]:
        """Register a trader-confirmed bookmaker bet (C.14/C.15)."""
        alert_id = confirmation.get("alert_id", "")
        # Find the alert
        alert = None
        for a in self.active_alerts:
            if a.get("alert_id") == alert_id:
                alert = a
                break

        if not alert:
            return False, f"Alert {alert_id} not found or expired"

        # Record confirmation
        conf = ExternalBetConfirmation(
            alert_id=alert_id,
            trader_id=confirmation.get("trader_id", "operator"),
            bookmaker=confirmation.get("bookmaker", alert.get("bookmaker", "")),
            runner_name=alert.get("runner_name", ""),
            stake=confirmation.get("stake", 0),
            odds=confirmation.get("odds", 0),
            placed_at=datetime.now(timezone.utc).isoformat(),
            bookmaker_reference=confirmation.get("bookmaker_reference", ""),
            notes=confirmation.get("notes", ""),
        )
        self.confirmations.append(conf.to_dict())

        # Mark alert confirmed
        alert["status"] = "CONFIRMED"
        self.alert_history.append(alert)
        self.active_alerts = [a for a in self.active_alerts if a.get("alert_id") != alert_id]

        # TODO: Create trade from external bet, activate lay ladder

        self._save_trades()
        return True, ""

    # ──────────────────────────────────────────────
    #  API KEY MANAGEMENT (from lay engine)
    # ──────────────────────────────────────────────

    def issue_ui_session(self, hours: int = 12) -> str:
        """Mint a browser session token after a successful Betfair login."""
        now = datetime.now(timezone.utc)
        token = f"ses_{secrets.token_hex(24)}"
        self.ui_sessions[token] = {
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=hours)).isoformat(),
        }
        self._prune_ui_sessions()
        self._save_state()
        return token

    def validate_ui_session(self, token: str) -> bool:
        rec = self.ui_sessions.get(token)
        if not rec:
            return False
        try:
            expires = datetime.fromisoformat(rec["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        if expires <= datetime.now(timezone.utc):
            self.ui_sessions.pop(token, None)
            return False
        return True

    def revoke_ui_session(self, token: str):
        self.ui_sessions.pop(token, None)
        self._save_state()

    def _prune_ui_sessions(self):
        now = datetime.now(timezone.utc)
        for tok in list(self.ui_sessions):
            try:
                exp = datetime.fromisoformat(self.ui_sessions[tok]["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now:
                    del self.ui_sessions[tok]
            except Exception:
                del self.ui_sessions[tok]

    def generate_api_key(self, label: str = "") -> dict:
        key = f"scp_{secrets.token_hex(24)}"
        record = {
            "key_id": secrets.token_hex(8),
            "key": key,
            "label": label or "Untitled",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used": None,
        }
        self.api_keys.append(record)
        self._save_api_keys()
        return record

    def validate_api_key(self, key: str) -> bool:
        for k in self.api_keys:
            if k["key"] == key:
                k["last_used"] = datetime.now(timezone.utc).isoformat()
                return True
        return False

    # ──────────────────────────────────────────────
    #  STATE GETTERS
    # ──────────────────────────────────────────────

    def _session_snapshot(self) -> Optional[dict]:
        """Serialise the live Betfair session so it can survive a cold start.

        Only the short-lived session token is persisted — never the password.
        """
        if not (self.client and self.client.session_token):
            return None
        expiry = self.client.session_expiry
        return {
            "token": self.client.session_token,
            "expiry": expiry.isoformat() if expiry else None,
        }

    def _restore_session(self, snapshot: Optional[dict]) -> bool:
        """Rebuild a BetfairClient from a persisted session token.

        Cloud Run scales to zero, so without this every cold start silently
        de-authenticates the engine and /api/engine/start returns 401.
        """
        if not snapshot or not snapshot.get("token"):
            return False
        try:
            raw = snapshot.get("expiry")
            expiry = datetime.fromisoformat(raw) if raw else None
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if not expiry or expiry <= datetime.now(timezone.utc):
                logger.info("Persisted Betfair session expired — login required")
                return False
            client = BetfairClient(app_key=BETFAIR_APP_KEY, username="", password="")
            client.session_token = snapshot["token"]
            client.session_expiry = expiry
            self.client = client
            logger.info(f"Betfair session restored (expires {expiry.isoformat()})")
            return True
        except Exception as e:
            logger.warning(f"Failed to restore Betfair session: {e}")
            return False

    @staticmethod
    def _parse_dt(raw) -> Optional[datetime]:
        """Parse an ISO timestamp, assuming UTC when no zone is given."""
        if not raw:
            return None
        try:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _purge_stale_trades(self) -> int:
        """Drop trades that can no longer do anything.

        _load_trades has no day filter, so without this the active list grows
        without bound: yesterday's PLANNED trades for races that have long
        since run keep showing in the dashboard as though they were live, and
        inflate the active-trade count.

        Anything still holding exposure is kept regardless of age — a stuck
        position is exactly what an operator needs to see.
        """
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            closed = {"SETTLED", "CANCELLED", "ERROR"}
            drop = []

            for trade_id, trade in self.trades.items():
                state = trade.get("state")
                if state == "PLANNED":
                    # Never committed money, and the race has already run
                    race_time = self._parse_dt(trade.get("race_time"))
                    if race_time and race_time < now:
                        drop.append(trade_id)
                elif state in closed:
                    created = str(trade.get("created_at") or "")[:10]
                    if created and created != today:
                        drop.append(trade_id)

            for trade_id in drop:
                self.trades.pop(trade_id, None)
                self.trade_plans.pop(trade_id, None)
                self.positions.pop(trade_id, None)

            if drop:
                logger.info(f"Purged {len(drop)} stale trades ({len(self.trades)} remain)")
            return len(drop)
        except Exception as e:
            logger.warning(f"Trade purge failed: {e}")
            return 0

    def _recompute_portfolio(self):
        """Rebuild portfolio_state from live trades and positions (B.13 Level 4).

        portfolio_state was previously only ever read, never written, so every
        Level-4 check (daily drawdown, rolling loss, capital utilisation)
        evaluated against zeros and could never fire.

        NOTE: settlements are never recorded by the engine, so rolling P&L is
        derived from per-trade realised P&L rather than a true N-day window.
        """
        try:
            closed = {"SETTLED", "CANCELLED", "ERROR"}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            p = PortfolioState()

            for trade_id, trade in self.trades.items():
                pos = self.positions.get(trade_id) or {}
                realised = float(pos.get("realized_pnl") or 0.0)
                p.rolling_pnl += realised
                if str(trade.get("created_at") or "")[:10] == today:
                    p.daily_realised_pnl += realised

                if trade.get("state") in closed:
                    continue

                exposure = float(pos.get("max_open_loss") or 0.0)
                p.total_open_trades += 1
                p.total_exposure += exposure
                p.daily_unrealised_pnl += float(pos.get("unrealized_pnl") or 0.0)
                if float(pos.get("hedge_completion_pct") or 0.0) < 100.0:
                    p.total_unhedged += exposure

                market_id = trade.get("market_id") or "unknown"
                p.exposure_by_market[market_id] = (
                    p.exposure_by_market.get(market_id, 0.0) + exposure
                )
                p.trades_by_market[market_id] = (
                    p.trades_by_market.get(market_id, 0) + 1
                )
                sport = trade.get("sport") or "HORSE_RACING"
                p.exposure_by_sport[sport] = (
                    p.exposure_by_sport.get(sport, 0.0) + exposure
                )

            self.portfolio_state = p
        except Exception as e:
            logger.warning(f"Portfolio recompute failed: {e}")

    def get_state(self) -> dict:
        """Full engine state for the dashboard."""
        self._recompute_portfolio()
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "balance": self.balance,
            "countries": self.countries,
            "process_window": self.process_window,
            "point_value": self.point_value,
            "ladder_profile": self.ladder_profile,
            "markets_count": len(self.markets),
            "active_trades": len([
                t for t in self.trades.values()
                if t.get("state") not in ("SETTLED", "CANCELLED", "ERROR")
            ]),
            "active_alerts": len(self.active_alerts),
            "last_scan": self.last_scan,
            "day_started": self.day_started,
            "errors": self.errors[-10:],
            "risk_config": self.risk_config.to_dict(),
            "portfolio": self.portfolio_state.to_dict(),
        }
