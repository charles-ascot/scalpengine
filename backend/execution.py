"""
CHIMERA Scalping Engine — Execution Service (B.14)
====================================================
Order intent versus venue reality.

Two venues sit behind one interface:

  BetfairVenue    Real orders. Refuses to submit unless LIVE_ORDERS_ENABLED is
                  true in the environment — an interlock independent of the
                  dashboard's dry-run flag, so real money needs a deliberate,
                  reviewed deploy and not just a click.

  SimulatedVenue  Dry run. Reads real Betfair prices, simulates matching, and
                  places nothing.

Both report order state in the shape of Betfair's own listCurrentOrders
records, so reconciliation, fill extraction and position building are the same
code for live and simulated orders (B.19: no separate approximate logic).

Positions are rebuilt from the fill log rather than accumulated, so a restart,
a repeated poll or a duplicate report can never double-count a fill (B.25:
reconstruct state from fills).
"""

import logging
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Callable, Optional

from position import PositionSnapshot
from state_machines import OrderState, StateTransition, validate_order_transition

logger = logging.getLogger("scalp_engine.execution")

LIVE_ORDERS_ENABLED = os.environ.get("LIVE_ORDERS_ENABLED", "false").lower() == "true"

STRATEGY_REF = "scalp-1.0.0"   # Betfair customerStrategyRef, max 15 chars (A.12)
MIN_STAKE = 1.00               # Betfair GBP minimum stake — confirm against current rules
SIZE_EPS = 0.005               # stakes are in pence; anything smaller is rounding noise
UNCERTAIN_POLL_LIMIT = 3       # polls before an unconfirmed submit is declared not placed

OPEN_STATES = {"NEW", "ACK", "PARTIAL", "CANCEL_PENDING", "REPLACE_PENDING"}

