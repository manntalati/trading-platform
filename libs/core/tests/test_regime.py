"""Market regime labels and returns broken down by them."""

import numpy as np
import pandas as pd
import pytest

from tp_core.regime import current, describe, market_regimes, performance_by_regime


def closes() -> pd.DataFrame:
    days = pd.bdate_range("2024-01-01", periods=400)
    rng = np.random.default_rng(4)
    up = 100 * np.exp(np.cumsum(np.r_[np.full(300, 0.002), np.full(100, -0.004)]))
    spy = up * np.exp(rng.normal(0, 0.003, 400))
    names = {
        f"S{i}": 50 * np.exp(np.cumsum(rng.normal(0.001 if i < 7 else -0.001, 0.01, 400)))
        for i in range(10)
    }
    return pd.DataFrame({"SPY": spy, **names}, index=days)


def test_labels_follow_the_market() -> None:
    r = market_regimes(closes())
    assert r["trend"].iloc[:199].isna().all()  # needs 200 sessions
    assert r["trend"].iloc[250] == "up"
    assert r["trend"].iloc[-1] == "down"
    assert r["volatility"].iloc[250] == "calm"
    assert set(r["breadth"].dropna()) <= {"broad", "mixed", "narrow"}
    now = current(r)
    assert now["trend"] == "down"
    assert "downtrend" in now["summary"]
    assert describe(r.iloc[0]) == ""


def test_returns_are_attributed_to_the_regime_known_the_day_before() -> None:
    c = closes()
    r = market_regimes(c)
    strat = pd.Series(0.001, index=c.index)
    strat[r["trend"].shift(1) == "down"] = -0.002
    perf = performance_by_regime(strat, r, c["SPY"].pct_change())
    trend = {row["label"]: row for row in perf["trend"]}
    assert trend["up"]["ann_return"] == pytest.approx(0.001 * 252)
    assert trend["down"]["ann_return"] == pytest.approx(-0.002 * 252)
    assert trend["up"]["days"] + trend["down"]["days"] == r["trend"].shift(1).notna().sum()
    assert "benchmark_ann_return" in trend["up"]
