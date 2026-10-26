"""The entry setups on hand-built price paths: each fires where its rule says, only looks back, and
stops counting once its exit has come."""

import numpy as np
import pandas as pd
import pytest

from tp_strategies.setups import (
    SETUPS,
    active_setups,
    exit_signal,
    indicators,
    leader_watch,
    rule_level,
    triggers,
    with_exits,
)

DAYS = pd.bdate_range("2022-01-03", periods=400)


def bars(close: np.ndarray) -> pd.DataFrame:
    c = pd.Series(close, index=DAYS[: len(close)])
    return pd.DataFrame({"open": c, "high": c * 1.005, "low": c * 0.995, "close": c})


def steady(n: int, daily: float, start: float = 100.0) -> np.ndarray:
    return start * np.exp(daily * np.arange(n))


def test_a_dip_in_an_uptrend_is_a_pullback_until_it_bounces() -> None:
    path = steady(300, 0.002)
    path[-3:] = path[-4] * np.array([0.98, 0.96, 0.94])
    ind = indicators(bars(path))
    fired, strength = triggers(ind)["pullback"]
    assert fired.iloc[-1]
    assert not fired.iloc[:-3].any()  # never during the steady climb
    assert 0 < strength.iloc[-1] <= 1
    (live,) = [a for a in active_setups(ind) if a.setup.name == "pullback"]
    assert live.age == 0
    bounced = indicators(bars(np.append(path, path[-4])))  # back above the 5-day average
    assert exit_signal(bounced, "above_ma5").iloc[-1]
    assert "pullback" not in {a.setup.name for a in active_setups(bounced)}


def test_a_squeeze_that_breaks_out() -> None:
    rng = np.random.default_rng(3)
    calm = np.linspace(0.02, 0.001, 300)  # the daily swings shrink: the tightest at the end
    path = 100 * np.exp(np.cumsum(0.0008 + calm * rng.standard_normal(300)))
    path[-1] = path[-51:-1].max() * 1.03
    ind = indicators(bars(path))
    assert ind["squeeze"].iloc[-2] <= 0.2
    fired, _ = triggers(ind)["breakout"]
    assert fired.iloc[-1]
    level = rule_level(ind, SETUPS["breakout"].exits.underlying)
    assert level is not None
    assert level["text"] == "a close below the 10-day average"
    assert level["level"] == pytest.approx(ind["ma10"].iloc[-1])


def test_a_leader_waits_for_its_50_day_then_fires() -> None:
    path = steady(320, 0.0015)
    ind = indicators(bars(path))
    assert ind["mom12_1"].iloc[-1] > 0.1
    assert leader_watch(ind)  # ~4% above the 50-day: wait
    assert not triggers(ind)["leader"][0].iloc[-1]
    path[-1] = path[-2] * 0.985
    ind = indicators(bars(path))
    assert triggers(ind)["leader"][0].iloc[-1]  # the first close near the 50-day
    assert not leader_watch(ind)


def test_bearish_setups_mirror_the_bullish_ones() -> None:
    falling = steady(300, -0.002)
    bounce = falling.copy()
    bounce[-3:] = bounce[-4] * np.array([1.02, 1.04, 1.06])
    assert triggers(indicators(bars(bounce)))["rally_fade"][0].iloc[-1]
    low = falling.copy()
    low[-1] = low[-2] * 0.97
    assert triggers(indicators(bars(low)))["breakdown"][0].iloc[-1]


def test_earnings_surprise_with_the_stock_agreeing() -> None:
    path = steady(300, 0.001)
    ind = indicators(bars(path))
    day = ind.index[-1]
    events = pd.DataFrame({"session": [day.date(), ind.index[-60].date()], "sue": [2.4, -3.0]})
    fired = triggers(ind, events)
    assert fired["earnings_up"][0].iloc[-1]
    assert fired["earnings_up"][1].iloc[-1] == pytest.approx(0.6)
    assert not fired["earnings_down"][0].any()  # a miss, but the stock was rising: no signal


def test_setups_only_look_back() -> None:
    path = steady(300, 0.002)
    path[-3:] = path[-4] * np.array([0.98, 0.96, 0.94])
    full = triggers(indicators(bars(path)))
    cut = triggers(indicators(bars(path[:-2])))
    for name, (fired, _) in cut.items():
        assert (full[name][0].iloc[: len(fired)] == fired).all(), name


def test_calibrated_exits_replace_the_defaults() -> None:
    setup = with_exits(SETUPS["pullback"], {"take_profit": 0.3, "time_stop": 6, "other": 1})
    assert setup.exits.take_profit == 0.3
    assert setup.exits.time_stop == 6
    assert setup.exits.stop_loss == SETUPS["pullback"].exits.stop_loss
    assert with_exits(SETUPS["pullback"], None) is SETUPS["pullback"]
