"""
CHIMERA Scalping Engine — Runner Model
========================================
Data class used by BetfairClient and FSUClient for market price retrieval.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Runner:
    """Represents a runner in a Betfair market with current pricing."""
    selection_id: int
    runner_name: str
    handicap: float = 0.0
    status: str = "ACTIVE"
    best_available_to_lay: Optional[float] = None
    best_available_to_back: Optional[float] = None
