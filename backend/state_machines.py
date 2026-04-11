"""
CHIMERA Scalping Engine — State Machines (B.9)
================================================
Trade, Order, and Control Mode state machines with validated transitions.
Every transition is auditable and evented.

Trade States (B.9.1):
  CANDIDATE → PLANNED → STAGE1_PENDING → STAGE1_ENTERED →
  AWAITING_CONFIRMATION → STAGE2_PENDING → LIVE_EXPOSED →
  LAYING_OUT → PARTIALLY_HEDGED → HEDGED → SETTLED

  Side paths: STOPPING_OUT, INVALIDATED, MANUAL_CONTROL, CANCELLED, ERROR

Control Modes (B.9.2):
  AUTO ↔ ASSISTED ↔ MANUAL_LOCK
  MANUAL_LOCK → AUTO requires explicit operator action

Order States (B.9.3):
  NEW → ACK → PARTIAL → FILLED
  NEW → CANCELLED / REJECTED / EXPIRED
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from enum import Enum


# ─── Trade States ────────────────────────────────────────────────────────────

class TradeState(str, Enum):
    CANDIDATE = "CANDIDATE"
    PLANNED = "PLANNED"
    STAGE1_PENDING = "STAGE1_PENDING"
    STAGE1_ENTERED = "STAGE1_ENTERED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    STAGE2_PENDING = "STAGE2_PENDING"
    LIVE_EXPOSED = "LIVE_EXPOSED"
    LAYING_OUT = "LAYING_OUT"
    PARTIALLY_HEDGED = "PARTIALLY_HEDGED"
    HEDGED = "HEDGED"
    STOPPING_OUT = "STOPPING_OUT"
    INVALIDATED = "INVALIDATED"
    MANUAL_CONTROL = "MANUAL_CONTROL"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


# Valid transitions: {from_state: [allowed_to_states]}
TRADE_TRANSITIONS = {
    TradeState.CANDIDATE: [TradeState.PLANNED, TradeState.CANCELLED],
    TradeState.PLANNED: [
        TradeState.STAGE1_PENDING, TradeState.CANCELLED, TradeState.ERROR,
    ],
    TradeState.STAGE1_PENDING: [
        TradeState.STAGE1_ENTERED, TradeState.CANCELLED, TradeState.ERROR,
    ],
    TradeState.STAGE1_ENTERED: [
        TradeState.AWAITING_CONFIRMATION, TradeState.INVALIDATED,
        TradeState.STOPPING_OUT, TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.AWAITING_CONFIRMATION: [
        TradeState.STAGE2_PENDING, TradeState.INVALIDATED,
        TradeState.STOPPING_OUT, TradeState.MANUAL_CONTROL,
        TradeState.LIVE_EXPOSED, TradeState.ERROR,
    ],
    TradeState.STAGE2_PENDING: [
        TradeState.LIVE_EXPOSED, TradeState.INVALIDATED,
        TradeState.STOPPING_OUT, TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.LIVE_EXPOSED: [
        TradeState.LAYING_OUT, TradeState.STOPPING_OUT,
        TradeState.MANUAL_CONTROL, TradeState.INVALIDATED, TradeState.ERROR,
    ],
    TradeState.LAYING_OUT: [
        TradeState.PARTIALLY_HEDGED, TradeState.HEDGED,
        TradeState.STOPPING_OUT, TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.PARTIALLY_HEDGED: [
        TradeState.HEDGED, TradeState.LAYING_OUT,
        TradeState.STOPPING_OUT, TradeState.MANUAL_CONTROL,
        TradeState.SETTLED, TradeState.ERROR,
    ],
    TradeState.HEDGED: [
        TradeState.SETTLED, TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.STOPPING_OUT: [
        TradeState.SETTLED, TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.INVALIDATED: [
        TradeState.STOPPING_OUT, TradeState.SETTLED,
        TradeState.MANUAL_CONTROL, TradeState.ERROR,
    ],
    TradeState.MANUAL_CONTROL: [
        # Can return to any active state or settle
        TradeState.LIVE_EXPOSED, TradeState.LAYING_OUT,
        TradeState.PARTIALLY_HEDGED, TradeState.HEDGED,
        TradeState.STOPPING_OUT, TradeState.SETTLED,
        TradeState.CANCELLED, TradeState.ERROR,
    ],
    TradeState.SETTLED: [],   # Terminal
    TradeState.CANCELLED: [],  # Terminal
    TradeState.ERROR: [
        TradeState.MANUAL_CONTROL, TradeState.CANCELLED,
    ],
}


# ─── Control Modes ───────────────────────────────────────────────────────────

class ControlMode(str, Enum):
    AUTO = "AUTO"
    ASSISTED = "ASSISTED"
    MANUAL_LOCK = "MANUAL_LOCK"


CONTROL_TRANSITIONS = {
    ControlMode.AUTO: [ControlMode.ASSISTED, ControlMode.MANUAL_LOCK],
    ControlMode.ASSISTED: [ControlMode.AUTO, ControlMode.MANUAL_LOCK],
    # MANUAL_LOCK → AUTO requires explicit operator action (allowed but audited)
    ControlMode.MANUAL_LOCK: [ControlMode.ASSISTED, ControlMode.AUTO],
}


# ─── Order States ────────────────────────────────────────────────────────────

class OrderState(str, Enum):
    NEW = "NEW"
    ACK = "ACK"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REPLACE_PENDING = "REPLACE_PENDING"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


ORDER_TRANSITIONS = {
    OrderState.NEW: [
        OrderState.ACK, OrderState.REJECTED, OrderState.CANCELLED,
    ],
    OrderState.ACK: [
        OrderState.PARTIAL, OrderState.FILLED,
        OrderState.CANCEL_PENDING, OrderState.REPLACE_PENDING,
        OrderState.EXPIRED,
    ],
    OrderState.PARTIAL: [
        OrderState.FILLED, OrderState.CANCEL_PENDING,
        OrderState.REPLACE_PENDING, OrderState.EXPIRED,
    ],
    OrderState.FILLED: [],  # Terminal
    OrderState.CANCEL_PENDING: [
        OrderState.CANCELLED, OrderState.FILLED,  # Race condition: filled before cancel
    ],
    OrderState.CANCELLED: [],  # Terminal
    OrderState.REPLACE_PENDING: [
        OrderState.ACK, OrderState.REJECTED, OrderState.FILLED,
    ],
    OrderState.REJECTED: [],  # Terminal
    OrderState.EXPIRED: [],   # Terminal
}


# ─── Transition Audit ────────────────────────────────────────────────────────

@dataclass
class StateTransition:
    """Immutable record of a state transition."""
    entity_type: str           # "trade", "order", "control"
    entity_id: str
    from_state: str
    to_state: str
    reason: str = ""
    user_id: Optional[str] = None   # None = system action
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
        }


def validate_trade_transition(current: TradeState, target: TradeState) -> bool:
    """Check if a trade state transition is allowed."""
    return target in TRADE_TRANSITIONS.get(current, [])


def validate_order_transition(current: OrderState, target: OrderState) -> bool:
    """Check if an order state transition is allowed."""
    return target in ORDER_TRANSITIONS.get(current, [])


def validate_control_transition(current: ControlMode, target: ControlMode) -> bool:
    """Check if a control mode transition is allowed."""
    return target in CONTROL_TRANSITIONS.get(current, [])


# ─── Side helpers ────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BACK = "BACK"
    LAY = "LAY"


class OrderSource(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class EntrySource(str, Enum):
    BOOKMAKER = "BOOKMAKER"
    EXCHANGE = "EXCHANGE"


class EntryMode(str, Enum):
    EARLY_PROBE = "EARLY_PROBE"
    CONFIRM_ADD = "CONFIRM_ADD"
    LATE_ADD = "LATE_ADD"


class ExitPolicy(str, Enum):
    FORCE_GREEN_PRE_OFF = "FORCE_GREEN_PRE_OFF"
    PRE_RACE_ONLY = "PRE_RACE_ONLY"
    ALLOW_RESIDUAL_INPLAY = "ALLOW_RESIDUAL_INPLAY"


class RaceType(str, Enum):
    FLAT = "FLAT"
    AW = "AW"
    JUMPS = "JUMPS"


class StakeTier(str, Enum):
    MICRO = "MICRO"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
    MAX = "MAX"


class LadderPurpose(str, Enum):
    PRIMARY_DERISK = "PRIMARY_DERISK"
    SECONDARY_DERISK = "SECONDARY_DERISK"
    OPTIONAL_EDGE = "OPTIONAL_EDGE"


class ConfidenceBand(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    PRIORITY = "PRIORITY"
