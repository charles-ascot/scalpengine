"""
CHIMERA Scalping Engine — Risk Engine (B.13)
==============================================
Authoritative risk service. Execution cannot override it.
Strategy cannot bypass it.

Risk Levels:
  Level 1 — Trade risk: max loss per trade, max unhedged, max liability
  Level 2 — Market risk: max exposure per market, max correlated exposure
  Level 3 — Sport risk: max exposure per sport/session
  Level 4 — Portfolio risk: daily drawdown cap, rolling loss cap, capital cap

Hard Controls:
  - Block trade creation
  - Block stage advance
  - Auto reduce
  - Auto flatten
  - Force manual review
  - Trigger kill switch

Non-Negotiable Rules (from Executive Summary):
  - No full-size morning entries
  - No large trade unless first-lay-only scenario acceptable
  - No trade whose safety depends on rung 3
  - No automatic in-play unless sufficient pre-race risk removed
  - Any materially adverse exchange open triggers reduction or cut
  - Any manual override can lock trade out of automatic control
  - No new trade if race-level or day-level risk caps breached
  - Cancel and flatten logic must account for suspension risk near off
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


@dataclass
class RiskConfig:
    """Configurable risk parameters across all four levels."""

    # Level 1 — Trade risk
    max_loss_per_trade: float = 100.0
    max_unhedged_loss: float = 150.0
    max_liability_per_trade: float = 500.0
    min_first_rung_only_pnl: float = -50.0   # First-lay-only scenario floor

    # Level 2 — Market risk
    max_exposure_per_market: float = 300.0
    max_simultaneous_trades_per_market: int = 2
    max_correlated_exposure: float = 500.0

    # Level 3 — Sport risk
    max_exposure_per_sport: float = 1000.0
    max_exposure_per_session: float = 600.0   # e.g., morning session cap

    # Level 4 — Portfolio risk
    daily_drawdown_cap: float = 500.0
    rolling_loss_cap: float = 1000.0        # Rolling 7-day
    rolling_loss_window_days: int = 7
    capital_utilisation_cap_pct: float = 0.50  # Max 50% of bankroll exposed
    bankroll: float = 5000.0

    # Kill switches
    kill_switch_enabled: bool = False
    flatten_only_mode: bool = False          # B.22: emergency mode

    def to_dict(self) -> dict:
        return {
            "level_1_trade": {
                "max_loss_per_trade": self.max_loss_per_trade,
                "max_unhedged_loss": self.max_unhedged_loss,
                "max_liability_per_trade": self.max_liability_per_trade,
                "min_first_rung_only_pnl": self.min_first_rung_only_pnl,
            },
            "level_2_market": {
                "max_exposure_per_market": self.max_exposure_per_market,
                "max_simultaneous_trades_per_market": self.max_simultaneous_trades_per_market,
                "max_correlated_exposure": self.max_correlated_exposure,
            },
            "level_3_sport": {
                "max_exposure_per_sport": self.max_exposure_per_sport,
                "max_exposure_per_session": self.max_exposure_per_session,
            },
            "level_4_portfolio": {
                "daily_drawdown_cap": self.daily_drawdown_cap,
                "rolling_loss_cap": self.rolling_loss_cap,
                "rolling_loss_window_days": self.rolling_loss_window_days,
                "capital_utilisation_cap_pct": self.capital_utilisation_cap_pct,
                "bankroll": self.bankroll,
            },
            "kill_switch_enabled": self.kill_switch_enabled,
            "flatten_only_mode": self.flatten_only_mode,
        }


@dataclass
class RiskDecision:
    """Output from a risk check (B.13)."""
    allowed: bool
    action: str            # ALLOW / ALLOW_WITH_REDUCTION / BLOCK / FORCE_REDUCE / FORCE_FLATTEN / ESCALATE_TO_MANUAL
    reasons: list = field(default_factory=list)
    reduction_factor: float = 1.0   # If ALLOW_WITH_REDUCTION, multiply stake by this
    level: str = ""                 # Which risk level triggered (L1/L2/L3/L4)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reasons": self.reasons,
            "reduction_factor": self.reduction_factor,
            "level": self.level,
        }


@dataclass
class PortfolioState:
    """Current portfolio exposure snapshot."""
    total_open_trades: int = 0
    total_exposure: float = 0.0          # Sum of max losses across open trades
    total_unhedged: float = 0.0          # Exposure on unhedged positions
    daily_realised_pnl: float = 0.0
    daily_unrealised_pnl: float = 0.0
    rolling_pnl: float = 0.0            # Rolling N-day P&L
    exposure_by_market: dict = field(default_factory=dict)   # market_id → exposure
    exposure_by_sport: dict = field(default_factory=dict)    # sport → exposure
    trades_by_market: dict = field(default_factory=dict)     # market_id → count

    def to_dict(self) -> dict:
        return {
            "total_open_trades": self.total_open_trades,
            "total_exposure": round(self.total_exposure, 2),
            "total_unhedged": round(self.total_unhedged, 2),
            "daily_realised_pnl": round(self.daily_realised_pnl, 2),
            "daily_unrealised_pnl": round(self.daily_unrealised_pnl, 2),
            "rolling_pnl": round(self.rolling_pnl, 2),
            "exposure_by_market": {k: round(v, 2) for k, v in self.exposure_by_market.items()},
        }


# ─── Risk Checks ────────────────────────────────────────────────────────────

def check_trade_risk(
    planned_max_loss: float,
    planned_liability: float,
    first_rung_only_pnl: float,
    config: RiskConfig,
) -> RiskDecision:
    """Level 1: Trade-level risk check."""
    reasons = []

    if config.kill_switch_enabled:
        return RiskDecision(
            allowed=False, action="BLOCK",
            reasons=["Kill switch active — all new trades blocked"],
            level="KILL",
        )

    if config.flatten_only_mode:
        return RiskDecision(
            allowed=False, action="BLOCK",
            reasons=["Flatten-only mode active — no new trades"],
            level="L4",
        )

    if planned_max_loss > config.max_loss_per_trade:
        reasons.append(
            f"Max loss £{planned_max_loss:.2f} exceeds trade cap £{config.max_loss_per_trade:.2f}"
        )

    if planned_liability > config.max_liability_per_trade:
        reasons.append(
            f"Liability £{planned_liability:.2f} exceeds trade cap £{config.max_liability_per_trade:.2f}"
        )

    if first_rung_only_pnl < config.min_first_rung_only_pnl:
        reasons.append(
            f"First-rung-only P&L £{first_rung_only_pnl:.2f} below floor £{config.min_first_rung_only_pnl:.2f}"
        )

    if reasons:
        return RiskDecision(allowed=False, action="BLOCK", reasons=reasons, level="L1")

    return RiskDecision(allowed=True, action="ALLOW", level="L1")


def check_market_risk(
    market_id: str,
    additional_exposure: float,
    portfolio: PortfolioState,
    config: RiskConfig,
) -> RiskDecision:
    """Level 2: Market-level risk check."""
    reasons = []

    current_market_exposure = portfolio.exposure_by_market.get(market_id, 0)
    new_market_exposure = current_market_exposure + additional_exposure

    if new_market_exposure > config.max_exposure_per_market:
        reasons.append(
            f"Market exposure £{new_market_exposure:.2f} would exceed cap £{config.max_exposure_per_market:.2f}"
        )

    current_trades = portfolio.trades_by_market.get(market_id, 0)
    if current_trades >= config.max_simultaneous_trades_per_market:
        reasons.append(
            f"Already {current_trades} trades in market (max {config.max_simultaneous_trades_per_market})"
        )

    if reasons:
        return RiskDecision(allowed=False, action="BLOCK", reasons=reasons, level="L2")

    return RiskDecision(allowed=True, action="ALLOW", level="L2")


def check_sport_risk(
    sport: str,
    additional_exposure: float,
    portfolio: PortfolioState,
    config: RiskConfig,
) -> RiskDecision:
    """Level 3: Sport-level risk check."""
    current_sport_exposure = portfolio.exposure_by_sport.get(sport, 0)
    new_sport_exposure = current_sport_exposure + additional_exposure

    if new_sport_exposure > config.max_exposure_per_sport:
        return RiskDecision(
            allowed=False, action="BLOCK",
            reasons=[
                f"Sport exposure £{new_sport_exposure:.2f} would exceed cap £{config.max_exposure_per_sport:.2f}"
            ],
            level="L3",
        )

    return RiskDecision(allowed=True, action="ALLOW", level="L3")


def check_portfolio_risk(
    additional_exposure: float,
    portfolio: PortfolioState,
    config: RiskConfig,
) -> RiskDecision:
    """Level 4: Portfolio-level risk check."""
    reasons = []

    # Daily drawdown
    current_drawdown = -(portfolio.daily_realised_pnl + portfolio.daily_unrealised_pnl)
    if current_drawdown > 0 and current_drawdown >= config.daily_drawdown_cap:
        reasons.append(
            f"Daily drawdown £{current_drawdown:.2f} at/exceeds cap £{config.daily_drawdown_cap:.2f}"
        )

    # Rolling loss
    if portfolio.rolling_pnl < -config.rolling_loss_cap:
        reasons.append(
            f"Rolling {config.rolling_loss_window_days}d P&L £{portfolio.rolling_pnl:.2f} "
            f"exceeds cap -£{config.rolling_loss_cap:.2f}"
        )

    # Capital utilisation
    new_total_exposure = portfolio.total_exposure + additional_exposure
    utilisation = new_total_exposure / config.bankroll if config.bankroll > 0 else 1.0
    if utilisation > config.capital_utilisation_cap_pct:
        reasons.append(
            f"Capital utilisation {utilisation:.0%} would exceed cap {config.capital_utilisation_cap_pct:.0%}"
        )

    if reasons:
        # Check if we should reduce vs block
        if len(reasons) == 1 and "utilisation" in reasons[0]:
            # Capital utilisation: allow with reduction
            allowed_additional = (config.capital_utilisation_cap_pct * config.bankroll) - portfolio.total_exposure
            if allowed_additional > 0:
                factor = allowed_additional / additional_exposure
                return RiskDecision(
                    allowed=True, action="ALLOW_WITH_REDUCTION",
                    reasons=reasons,
                    reduction_factor=max(0.1, min(1.0, factor)),
                    level="L4",
                )
        return RiskDecision(allowed=False, action="BLOCK", reasons=reasons, level="L4")

    return RiskDecision(allowed=True, action="ALLOW", level="L4")


# ─── Master Risk Check ──────────────────────────────────────────────────────

def evaluate_risk(
    market_id: str,
    sport: str,
    planned_max_loss: float,
    planned_liability: float,
    first_rung_only_pnl: float,
    portfolio: PortfolioState,
    config: RiskConfig,
) -> RiskDecision:
    """
    Run all four risk levels. Returns the most restrictive decision.

    Priority: BLOCK > FORCE_FLATTEN > FORCE_REDUCE > ESCALATE_TO_MANUAL > ALLOW_WITH_REDUCTION > ALLOW
    """
    ACTION_PRIORITY = {
        "BLOCK": 0,
        "FORCE_FLATTEN": 1,
        "FORCE_REDUCE": 2,
        "ESCALATE_TO_MANUAL": 3,
        "ALLOW_WITH_REDUCTION": 4,
        "ALLOW": 5,
    }

    checks = [
        check_trade_risk(planned_max_loss, planned_liability, first_rung_only_pnl, config),
        check_market_risk(market_id, planned_max_loss, portfolio, config),
        check_sport_risk(sport, planned_max_loss, portfolio, config),
        check_portfolio_risk(planned_max_loss, portfolio, config),
    ]

    # Find the most restrictive decision
    most_restrictive = RiskDecision(allowed=True, action="ALLOW")
    all_reasons = []

    for check in checks:
        all_reasons.extend(check.reasons)
        if ACTION_PRIORITY.get(check.action, 5) < ACTION_PRIORITY.get(most_restrictive.action, 5):
            most_restrictive = check

    most_restrictive.reasons = all_reasons

    if most_restrictive.action in ("BLOCK", "FORCE_FLATTEN", "FORCE_REDUCE"):
        most_restrictive.allowed = False

    return most_restrictive
