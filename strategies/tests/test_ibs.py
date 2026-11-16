import math
from datetime import date

import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_strategies.library import REGISTRY, build
from tp_strategies.library.ibs import IbsReversion, ibs, ibs_signals, ibs_targets, in_tom_window
from tp_trading.costs import CostModel
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, BacktestResult, EngineConfig
from tp_trading.execution import ExecutionConfig

FREE = ExecutionConfig(fill_at="next_open", fractional=True, costs=CostModel.free())


def test_ibs_by_hand_and_its_edge_cases() -> None:
    assert ibs(high=110.0, low=100.0, close=101.0) == pytest.approx(0.1)
    assert ibs(110.0, 100.0, 100.0) == 0.0  # closed on the low
    assert ibs(110.0, 100.0, 110.0) == 1.0  # closed on the high
    assert math.isnan(ibs(100.0, 100.0, 100.0))  # no range: no reading
    assert math.isnan(ibs(110.0, math.nan, 101.0))  # missing bar
    assert ibs(110.0, 100.0, 110.0000001) == 1.0  # rounding outside the range is clamped


def test_signals_need_a_range_a_bar_and_a_close_strictly_below_entry() -> None:
    high = pd.Series({"LOW": 110.0, "EDGE": 110.0, "FLAT": 100.0, "HIGH": 110.0, "GONE": math.nan})
    low = pd.Series({"LOW": 100.0, "EDGE": 100.0, "FLAT": 100.0, "HIGH": 100.0, "GONE": math.nan})
    close = pd.Series({"LOW": 101.0, "EDGE": 102.0, "FLAT": 100.0, "HIGH": 109.0, "GONE": math.nan})
    universe = ["LOW", "EDGE", "FLAT", "HIGH", "GONE", "UNKNOWN"]
    signals = {
        s.symbol: s for s in ibs_signals(high, low, close, universe, entry=0.2, tom_next=False)
    }
    assert [s for s, sig in signals.items() if sig.hold] == ["LOW"]
    assert signals["EDGE"].ibs == pytest.approx(0.2)  # exactly at the threshold: no signal
    assert math.isnan(signals["FLAT"].ibs)
    # The turn-of-month leg holds every symbol that has a bar, whatever its IBS.
    tom = {
        s.symbol: s.hold for s in ibs_signals(high, low, close, universe, entry=0.2, tom_next=True)
    }
    assert tom == {"LOW": True, "EDGE": True, "FLAT": True, "HIGH": True, "GONE": False,
                   "UNKNOWN": False}  # fmt: skip


def test_targets_split_the_sleeve_among_the_signals_and_park_the_rest() -> None:
    high = pd.Series({"QQQ": 110.0, "SPY": 110.0, "DIA": 110.0})
    low = pd.Series({"QQQ": 100.0, "SPY": 100.0, "DIA": 100.0})
    close = pd.Series({"QQQ": 101.0, "SPY": 101.5, "DIA": 108.0})
    signals = ibs_signals(high, low, close, ["QQQ", "SPY", "DIA"], entry=0.2, tom_next=False)
    targets, reasons = ibs_targets(signals, "BIL", 0.2)
    assert targets == {"QQQ": 0.5, "SPY": 0.5, "DIA": 0.0, "BIL": 0.0}
    assert reasons["QQQ"] == "QQQ IBS 0.10 below 0.2: hold through the next session"
    assert "out at the next open" in reasons["DIA"]

    quiet = ibs_signals(high, low, pd.Series({"QQQ": 109.0}), ["QQQ"], entry=0.2, tom_next=False)
    assert ibs_targets(quiet, "BIL", 0.2)[0] == {"QQQ": 0.0, "BIL": 1.0}
    assert ibs_targets(quiet, None, 0.2)[0] == {"QQQ": 0.0}


@pytest.mark.parametrize(
    ("day", "inside"),
    [
        (date(2026, 10, 29), False),
        (date(2026, 10, 30), True),  # last session of October
        (date(2026, 11, 2), True),
        (date(2026, 11, 4), True),  # third session of November
        (date(2026, 11, 5), False),
        (date(2026, 12, 31), True),
        (date(2027, 1, 4), True),  # Jan 1 is a holiday: the window counts sessions
        (date(2027, 1, 6), True),
        (date(2027, 1, 7), False),
    ],
)
def test_turn_of_month_window_follows_the_exchange_calendar(day: date, inside: bool) -> None:
    assert in_tom_window(day) is inside


