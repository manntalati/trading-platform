"""Messages between strategy, risk, execution and portfolio.

Strategies only ever produce ``OrderIntent``: a request, not an order. Whether it becomes an order
is decided outside strategy code (risk checks, and a human in paper/live trading).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"  # approved, waiting for the next fill opportunity
    FILLED = "filled"
    PARTIAL = "partial"  # filled for less than asked (e.g. not enough cash)
    EXPIRED = "expired"  # day order that could not fill (no bar, limit not reached, no cash)
    REJECTED = "rejected"  # stopped by the risk gate


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants to trade, sized at the signal close.

    ``quantity`` is always positive; ``side`` says which way. ``target_weight`` is set when the
    intent comes from a target-weight rebalance, so an execution model may re-size it at the
    fill price. ``stop_price`` is the protective stop the strategy plans to use, if any; the risk
    gate uses it for the risk-per-trade limit.
    """

    strategy: str
    symbol: str
    side: Side
    quantity: float
    session: date  # signal session: the close whose data produced this intent
    reference_price: float  # price used for sizing (the signal close)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    target_weight: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.quantity > 0 or not math.isfinite(self.quantity):
            raise ValueError(f"intent quantity must be positive and finite, got {self.quantity}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit intents need a limit_price")

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity

    @property
    def notional(self) -> float:
        return self.quantity * self.reference_price

    def order_id(self, seq: int) -> str:
        """Deterministic id: re-running the same signal produces the same ids, so a broker that
        rejects duplicate client order ids makes resubmission harmless."""
        return f"{self.strategy}-{self.session:%Y%m%d}-{self.symbol}-{self.side}-{seq}"


@dataclass(frozen=True)
class CheckResult:
    """One pre-trade check on one intent."""

    check: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    intent: OrderIntent
    checks: tuple[CheckResult, ...] = ()

    @property
    def approved(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.check}: {c.detail}" for c in self.checks if not c.passed]


@dataclass(frozen=True)
class Fill:
    order_id: str
    strategy: str
    symbol: str
    side: Side
    quantity: float  # positive
    price: float  # per share, after slippage
    fees: float  # commissions and regulatory fees, in currency
    session: date
    slippage: float = 0.0  # cost of the price concession vs the bar price, in currency

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass
class Order:
    """An approved intent travelling to execution, and what happened to it."""

    id: str
    intent: OrderIntent
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    fill_price: float | None = None
    note: str = ""
    fills: list[Fill] = field(default_factory=list)
    # Signed quantity decided at fill time, when the execution model re-sizes target-weight
    # orders (``size_at="fill"``); None means trade the intent as sized at the signal.
    resized_quantity: float | None = None

    @property
    def side(self) -> Side:
        if self.resized_quantity is not None and self.resized_quantity != 0:
            return Side.BUY if self.resized_quantity > 0 else Side.SELL
        return self.intent.side
