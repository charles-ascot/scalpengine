"""
CHIMERA Scalping Engine — Position & P&L Service (B.15)
=========================================================
Aggregates matched orders, calculates exposure, scenario P&L,
realised P&L, and reconciles with settlement.

Commission-aware: separate calculators for bookmaker→exchange and
exchange→exchange flows.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


BETFAIR_COMMISSION = 0.05


@dataclass
class PositionSnapshot:
    """Current position state for a trade (B.15)."""
    trade_id: str
    gross_back_stake: float = 0.0
    avg_back_odds: float = 0.0
    gross_lay_stake: float = 0.0
    avg_lay_odds: float = 0.0
    gross_lay_liability: float = 0.0
    pnl_if_win: float = 0.0
    pnl_if_lose: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    max_open_loss: float = 0.0
    hedge_completion_pct: float = 0.0
    commission_estimate: float = 0.0
    entry_source: str = "EXCHANGE"
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def recalculate(self):
        """Recalculate P&L from current fills."""
        c = BETFAIR_COMMISSION

        # A lay can be matched before its back is recorded (or with none at
        # all); the exchange formula below handles S = 0 correctly, so only
        # skip when nothing is matched on either side.
        if self.gross_back_stake <= 0 and self.gross_lay_stake <= 0:
            return

        if self.entry_source == "BOOKMAKER":
            # Bookmaker back → exchange lay
            self.pnl_if_win = round(
                self.gross_back_stake * (self.avg_back_odds - 1)
                - self.gross_lay_stake * (self.avg_lay_odds - 1),
                2,
            )
            self.pnl_if_lose = round(
                self.gross_lay_stake * (1 - c) - self.gross_back_stake,
                2,
            )
        else:
            # Exchange back → exchange lay
            gross_win = (self.gross_back_stake * (self.avg_back_odds - 1)
                         - self.gross_lay_stake * (self.avg_lay_odds - 1))
            gross_lose = self.gross_lay_stake - self.gross_back_stake

            self.pnl_if_win = round(
                gross_win * (1 - c) if gross_win > 0 else gross_win, 2
            )
            self.pnl_if_lose = round(
                gross_lose * (1 - c) if gross_lose > 0 else gross_lose, 2
            )

        self.max_open_loss = round(min(self.pnl_if_win, self.pnl_if_lose), 2)
        self.gross_lay_liability = round(
            self.gross_lay_stake * (self.avg_lay_odds - 1) if self.avg_lay_odds > 1 else 0, 2
        )

        # Hedge completion: proportion of back stake covered by lay
        if self.gross_back_stake > 0:
            self.hedge_completion_pct = round(
                min(1.0, self.gross_lay_stake / self.gross_back_stake), 4
            )

        # Commission estimate
        winning_pnl = max(self.pnl_if_win, self.pnl_if_lose)
        self.commission_estimate = round(winning_pnl * c if winning_pnl > 0 else 0, 2)

        # Unrealised P&L: midpoint of win/lose as proxy
        self.unrealized_pnl = round((self.pnl_if_win + self.pnl_if_lose) / 2, 2)

        self.updated_at = datetime.now(timezone.utc).isoformat()

    def add_back_fill(self, stake: float, odds: float):
        """Register a back order fill."""
        if self.gross_back_stake == 0:
            self.avg_back_odds = odds
        else:
            # Weighted average
            total = self.gross_back_stake + stake
            self.avg_back_odds = round(
                (self.avg_back_odds * self.gross_back_stake + odds * stake) / total, 4
            )
        self.gross_back_stake = round(self.gross_back_stake + stake, 2)
        self.recalculate()

    def add_lay_fill(self, stake: float, odds: float):
        """Register a lay order fill."""
        if self.gross_lay_stake == 0:
            self.avg_lay_odds = odds
        else:
            total = self.gross_lay_stake + stake
            self.avg_lay_odds = round(
                (self.avg_lay_odds * self.gross_lay_stake + odds * stake) / total, 4
            )
        self.gross_lay_stake = round(self.gross_lay_stake + stake, 2)
        self.recalculate()

    def settle(self, outcome_win: bool) -> float:
        """Settle the position. Returns net P&L."""
        if outcome_win:
            self.realized_pnl = self.pnl_if_win
        else:
            self.realized_pnl = self.pnl_if_lose
        self.unrealized_pnl = 0.0
        return self.realized_pnl

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "gross_back_stake": self.gross_back_stake,
            "avg_back_odds": self.avg_back_odds,
            "gross_lay_stake": self.gross_lay_stake,
            "avg_lay_odds": self.avg_lay_odds,
            "gross_lay_liability": self.gross_lay_liability,
            "pnl_if_win": self.pnl_if_win,
            "pnl_if_lose": self.pnl_if_lose,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "max_open_loss": self.max_open_loss,
            "hedge_completion_pct": self.hedge_completion_pct,
            "commission_estimate": self.commission_estimate,
            "entry_source": self.entry_source,
            "updated_at": self.updated_at,
        }


@dataclass
class Settlement:
    """Settlement record for a completed trade."""
    trade_id: str
    outcome_win: bool
    gross_pnl: float = 0.0
    commission_paid: float = 0.0
    net_pnl: float = 0.0
    max_adverse_excursion: float = 0.0
    max_favourable_excursion: float = 0.0
    first_lay_filled: bool = False
    full_ladder_filled: bool = False
    used_manual_override: bool = False
    settled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "outcome_win": self.outcome_win,
            "gross_pnl": round(self.gross_pnl, 2),
            "commission_paid": round(self.commission_paid, 2),
            "net_pnl": round(self.net_pnl, 2),
            "max_adverse_excursion": round(self.max_adverse_excursion, 2),
            "max_favourable_excursion": round(self.max_favourable_excursion, 2),
            "first_lay_filled": self.first_lay_filled,
            "full_ladder_filled": self.full_ladder_filled,
            "used_manual_override": self.used_manual_override,
            "settled_at": self.settled_at,
        }
