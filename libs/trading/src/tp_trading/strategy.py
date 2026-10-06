"""The strategy contract.

A strategy is a frozen dataclass of parameters plus ``on_bar``. It reads the world through a
``Context`` and says what it wants with ``ctx.order(...)`` or ``ctx.order_target_weights(...)``.
It never sees the broker, the risk limits, or any data later than ``ctx.now``. The same object
runs in the backtester (``BacktestContext``) and in paper trading, so a paper result is a test of
the backtest, not of a re-implementation.

Strategies should be stateless between bars: derive everything from ``ctx.history`` and
``ctx.positions``. A paper-trading run starts a fresh process each day, so state kept on the
strategy object would not survive.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, ClassVar

import pandas as pd

from tp_core.calendar import is_last_session_of_month
from tp_trading.data import Field
from tp_trading.events import Fill, OrderIntent, OrderType, Side
from tp_trading.portfolio import EPSILON, Position


@dataclass(frozen=True)
class Strategy(ABC):
    """Base class. Subclasses are frozen dataclasses whose fields are the parameters."""

    name: ClassVar[str]  # stable id: config keys, order ids, ledger sleeves
    title: ClassVar[str] = ""
    spec: ClassVar[str] = ""  # path of the written spec under docs/strategies

    @abstractmethod
    def symbols(self) -> list[str]:
        """Every symbol the strategy may read or trade."""

    @abstractmethod
    def on_bar(self, ctx: Context) -> None:
        """Called after each session's close with data up to and including that close."""

    def on_fill(self, ctx: Context, fill: Fill) -> None:  # noqa: B027  # optional hook
        """Called for each fill of this strategy's orders."""

    def params(self) -> dict[str, Any]:
        return asdict(self)


