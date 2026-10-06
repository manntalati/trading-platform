"""Strategy sleeves rebuilt from recorded fills, and the context a strategy sees in paper trading.

Each strategy's positions and cash are derived from its own fills (never from the broker's
positions, which are the sum of every sleeve), so two strategies can hold the same symbol and
each still knows its own share. Reconciliation checks that the sum of sleeves equals the broker.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date

import pandas as pd

from tp_core.bars import close_matrix
from tp_paper.store import FillRecord
from tp_trading.data import Field, MarketData
from tp_trading.events import Fill, Side
from tp_trading.portfolio import Portfolio, Position
from tp_trading.strategy import Context, Strategy


def sleeve_portfolio(capital: float, fills: Iterable[FillRecord]) -> Portfolio:
    portfolio = Portfolio(capital)
    for f in fills:
        portfolio.apply(
            Fill(
                order_id=f.proposal_id,
                strategy=f.strategy,
                symbol=f.symbol,
                side=Side(f.side),
                quantity=f.quantity,
                price=f.price,
                fees=f.fees,
                session=date.fromisoformat(f.session),
            )
        )
    return portfolio


def book_positions(fills: Iterable[FillRecord]) -> dict[str, float]:
    """Shares per symbol summed over every sleeve: what the broker should hold."""
    out: dict[str, float] = {}
    for f in fills:
        out[f.symbol] = out.get(f.symbol, 0.0) + (f.quantity if f.side == "buy" else -f.quantity)
    return {s: q for s, q in out.items() if abs(q) > 1e-9}


class PaperData:
    """The lake's bars for paper trading: adjusted for signals, raw for sizing and valuation."""

    def __init__(self, bars: pd.DataFrame) -> None:
        self.adjusted = MarketData.from_bars(bars, adjusted=True)
        raw_close = close_matrix(bars, adjusted=False).reindex(self.adjusted.index)
        self.raw_close = raw_close.ffill()
        raw_volume = bars.pivot(index="session", columns="symbol", values="volume")
        raw_volume.index = pd.DatetimeIndex(raw_volume.index)
        self.raw_adv = raw_volume.reindex(self.adjusted.index).rolling(20, min_periods=1).mean()
        raw_open = bars.pivot(index="session", columns="symbol", values="open")
        raw_open.index = pd.DatetimeIndex(raw_open.index)
        self.raw_open = raw_open.reindex(self.adjusted.index)

    @property
    def last_session(self) -> date:
        return self.adjusted.sessions[-1]

    def position_of(self, day: date) -> int:
        return self.adjusted.position_of(day)

    def closes(self, t: int) -> dict[str, float]:
        row = self.raw_close.iloc[t]
        return {str(s): float(v) for s, v in row.items() if v == v}

    def average_volume(self, t: int) -> dict[str, float]:
        row = self.raw_adv.iloc[t]
        return {str(s): float(v) for s, v in row.items() if v == v}

    def open_on(self, symbol: str, day: date) -> float | None:
        ts = pd.Timestamp(day)
        if symbol not in self.raw_open.columns or ts not in self.raw_open.index:
            return None
        value = float(self.raw_open[symbol].to_numpy()[self.raw_open.index.get_loc(ts)])
        return value if math.isfinite(value) else None


class PaperContext(Context):
    """A strategy's view in paper trading: the same contract as a backtest, with raw prices for
    sizing (orders are real shares) and its sleeve's positions."""

    def __init__(
        self,
        strategy: Strategy,
        data: PaperData,
        t: int,
        portfolio: Portfolio,
        sectors: Mapping[str, str] | None = None,
        *,
        catch_up: bool = False,
    ) -> None:
        super().__init__(strategy.name, fractional=False, sectors=sectors, catch_up=catch_up)
        self._data = data
        self._t = t
        self._portfolio = portfolio
        self._symbols = strategy.symbols()
        self._prices = data.closes(t)

    @property
    def now(self) -> date:
        return self._data.adjusted.sessions[self._t]

    def history(self, field: Field = "close", lookback: int | None = None) -> pd.DataFrame:
        return self._data.adjusted.window(field, self._t, self._symbols, lookback)

    def price(self, symbol: str) -> float:
        return self._prices.get(symbol, math.nan)

    @property
    def positions(self) -> Mapping[str, Position]:
        return self._portfolio.positions

    @property
    def equity(self) -> float:
        return self._portfolio.equity(self._prices)

    @property
    def cash(self) -> float:
        return self._portfolio.cash
