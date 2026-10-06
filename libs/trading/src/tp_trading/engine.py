"""Event-driven backtester: replays daily bars through a strategy one session at a time.

Each session ``t``:

1. orders approved at the previous close fill (at ``t``'s open, or close; see ``execution``),
   and the strategy's ``on_fill`` sees each fill;
2. the portfolio is marked at ``t``'s close;
3. ``on_bar`` runs with data up to and including ``t`` and returns order intents;
4. the risk gate approves or rejects them; approved ones wait for session ``t + 1``.

So a signal can never trade on its own bar, and the strategy can never see a later bar.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from tp_core import metrics
from tp_trading.data import Field, MarketData
from tp_trading.events import Fill, Order, OrderStatus
from tp_trading.execution import ExecutionConfig, SimulatedExecution
from tp_trading.portfolio import Portfolio, Position
from tp_trading.risk import AllowAll, Book, RiskGate
from tp_trading.strategy import Context, Strategy

ORDER_COLUMNS = [
    "id", "session", "symbol", "side", "quantity", "order_type", "limit_price", "stop_price",
    "target_weight", "reference_price", "reason", "status", "filled_quantity", "fill_price",
    "note",
]  # fmt: skip
FILL_COLUMNS = [
    "order_id", "session", "symbol", "side", "quantity", "price", "fees", "slippage", "notional",
]  # fmt: skip


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 100_000.0
    start: date | None = None  # first session the strategy runs (earlier bars are warm-up history)
    end: date | None = None
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


class BacktestContext(Context):
    def __init__(
        self, strategy: Strategy, data: MarketData, portfolio: Portfolio, fractional: bool
    ):
        super().__init__(strategy.name, fractional=fractional)
        self._data = data
        self._portfolio = portfolio
        self._symbols = strategy.symbols()
        self._t = 0
        self._marks: dict[str, float] = {}

    def at(self, t: int) -> None:
        self._t = t
        self._marks = self._data.marks(t)

    @property
    def now(self) -> date:
        return self._data.sessions[self._t]

    def history(self, field: Field = "close", lookback: int | None = None) -> pd.DataFrame:
        return self._data.window(field, self._t, self._symbols, lookback)

    def price(self, symbol: str) -> float:
        return self._marks.get(symbol, math.nan)

    @property
    def positions(self) -> Mapping[str, Position]:
        return self._portfolio.positions

    @property
    def equity(self) -> float:
        return self._portfolio.equity(self._marks)

    @property
    def cash(self) -> float:
        return self._portfolio.cash


@dataclass(frozen=True)
class BacktestResult:
    strategy: str
    params: dict[str, Any]
    config: EngineConfig
    equity: pd.Series  # at each close, from the first session the strategy ran
    positions: pd.DataFrame  # shares held at each close
    weights: pd.DataFrame  # fraction of equity per symbol at each close, plus "cash"
    orders: (
        pd.DataFrame
    )  # every intent: filled, partial, expired, rejected (with reasons) or pending
    fills: pd.DataFrame

    @property
    def returns(self) -> pd.Series:
        previous = self.equity.shift(1).fillna(self.config.initial_cash)
        return (self.equity / previous - 1.0).rename(self.strategy)

    @property
    def turnover(self) -> pd.Series:
        """Traded notional per session as a fraction of the prior close's equity."""
        if self.fills.empty:
            return pd.Series(0.0, index=self.equity.index, name="turnover")
        traded = self.fills.groupby("session")["notional"].sum()
        traded.index = pd.DatetimeIndex(traded.index)
        previous = self.equity.shift(1).fillna(self.config.initial_cash)
        return (traded.reindex(self.equity.index, fill_value=0.0) / previous).rename("turnover")

    @property
    def exposure(self) -> pd.Series:
        """Fraction of equity in positions at each close."""
        return self.weights.drop(columns="cash").sum(axis=1).rename("exposure")

    def summary(self, benchmark: pd.Series | None = None) -> dict[str, Any]:
        """The plan's standard tear sheet plus trading statistics."""
        sheet = metrics.tear_sheet(self.returns, benchmark=benchmark)
        assert isinstance(sheet, pd.Series)
        years = len(self.equity) / metrics.TRADING_DAYS
        statuses = self.orders["status"].value_counts().to_dict() if len(self.orders) else {}
        return {
            **{str(k): _plain(v) for k, v in sheet.items()},
            "final_equity": float(self.equity.iloc[-1]) if len(self.equity) else math.nan,
            "trades": len(self.fills),
            "trades_per_year": len(self.fills) / years if years else math.nan,
            "annual_turnover": float(self.turnover.sum() / years) if years else math.nan,
            "avg_exposure": float(self.exposure.mean()) if len(self.exposure) else math.nan,
            "fees": float(self.fills["fees"].sum()) if len(self.fills) else 0.0,
            "slippage": float(self.fills["slippage"].sum()) if len(self.fills) else 0.0,
            "orders": {str(k): int(v) for k, v in statuses.items()},
        }


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        data: MarketData,
        config: EngineConfig | None = None,
        risk: RiskGate | None = None,
    ) -> None:
        missing = sorted(set(strategy.symbols()) - set(data.symbols))
        if missing:
            raise KeyError(f"{strategy.name}: no bars for {missing}; backfill them first")
        self.strategy = strategy
        self.data = data
        self.config = config or EngineConfig()
        self.risk: RiskGate = risk if risk is not None else AllowAll()

    def run(self) -> BacktestResult:
        data, cfg, strategy = self.data, self.config, self.strategy
        first = 0 if cfg.start is None else int(data.index.searchsorted(pd.Timestamp(cfg.start)))
        last = len(data) - 1 if cfg.end is None else data.position_of(cfg.end)
        if first > last:
            raise ValueError("no sessions between start and end")

        portfolio = Portfolio(cfg.initial_cash)
        ctx = BacktestContext(strategy, data, portfolio, cfg.execution.fractional)
        execution = SimulatedExecution(data, cfg.execution)
        pending: list[Order] = []
        orders: list[Order] = []
        fills: list[Fill] = []
        equity_rows: list[float] = []
        position_rows: list[dict[str, float]] = []
        previous_equity = cfg.initial_cash
        seq = 0

        for t in range(first, last + 1):
            ctx.at(t)
            if pending:
                todays = execution.execute(pending, t, portfolio)
                pending = []
                fills.extend(todays)
                for fill in todays:
                    strategy.on_fill(ctx, fill)
            marks = data.marks(t)
            equity = portfolio.equity(marks)

            strategy.on_bar(ctx)
            intents = ctx.drain()
            if intents:
                book = Book(
                    session=data.sessions[t],
                    equity=equity,
                    cash=portfolio.cash,
                    positions={s: p.quantity for s, p in portfolio.positions.items()},
                    prices=marks,
                    previous_equity=previous_equity,
                    average_volume=data.average_volume(t),
                    last_bar=data.last_bar(t),
                )
                for decision in self.risk.review(intents, book):
                    seq += 1
                    order = Order(decision.intent.order_id(seq), decision.intent)
                    if decision.approved:
                        pending.append(order)
                    else:
                        order.status = OrderStatus.REJECTED
                        order.note = "; ".join(decision.reasons)
                    orders.append(order)

            self.risk.end_of_day(data.sessions[t], strategy.name, equity)
            equity_rows.append(equity)
            position_rows.append({s: p.quantity for s, p in portfolio.positions.items()})
            previous_equity = equity

        return self._result(first, last, equity_rows, position_rows, orders, fills)

    def _result(
        self,
        first: int,
        last: int,
        equity_rows: list[float],
        position_rows: list[dict[str, float]],
        orders: list[Order],
        fills: list[Fill],
    ) -> BacktestResult:
        index = self.data.index[first : last + 1]
        equity = pd.Series(equity_rows, index=index, name=self.strategy.name)
        symbols = self.strategy.symbols()
        positions = pd.DataFrame(position_rows, index=index).reindex(columns=symbols).fillna(0.0)
        marks = self.data.frame("close").ffill().iloc[first : last + 1].reindex(columns=symbols)
        values = (positions * marks.fillna(0.0)).div(equity, axis=0)
        values["cash"] = 1.0 - values.sum(axis=1)
        return BacktestResult(
            strategy=self.strategy.name,
            params=self.strategy.params(),
            config=self.config,
            equity=equity,
            positions=positions,
            weights=values,
            orders=pd.DataFrame([_order_row(o) for o in orders], columns=ORDER_COLUMNS),
            fills=pd.DataFrame(
                [{**asdict(f), "side": str(f.side), "notional": f.notional} for f in fills],
                columns=FILL_COLUMNS,
            ),
        )


def _order_row(order: Order) -> dict[str, Any]:
    i = order.intent
    return {
        "id": order.id,
        "session": i.session,
        "symbol": i.symbol,
        "side": str(order.side),
        "quantity": i.quantity,
        "order_type": str(i.order_type),
        "limit_price": i.limit_price,
        "stop_price": i.stop_price,
        "target_weight": i.target_weight,
        "reference_price": i.reference_price,
        "reason": i.reason,
        "status": str(order.status),
        "filled_quantity": order.filled_quantity,
        "fill_price": order.fill_price,
        "note": order.note,
    }


def _plain(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value
