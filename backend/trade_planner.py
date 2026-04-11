"""
CHIMERA Scalping Engine — Trade Planner (A.7, A.9)
=====================================================
Staged entry policy and front-loaded lay ladder construction.

Staged Entry (A.7):
  Stage 1: Early Probe — 20-40% of total size
  Stage 2: Open Confirmation Add — 30-50%
  Stage 3: Late Add — 0-30% (optional, strongest setups only)
  Default Rule: NEVER allocate full size at morning stage.

Ladder Construction (A.9):
  VERY_DEFENSIVE: [0.65, 0.25, 0.10]
  DEFENSIVE:      [0.55, 0.30, 0.15]
  BALANCED:       [0.45, 0.35, 0.20]

  Rung 1 = primary de-risk
  Rung 2 = secondary de-risk
  Rung 3 = optional edge
  NO production trade should REQUIRE rung 3 to be safe.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

from state_machines import (
    EntryMode, ExitPolicy, RaceType, StakeTier,
    LadderPurpose, ControlMode,
)


# ─── Ladder Profiles (A.9) ──────────────────────────────────────────────────

LADDER_PROFILES = {
    "VERY_DEFENSIVE": [0.65, 0.25, 0.10],
    "DEFENSIVE":      [0.55, 0.30, 0.15],
    "BALANCED":       [0.45, 0.35, 0.20],
}


# ─── Stage Weight Function (A.7) ────────────────────────────────────────────

def compute_stage_weights(quality_score: float) -> tuple[float, float, float]:
    """
    Returns (stage1_pct, stage2_pct, stage3_pct) based on trade quality score.
    Higher quality = more weight on early entry.
    From A.7:
      score >= 0.90 → [0.35, 0.45, 0.20]
      score >= 0.80 → [0.30, 0.45, 0.25]
      score >= 0.70 → [0.25, 0.50, 0.25]
      else           → [0.20, 0.50, 0.30]
    """
    if quality_score >= 0.90:
        return (0.35, 0.45, 0.20)
    elif quality_score >= 0.80:
        return (0.30, 0.45, 0.25)
    elif quality_score >= 0.70:
        return (0.25, 0.50, 0.25)
    else:
        return (0.20, 0.50, 0.30)


# ─── Stake Tier Sizing ──────────────────────────────────────────────────────

STAKE_TIER_SIZES = {
    StakeTier.MICRO:  10.0,
    StakeTier.SMALL:  25.0,
    StakeTier.MEDIUM: 50.0,
    StakeTier.LARGE:  100.0,
    StakeTier.MAX:    200.0,
}


def determine_stake_tier(
    quality_score: float,
    first_lay_hit_prob: float,
    adverse_open_risk: float,
    stop_out_cost_score: float,
) -> StakeTier:
    """
    Determine the stake tier based on trade quality and risk profile.

    Largest stakes (A.2) only for:
      - Clear market principals
      - Strong favourite persistence
      - High first-lay hit probability
      - Low adverse-open risk
      - Acceptable first-lay-only and stop-out scenarios
    """
    # All conditions must be strong for LARGE/MAX
    if (quality_score >= 0.85 and first_lay_hit_prob >= 0.75
            and adverse_open_risk <= 0.20 and stop_out_cost_score >= 0.70):
        return StakeTier.MAX

    if (quality_score >= 0.75 and first_lay_hit_prob >= 0.60
            and adverse_open_risk <= 0.35 and stop_out_cost_score >= 0.55):
        return StakeTier.LARGE

    if (quality_score >= 0.60 and first_lay_hit_prob >= 0.45
            and adverse_open_risk <= 0.50):
        return StakeTier.MEDIUM

    if quality_score >= 0.45:
        return StakeTier.SMALL

    return StakeTier.MICRO


# ─── Data Types ──────────────────────────────────────────────────────────────

@dataclass
class EntryStage:
    """One stage of the staged entry plan."""
    stage_no: int
    mode: str               # EARLY_PROBE / CONFIRM_ADD / LATE_ADD
    planned_stake: float
    target_price: Optional[float] = None
    gating_rule: str = "NONE"   # NONE / OPEN_CONFIRMATION / LATE_CONFIRMATION
    weight_pct: float = 0.0
    executed: bool = False
    actual_stake: float = 0.0
    actual_price: float = 0.0

    def to_dict(self) -> dict:
        return {
            "stage_no": self.stage_no,
            "mode": self.mode,
            "planned_stake": self.planned_stake,
            "target_price": self.target_price,
            "gating_rule": self.gating_rule,
            "weight_pct": self.weight_pct,
            "executed": self.executed,
            "actual_stake": self.actual_stake,
            "actual_price": self.actual_price,
        }


@dataclass
class LayRung:
    """One rung of the lay ladder."""
    rung: int
    target_odds: float
    target_stake: float
    purpose: str             # PRIMARY_DERISK / SECONDARY_DERISK / OPTIONAL_EDGE
    cancel_before_off_secs: int = 30
    matched_stake: float = 0.0
    avg_matched_price: float = 0.0
    order_id: Optional[str] = None
    status: str = "PENDING"   # PENDING / PLACED / PARTIAL / FILLED / CANCELLED

    def to_dict(self) -> dict:
        return {
            "rung": self.rung,
            "target_odds": self.target_odds,
            "target_stake": self.target_stake,
            "purpose": self.purpose,
            "cancel_before_off_secs": self.cancel_before_off_secs,
            "matched_stake": self.matched_stake,
            "avg_matched_price": self.avg_matched_price,
            "order_id": self.order_id,
            "status": self.status,
        }


@dataclass
class InvalidationRules:
    """Invalidation thresholds for adverse-open detection (A.8)."""
    green_max_open_drift_ticks: int = 3
    amber_max_open_drift_ticks: int = 8
    hard_cut_max_open_drift_ticks: int = 15
    max_open_drift_pct: float = 0.10

    def to_dict(self) -> dict:
        return {
            "green_max_drift_ticks": self.green_max_open_drift_ticks,
            "amber_max_drift_ticks": self.amber_max_open_drift_ticks,
            "hard_cut_max_drift_ticks": self.hard_cut_max_open_drift_ticks,
            "max_open_drift_pct": self.max_open_drift_pct,
        }


@dataclass
class StopRules:
    """Stop-loss rules for a trade."""
    max_adverse_ticks: int = 20
    max_adverse_pct: float = 0.15
    flatten_before_off_secs: int = 60
    in_play_allowed_only_if_matched_pct: float = 0.50

    def to_dict(self) -> dict:
        return {
            "max_adverse_ticks": self.max_adverse_ticks,
            "max_adverse_pct": self.max_adverse_pct,
            "flatten_before_off_secs": self.flatten_before_off_secs,
            "in_play_allowed_only_if_matched_pct": self.in_play_allowed_only_if_matched_pct,
        }


@dataclass
class TradePlan:
    """Complete trade plan for a qualified candidate."""
    trade_id: str
    race_id: str
    market_id: str
    selection_id: int
    horse_name: str
    strategy_version: str = "1.0.0"

    entry_source: str = "EXCHANGE"        # BOOKMAKER or EXCHANGE
    exit_policy: str = "PRE_RACE_ONLY"
    race_type: str = "FLAT"

    stake_tier: str = "SMALL"
    total_intended_stake: float = 0.0

    entry_stages: list = field(default_factory=list)   # List[EntryStage]
    lay_ladder: list = field(default_factory=list)      # List[LayRung]
    invalidation: InvalidationRules = field(default_factory=InvalidationRules)
    stop_rules: StopRules = field(default_factory=StopRules)

    # Quality metrics at plan time
    quality_score: float = 0.0
    first_lay_hit_prob: float = 0.0
    adverse_open_risk: float = 0.0

    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "race_id": self.race_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "horse_name": self.horse_name,
            "strategy_version": self.strategy_version,
            "entry_source": self.entry_source,
            "exit_policy": self.exit_policy,
            "race_type": self.race_type,
            "stake_tier": self.stake_tier,
            "total_intended_stake": self.total_intended_stake,
            "entry_stages": [s.to_dict() for s in self.entry_stages],
            "lay_ladder": [r.to_dict() for r in self.lay_ladder],
            "invalidation": self.invalidation.to_dict(),
            "stop_rules": self.stop_rules.to_dict(),
            "quality_score": self.quality_score,
            "first_lay_hit_prob": self.first_lay_hit_prob,
            "adverse_open_risk": self.adverse_open_risk,
            "created_at": self.created_at,
        }


# ─── Plan Builder ────────────────────────────────────────────────────────────

def build_trade_plan(
    trade_id: str,
    race_id: str,
    market_id: str,
    selection_id: int,
    horse_name: str,
    quality_score: float,
    first_lay_hit_prob: float,
    adverse_open_risk: float,
    stop_out_cost_score: float,
    entry_odds: float,
    entry_source: str = "EXCHANGE",
    race_type: str = "FLAT",
    ladder_profile: str = "DEFENSIVE",
    exit_policy: str = "PRE_RACE_ONLY",
    point_value: float = 1.0,
) -> TradePlan:
    """
    Build a complete trade plan for a qualified candidate.

    1. Determine stake tier from quality metrics
    2. Compute staged entry weights
    3. Build entry stages with gating rules
    4. Build front-loaded lay ladder
    5. Set invalidation and stop rules calibrated to risk profile
    """

    # 1. Stake tier
    tier = determine_stake_tier(
        quality_score, first_lay_hit_prob, adverse_open_risk, stop_out_cost_score,
    )
    total_stake = round(STAKE_TIER_SIZES[tier] * point_value, 2)

    # 2. Stage weights
    s1_pct, s2_pct, s3_pct = compute_stage_weights(quality_score)

    # 3. Entry stages
    stages = [
        EntryStage(
            stage_no=1,
            mode="EARLY_PROBE",
            planned_stake=round(total_stake * s1_pct, 2),
            target_price=entry_odds,
            gating_rule="NONE",
            weight_pct=s1_pct,
        ),
        EntryStage(
            stage_no=2,
            mode="CONFIRM_ADD",
            planned_stake=round(total_stake * s2_pct, 2),
            target_price=None,  # Determined at exchange open
            gating_rule="OPEN_CONFIRMATION",
            weight_pct=s2_pct,
        ),
    ]
    if s3_pct > 0.05:
        stages.append(EntryStage(
            stage_no=3,
            mode="LATE_ADD",
            planned_stake=round(total_stake * s3_pct, 2),
            target_price=None,
            gating_rule="LATE_CONFIRMATION",
            weight_pct=s3_pct,
        ))

    # 4. Lay ladder
    profile_weights = LADDER_PROFILES.get(ladder_profile, LADDER_PROFILES["DEFENSIVE"])

    # Estimate lay target prices (spread across expected shortening range)
    # Rung 1: at or near current price (primary de-risk)
    # Rung 2: 3-5% shorter
    # Rung 3: 8-12% shorter (optional)
    ladder = []
    for i, (weight, purpose) in enumerate(zip(
        profile_weights,
        [LadderPurpose.PRIMARY_DERISK, LadderPurpose.SECONDARY_DERISK, LadderPurpose.OPTIONAL_EDGE],
    )):
        # Price targets: each rung targets a shorter (lower) lay price
        price_offset = 1.0 - (0.03 * (i + 1))  # 97%, 94%, 91%
        rung_odds = round(entry_odds * price_offset, 2)
        rung_odds = max(rung_odds, 1.01)  # Cannot be below Betfair minimum

        rung_stake = round(total_stake * weight, 2)

        cancel_secs = 30 if i < 2 else 60  # Cancel optional rung earlier

        ladder.append(LayRung(
            rung=i + 1,
            target_odds=rung_odds,
            target_stake=rung_stake,
            purpose=purpose.value,
            cancel_before_off_secs=cancel_secs,
        ))

    # 5. Invalidation rules — calibrated to adverse-open risk
    if adverse_open_risk > 0.50:
        inval = InvalidationRules(
            green_max_open_drift_ticks=2,
            amber_max_open_drift_ticks=5,
            hard_cut_max_open_drift_ticks=10,
            max_open_drift_pct=0.06,
        )
    elif adverse_open_risk > 0.30:
        inval = InvalidationRules(
            green_max_open_drift_ticks=3,
            amber_max_open_drift_ticks=8,
            hard_cut_max_open_drift_ticks=15,
            max_open_drift_pct=0.10,
        )
    else:
        inval = InvalidationRules()  # Defaults

    # Stop rules — tighter for higher risk
    stop = StopRules(
        max_adverse_ticks=15 if adverse_open_risk > 0.40 else 20,
        max_adverse_pct=0.10 if adverse_open_risk > 0.40 else 0.15,
        flatten_before_off_secs=90 if tier in (StakeTier.LARGE, StakeTier.MAX) else 60,
    )

    return TradePlan(
        trade_id=trade_id,
        race_id=race_id,
        market_id=market_id,
        selection_id=selection_id,
        horse_name=horse_name,
        entry_source=entry_source,
        exit_policy=exit_policy,
        race_type=race_type,
        stake_tier=tier.value,
        total_intended_stake=total_stake,
        entry_stages=stages,
        lay_ladder=ladder,
        invalidation=inval,
        stop_rules=stop,
        quality_score=quality_score,
        first_lay_hit_prob=first_lay_hit_prob,
        adverse_open_risk=adverse_open_risk,
    )


# ─── Adverse-Open Classification (A.8) ──────────────────────────────────────

def classify_open(
    entry_odds: float,
    exchange_open_odds: float,
    inval: InvalidationRules,
) -> str:
    """
    Classify the exchange open relative to our entry.

    GREEN:  Exchange opens flat or stronger → keep plan, allow add, keep ladder
    AMBER:  Exchange opens modestly weaker → reduce size, cancel deep rungs, tighten stops
    RED:    Exchange opens materially weaker → cut immediately or flatten, mark invalidated

    Returns: "GREEN", "AMBER", or "RED"
    """
    if exchange_open_odds <= entry_odds:
        return "GREEN"

    drift_pct = (exchange_open_odds - entry_odds) / entry_odds

    if drift_pct <= inval.max_open_drift_pct / 2:
        return "AMBER"

    return "RED"


# ─── Large-Stake Qualification (A.9) ────────────────────────────────────────

def qualifies_for_large_size(
    first_lay_only_min_pnl: float,
    stop_out_min_pnl: float,
    max_partial_loss: float = 50.0,
    max_stop_loss: float = 100.0,
) -> bool:
    """
    Rule from A.9: a trade qualifies for large size only if the first-lay-only
    and stop-out scenarios are both within acceptable loss caps.
    """
    return (
        first_lay_only_min_pnl >= -max_partial_loss and
        stop_out_min_pnl >= -max_stop_loss
    )
