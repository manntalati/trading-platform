"""Regime gates: out of the market while risk is off, straight back in when it clears."""

from datetime import date

import numpy as np
import pandas as pd

from tp_core.calendar import sessions
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig
from tp_trading.regime import SeriesGate
from tp_trading.strategy import Context
from tp_trading.testing import Scripted

DAYS = pd.DatetimeIndex(sessions(date(2023, 1, 3), date(2023, 12, 29)))


def test_series_gate_reads_the_last_decision() -> None:
    off = pd.Series([False, True, True, False], index=DAYS[[10, 20, 30, 40]])
    gate = SeriesGate("test", off)
    assert not gate.off(DAYS[5].date())  # before the first value: risk on
    assert gate.off(DAYS[25].date())  # between decisions: the last one
    assert not gate.off(DAYS[45].date())
    assert gate.turned_on(DAYS[40].date())
    assert not gate.turned_on(DAYS[41].date())  # only on the day it changed
    assert not gate.turned_on(DAYS[30].date())
    assert gate.share_off(DAYS[10].date(), DAYS[40].date()) == 0.5


def monthly_buyer(ctx: Context) -> None:
    """Holds AAA, rebalancing only at month end (or when told to catch up)."""
    if ctx.is_rebalance_day():
        ctx.order_target_weights({"AAA": 0.9})


def run(gate: SeriesGate | None) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    closes = pd.DataFrame(
        {"AAA": 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, len(DAYS))))}, index=DAYS
    )
    frames = dict.fromkeys(("open", "high", "low", "close"), closes) | {"volume": closes * 0 + 1e6}
    data = MarketData(frames)  # type: ignore[arg-type]
    strategy = Scripted(universe=("AAA",), act=monthly_buyer)
    return BacktestEngine(strategy, data, EngineConfig(regime=gate)).run().weights


def test_out_while_risk_is_off_and_back_in_at_once() -> None:
    off_from, on_from = DAYS[60], DAYS[95]  # mid-month both
    flags = pd.Series(False, index=DAYS)
    flags[(off_from <= DAYS) & (on_from > DAYS)] = True
    gated = run(SeriesGate("test", flags))
    plain = run(None)
    i_off, i_on = DAYS.get_loc(off_from), DAYS.get_loc(on_from)
    assert plain["AAA"].iloc[i_off + 2] > 0.8
    assert gated["AAA"].iloc[i_off + 1] == 0  # sold at the next open
    # month ends inside the off spell bought nothing
    assert (gated["AAA"].iloc[i_off + 1 : i_on + 1] == 0).all()
    # back on mid-month: the strategy takes its position the next session, not at month end
    assert gated["AAA"].iloc[i_on + 1] > 0.8


def test_buys_are_dropped_while_off() -> None:
    gate = SeriesGate("test", pd.Series(True, index=DAYS))
    gated = run(gate)
    assert (gated["AAA"] == 0).all()
