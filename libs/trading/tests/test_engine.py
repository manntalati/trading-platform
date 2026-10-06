from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_trading.costs import CostModel
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig
from tp_trading.events import CheckResult, Fill, OrderIntent, RiskDecision
from tp_trading.execution import ExecutionConfig
from tp_trading.risk import Book
from tp_trading.strategy import Context, Strategy

DAYS = pd.DatetimeIndex(sessions(date(2024, 1, 2), date(2024, 3, 28)))
FREE = ExecutionConfig(costs=CostModel.free())


def walk(seed: int = 1, columns: Sequence[str] = ("AAA", "BBB")) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.01, (len(DAYS), len(columns)))
    return pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=list(columns))


@dataclass(frozen=True)
class Scripted(Strategy):
    """Calls ``act(ctx)`` on every bar and records what it saw."""

    name: ClassVar[str] = "scripted"
    act: Callable[[Context], None] = lambda ctx: None
    seen: list[tuple[date, date]] = field(default_factory=list, compare=False)
    fills: list[Fill] = field(default_factory=list, compare=False)

    def symbols(self) -> list[str]:
        return ["AAA", "BBB"]

    def on_bar(self, ctx: Context) -> None:
        self.seen.append((ctx.now, ctx.history().index[-1].date()))
        self.act(ctx)

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        self.fills.append(fill)


def test_history_never_extends_past_now() -> None:
    s = Scripted()
    BacktestEngine(s, MarketData.from_closes(walk()), EngineConfig(execution=FREE)).run()
    assert len(s.seen) == len(DAYS)
    assert all(now == last for now, last in s.seen)


def test_an_order_fills_on_the_session_after_its_signal() -> None:
    signal_day = DAYS[10].date()

    def act(ctx: Context) -> None:
        if ctx.now == signal_day:
            ctx.order("AAA", 10, reason="test")

    s = Scripted(act=act)
    result = BacktestEngine(s, MarketData.from_closes(walk()), EngineConfig(execution=FREE)).run()
    [fill] = s.fills
    assert fill.session == DAYS[11].date()
    assert result.positions.loc[DAYS[10], "AAA"] == 0
    assert result.positions.loc[DAYS[11], "AAA"] == 10
    assert result.orders.iloc[0]["status"] == "filled"
    assert result.orders.iloc[0]["reason"] == "test"


def test_changing_future_prices_never_changes_earlier_decisions() -> None:
    def act(ctx: Context) -> None:
        closes = ctx.history("close", lookback=5)
        if len(closes) == 5:
            ctx.order_target_weights(
                {"AAA": 0.5 if closes["AAA"].iloc[-1] > closes["AAA"].mean() else 0.0}
            )

    base = walk()
    shocked = base.copy()
    shocked.iloc[40:] *= 3.0  # rewrite the future from session 40 on
    a = BacktestEngine(
        Scripted(act=act), MarketData.from_closes(base), EngineConfig(execution=FREE)
    ).run()
    b = BacktestEngine(
        Scripted(act=act), MarketData.from_closes(shocked), EngineConfig(execution=FREE)
    ).run()
    early_a = a.orders[pd.to_datetime(a.orders["session"]) < DAYS[40]]
    early_b = b.orders[pd.to_datetime(b.orders["session"]) < DAYS[40]]
    pd.testing.assert_frame_equal(
        early_a.drop(columns=["status", "filled_quantity", "fill_price", "note"]),
        early_b.drop(columns=["status", "filled_quantity", "fill_price", "note"]),
    )
    pd.testing.assert_series_equal(a.equity.iloc[:40], b.equity.iloc[:40])


def test_target_weights_rebalance_and_report_equity_and_weights() -> None:
    def act(ctx: Context) -> None:
        if ctx.now == DAYS[0].date():
            ctx.order_target_weights({"AAA": 0.5, "BBB": 0.3})

    cfg = EngineConfig(
        initial_cash=10_000, execution=ExecutionConfig(fractional=True, costs=CostModel.free())
    )
    result = BacktestEngine(Scripted(act=act), MarketData.from_closes(walk()), cfg).run()
    w = result.weights.loc[DAYS[1]]
    assert w["AAA"] == pytest.approx(0.5, abs=0.02)  # sized at the signal close, filled next day
    assert w["BBB"] == pytest.approx(0.3, abs=0.02)
    assert w.sum() == pytest.approx(1.0)
    assert result.exposure.iloc[0] == 0
    assert result.returns.iloc[0] == 0
    summary = result.summary()
    assert summary["trades"] == 2
    assert summary["orders"] == {"filled": 2}
    assert summary["fees"] == 0