# Betfair price ladder: (upper bound of band, tick size within it)
_LADDER = [(2, 0.01), (3, 0.02), (4, 0.05), (6, 0.1), (10, 0.2),
           (20, 0.5), (30, 1), (50, 2), (100, 5), (1000, 10)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_price(price: float) -> bool:
    """True if price sits exactly on Betfair's tick ladder."""
    if price < 1.01 or price > 1000:
        return False
    lower = 1.0
    for upper, tick in _LADDER:
        if price <= upper + 1e-9:
            steps = (price - lower) / tick
            return abs(steps - round(steps)) < 1e-6
        lower = upper
    return False


# ──────────────────────────────────────────────
#  RECORDS
# ──────────────────────────────────────────────

@dataclass
class Order:
    """One order, as intended and as the venue last reported it (B.8, B.17)."""
    order_id: str
    trade_id: str
    market_id: str
    selection_id: int
    side: str                         # BACK / LAY
    price: float
    requested_stake: float
    venue: str                        # BETFAIR / SIMULATED
    source: str = "AUTO"              # AUTO / MANUAL
    rung: Optional[int] = None
    status: str = OrderState.NEW.value
    bet_id: Optional[str] = None
    matched_stake: float = 0.0
    avg_matched_price: float = 0.0
    size_lapsed: float = 0.0
    size_cancelled: float = 0.0
    customer_order_ref: str = ""
    strategy_ref: str = STRATEGY_REF
    error_code: Optional[str] = None
    submit_uncertain: bool = False
    uncertain_polls: int = 0
    review_reason: Optional[str] = None
    sim_consumed: dict = field(default_factory=dict)   # SIMULATED only: "price" -> stake taken
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def remaining(self) -> float:
        left = self.requested_stake - self.matched_stake - self.size_lapsed - self.size_cancelled
        return max(0.0, round(left, 2))

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["remaining"] = self.remaining
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Fill:
    """A matched increment of an order (B.17 fills table)."""
    fill_id: str
    order_id: str
    trade_id: str
    side: str
    price: float
    stake: float
    venue: str
    occurred_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Fill":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def build_position(trade_id: str, fills: list, entry_source: str = "EXCHANGE") -> PositionSnapshot:
    """Rebuild a trade's position from its fills, oldest first (B.15)."""
    pos = PositionSnapshot(trade_id=trade_id, entry_source=entry_source)
    for f in sorted(fills, key=lambda f: f.occurred_at):
        if f.side == "BACK":
            pos.add_back_fill(f.stake, f.price)
        else:
            pos.add_lay_fill(f.stake, f.price)
    return pos


# ──────────────────────────────────────────────
#  VENUES
# ──────────────────────────────────────────────
#
# submit(order) -> {"accepted": True|False, "bet_id", "size_matched", "avg_price",
#                   "order_status", "error_code"}, or {"uncertain": True, ...}
#                   when the venue cannot say whether the order exists.
# poll(market_id, orders) -> list of listCurrentOrders-shaped dicts, or None if
#                   the market could not be read (change nothing, flag nothing).
# cancel(order) -> {"ok": bool, "size_cancelled", "error_code"}


class BetfairVenue:
    name = "BETFAIR"

    def __init__(self, get_client: Callable[[], object], live_enabled: Optional[bool] = None):
        self.get_client = get_client
        self.live_enabled = LIVE_ORDERS_ENABLED if live_enabled is None else live_enabled

    def submit(self, order: Order) -> dict:
        if not self.live_enabled:
            return {"accepted": False, "error_code": "LIVE_ORDERS_DISABLED"}
        client = self.get_client()
        if client is None:
            return {"accepted": False, "error_code": "NOT_AUTHENTICATED"}
        r = client.place_order(
            order.market_id, order.selection_id, order.side, order.price, order.requested_stake,
            customer_order_ref=order.customer_order_ref,
            customer_strategy_ref=order.strategy_ref,
            customer_ref=order.order_id,
        )
        if r is None:
            return {"uncertain": True, "error_code": "SUBMIT_UNCONFIRMED"}
        if r.get("status") != "SUCCESS":
            return {"accepted": False, "error_code": r.get("error_code") or "REJECTED"}
        return {
            "accepted": True,
            "bet_id": r.get("bet_id"),
            "size_matched": r.get("size_matched", 0.0),
            "avg_price": r.get("avg_price_matched", 0.0),
            "order_status": r.get("order_status"),
        }

    def poll(self, market_id: str, orders: list) -> Optional[list]:
        client = self.get_client()
        if client is None:
            return None
        return client.get_current_orders(market_ids=[market_id])

    def cancel(self, order: Order) -> dict:
        # Always allowed, even with live orders disabled: cancelling only ever
        # reduces risk, and real orders may outlive the flag being turned off.
        client = self.get_client()
        if client is None or not order.bet_id:
            return {"ok": False, "error_code": "NOT_AUTHENTICATED" if client is None else "NO_BET_ID"}
        r = client.cancel_order(order.market_id, order.bet_id)
        if r.get("status") == "SUCCESS":
            return {"ok": True, "size_cancelled": r.get("size_cancelled", 0.0)}
        return {"ok": False, "error_code": r.get("error_code", "UNKNOWN")}


class SimulatedVenue:
    """Dry-run venue: real Betfair prices, simulated matching, nothing placed.

    Fill model — deliberately conservative, so a dry run never looks better
    than the market would have allowed:

      * At submission, the order matches immediately against displayed
        liquidity that crosses its price (a BACK at P takes back-side offers
        at P or better, best first), at those offered prices.
      * A resting order matches later only when the market trades through
        it — liquidity appears that crosses its price — and then at the
        order's own price, as the resting side of a real match would be.
      * Queue position is never assumed: a resting order sitting at the
        best price is not filled by trading at that price.
      * Displayed liquidity is never reused: what an order has already taken
        at a price is remembered across polls, and liquidity is shared
        between our own orders within a poll.
      * Unmatched stake lapses when the market turns in-play or closes,
        matching LAPSE persistence. Suspension changes nothing.
      * In-play submission is refused rather than simulated without the
        in-play bet delay.

    If market data cannot be fetched the order is refused or left unchanged.
    Nothing is ever filled against invented prices.
    """
    name = "SIMULATED"

    def __init__(self, get_book: Callable[[str], Optional[dict]]):
        self.get_book = get_book

    @staticmethod
    def _runner(book: dict, selection_id: int) -> Optional[dict]:
        for r in book.get("runners", []):
            if int(r.get("selectionId", -1)) == int(selection_id):
                return r
        return None

    @staticmethod
    def _match(order: Order, size: float, runner: dict, poll_used: dict, resting: bool):
        ex = runner.get("ex", {}) or {}
        if order.side == "BACK":
            levels = sorted(ex.get("availableToBack", []), key=lambda l: -l["price"])
            crosses = lambda p: p >= order.price - 1e-9
        else:
            levels = sorted(ex.get("availableToLay", []), key=lambda l: l["price"])
            crosses = lambda p: p <= order.price + 1e-9

        consumed = dict(order.sim_consumed)
        left, notional = round(size, 2), 0.0
        for lvl in levels:
            p = float(lvl["price"])
            if not crosses(p):
                break
            key = f"{p:.2f}"
            pk = (order.selection_id, order.side, key)
            avail = float(lvl["size"]) - consumed.get(key, 0.0) - poll_used.get(pk, 0.0)
            take = round(min(left, avail), 2)
            if take <= SIZE_EPS:
                continue
            notional += take * (order.price if resting else p)
            left = round(left - take, 2)
            consumed[key] = round(consumed.get(key, 0.0) + take, 2)
            poll_used[pk] = round(poll_used.get(pk, 0.0) + take, 2)
            if left <= SIZE_EPS:
                break
        matched = round(size - left, 2)
        return matched, (notional / matched if matched > 0 else 0.0), consumed

    def submit(self, order: Order) -> dict:
        book = self.get_book(order.market_id)
        if book is None:
            return {"accepted": False, "error_code": "NO_MARKET_DATA"}
        if book.get("status") != "OPEN":
            return {"accepted": False, "error_code": f"MARKET_{book.get('status', 'UNKNOWN')}"}
        if book.get("inplay"):
            return {"accepted": False, "error_code": "SIM_INPLAY_NOT_SIMULATED"}
        runner = self._runner(book, order.selection_id)
        if runner is None or runner.get("status", "ACTIVE") != "ACTIVE":
            return {"accepted": False, "error_code": "RUNNER_NOT_ACTIVE"}

        matched, avg, consumed = self._match(order, order.requested_stake, runner, {}, resting=False)
        order.sim_consumed = consumed
        return {
            "accepted": True,
            "bet_id": f"SIM-{secrets.token_hex(6)}",
            "size_matched": matched,
            "avg_price": round(avg, 4),
            "order_status": "EXECUTION_COMPLETE" if matched >= order.requested_stake - SIZE_EPS else "EXECUTABLE",
        }

    def poll(self, market_id: str, orders: list) -> Optional[list]:
        book = self.get_book(market_id)
        if book is None:
            return None
        poll_used: dict = {}
        closed = book.get("status") == "CLOSED" or bool(book.get("inplay"))
        suspended = book.get("status") == "SUSPENDED"
        reports = []
        for o in orders:
            matched, avg = o.matched_stake, o.avg_matched_price
            lapsed, cancelled = o.size_lapsed, o.size_cancelled
            remaining = o.remaining
            runner = self._runner(book, o.selection_id)

            if remaining > SIZE_EPS and o.status != OrderState.CANCEL_PENDING.value:
                if closed or runner is None or runner.get("status", "ACTIVE") != "ACTIVE":
                    lapsed = round(lapsed + remaining, 2)
                elif not suspended:
                    got, got_avg, consumed = self._match(o, remaining, runner, poll_used, resting=True)
                    if got > 0:
                        avg = (avg * matched + got_avg * got) / (matched + got)
                        matched = round(matched + got, 2)
                        o.sim_consumed = consumed
            left = round(o.requested_stake - matched - lapsed - cancelled, 2)
            reports.append({
                "betId": o.bet_id,
                "customerOrderRef": o.customer_order_ref,
                "status": "EXECUTABLE" if left > SIZE_EPS else "EXECUTION_COMPLETE",
                "sizeMatched": matched,
                "averagePriceMatched": round(avg, 4),
                "sizeRemaining": max(0.0, left),
                "sizeLapsed": lapsed,
                "sizeCancelled": cancelled,
                "sizeVoided": 0.0,
            })
        return reports

    def cancel(self, order: Order) -> dict:
        return {"ok": True, "size_cancelled": order.remaining}


# ──────────────────────────────────────────────
#  ORDER MANAGER
# ──────────────────────────────────────────────

class OrderManager:
    """Owns orders and fills; submits, cancels and reconciles against venues."""

    def __init__(self, venues: dict):
        self.venues = venues                  # {"BETFAIR": ..., "SIMULATED": ...}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.events: list[dict] = []          # StateTransition dicts; the engine drains these
        self.flagged: dict[str, str] = {}     # trade_id -> reason; the engine drains these

    # ── persistence ──

    def load(self, orders: list, fills: list):
        self.orders = {d["order_id"]: Order.from_dict(d) for d in orders or []}
        self.fills = [Fill.from_dict(d) for d in fills or []]

    def dump(self) -> tuple[list, list]:
        return [o.to_dict() for o in self.orders.values()], [f.to_dict() for f in self.fills]

    def orders_for(self, trade_id: str) -> list:
        return [o for o in self.orders.values() if o.trade_id == trade_id]

    def fills_for(self, trade_id: str) -> list:
        return [f for f in self.fills if f.trade_id == trade_id]

    # ── audit ──

    def _event(self, order: Order, from_state: str, to_state: str, reason: str, user_id=None):
        self.events.append(StateTransition(
            entity_type="order", entity_id=order.order_id,
            from_state=from_state, to_state=to_state, reason=reason, user_id=user_id,
        ).to_dict())

    def _flag(self, order: Order, reason: str):
        """Venue reality diverged from intent: record it and escalate (B.22)."""
        order.review_reason = reason if not order.review_reason else f"{order.review_reason}; {reason}"
        self.flagged[order.trade_id] = reason
        self._event(order, order.status, order.status, f"REVIEW: {reason}")
        logger.warning(f"Order {order.order_id} ({order.trade_id}) needs review: {reason}")

    def _transition(self, order: Order, target: str, reason: str, user_id=None):
        if target == order.status:
            return
        # A report can skip ACK (an order that matched on placement); walk through it.
        steps = [target]
        if order.status == OrderState.NEW.value and target != OrderState.ACK.value \
                and validate_order_transition(OrderState.ACK, OrderState(target)):
            steps = [OrderState.ACK.value, target]
        for step in steps:
            if not validate_order_transition(OrderState(order.status), OrderState(step)):
                # Venue truth wins — we cannot pretend otherwise — but a move the
                # state machine forbids means intent and reality have diverged.
                self._flag(order, f"venue moved order {order.status} -> {step}, which the order state machine forbids")
            self._event(order, order.status, step, reason, user_id)
            order.status = step
        order.updated_at = _now()

    # ── submission ──

    def submit(self, trade_id: str, market_id: str, selection_id: int, side: str,
               price: float, size: float, simulated: bool, source: str = "AUTO",
               rung: Optional[int] = None, user_id: Optional[str] = None) -> Order:
        seq = len(self.orders_for(trade_id)) + 1
        order = Order(
            order_id=f"ord_{secrets.token_hex(6)}",
            trade_id=trade_id, market_id=market_id, selection_id=int(selection_id),
            side=side, price=round(float(price), 2), requested_stake=round(float(size), 2),
            venue="SIMULATED" if simulated else "BETFAIR",
            source=source, rung=rung,
            customer_order_ref=f"{trade_id}-{seq}"[:32],
        )
        self.orders[order.order_id] = order
        self._event(order, "NONE", OrderState.NEW.value, f"{source} {side} {size} @ {price}", user_id)

        # Refuse locally anything the venue would refuse, so dry run and live agree.
        error = None
        if side not in ("BACK", "LAY"):
            error = "INVALID_SIDE"
        elif not is_valid_price(order.price):
            error = "INVALID_ODDS"
        elif order.requested_stake < MIN_STAKE - 1e-9:
            error = "INVALID_BET_SIZE"
        if error:
            order.error_code = error
            self._transition(order, OrderState.REJECTED.value, error, user_id)
            return order

        result = self.venues[order.venue].submit(order)

        if result.get("uncertain"):
            order.submit_uncertain = True
            order.error_code = result.get("error_code")
            self._flag(order, "submit unconfirmed — the order may exist at the venue; reconciling by customerOrderRef")
            return order
        if not result.get("accepted"):
            order.error_code = result.get("error_code")
            self._transition(order, OrderState.REJECTED.value, order.error_code or "REJECTED", user_id)
            return order

        order.bet_id = result.get("bet_id")
        self._transition(order, OrderState.ACK.value, "venue accepted", user_id)
        self._apply_report(order, {
            "status": result.get("order_status") or "EXECUTABLE",
            "sizeMatched": result.get("size_matched", 0.0),
            "averagePriceMatched": result.get("avg_price", 0.0),
        })
        return order

    def cancel(self, order_id: str, reason: str = "", user_id: Optional[str] = None) -> dict:
        order = self.orders.get(order_id)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if not order.is_open or order.status == OrderState.NEW.value:
            return {"ok": False, "error_code": f"NOT_CANCELLABLE_{order.status}"}
        r = self.venues[order.venue].cancel(order)
        if not r.get("ok"):
            # Leave the state alone: marking a live order CANCEL_PENDING after a
            # failed cancel would make it look safe when it is not.
            self._event(order, order.status, order.status, f"cancel failed: {r.get('error_code')}", user_id)
            return r
        self._transition(order, OrderState.CANCEL_PENDING.value, reason or "cancel requested", user_id)
        if order.venue == "SIMULATED":
            order.size_cancelled = round(order.size_cancelled + r.get("size_cancelled", 0.0), 2)
            self._transition(order, OrderState.CANCELLED.value, "cancelled", user_id)
        # A real cancel is confirmed by the next reconcile, not assumed here.
        return r

    # ── reconciliation ──

    def _apply_report(self, order: Order, rep: dict) -> list:
        new_matched = round(float(rep.get("sizeMatched") or 0.0), 2)
        new_avg = float(rep.get("averagePriceMatched") or 0.0)
        new_fills = []

        delta = round(new_matched - order.matched_stake, 2)
        if delta > SIZE_EPS:
            notional = new_avg * new_matched - order.avg_matched_price * order.matched_stake
            fill = Fill(
                fill_id=f"fil_{secrets.token_hex(6)}", order_id=order.order_id,
                trade_id=order.trade_id, side=order.side,
                price=round(notional / delta, 4), stake=delta, venue=order.venue,
            )
            new_fills.append(fill)
            self.fills.append(fill)
            order.matched_stake, order.avg_matched_price = new_matched, round(new_avg, 4)
        elif delta < -SIZE_EPS:
            # Matched stake fell — bets voided at the venue. The fill log keeps
            # the larger figure (overstating exposure is the safe direction).
            self._flag(order, f"matched stake fell {order.matched_stake} -> {new_matched}")
            order.matched_stake, order.avg_matched_price = new_matched, round(new_avg, 4)

        order.size_lapsed = round(float(rep.get("sizeLapsed") or order.size_lapsed), 2)
        order.size_cancelled = round(float(rep.get("sizeCancelled") or order.size_cancelled), 2)
        if float(rep.get("sizeVoided") or 0.0) > SIZE_EPS:
            self._flag(order, f"{rep['sizeVoided']} voided at venue")

        if rep.get("status") == "EXECUTION_COMPLETE":
            if order.matched_stake >= order.requested_stake - SIZE_EPS:
                target = OrderState.FILLED.value
            elif order.size_lapsed > SIZE_EPS:
                target = OrderState.EXPIRED.value
            elif order.size_cancelled > SIZE_EPS:
                target = OrderState.CANCELLED.value
            else:
                target = OrderState.FILLED.value if order.matched_stake > 0 else OrderState.CANCELLED.value
        elif order.status == OrderState.CANCEL_PENDING.value:
            target = order.status
        else:
            target = OrderState.PARTIAL.value if order.matched_stake > SIZE_EPS else OrderState.ACK.value

        self._transition(order, target, "venue report")
        return new_fills

    def reconcile(self) -> list:
        """Poll every venue for every open order; return the new fills."""
        new_fills = []
        for venue_name, venue in self.venues.items():
            by_market: dict[str, list] = {}
            for o in self.orders.values():
                if o.venue == venue_name and o.is_open:
                    by_market.setdefault(o.market_id, []).append(o)

            for market_id, orders in by_market.items():
                reports = venue.poll(market_id, orders)
                if reports is None:
                    continue     # market unreadable: change nothing, flag nothing
                by_bet = {r.get("betId"): r for r in reports if r.get("betId")}
                by_ref = {r.get("customerOrderRef"): r for r in reports if r.get("customerOrderRef")}

                for o in orders:
                    rep = by_bet.get(o.bet_id) if o.bet_id else None
                    if rep is None and o.submit_uncertain:
                        rep = by_ref.get(o.customer_order_ref)
                        if rep is None:
                            o.uncertain_polls += 1
                            if o.uncertain_polls >= UNCERTAIN_POLL_LIMIT:
                                o.submit_uncertain = False
                                o.error_code = "NOT_PLACED"
                                self._transition(o, OrderState.REJECTED.value,
                                                 f"not at venue after {o.uncertain_polls} polls")
                            continue
                        o.bet_id, o.submit_uncertain = rep.get("betId"), False
                    if rep is None:
                        self._flag(o, "open order missing from the venue's current orders")
                        continue
                    new_fills += self._apply_report(o, rep)
        return new_fills
