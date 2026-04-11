"""
CHIMERA Scalping Engine — Scenario Engine (A.10)
==================================================
Mandatory pre-trade simulation. Every trade must pass the scenario gate
before entry is allowed.

Eight Scenarios (A.10):
  1. Full ladder filled
  2. First lay only
  3. First lay 50% only
  4. No lay filled
  5. Adverse open cut
  6. Stop-out before first lay
  7. Pre-race flatten at bad price
  8. In-play residual (if enabled)

Acceptance Rule:
  A trade is allowed only if the full ladder, first-lay only, stop-out,
  and no-lay scenarios are all within acceptable hard caps.

P&L Mathematics (A.11):
  Bookmaker Back → Betfair Lay:
    winPnl  = S * (B - 1) - x * (L - 1)
    losePnl = x * (1 - c) - S

  Exchange Back → Exchange Lay:
    grossWin  = S * (B - 1) - x * (L - 1)
    grossLose = x - S
    netWin  = grossWin * (1 - c)  if grossWin > 0  else grossWin
    netLose = grossLose * (1 - c) if grossLose > 0 else grossLose
"""

from dataclasses import dataclass, field
from typing import Optional


# ─── Default Betfair commission ──────────────────────────────────────────────
BETFAIR_COMMISSION = 0.05   # 5% on net winnings


@dataclass
class ScenarioResult:
    """P&L result from a single scenario simulation."""
    scenario_name: str
    pnl_if_win: float = 0.0
    pnl_if_lose: float = 0.0
    min_pnl: float = 0.0       # Worst case of win/lose
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario_name,
            "pnl_if_win": round(self.pnl_if_win, 2),
            "pnl_if_lose": round(self.pnl_if_lose, 2),
            "min_pnl": round(self.min_pnl, 2),
            "description": self.description,
        }


@dataclass
class ScenarioEnvelope:
    """Complete pre-trade scenario envelope (A.10)."""
    full_ladder: ScenarioResult = field(default_factory=lambda: ScenarioResult("full_ladder"))
    first_lay_only: ScenarioResult = field(default_factory=lambda: ScenarioResult("first_lay_only"))
    first_lay_half: ScenarioResult = field(default_factory=lambda: ScenarioResult("first_lay_half"))
    no_lay: ScenarioResult = field(default_factory=lambda: ScenarioResult("no_lay"))
    adverse_open_cut: ScenarioResult = field(default_factory=lambda: ScenarioResult("adverse_open_cut"))
    stop_out: ScenarioResult = field(default_factory=lambda: ScenarioResult("stop_out"))
    pre_race_flatten: ScenarioResult = field(default_factory=lambda: ScenarioResult("pre_race_flatten"))
    in_play_residual: ScenarioResult = field(default_factory=lambda: ScenarioResult("in_play_residual"))

    gate_passed: bool = False
    gate_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "full_ladder": self.full_ladder.to_dict(),
            "first_lay_only": self.first_lay_only.to_dict(),
            "first_lay_half": self.first_lay_half.to_dict(),
            "no_lay": self.no_lay.to_dict(),
            "adverse_open_cut": self.adverse_open_cut.to_dict(),
            "stop_out": self.stop_out.to_dict(),
            "pre_race_flatten": self.pre_race_flatten.to_dict(),
            "in_play_residual": self.in_play_residual.to_dict(),
            "gate_passed": self.gate_passed,
            "gate_reason": self.gate_reason,
        }


@dataclass
class TradeGateConfig:
    """Hard caps for the trade gate (A.10 acceptance rule)."""
    max_first_lay_only_loss: float = 50.0
    max_no_lay_loss: float = 100.0
    max_stop_loss: float = 80.0
    max_adverse_open_loss: float = 60.0


# ─── P&L Calculators (A.11) ─────────────────────────────────────────────────

def calc_bookmaker_back_exchange_lay(
    back_stake: float,
    back_odds: float,
    lay_stake: float,
    lay_odds: float,
    commission: float = BETFAIR_COMMISSION,
) -> tuple[float, float]:
    """
    Bookmaker Back → Betfair Lay P&L.

    Returns (pnl_if_win, pnl_if_lose) where:
      win  = horse wins (back wins, lay loses)
      lose = horse loses (back loses, lay wins)
    """
    # If the horse wins: we win the back, lose the lay
    pnl_win = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    # If the horse loses: we lose the back, win the lay (minus commission)
    pnl_lose = lay_stake * (1 - commission) - back_stake
    return round(pnl_win, 4), round(pnl_lose, 4)