class RejectBBB:
    def __init__(self) -> None:
        self.books: list[Book] = []
        self.days: list[date] = []

    def review(self, intents: Sequence[OrderIntent], book: Book) -> list[RiskDecision]:
        self.books.append(book)
        return [
            RiskDecision(i, (CheckResult("no_bbb", i.symbol != "BBB", "BBB is not allowed"),))
            for i in intents
        ]

    def end_of_day(self, session: date, strategy: str, equity: float) -> None:
        self.days.append(session)


def test_rejected_intents_are_recorded_with_reasons_and_never_fill() -> None:
    def act(ctx: Context) -> None:
        if ctx.now == DAYS[3].date():
            ctx.order("AAA", 5)
            ctx.order("BBB", 5)

    gate = RejectBBB()
    s = Scripted(act=act)
    result = BacktestEngine(
        s, MarketData.from_closes(walk()), EngineConfig(execution=FREE), gate
    ).run()
    assert [f.symbol for f in s.fills] == ["AAA"]
    rejected = result.orders[result.orders["status"] == "rejected"].iloc[0]
    assert rejected["symbol"] == "BBB"
    assert rejected["note"] == "no_bbb: BBB is not allowed"
    [book] = gate.books
    assert book.session == DAYS[3].date()
    assert book.equity == pytest.approx(100_000)
    assert gate.days == [d.date() for d in DAYS]


def test_start_keeps_earlier_bars_as_warm_up_history() -> None:
    s = Scripted()
    start = DAYS[20].date()
    result = BacktestEngine(s, MarketData.from_closes(walk()), EngineConfig(start=start)).run()
    assert result.equity.index[0] == DAYS[20]
    assert s.seen[0][0] == start


def test_orders_still_open_at_the_end_are_reported_pending() -> None:
    def act(ctx: Context) -> None:
        if ctx.now == DAYS[-1].date():
            ctx.order("AAA", 1)

    result = BacktestEngine(Scripted(act=act), MarketData.from_closes(walk())).run()
    assert result.orders["status"].tolist() == ["pending"]


def test_missing_symbols_fail_fast() -> None:
    with pytest.raises(KeyError, match="BBB"):
        BacktestEngine(Scripted(), MarketData.from_closes(walk(columns=["AAA"])))


def test_order_target_weights_sells_first_truncates_and_closes_unlisted_positions() -> None:
    log: list[list[OrderIntent]] = []

    def act(ctx: Context) -> None:
        if ctx.now == DAYS[0].date():
            ctx.order("AAA", 10)
        elif ctx.now == DAYS[2].date():
            ctx.order_target_weights({"BBB": 0.333}, reason={"BBB": "why BBB"})
            log.append(ctx.drain())  # inspect, then nothing reaches the engine

    BacktestEngine(
        Scripted(act=act), MarketData.from_closes(walk()), EngineConfig(initial_cash=10_000)
    ).run()
    [intents] = log
    assert [(i.symbol, i.side) for i in intents] == [("AAA", "sell"), ("BBB", "buy")]
    assert intents[0].quantity == 10  # closed entirely
    assert intents[1].quantity == float(int(intents[1].quantity))  # whole shares
    assert intents[1].reason == "why BBB"
    assert intents[1].target_weight == 0.333


@pytest.mark.parametrize("targets", [{"AAA": -0.1}, {"AAA": 0.7, "BBB": 0.4}])
def test_order_target_weights_refuses_shorts_and_leverage(targets: dict[str, float]) -> None:
    def act(ctx: Context) -> None:
        ctx.order_target_weights(targets)

    with pytest.raises(ValueError, match="target weights"):
        BacktestEngine(Scripted(act=act), MarketData.from_closes(walk())).run()


def test_min_change_skips_small_rebalances() -> None:
    log: list[list[OrderIntent]] = []

    def act(ctx: Context) -> None:
        if ctx.now == DAYS[0].date():
            ctx.order_target_weights({"AAA": 0.5})
        elif ctx.now == DAYS[5].date():
            ctx.order_target_weights({"AAA": 0.5}, min_change=0.05)
            log.append(ctx.drain())

    cfg = EngineConfig(execution=ExecutionConfig(fractional=True, costs=CostModel.free()))
    BacktestEngine(Scripted(act=act), MarketData.from_closes(walk()), cfg).run()
    assert log == [[]]


def test_ordering_without_a_price_is_an_error() -> None:
    closes = walk()
    closes.loc[:, "BBB"] = np.nan

    def act(ctx: Context) -> None:
        ctx.order("BBB", 1)

    with pytest.raises(ValueError, match="no usable price for BBB"):
        BacktestEngine(Scripted(act=act), MarketData.from_closes(closes)).run()
