"""Simulated execution: approved orders fill on the session after their signal, never on it.

- ``fill_at="next_open"`` (default): market orders fill at the next session's open, the realistic
  timing for a daily strategy that decides after the close (a market-on-open order).
- ``fill_at="next_close"``: the next session's close; matches the vectorised research functions
  (``lag_days=1``) for like-for-like comparisons.

Market orders pay ``CostModel`` slippage and fees. Limit orders are day orders: a buy fills at the
open if it opens at or below the limit, else at the limit if the day's low reaches it (mirror
image for sells); they never get a better price than the limit, and pay fees but no slippage.

Sells are processed before buys. A buy that the cash cannot cover is cut down (``PARTIAL``) or
dropped (``EXPIRED``), and a sell can never exceed the shares held: the simulation cannot create
leverage or a short position by accident.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from tp_trading.costs import CostModel
from tp_trading.data import MarketData
from tp_trading.events import Fill, Order, OrderStatus, OrderType, Side
from tp_trading.portfolio import EPSILON, Portfolio

FillAt = Literal["next_open", "next_close"]
SizeAt = Literal["signal", "fill"]


@dataclass(frozen=True)
class ExecutionConfig:
    fill_at: FillAt = "next_open"
    # "signal": quantities are fixed at the signal close (what a real order sent overnight does).
    # "fill": target-weight orders are re-sized at the fill price, so weights land exactly on
    # target; only for reproducing vectorised research results.
    size_at: SizeAt = "signal"
    fractional: bool = False
    costs: CostModel = field(default_factory=CostModel)


class SimulatedExecution:
    def __init__(self, data: MarketData, config: ExecutionConfig) -> None:
        self.data = data
        self.config = config
        self._field: Literal["open", "close"] = "open" if config.fill_at == "next_open" else "close"

    def execute(self, orders: list[Order], t: int, portfolio: Portfolio) -> list[Fill]:
        """Fill ``orders`` on session ``t``, applying each fill to ``portfolio`` as it happens."""
        if self.config.size_at == "fill":
            self._resize(orders, t, portfolio)
        fills: list[Fill] = []
        for order in sorted(orders, key=lambda o: o.side is Side.BUY):
            fill = self._fill(order, t, portfolio)
            if fill is not None:
                portfolio.apply(fill)
                order.fills.append(fill)
                fills.append(fill)
        return fills

    # -- internals --------------------------------------------------------------------------------

    def _resize(self, orders: list[Order], t: int, portfolio: Portfolio) -> None:
        marks = self.data.marks(t - 1) if t > 0 else {}
        prices = dict(marks)
        for symbol in portfolio.positions:
            price = self.data.value(self._field, t, symbol)
            if math.isfinite(price):
                prices[symbol] = price
        equity = portfolio.equity(prices)
        for order in orders:
            intent = order.intent
            if intent.target_weight is None:
                continue
            price = self.data.value(self._field, t, intent.symbol)
            if not math.isfinite(price) or price <= 0:
                continue
            delta = intent.target_weight * equity / price - portfolio.quantity(intent.symbol)
            if not self.config.fractional:
                delta = float(math.trunc(delta))
            order.resized_quantity = delta

    def _fill(self, order: Order, t: int, portfolio: Portfolio) -> Fill | None:
        intent = order.intent
        side = order.side
        quantity = intent.quantity
        if order.resized_quantity is not None:
            if abs(order.resized_quantity) < EPSILON:
                return self._finish(order, OrderStatus.FILLED, "nothing left to trade at the fill")
            quantity = abs(order.resized_quantity)

        bar_price = self.data.value(self._field, t, intent.symbol)
        if not math.isfinite(bar_price) or bar_price <= 0:
            return self._finish(order, OrderStatus.EXPIRED, "no bar on the fill session")

        if intent.order_type is OrderType.LIMIT:
            price = self._limit_price(side, intent.limit_price, t, intent.symbol)
            if price is None:
                return self._finish(order, OrderStatus.EXPIRED, "limit not reached")
            slippage_per_share = 0.0
        else:
            price = self.config.costs.fill_price(side, bar_price)
            slippage_per_share = abs(price - bar_price)

        status = OrderStatus.FILLED
        note = ""
        if side is Side.SELL:
            held = max(portfolio.quantity(intent.symbol), 0.0)
            if quantity > held + EPSILON:
                quantity, status, note = held, OrderStatus.PARTIAL, "sell capped at shares held"
        else:
            affordable = self._affordable(portfolio.cash, price)
            if quantity > affordable + EPSILON:
                quantity, status, note = affordable, OrderStatus.PARTIAL, "cut to available cash"
        if not self.config.fractional:
            quantity = float(math.floor(quantity + EPSILON))
        if quantity < EPSILON:
            return self._finish(order, OrderStatus.EXPIRED, note or "nothing to trade")

        fees = self.config.costs.fees(side, quantity, price)
        fill = Fill(
            order_id=order.id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side=side,
            quantity=quantity,
            price=price,
            fees=fees,
            session=self.data.sessions[t],
            slippage=slippage_per_share * quantity,
        )
        order.status = status
        order.filled_quantity = quantity
        order.fill_price = price
        order.note = note
        return fill

    def _limit_price(self, side: Side, limit: float | None, t: int, symbol: str) -> float | None:
        assert limit is not None
        if self._field == "close":
            close = self.data.value("close", t, symbol)
            ok = close <= limit if side is Side.BUY else close >= limit
            return close if ok else None
        open_ = self.data.value("open", t, symbol)
        if side is Side.BUY:
            if open_ <= limit:
                return open_
            return limit if self.data.value("low", t, symbol) <= limit else None
        if open_ >= limit:
            return open_
        return limit if self.data.value("high", t, symbol) >= limit else None

    def _affordable(self, cash: float, price: float) -> float:
        costs = self.config.costs
        budget = cash - costs.commission_min
        if budget <= 0:
            return 0.0
        return budget / (price + costs.commission_per_share)

    @staticmethod
    def _finish(order: Order, status: OrderStatus, note: str) -> None:
        order.status = status
        order.note = note
        return None
