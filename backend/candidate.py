"""
CHIMERA Scalping Engine — Candidate Scoring (A.6)
===================================================
Composite pre-trade quality score for runners.

tradeQualityScore =
    0.30 * favouritePersistenceScore +
    0.25 * firstLayHitProb +
    0.15 * openConfirmationScore +
    0.10 * liquidityScore +
    0.10 * stopOutSurvivalScore +
    0.10 * bookmakerConsensusScore

Required Sub-Models (A.6):
  A. Favourite persistence — prob runner is fav at open, top-2 at 60m, top-2 at 10m
  B. First-lay hit — prob first lay target will be touched before cutoff
  C. Adverse-open — prob exchange opens worse than bookmaker signal by X ticks / Y%
  D. Stop-out cost — expected realised loss if trade invalidated before de-risk
  E. Fill-quality — likelihood of enough matched at rung 1 without deeper rungs

Universe filter (A.2):
  - Projected favourite or strong second favourite
  - Sufficiently liquid markets
  - Race segments supported by data
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


# ─── Weights from A.6 ────────────────────────────────────────────────────────

SCORE_WEIGHTS = {
    "favourite_persistence": 0.30,
    "first_lay_hit": 0.25,
    "open_confirmation": 0.15,
    "liquidity": 0.10,
    "stop_out_survival": 0.10,
    "bookmaker_consensus": 0.10,
}


@dataclass
class CandidateScore:
    """Composite pre-trade quality score for a runner."""
    race_id: str
    market_id: str
    selection_id: int
    horse_name: str
    race_type: str            # FLAT / AW / JUMPS
    meeting: str
    off_time_utc: str
    runner_count: int

    # ── Bookmaker data (from FSU-1X / The Odds API) ──
    bookmaker_best_price: float = 0.0
    bookmaker_median_price: float = 0.0
    bookmaker_dispersion: float = 0.0    # std dev of prices across books
    bookmaker_trend_1h: float = 0.0      # negative = shortening
    bookmaker_trend_3h: float = 0.0
    bookmaker_trend_12h: float = 0.0

    # ── Exchange data (from Betfair) ──
    projected_exchange_open: Optional[float] = None
    projected_near_off: Optional[float] = None
    projected_rank_near_off: Optional[int] = None

    # ── Sub-model scores (0.0 to 1.0) ──
    favourite_persistence_score: float = 0.0
    likely_fav_at_off_score: float = 0.0
    first_lay_hit_prob: float = 0.0
    adverse_open_risk: float = 0.0        # Higher = worse
    stop_out_cost_score: float = 0.0      # Higher = better (lower cost)
    liquidity_score: float = 0.0
    open_confirmation_score: float = 0.0
    bookmaker_consensus_score: float = 0.0

    # ── Derived ──
    trade_quality_score: float = 0.0
    qualified: bool = False
    qualification_reason: str = ""

    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_quality_score(self) -> float:
        """Compute the composite trade quality score per A.6 formula."""
        self.trade_quality_score = round(
            SCORE_WEIGHTS["favourite_persistence"] * self.favourite_persistence_score +
            SCORE_WEIGHTS["first_lay_hit"] * self.first_lay_hit_prob +
            SCORE_WEIGHTS["open_confirmation"] * self.open_confirmation_score +
            SCORE_WEIGHTS["liquidity"] * self.liquidity_score +
            SCORE_WEIGHTS["stop_out_survival"] * self.stop_out_cost_score +
            SCORE_WEIGHTS["bookmaker_consensus"] * self.bookmaker_consensus_score,
            4,
        )
        return self.trade_quality_score

    def to_dict(self) -> dict:
        return {
            "race_id": self.race_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "horse_name": self.horse_name,
            "race_type": self.race_type,
            "meeting": self.meeting,
            "off_time_utc": self.off_time_utc,
            "runner_count": self.runner_count,
            "bookmaker_best_price": self.bookmaker_best_price,
            "bookmaker_median_price": self.bookmaker_median_price,
            "bookmaker_dispersion": self.bookmaker_dispersion,
            "bookmaker_trend_1h": self.bookmaker_trend_1h,
            "projected_exchange_open": self.projected_exchange_open,
            "projected_rank_near_off": self.projected_rank_near_off,
            "scores": {
                "favourite_persistence": self.favourite_persistence_score,
                "first_lay_hit_prob": self.first_lay_hit_prob,
                "open_confirmation": self.open_confirmation_score,
                "liquidity": self.liquidity_score,
                "stop_out_survival": self.stop_out_cost_score,
                "bookmaker_consensus": self.bookmaker_consensus_score,
                "adverse_open_risk": self.adverse_open_risk,
            },
            "trade_quality_score": self.trade_quality_score,
            "qualified": self.qualified,
            "qualification_reason": self.qualification_reason,
            "created_at": self.created_at,
        }


# ─── Sub-Model A: Favourite Persistence ──────────────────────────────────────

def compute_favourite_persistence(
    bookmaker_rank: int,
    bookmaker_trend_1h: float,
    bookmaker_trend_3h: float,
    bookmaker_dispersion: float,
    runner_count: int,
) -> float:
    """
    Predict probability the runner remains favourite/top-2 through to race time.

    Phase 1: Heuristic model. Future: ML on historical bookmaker→exchange transitions.

    Signals:
      - Current bookmaker rank (1 = favourite)
      - Price trend (shortening = positive signal)
      - Low cross-book dispersion = strong consensus
      - Smaller fields = less volatility
    """
    score = 0.0

    # Rank contribution (ranked 1 = strong, 2 = moderate, 3+ = weak)
    if bookmaker_rank == 1:
        score += 0.50
    elif bookmaker_rank == 2:
        score += 0.30
    else:
        score += 0.10

    # Trend contribution (shortening over 1h and 3h = positive)
    # bookmaker_trend is negative when shortening (price dropping)
    if bookmaker_trend_1h < -0.05:
        score += 0.15   # Strong shortening
    elif bookmaker_trend_1h < 0:
        score += 0.08   # Mild shortening
    elif bookmaker_trend_1h > 0.05:
        score -= 0.10   # Drifting

    if bookmaker_trend_3h < -0.05:
        score += 0.10

    # Consensus (low dispersion = books agree)
    if bookmaker_dispersion < 0.10:
        score += 0.15   # Very tight consensus
    elif bookmaker_dispersion < 0.25:
        score += 0.08
    elif bookmaker_dispersion > 0.50:
        score -= 0.10   # Books disagree significantly

    # Field size (smaller = less upset probability)
    if runner_count <= 8:
        score += 0.10
    elif runner_count <= 12:
        score += 0.05
    elif runner_count > 16:
        score -= 0.05

    return max(0.0, min(1.0, score))


# ─── Sub-Model B: First-Lay Hit Probability ─────────────────────────────────

def compute_first_lay_hit_prob(
    current_lay_price: Optional[float],
    lay_target_price: float,
    spread_ticks: float,
    traded_volume: float,
    minutes_to_off: float,
) -> float:
    """
    Predict probability the first lay rung will be touched and filled.

    Phase 1: Heuristic based on price gap, spread, volume, and time.
    Future: ML on historical ladder fill rates.
    """
    if current_lay_price is None:
        return 0.0

    score = 0.0

    # Price gap: how far is target from current? Closer = higher prob
    gap_pct = abs(lay_target_price - current_lay_price) / current_lay_price if current_lay_price > 1 else 1.0
    if gap_pct < 0.02:
        score += 0.40    # Very close to touch
    elif gap_pct < 0.05:
        score += 0.30
    elif gap_pct < 0.10:
        score += 0.20
    else:
        score += 0.05    # Far away

    # Spread quality (tight = liquid = fills more likely)
    if spread_ticks <= 1:
        score += 0.20
    elif spread_ticks <= 2:
        score += 0.12
    elif spread_ticks <= 3:
        score += 0.05

    # Volume (higher = more active market)
    if traded_volume > 50000:
        score += 0.20
    elif traded_volume > 10000:
        score += 0.12
    elif traded_volume > 2000:
        score += 0.05

    # Time to off (more time = more price movement opportunity)
    if minutes_to_off > 30:
        score += 0.15
    elif minutes_to_off > 10:
        score += 0.10
    elif minutes_to_off > 3:
        score += 0.05

    return max(0.0, min(1.0, score))


# ─── Sub-Model C: Adverse-Open Risk ─────────────────────────────────────────

def compute_adverse_open_risk(
    bookmaker_best_price: float,
    projected_exchange_open: Optional[float],
    bookmaker_dispersion: float,
    bookmaker_trend_1h: float,
) -> float:
    """
    Predict probability the exchange opens materially worse than bookmaker signal.

    Returns 0-1 where HIGHER = MORE RISK (worse).
    For the quality score, we invert this: stop_out_survival = 1 - adverse_open_risk.
    """
    if projected_exchange_open is None:
        return 0.50   # Unknown — moderate risk assumed

    risk = 0.0

    # Exchange projected higher than bookmaker = drift risk
    if projected_exchange_open > bookmaker_best_price:
        drift_pct = (projected_exchange_open - bookmaker_best_price) / bookmaker_best_price
        if drift_pct > 0.10:
            risk += 0.50   # Major adverse opening
        elif drift_pct > 0.05:
            risk += 0.30
        elif drift_pct > 0.02:
            risk += 0.15

    # High bookmaker dispersion = uncertainty about true price
    if bookmaker_dispersion > 0.40:
        risk += 0.20
    elif bookmaker_dispersion > 0.25:
        risk += 0.10

    # Drifting trend = market moving against us
    if bookmaker_trend_1h > 0.05:
        risk += 0.20
    elif bookmaker_trend_1h > 0.02:
        risk += 0.10

    return max(0.0, min(1.0, risk))


# ─── Sub-Model D: Stop-Out Cost ─────────────────────────────────────────────

def compute_stop_out_survival(
    planned_stake: float,
    entry_odds: float,
    stop_loss_ticks: int,
    first_lay_hit_prob: float,
) -> float:
    """
    Score how survivable a stop-out would be.

    Higher score = lower expected stop-out cost relative to stake.
    Uses first-lay hit prob as a modifier: if we're likely to get hedged
    quickly, stop-out cost is much lower.
    """
    if planned_stake <= 0 or entry_odds <= 1.0:
        return 0.5

    # Worst case loss if stopped before any lay
    max_loss = planned_stake * (entry_odds - 1) * (stop_loss_ticks * 0.01)

    # Normalise against stake
    loss_ratio = max_loss / planned_stake if planned_stake > 0 else 1.0

    # Base survival score (lower loss ratio = higher score)
    if loss_ratio < 0.10:
        base = 0.85
    elif loss_ratio < 0.25:
        base = 0.65
    elif loss_ratio < 0.50:
        base = 0.40
    else:
        base = 0.15

    # Boost by first-lay hit prob (if we hedge quickly, stop-out is less painful)
    adjusted = base * 0.6 + first_lay_hit_prob * 0.4

    return max(0.0, min(1.0, adjusted))


# ─── Sub-Model E: Liquidity Score ───────────────────────────────────────────

def compute_liquidity_score(
    traded_volume: float,
    back_depth: float,
    lay_depth: float,
    spread_ticks: float,
    minutes_to_off: float,
) -> float:
    """
    Score the market's liquidity for our purposes.

    We need enough depth on both sides to enter and exit.
    Time-adjusted: less liquid markets close to the off are worse.
    """
    score = 0.0

    # Volume
    if traded_volume > 100000:
        score += 0.30
    elif traded_volume > 30000:
        score += 0.20
    elif traded_volume > 5000:
        score += 0.10

    # Depth at top of book
    total_depth = back_depth + lay_depth
    if total_depth > 5000:
        score += 0.25
    elif total_depth > 1000:
        score += 0.15
    elif total_depth > 200:
        score += 0.08

    # Spread
    if spread_ticks <= 1:
        score += 0.25
    elif spread_ticks <= 2:
        score += 0.15
    elif spread_ticks <= 3:
        score += 0.08

    # Time penalty (illiquid close to off = very bad)
    if minutes_to_off < 5 and traded_volume < 5000:
        score -= 0.20
    elif minutes_to_off < 2:
        score -= 0.10

    return max(0.0, min(1.0, score))


# ─── Sub-Model F: Bookmaker Consensus ───────────────────────────────────────

def compute_bookmaker_consensus(
    bookmaker_best_price: float,
    bookmaker_median_price: float,
    bookmaker_dispersion: float,
    num_bookmakers_offering: int,
) -> float:
    """
    Score how strongly bookmakers agree on this runner's price.

    Tight consensus across many bookmakers = strong signal.
    Wide dispersion or few bookmakers offering = weak signal.
    """
    score = 0.0

    # Number of bookmakers (more = stronger signal)
    if num_bookmakers_offering >= 15:
        score += 0.30
    elif num_bookmakers_offering >= 8:
        score += 0.20
    elif num_bookmakers_offering >= 4:
        score += 0.10

    # Dispersion (lower = tighter consensus)
    if bookmaker_dispersion < 0.08:
        score += 0.35   # Very tight
    elif bookmaker_dispersion < 0.15:
        score += 0.25
    elif bookmaker_dispersion < 0.30:
        score += 0.12
    else:
        score += 0.0    # Wide disagreement

    # Best vs median gap (small gap = consistent pricing)
    if bookmaker_best_price > 1.0 and bookmaker_median_price > 1.0:
        gap = abs(bookmaker_best_price - bookmaker_median_price) / bookmaker_median_price
        if gap < 0.02:
            score += 0.25
        elif gap < 0.05:
            score += 0.15
        elif gap < 0.10:
            score += 0.08

    return max(0.0, min(1.0, score))


# ─── Universe Qualification (A.2 + A.15) ────────────────────────────────────

@dataclass
class QualificationConfig:
    """Configurable qualification filters per A.2 and A.15."""
    min_quality_score: float = 0.45
    max_rank: int = 2                    # Must be projected 1st or 2nd
    min_liquidity_score: float = 0.20
    max_adverse_open_risk: float = 0.70
    min_runner_count: int = 3
    max_runner_count: int = 20           # Avoid very large fields initially
    allowed_race_types: list = field(default_factory=lambda: ["FLAT", "AW"])
    min_bookmakers_offering: int = 4


def qualify_candidate(
    candidate: CandidateScore,
    config: QualificationConfig,
) -> tuple[bool, str]:
    """
    Apply universe filters to determine if a candidate qualifies for trading.

    Returns (qualified: bool, reason: str).
    """
    # Rank check
    rank = candidate.projected_rank_near_off
    if rank is not None and rank > config.max_rank:
        return False, f"Projected rank {rank} exceeds max {config.max_rank}"

    # Race type check
    if candidate.race_type not in config.allowed_race_types:
        return False, f"Race type {candidate.race_type} not in allowed types"

    # Field size
    if candidate.runner_count < config.min_runner_count:
        return False, f"Only {candidate.runner_count} runners (min {config.min_runner_count})"
    if candidate.runner_count > config.max_runner_count:
        return False, f"Field of {candidate.runner_count} exceeds max {config.max_runner_count}"

    # Liquidity
    if candidate.liquidity_score < config.min_liquidity_score:
        return False, f"Liquidity {candidate.liquidity_score:.2f} below min {config.min_liquidity_score}"

    # Adverse open risk
    if candidate.adverse_open_risk > config.max_adverse_open_risk:
        return False, f"Adverse open risk {candidate.adverse_open_risk:.2f} exceeds max {config.max_adverse_open_risk}"

    # Quality score
    candidate.compute_quality_score()
    if candidate.trade_quality_score < config.min_quality_score:
        return False, f"Quality score {candidate.trade_quality_score:.4f} below min {config.min_quality_score}"

    return True, f"Qualified: score={candidate.trade_quality_score:.4f}"