def calc_exchange_back_exchange_lay(
    back_stake: float,
    back_odds: float,
    lay_stake: float,
    lay_odds: float,
    commission: float = BETFAIR_COMMISSION,
) -> tuple[float, float]:
    """
    Exchange Back → Exchange Lay P&L.

    Betfair charges commission on net winnings on the market.
    Gross outcomes net internally before commission.

    Returns (pnl_if_win, pnl_if_lose).
    """
    gross_win = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    gross_lose = lay_stake - back_stake

    # Commission only on positive net
    net_win = gross_win * (1 - commission) if gross_win > 0 else gross_win
    net_lose = gross_lose * (1 - commission) if gross_lose > 0 else gross_lose

    return round(net_win, 4), round(net_lose, 4)


# ─── Scenario Simulator ─────────────────────────────────────────────────────

def simulate_scenarios(
    back_stake: float,
    back_odds: float,
    lay_rungs: list,          # List of (lay_odds, lay_stake) tuples
    entry_source: str = "EXCHANGE",
    commission: float = BETFAIR_COMMISSION,
    adverse_open_odds: Optional[float] = None,
    stop_out_odds: Optional[float] = None,
    flatten_odds: Optional[float] = None,
) -> ScenarioEnvelope:
    """
    Run all 8 mandatory scenarios from A.10.

    lay_rungs: [(target_odds_1, target_stake_1), (target_odds_2, ...), ...]
    """
    envelope = ScenarioEnvelope()

    # Choose P&L calculator based on entry source
    calc = (calc_bookmaker_back_exchange_lay if entry_source == "BOOKMAKER"
            else calc_exchange_back_exchange_lay)

    # ── Scenario 1: Full ladder filled ──
    total_lay_stake = sum(s for _, s in lay_rungs)
    # Use weighted average lay odds for aggregate calc
    if total_lay_stake > 0:
        avg_lay_odds = sum(o * s for o, s in lay_rungs) / total_lay_stake
    else:
        avg_lay_odds = back_odds  # Fallback
    pnl_w, pnl_l = calc(back_stake, back_odds, total_lay_stake, avg_lay_odds, commission)
    envelope.full_ladder = ScenarioResult(
        scenario_name="full_ladder",
        pnl_if_win=pnl_w,
        pnl_if_lose=pnl_l,
        min_pnl=min(pnl_w, pnl_l),
        description=f"All {len(lay_rungs)} rungs filled: avg lay {avg_lay_odds:.2f}, total lay £{total_lay_stake:.2f}",
    )

    # ── Scenario 2: First lay only ──
    if lay_rungs:
        r1_odds, r1_stake = lay_rungs[0]
        pnl_w, pnl_l = calc(back_stake, back_odds, r1_stake, r1_odds, commission)
        envelope.first_lay_only = ScenarioResult(
            scenario_name="first_lay_only",
            pnl_if_win=pnl_w,
            pnl_if_lose=pnl_l,
            min_pnl=min(pnl_w, pnl_l),
            description=f"Only rung 1 filled: lay {r1_odds:.2f} × £{r1_stake:.2f}",
        )

        # ── Scenario 3: First lay 50% only ──
        half_stake = round(r1_stake * 0.5, 2)
        pnl_w, pnl_l = calc(back_stake, back_odds, half_stake, r1_odds, commission)
        envelope.first_lay_half = ScenarioResult(
            scenario_name="first_lay_half",
            pnl_if_win=pnl_w,
            pnl_if_lose=pnl_l,
            min_pnl=min(pnl_w, pnl_l),
            description=f"Rung 1 half-filled: lay {r1_odds:.2f} × £{half_stake:.2f}",
        )

    # ── Scenario 4: No lay filled ──
    # Full back exposure, no hedge
    pnl_win_no_lay = round(back_stake * (back_odds - 1), 2)    # Win = profit
    pnl_lose_no_lay = round(-back_stake, 2)                      # Lose = lose stake
    if entry_source == "EXCHANGE":
        pnl_win_no_lay = round(pnl_win_no_lay * (1 - commission), 2)
    envelope.no_lay = ScenarioResult(
        scenario_name="no_lay",
        pnl_if_win=pnl_win_no_lay,
        pnl_if_lose=pnl_lose_no_lay,
        min_pnl=pnl_lose_no_lay,
        description=f"No lay filled: full back exposure £{back_stake:.2f} @ {back_odds:.2f}",
    )

    # ── Scenario 5: Adverse open cut ──
    if adverse_open_odds and adverse_open_odds > back_odds:
        # We cut at the adverse open price — take a loss on the spread
        spread_loss = round(back_stake * (adverse_open_odds - back_odds) / back_odds, 2)
        envelope.adverse_open_cut = ScenarioResult(
            scenario_name="adverse_open_cut",
            pnl_if_win=-spread_loss,  # We've already cut — loss is realised
            pnl_if_lose=-spread_loss,
            min_pnl=-spread_loss,
            description=f"Cut at adverse open {adverse_open_odds:.2f} vs entry {back_odds:.2f}",
        )
    else:
        envelope.adverse_open_cut = ScenarioResult(
            scenario_name="adverse_open_cut",
            description="N/A — no adverse open scenario configured",
        )

    # ── Scenario 6: Stop-out before first lay ──
    if stop_out_odds and stop_out_odds > back_odds:
        stop_loss = round(back_stake * (stop_out_odds - back_odds) / back_odds, 2)
        envelope.stop_out = ScenarioResult(
            scenario_name="stop_out",
            pnl_if_win=-stop_loss,
            pnl_if_lose=-stop_loss,
            min_pnl=-stop_loss,
            description=f"Stopped out at {stop_out_odds:.2f} vs entry {back_odds:.2f}",
        )
    else:
        # Default: assume stop-out at 15% adverse move
        stop_price = round(back_odds * 1.15, 2)
        stop_loss = round(back_stake * 0.15, 2)
        envelope.stop_out = ScenarioResult(
            scenario_name="stop_out",
            pnl_if_win=-stop_loss,
            pnl_if_lose=-stop_loss,
            min_pnl=-stop_loss,
            description=f"Default stop-out at {stop_price:.2f} (15% adverse)",
        )

    # ── Scenario 7: Pre-race flatten at bad price ──
    if flatten_odds:
        pnl_w, pnl_l = calc(back_stake, back_odds, back_stake, flatten_odds, commission)
        envelope.pre_race_flatten = ScenarioResult(
            scenario_name="pre_race_flatten",
            pnl_if_win=pnl_w,
            pnl_if_lose=pnl_l,
            min_pnl=min(pnl_w, pnl_l),
            description=f"Flatten at {flatten_odds:.2f}: lay {back_stake:.2f} to close",
        )
    else:
        # Assume flatten at 5% worse
        flat_odds = round(back_odds * 1.05, 2)
        pnl_w, pnl_l = calc(back_stake, back_odds, back_stake, flat_odds, commission)
        envelope.pre_race_flatten = ScenarioResult(
            scenario_name="pre_race_flatten",
            pnl_if_win=pnl_w,
            pnl_if_lose=pnl_l,
            min_pnl=min(pnl_w, pnl_l),
            description=f"Default flatten at {flat_odds:.2f} (5% worse)",
        )

    # ── Scenario 8: In-play residual ──
    # Placeholder — depends on exit policy and in-play configuration
    envelope.in_play_residual = ScenarioResult(
        scenario_name="in_play_residual",
        description="In-play residual — configurable per exit policy",
    )

    return envelope


