"""Cash, positions and P&L from fills: the bookkeeping shared by backtests and paper sleeves."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date

from tp_trading.events import Fill

EPSILON = 1e-9  # quantities closer to zero than this are flat


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float  # shares; negative would be short (the risk gate forbids opening shorts)
    avg_price: float  # average cost per share of the open quantity, including fees
    opened: date  # session of the fill that opened the current position


class Portfolio:
    """Positions and cash, updated only by fills.

    Average cost includes fees, so realized P&L is net of costs. Prices passed to valuation
    methods must be in the same units as the fills (adjusted prices in a backtest, raw broker
    prices in paper trading).
    """

    def __init__(self, cash: float, positions: Iterable[Position] = ()) -> None:
        self.cash = float(cash)
        self.positions: dict[str, Position] = {p.symbol: p for p in positions}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0

    def quantity(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return p.quantity if p else 0.0

    def apply(self, fill: Fill) -> None:
        signed = fill.signed_quantity
        self.cash -= signed * fill.price + fill.fees
        self.fees_paid += fill.fees
        current = self.positions.get(fill.symbol)
        if current is None or abs(current.quantity) < EPSILON:
            cost = fill.price + fill.fees / fill.quantity
            self.positions[fill.symbol] = Position(fill.symbol, signed, cost, fill.session)
            return
        new_qty = current.quantity + signed
        if current.quantity * signed > 0:  # adding to the position
            total_cost = current.avg_price * abs(current.quantity) + fill.notional + fill.fees
            avg = total_cost / abs(new_qty)
            self.positions[fill.symbol] = replace(current, quantity=new_qty, avg_price=avg)
            return
        # Reducing (or flipping): realize P&L on the closed part.
        closed = min(abs(signed), abs(current.quantity))
        direction = 1.0 if current.quantity > 0 else -1.0
        fee_share = fill.fees * closed / fill.quantity
        self.realized_pnl += direction * closed * (fill.price - current.avg_price) - fee_share
        if abs(new_qty) < EPSILON:
            del self.positions[fill.symbol]
        elif new_qty * current.quantity > 0:
            self.positions[fill.symbol] = replace(current, quantity=new_qty)
        else:  # flipped through zero: the remainder opens a new position
            rest_fees = fill.fees - fee_share
            cost = fill.price + rest_fees / abs(new_qty)
            self.positions[fill.symbol] = Position(fill.symbol, new_qty, cost, fill.session)

    def market_value(self, prices: Mapping[str, float]) -> float:
        return sum(p.quantity * _price(prices, p) for p in self.positions.values())

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: Mapping[str, float]) -> float:
        return sum(abs(p.quantity) * _price(prices, p) for p in self.positions.values())

    def weights(self, prices: Mapping[str, float]) -> dict[str, float]:
        equity = self.equity(prices)
        if equity <= 0:
            return {}
        return {s: p.quantity * _price(prices, p) / equity for s, p in self.positions.items()}

    def unrealized_pnl(self, prices: Mapping[str, float]) -> float:
        return sum(p.quantity * (_price(prices, p) - p.avg_price) for p in self.positions.values())


def _price(prices: Mapping[str, float], position: Position) -> float:
    """Valuation price; a position with no price yet (no bar) is held at cost."""
    price = prices.get(position.symbol)
    return position.avg_price if price is None or not math.isfinite(price) else price
