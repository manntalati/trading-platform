"""The seam between strategies and pre-trade risk.

The engine hands every batch of intents to a ``RiskGate`` together with a ``Book`` (what is held
and what it is worth). Strategies never get a reference to the gate, so they cannot skip or
reconfigure it. The real limits live in ``tp_risk``; ``AllowAll`` is for frictionless research
comparisons only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from tp_trading.events import OrderIntent, RiskDecision


@dataclass(frozen=True)
class Book:
    """The portfolio the limits are measured against, at the signal close.

    In a backtest this is the strategy's own portfolio. In paper trading it is the whole broker
    account (every strategy sleeve), with approved-but-unfilled orders already folded into
    ``positions`` and ``cash``.
    """

    session: date
    equity: float
    cash: float
    positions: Mapping[str, float]  # quantity by symbol
    prices: Mapping[str, float]  # reference price by symbol (latest close)
    previous_equity: float  # equity at the prior close, for the daily loss limit
    average_volume: Mapping[str, float] = field(default_factory=dict)  # 20-session mean shares
    last_bar: Mapping[str, date] = field(default_factory=dict)  # newest bar per symbol


class RiskGate(Protocol):
    def review(self, intents: Sequence[OrderIntent], book: Book) -> list[RiskDecision]:
        """Approve or reject each intent, in order (earlier approvals count against later ones)."""
        ...

    def end_of_day(self, session: date, strategy: str, equity: float) -> None:
        """Observe each strategy's equity after every close (drawdown tracking)."""
        ...


class AllowAll:
    """Approves everything. Only for reproducing frictionless research numbers."""

    def review(self, intents: Sequence[OrderIntent], book: Book) -> list[RiskDecision]:
        return [RiskDecision(intent) for intent in intents]

    def end_of_day(self, session: date, strategy: str, equity: float) -> None:
        return None
