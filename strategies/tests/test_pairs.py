"""Pairs trading: the cointegration test, formation, the engine's long-only version and the
long/short research version, on synthetic prices (made-up symbols)."""

from datetime import date

import numpy as np
import pandas as pd

from tp_core.calendar import sessions
from tp_strategies.library.pairs import (
    Pairs,
    adf_t,
    form_pairs,
    formation_date,
    half_life,
    long_short_returns,
)
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig

DAYS = pd.DatetimeIndex(sessions(date(2018, 1, 2), date(2021, 12, 31)))
SECTORS = {"AAA": "Energy", "BBB": "Energy", "CCC": "Energy", "DDD": "Tech"}


def ar1(n: int, phi: float, seed: int, sd: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal(0, sd)
    return x


def market(seed: int = 7) -> pd.DataFrame:
    """AAA and BBB share a random walk and differ by a mean-reverting spread; CCC (same sector)
    and DDD (another sector) wander on their own."""
    n = len(DAYS)
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0.0003, 0.012, n))
    log_b = 4 + common
    log_a = 0.5 + 1.0 * log_b + ar1(n, 0.9, seed + 1)
    return pd.DataFrame(
        {
            "AAA": np.exp(log_a),
            "BBB": np.exp(log_b),
            "CCC": np.exp(4 + np.cumsum(rng.normal(0.0003, 0.015, n))),
            "DDD": np.exp(log_b + 0.3 + ar1(n, 0.9, seed + 2)),  # cointegrated, another sector
        },
        index=DAYS,
    )


def test_adf_and_half_life() -> None:
    reverting = ar1(600, 0.8, 3)
    walk = np.cumsum(np.random.default_rng(4).normal(0, 1, 600))
    assert adf_t(reverting) < -5
    assert adf_t(walk) > -3
    assert 2 < half_life(reverting) < 5  # ln 2 / -ln 0.8 is about 3.1
    assert half_life(walk) > 50


def test_formation_keeps_same_sector_cointegrated_pairs() -> None:
    px = market()
    pairs = form_pairs(np.log(px.iloc[:252]), SECTORS.get)
    assert [(p.a, p.b) for p in pairs] == [("AAA", "BBB")]  # DDD is in another sector
    p = pairs[0]
    assert 0.8 < p.beta < 1.2
    assert p.t_stat < -3.34
    assert 5 <= p.half_life <= 60  # the formation's range
    assert formation_date(DAYS[:30], today_is_month_end=False) == pd.Timestamp("2018-01-31")


def test_engine_buys_the_cheap_leg_and_exits_on_reversion() -> None:
    px = market()
    result = BacktestEngine(
        Pairs(universe=("AAA", "BBB", "CCC", "DDD")),
        MarketData.from_closes(px),
        EngineConfig(initial_cash=100_000),
        sectors=SECTORS,
    ).run()
    fills = result.fills
    assert len(fills) >= 6
    assert set(fills["symbol"]) <= {"AAA", "BBB"}
    reasons = " ".join(result.orders["reason"])
    assert "cheap against" in reasons
    assert "exit (reverted)" in reasons
    assert result.exposure.max() <= 0.13  # one leg of one pair: 10%, drifting with its price


def test_long_short_version_is_market_neutral() -> None:
    px = market()
    returns, trips = long_short_returns(px, SECTORS.get, max_pairs=1)
    spy_like = px["BBB"].pct_change().fillna(0)
    active = returns[returns != 0]
    assert trips > 5
    assert returns.sum() > 0  # the spread reverts by construction
    assert abs(active.corr(spy_like.loc[active.index])) < 0.3
