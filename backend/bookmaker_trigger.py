"""
CHIMERA Scalping Engine — Bookmaker Trigger Extension (Part C)
================================================================
Real-time bookmaker/exchange trigger engine. Sits upstream of the core
strategy. Identifies favourable moments to place a bookmaker win bet
before the market fully adjusts.

Trigger Families (C.10):
  A. Near-Level — bookmaker ≈ exchange back (within 2%)
  B. Bookmaker Premium — bookmaker > exchange back by meaningful margin
  C. Exchange Momentum + Bookmaker Stickiness — exchange shortening, bookmaker static
  D. Consensus Stability — multiple books agree, one/two stale, exchange confirms

Scoring (C.11):
  overallSignalScore =
    0.22 * spreadScore +
    0.18 * exchangeMomentumScore +
    0.16 * bookmakerStickinessScore +
    0.16 * marketPrincipalScore +
    0.10 * preRaceTimingScore +
    0.10 * liquidityScore +
    0.08 * layabilityScore

Layability (C.12):
  A bookmaker entry should not trigger just because the price looks good.
  Must pass layability test: can the ladder engine trade out safely?
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
import uuid


# ─── Scoring Weights (C.11) ─────────────────────────────────────────────────

TRIGGER_SCORE_WEIGHTS = {
    "spread": 0.22,
    "exchange_momentum": 0.18,
    "bookmaker_stickiness": 0.16,
    "market_principal": 0.16,
    "pre_race_timing": 0.10,
    "liquidity": 0.10,
    "layability": 0.08,
}


@dataclass
class TriggerSignalScore:
    """Signal quality score for a bookmaker trigger (C.11)."""
    spread_score: float = 0.0
    exchange_momentum_score: float = 0.0
    bookmaker_stickiness_score: float = 0.0
    market_principal_score: float = 0.0
    pre_race_timing_score: float = 0.0
    liquidity_score: float = 0.0
    layability_score: float = 0.0
    overall_signal_score: float = 0.0

    def compute(self) -> float:
        w = TRIGGER_SCORE_WEIGHTS
        self.overall_signal_score = round(
            w["spread"] * self.spread_score +
            w["exchange_momentum"] * self.exchange_momentum_score +
            w["bookmaker_stickiness"] * self.bookmaker_stickiness_score +
            w["market_principal"] * self.market_principal_score +
            w["pre_race_timing"] * self.pre_race_timing_score +
            w["liquidity"] * self.liquidity_score +
            w["layability"] * self.layability_score,
            4,
        )
        return self.overall_signal_score

    def to_dict(self) -> dict:
        return {
            "spread_score": self.spread_score,
            "exchange_momentum_score": self.exchange_momentum_score,
            "bookmaker_stickiness_score": self.bookmaker_stickiness_score,
            "market_principal_score": self.market_principal_score,
            "pre_race_timing_score": self.pre_race_timing_score,
            "liquidity_score": self.liquidity_score,
            "layability_score": self.layability_score,
            "overall_signal_score": self.overall_signal_score,
        }


@dataclass
class ExternalEntryAlert:
    """Trader notification for a bookmaker trigger opportunity (C.13)."""
    alert_id: str = field(default_factory=lambda: f"alert_{uuid.uuid4().hex[:12]}")
    race_id: str = ""
    market_id: str = ""
    selection_id: int = 0
    runner_name: str = ""
    bookmaker: str = ""
    bookmaker_price: float = 0.0
    exchange_back: float = 0.0
    exchange_lay: float = 0.0
    relative_spread_pct: float = 0.0
    exchange_momentum: float = 0.0
    trigger_family: str = ""          # A / B / C / D
    score: float = 0.0
    score_detail: TriggerSignalScore = field(default_factory=TriggerSignalScore)
    confidence_band: str = "LOW"      # LOW / MEDIUM / HIGH / PRIORITY
    recommended_stake: float = 0.0
    recommended_action: str = "MONITOR"   # PLACE_BACK_NOW / MONITOR / IGNORE
    expires_at: str = ""
    status: str = "ACTIVE"            # ACTIVE / EXPIRED / CONFIRMED / DISMISSED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "race_id": self.race_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "runner_name": self.runner_name,
            "bookmaker": self.bookmaker,
            "bookmaker_price": self.bookmaker_price,
            "exchange_back": self.exchange_back,
            "exchange_lay": self.exchange_lay,
            "relative_spread_pct": round(self.relative_spread_pct, 4),
            "exchange_momentum": self.exchange_momentum,
            "trigger_family": self.trigger_family,
            "score": self.score,
            "score_detail": self.score_detail.to_dict(),
            "confidence_band": self.confidence_band,
            "recommended_stake": self.recommended_stake,
            "recommended_action": self.recommended_action,
            "expires_at": self.expires_at,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class ExternalBetConfirmation:
    """Trader confirmation of a bookmaker bet placement (C.14)."""
    confirmation_id: str = field(default_factory=lambda: f"conf_{uuid.uuid4().hex[:12]}")
    alert_id: str = ""
    trader_id: str = "operator"
    bookmaker: str = ""
    runner_name: str = ""
    stake: float = 0.0
    odds: float = 0.0
    placed_at: str = ""
    source: str = "MANUAL_BOOKMAKER"
    bookmaker_reference: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "confirmation_id": self.confirmation_id,
            "alert_id": self.alert_id,
            "trader_id": self.trader_id,
            "bookmaker": self.bookmaker,
            "runner_name": self.runner_name,
            "stake": self.stake,
            "odds": self.odds,
            "placed_at": self.placed_at,
            "source": self.source,
            "bookmaker_reference": self.bookmaker_reference,
            "notes": self.notes,
        }


@dataclass
class TriggerConfig:
    """Configurable thresholds for the trigger engine."""
    # Trigger Family A: Near-Level
    near_level_max_spread_pct: float = 0.02      # 2%
    near_level_min_momentum: float = 0.30

    # Trigger Family B: Bookmaker Premium
    premium_min_pct: float = 0.03                 # Bookmaker 3%+ better
    premium_min_principal_score: float = 0.50

    # Trigger Family C: Momentum + Stickiness
    momentum_shortening_threshold: float = 0.02   # 2% exchange shortening
    stickiness_unchanged_secs: int = 120           # Bookmaker static for 2+ min
    momentum_max_outer_spread: float = 0.05

    # Trigger Family D: Consensus
    consensus_min_bookmakers: int = 5
    consensus_max_stale_firms: int = 2

    # Layability
    min_layability_score: float = 0.30

    # Qualification
    max_projected_rank: int = 2
    min_timing_minutes: float = 5.0
    max_timing_minutes: float = 120.0
    min_liquidity_score: float = 0.20
    min_quality_score: float = 0.40

    # Notification expiry (C.16)
    alert_expiry_secs: int = 45


# ─── Trigger Evaluation ─────────────────────────────────────────────────────

def _compute_spread_score(bookmaker_price: float, exchange_back: float) -> float:
    """Score how close bookmaker is to exchange."""
    if exchange_back <= 1.0:
        return 0.0
    spread = abs(bookmaker_price - exchange_back) / exchange_back
    if spread <= 0.005:
        return 1.0
    elif spread <= 0.01:
        return 0.85
    elif spread <= 0.02:
        return 0.65
    elif spread <= 0.03:
        return 0.40
    elif spread <= 0.05:
        return 0.20
    return 0.0


def _compute_momentum_score(
    exchange_price_history: list,  # [(timestamp, price), ...] newest last
    window_secs: float = 300,
) -> float:
    """Score exchange momentum (shortening velocity)."""
    if len(exchange_price_history) < 2:
        return 0.0

    oldest_price = exchange_price_history[0][1]
    newest_price = exchange_price_history[-1][1]

    if oldest_price <= 1.0:
        return 0.0

    change_pct = (oldest_price - newest_price) / oldest_price  # Positive = shortening

    if change_pct >= 0.05:
        return 1.0
    elif change_pct >= 0.03:
        return 0.80
    elif change_pct >= 0.02:
        return 0.60
    elif change_pct >= 0.01:
        return 0.35
    elif change_pct > 0:
        return 0.15
    return 0.0


def _compute_stickiness_score(
    bookmaker_last_change_secs: float,
    exchange_change_pct: float,
) -> float:
    """Score how sticky (slow to reprice) the bookmaker is."""
    if bookmaker_last_change_secs > 300 and exchange_change_pct > 0.02:
        return 1.0
    elif bookmaker_last_change_secs > 180 and exchange_change_pct > 0.01:
        return 0.75
    elif bookmaker_last_change_secs > 120:
        return 0.50
    elif bookmaker_last_change_secs > 60:
        return 0.25
    return 0.0


def _compute_timing_score(minutes_to_off: float, config: TriggerConfig) -> float:
    """Score the pre-race timing window."""
    if minutes_to_off < config.min_timing_minutes:
        return 0.2   # Too close to off
    if minutes_to_off > config.max_timing_minutes:
        return 0.3   # Too far out
    # Sweet spot: 15-60 minutes
    if 15 <= minutes_to_off <= 60:
        return 1.0
    elif 10 <= minutes_to_off <= 90:
        return 0.75
    return 0.50


def _classify_confidence(score: float) -> str:
    """Map overall signal score to confidence band (C.19)."""
    if score >= 0.80:
        return "PRIORITY"
    elif score >= 0.65:
        return "HIGH"
    elif score >= 0.45:
        return "MEDIUM"
    return "LOW"


def evaluate_trigger(
    runner_name: str,
    race_id: str,
    market_id: str,
    selection_id: int,
    bookmaker: str,
    bookmaker_price: float,
    exchange_back: float,
    exchange_lay: float,
    exchange_price_history: list,
    bookmaker_last_change_secs: float,
    projected_rank: int,
    minutes_to_off: float,
    liquidity_score: float,
    layability_score: float,
    market_principal_score: float,
    config: TriggerConfig,
) -> Optional[ExternalEntryAlert]:
    """
    Evaluate all trigger families against a single runner/bookmaker combination.
    Returns an ExternalEntryAlert if any trigger fires, else None.

    Base Qualification (C.18):
      - Rank projected 1 or 2
      - Race inside timing window
      - Liquidity above threshold
      - Quality score above threshold
    """
    # ── Base qualification ──
    if projected_rank > config.max_projected_rank:
        return None
    if minutes_to_off < config.min_timing_minutes or minutes_to_off > config.max_timing_minutes:
        return None
    if liquidity_score < config.min_liquidity_score:
        return None
    if layability_score < config.min_layability_score:
        return None

    # ── Compute relative spread ──
    if exchange_back <= 1.0:
        return None
    relative_spread = abs(bookmaker_price - exchange_back) / exchange_back
    bookmaker_premium = (bookmaker_price - exchange_back) / exchange_back  # Positive = bookmaker better

    # ── Compute exchange momentum ──
    momentum_score = _compute_momentum_score(exchange_price_history)
    exchange_change_pct = 0.0
    if len(exchange_price_history) >= 2:
        exchange_change_pct = (
            (exchange_price_history[0][1] - exchange_price_history[-1][1])
            / exchange_price_history[0][1]
        ) if exchange_price_history[0][1] > 1.0 else 0.0

    # ── Check trigger families ──
    trigger_family = None

    # Family A: Near-Level
    if relative_spread <= config.near_level_max_spread_pct and momentum_score >= config.near_level_min_momentum:
        trigger_family = "A"

    # Family B: Bookmaker Premium
    elif bookmaker_premium >= config.premium_min_pct and market_principal_score >= config.premium_min_principal_score:
        trigger_family = "B"

    # Family C: Momentum + Stickiness
    elif (exchange_change_pct >= config.momentum_shortening_threshold
          and bookmaker_last_change_secs >= config.stickiness_unchanged_secs
          and relative_spread <= config.momentum_max_outer_spread):
        trigger_family = "C"

    # Family D: Consensus (simplified — needs multi-bookmaker data)
    # Would check: multiple bookmakers aligned, 1-2 stale, exchange confirms
    # For Phase 1, D fires when A or B conditions are met AND stickiness is high
    elif (relative_spread <= 0.03 and bookmaker_last_change_secs >= 180
          and momentum_score >= 0.40):
        trigger_family = "D"

    if trigger_family is None:
        return None

    # ── Compute full signal score ──
    scores = TriggerSignalScore(
        spread_score=_compute_spread_score(bookmaker_price, exchange_back),
        exchange_momentum_score=momentum_score,
        bookmaker_stickiness_score=_compute_stickiness_score(bookmaker_last_change_secs, exchange_change_pct),
        market_principal_score=market_principal_score,
        pre_race_timing_score=_compute_timing_score(minutes_to_off, config),
        liquidity_score=liquidity_score,
        layability_score=layability_score,
    )
    overall = scores.compute()

    if overall < config.min_quality_score:
        return None

    confidence = _classify_confidence(overall)

    # ── Recommended action ──
    if confidence in ("PRIORITY", "HIGH"):
        action = "PLACE_BACK_NOW"
    elif confidence == "MEDIUM":
        action = "MONITOR"
    else:
        action = "IGNORE"

    # ── Build alert ──
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=config.alert_expiry_secs)

    return ExternalEntryAlert(
        race_id=race_id,
        market_id=market_id,
        selection_id=selection_id,
        runner_name=runner_name,
        bookmaker=bookmaker,
        bookmaker_price=bookmaker_price,
        exchange_back=exchange_back,
        exchange_lay=exchange_lay,
        relative_spread_pct=relative_spread,
        exchange_momentum=exchange_change_pct,
        trigger_family=trigger_family,
        score=overall,
        score_detail=scores,
        confidence_band=confidence,
        recommended_action=action,
        expires_at=expires.isoformat(),
    )
