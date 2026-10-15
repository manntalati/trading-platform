from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tp_ml.labels import daily_vol, rule_labels, triple_barrier

PRE = 60  # sessions before the signal, to give the volatility estimate history


def frames(post: list[tuple[float, float, float, float]]) -> dict[str, pd.DataFrame]:
    """One symbol, XYZ: ``PRE`` quiet sessions alternating +-1% around 100, then the signal
    session (close 100) and ``post`` bars as (open, high, low, close)."""
    closes = [100 * (1.01 if i % 2 else 1.0) for i in range(PRE)] + [100.0]
    rows = [(c, c, c, c) for c in closes] + post
    index = pd.bdate_range("2024-01-02", periods=len(rows))
    o, h, lo, c = (pd.DataFrame({"XYZ": [r[k] for r in rows]}, index=index) for k in range(4))
    return {"open": o, "high": h, "low": lo, "close": c}


def sigma() -> float:
    f = frames([])
    return float(daily_vol(f["close"])["XYZ"].iloc[-1])


def label(post: list[tuple[float, float, float, float]], side: int = 1, **kw: int) -> pd.Series:
    f = frames(post)
    events = pd.DataFrame({"symbol": ["XYZ"], "date": [f["close"].index[PRE]], "side": [side]})
    out = triple_barrier(events, f["open"], f["high"], f["low"], f["close"], **kw)
    assert len(out) == 1
    return out.iloc[0]


def flat(n: int, price: float = 100.0) -> list[tuple[float, float, float, float]]:
    return [(price, price, price, price)] * n


def test_volatility_is_the_daily_move() -> None:
    assert sigma() == pytest.approx(0.01, rel=0.15)


def test_target_first_is_a_win_at_the_target() -> None:
    s = sigma()
    target = 100 * (1 + 2 * s)
    row = label([(100, 100.2, 99.8, 100), (100, target + 0.01, 99.9, 100.5), *flat(10)], horizon=5)
    assert row["barrier"] == "target"
    assert row["label"] == 1
    assert row["ret"] == pytest.approx(2 * s)
    assert row["start"] == frames([])["close"].index[PRE] + pd.offsets.BDay(1)
    assert row["end"] == row["start"] + pd.offsets.BDay(1)


def test_stop_first_is_a_loss_and_a_tie_goes_to_the_stop() -> None:
    s = sigma()
    stop = 100 * (1 - 2 * s)
    row = label([(100, 100.1, stop - 0.01, 99.5), *flat(10)], horizon=5)
    assert (row["barrier"], row["label"]) == ("stop", 0)
    assert row["ret"] == pytest.approx(-2 * s)
    # both barriers inside one bar: assume the worse happened first
    both = label([(100, 100 * (1 + 3 * s), 100 * (1 - 3 * s), 100), *flat(10)], horizon=5)
    assert both["barrier"] == "stop"


def test_a_gap_through_the_stop_fills_at_the_open() -> None:
    s = sigma()
    row = label([(100, 100, 99.9, 100), (95, 95.5, 94, 95), *flat(10, 95)], horizon=5)
    assert row["barrier"] == "stop"
    assert row["ret"] == pytest.approx(-0.05)
    assert row["ret"] < -2 * s


def test_vertical_barrier_labels_by_the_sign_of_the_return() -> None:
    up = label([*flat(4), (100.3, 100.4, 100.2, 100.3), *flat(5)], horizon=5)
    assert (up["barrier"], up["label"]) == ("vertical", 1)
    assert up["ret"] == pytest.approx(0.003)
    down = label([*flat(4), (99.7, 99.8, 99.6, 99.7), *flat(5)], horizon=5)
    assert (down["barrier"], down["label"]) == ("vertical", 0)


def test_short_side_mirrors_the_barriers() -> None:
    s = sigma()
    row = label([(100, 100.1, 100 * (1 - 2 * s) - 0.01, 99), *flat(10)], side=-1, horizon=5)
    assert (row["barrier"], row["label"]) == ("target", 1)
    assert row["ret"] == pytest.approx(2 * s)


def test_trades_that_have_not_ended_are_dropped() -> None:
    f = frames(flat(3))
    events = pd.DataFrame({"symbol": ["XYZ", "NOPE"], "date": [f["close"].index[PRE]] * 2})
    out = triple_barrier(events, f["open"], f["high"], f["low"], f["close"], horizon=5)
    assert out.empty


def rule(post_closes: list[float], exit_on: list[bool], max_hold: int = 3) -> pd.DataFrame:
    f = frames([(c, c, c, c) for c in post_closes])
    signal = pd.DataFrame(False, index=f["close"].index, columns=["XYZ"])
    for k, flag in enumerate(exit_on):
        signal.iloc[PRE + 1 + k] = flag
    events = pd.DataFrame({"symbol": ["XYZ"], "date": [f["close"].index[PRE]]})
    return rule_labels(events, f["open"], f["close"], signal, max_hold=max_hold)


def test_rule_labels_sell_the_open_after_the_first_exit_signal() -> None:
    # entry at the next open (101); the entry session's own close can trigger the exit
    out = rule([101, 103, 104, 99, 99, 99], [True, False, False])
    row = out.iloc[0]
    assert (row["barrier"], row["entry"]) == ("rule", 101)
    assert row["ret"] == pytest.approx(103 / 101 - 1)
    assert row["label"] == 1
    assert row["end"] == row["start"] + pd.offsets.BDay(1)


def test_rule_labels_time_stop_and_losses() -> None:
    out = rule([101, 100, 99, 98, 97, 96], [False, False, False, False])
    row = out.iloc[0]
    assert row["barrier"] == "time"
    # held 0, 1, 2 sessions without a signal; the time stop fires at 3 and sells the next open
    assert row["ret"] == pytest.approx(97 / 101 - 1)
    assert row["label"] == 0
    assert row["end"] == row["start"] + pd.offsets.BDay(4)


def test_rule_labels_need_the_whole_holding_window() -> None:
    assert rule([101, 102, 103], [False, False, False]).empty


def test_rule_labels_fall_back_to_the_close_without_an_open() -> None:
    f = frames([(c, c, c, c) for c in [101, 103, 104, 105, 106, 107]])
    f["open"].iloc[PRE + 2] = math.nan
    signal = pd.DataFrame(False, index=f["close"].index, columns=["XYZ"])
    signal.iloc[PRE + 1] = True
    events = pd.DataFrame({"symbol": ["XYZ"], "date": [f["close"].index[PRE]]})
    row = rule_labels(events, f["open"], f["close"], signal, max_hold=3).iloc[0]
    assert row["ret"] == pytest.approx(101 / 101 - 1)


def test_labels_run_on_many_events(bars: dict[str, pd.DataFrame]) -> None:
    close = bars["close"]
    stacked = (close.pct_change() < -0.02).stack()  # noqa: PD013
    events = stacked[stacked.to_numpy(dtype=bool)].index.to_frame(index=False)
    events.columns = ["date", "symbol"]
    out = triple_barrier(events, bars["open"], bars["high"], bars["low"], close, horizon=10)
    assert len(out) > 100
    assert set(out["barrier"]) <= {"target", "stop", "vertical"}
    assert (out["end"] >= out["start"]).all()
    assert (out["start"] > out["date"]).all()
    assert np.isfinite(out["ret"]).all()