class Context(ABC):
    """What a strategy can see and do during ``on_bar``.

    Subclasses provide data, positions and equity; ordering and sizing live here so that backtest
    and paper size orders identically.
    """

    def __init__(
        self,
        strategy: str,
        *,
        fractional: bool = False,
        sectors: Mapping[str, str] | None = None,
        catch_up: bool = False,
    ) -> None:
        self.strategy = strategy
        self.fractional = fractional
        self._sectors = dict(sectors or {})
        # True when a strategy starts trading part-way through its cycle (a new paper sleeve):
        # it should take the positions its rules call for now, not wait for the next rebalance.
        self.catch_up = catch_up
        self._intents: list[OrderIntent] = []

    # -- provided by the runtime -------------------------------------------------------------------

    @property
    @abstractmethod
    def now(self) -> date:
        """The session whose close was just observed."""

    @abstractmethod
    def history(self, field: Field = "close", lookback: int | None = None) -> pd.DataFrame:
        """Daily adjusted bars up to and including ``now``: one column per strategy symbol.
        ``lookback`` limits it to the last N sessions."""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """The price orders are sized with: the latest close (NaN if the symbol has none)."""

    @property
    @abstractmethod
    def positions(self) -> Mapping[str, Position]:
        """This strategy's open positions."""

    @property
    @abstractmethod
    def equity(self) -> float:
        """This strategy's capital at the latest close: cash plus positions."""

    @property
    @abstractmethod
    def cash(self) -> float: ...

    # -- helpers ----------------------------------------------------------------------------------

    def quantity(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return p.quantity if p else 0.0

    def weight(self, symbol: str) -> float:
        equity = self.equity
        return self.quantity(symbol) * self.price(symbol) / equity if equity > 0 else 0.0

    def is_last_session_of_month(self) -> bool:
        return is_last_session_of_month(self.now)

    def is_rebalance_day(self) -> bool:
        """For monthly strategies: the last session of the month, or any session while catching
        up (a sleeve that has never traded shouldn't sit in cash until month end)."""
        return self.catch_up or self.is_last_session_of_month()

    def sector(self, symbol: str) -> str | None:
        """GICS sector of a single name (reference data from config/classifications.toml), or
        None for funds and unclassified symbols."""
        return self._sectors.get(symbol)

    # -- ordering ---------------------------------------------------------------------------------

    def order(
        self,
        symbol: str,
        quantity: float,
        *,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        reason: str = "",
        target_weight: float | None = None,
    ) -> None:
        """Ask to buy (``quantity > 0``) or sell (``< 0``) shares at the next fill opportunity."""
        if abs(quantity) < EPSILON:
            return
        price = self.price(symbol)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"{self.strategy}: no usable price for {symbol} on {self.now}")
        self._intents.append(
            OrderIntent(
                strategy=self.strategy,
                symbol=symbol,
                side=Side.BUY if quantity > 0 else Side.SELL,
                quantity=abs(quantity),
                session=self.now,
                reference_price=price,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                target_weight=target_weight,
                reason=reason,
            )
        )

    def order_target_weights(
        self,
        targets: Mapping[str, float],
        *,
        reason: str | Mapping[str, str] = "",
        min_change: float = 0.0,
    ) -> None:
        """Rebalance to ``targets`` (fraction of this strategy's equity per symbol).

        Held symbols missing from ``targets`` are sold. Changes smaller than ``min_change`` (in
        weight) are skipped, except closing a position entirely. If skipping small trims would
        leave the book above 100% of equity after the buys, the buys are scaled down: a
        rebalance never borrows. Sells come first so a broker that executes in order frees cash
        before buying. ``reason`` is one explanation for the whole rebalance, or one per symbol.
        """
        if any(w < 0 for w in targets.values()):
            raise ValueError("target weights must be >= 0 (no shorting)")
        if sum(targets.values()) > 1 + 1e-9:
            raise ValueError("target weights must sum to at most 1 (no leverage)")
        equity = self.equity
        wanted = dict(targets)
        for symbol in self.positions:
            wanted.setdefault(symbol, 0.0)
        orders: list[tuple[str, float, float]] = []
        for symbol, target in wanted.items():
            held = self.quantity(symbol)
            if target == 0.0:
                if abs(held) >= EPSILON:
                    orders.append((symbol, -held, 0.0))
                continue
            price = self.price(symbol)
            if not math.isfinite(price) or price <= 0:
                continue  # no price, no order (e.g. not listed yet)
            if abs(target - held * price / equity) < min_change:
                continue
            orders.append((symbol, target * equity / price - held, target))
        orders = self._within_equity(orders, equity)
        if not self.fractional:
            # Whole shares, rounded down after the trade: buys never overshoot the cash or the
            # target, and trims never leave a position above it (which would breach a cap).
            orders = [(s, _whole(self.quantity(s), d), t) for s, d, t in orders]
        for symbol, delta, target in sorted(orders, key=lambda o: o[1] > 0):
            why = reason if isinstance(reason, str) else reason.get(symbol, "")
            self.order(symbol, delta, reason=why, target_weight=target)

    def _within_equity(
        self, orders: list[tuple[str, float, float]], equity: float
    ) -> list[tuple[str, float, float]]:
        after = {symbol: self.quantity(symbol) for symbol in self.positions}
        for symbol, delta, _ in orders:
            after[symbol] = after.get(symbol, 0.0) + delta
        gross = 0.0
        for symbol, quantity in after.items():
            price = self.price(symbol)
            gross += abs(quantity) * (price if math.isfinite(price) else 0.0)
        buys = sum(d * self.price(s) for s, d, _ in orders if d > 0)
        if gross <= equity * (1 + 1e-12) or buys <= 0:
            return orders
        scale = max(0.0, 1.0 - (gross - equity) / buys)
        return [(s, d * scale if d > 0 else d, t) for s, d, t in orders]

    def drain(self) -> list[OrderIntent]:
        """Hand the collected intents to the runtime (and forget them)."""
        intents, self._intents = self._intents, []
        return intents


def _whole(held: float, delta: float) -> float:
    """The change that leaves a whole number of shares at or below ``held + delta``."""
    return float(math.floor(held + delta + EPSILON) - held)