def bars(days: pd.DatetimeIndex, ibs_by_day: list[float], cash: bool) -> MarketData:
    """QQQ with open 100, range 99..101 and the close placed at the given IBS; BIL flat."""
    close = [99.0 + 2.0 * v for v in ibs_by_day]
    frames = {
        "open": pd.DataFrame({"QQQ": 100.0}, index=days),
        "high": pd.DataFrame({"QQQ": 101.0}, index=days),
        "low": pd.DataFrame({"QQQ": 99.0}, index=days),
        "close": pd.DataFrame({"QQQ": close}, index=days),
        "volume": pd.DataFrame({"QQQ": 1e7}, index=days),
    }
    if cash:
        for name in ("open", "high", "low", "close"):
            frames[name]["BIL"] = 91.5
        frames["volume"]["BIL"] = 1e7
    return MarketData(frames)  # type: ignore[arg-type]


def run(strategy: IbsReversion, data: MarketData) -> BacktestResult:
    return BacktestEngine(strategy, data, EngineConfig(execution=FREE)).run()


def fills(result: BacktestResult, symbol: str) -> list[tuple[date, str]]:
    f = result.fills[result.fills["symbol"] == symbol]
    return [
        (pd.Timestamp(s).date(), str(side)) for s, side in zip(f["session"], f["side"], strict=True)
    ]


def test_holds_one_session_and_extends_only_while_the_close_stays_low() -> None:
    days = pd.DatetimeIndex(sessions(date(2026, 11, 9), date(2026, 11, 20)))
    # d1 and d2 close low (the hold is extended), d5 closes low once; the rest don't.
    pattern = [0.5, 0.1, 0.1, 0.5, 0.5, 0.1, 0.9, 0.5, 0.5, 0.5]
    result = run(IbsReversion(cash_symbol=None), bars(days, pattern, cash=False))
    d = [x.date() for x in days]
    assert fills(result, "QQQ") == [
        (d[2], "buy"),  # the open after the first low close
        (d[4], "sell"),  # the open after d3, the first close that wasn't low
        (d[6], "buy"),
        (d[7], "sell"),  # held exactly one session
    ]
    held = (result.positions["QQQ"].fillna(0) > 0).tolist()
    assert held == [False, False, True, True, False, False, True, False, False, False]


def test_idle_cash_rotates_through_bil_at_the_same_open() -> None:
    days = pd.DatetimeIndex(sessions(date(2026, 11, 9), date(2026, 11, 20)))
    pattern = [0.5, 0.5, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    result = run(IbsReversion(), bars(days, pattern, cash=True))
    d = [x.date() for x in days]
    assert fills(result, "BIL") == [(d[1], "buy"), (d[3], "sell"), (d[4], "buy")]
    assert fills(result, "QQQ") == [(d[3], "buy"), (d[4], "sell")]
    weights = result.weights.fillna(0)
    assert weights["QQQ"].iloc[3] == pytest.approx(1.0, abs=0.01)
    assert weights["BIL"].iloc[-1] == pytest.approx(1.0, abs=0.01)
    assert len(result.orders) == len(result.fills)  # no daily top-ups of a held position


def test_turn_of_month_option_holds_from_the_last_session_to_the_fourth_open() -> None:
    days = pd.DatetimeIndex(sessions(date(2026, 10, 20), date(2026, 11, 13)))
    flat = [0.5] * len(days)
    assert len(run(IbsReversion(cash_symbol=None), bars(days, flat, cash=False)).fills) == 0
    result = run(IbsReversion(cash_symbol=None, tom=True), bars(days, flat, cash=False))
    assert fills(result, "QQQ") == [(date(2026, 10, 30), "buy"), (date(2026, 11, 5), "sell")]


def test_registered_and_built_from_strings() -> None:
    assert REGISTRY["ibs"] is IbsReversion
    s = build("ibs", {"universe": "qqq, spy, dia", "cash-symbol": "none", "tom": "true"})
    assert s == IbsReversion(universe=("QQQ", "SPY", "DIA"), cash_symbol=None, tom=True)
    assert IbsReversion().symbols() == ["QQQ", "BIL"]
    assert IbsReversion(cash_symbol=None).symbols() == ["QQQ"]