# ─── Trade Gate (A.10 Acceptance Rule) ───────────────────────────────────────

def passes_trade_gate(
    envelope: ScenarioEnvelope,
    config: TradeGateConfig,
) -> tuple[bool, str]:
    """
    Apply the trade gate: a trade is allowed only if all critical scenarios
    are within acceptable hard caps.

    From A.10:
      firstLayOnlyMinPnl >= -maxFirstLayOnlyLoss AND
      noLayMinPnl        >= -maxNoLayLoss AND
      stopOutMinPnl      >= -maxStopLoss
    """
    failures = []

    if envelope.first_lay_only.min_pnl < -config.max_first_lay_only_loss:
        failures.append(
            f"First-lay-only loss £{abs(envelope.first_lay_only.min_pnl):.2f} "
            f"exceeds cap £{config.max_first_lay_only_loss:.2f}"
        )

    if envelope.no_lay.min_pnl < -config.max_no_lay_loss:
        failures.append(
            f"No-lay loss £{abs(envelope.no_lay.min_pnl):.2f} "
            f"exceeds cap £{config.max_no_lay_loss:.2f}"
        )

    if envelope.stop_out.min_pnl < -config.max_stop_loss:
        failures.append(
            f"Stop-out loss £{abs(envelope.stop_out.min_pnl):.2f} "
            f"exceeds cap £{config.max_stop_loss:.2f}"
        )

    if envelope.adverse_open_cut.min_pnl < -config.max_adverse_open_loss:
        failures.append(
            f"Adverse-open loss £{abs(envelope.adverse_open_cut.min_pnl):.2f} "
            f"exceeds cap £{config.max_adverse_open_loss:.2f}"
        )

    if failures:
        envelope.gate_passed = False
        envelope.gate_reason = " | ".join(failures)
        return False, envelope.gate_reason

    envelope.gate_passed = True
    envelope.gate_reason = "All scenarios within acceptable caps"
    return True, envelope.gate_reason
