"""Buy-write and put-write priced at the VIX, on synthetic prices."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_strategies.library.vrp import VrpConfig, expiration_days, premium_returns

DAYS = pd.DatetimeIndex(sessions(date(2019, 1, 2), date(2021, 12, 31)))


def spy(drift: float = 0.0003, seed: int = 3) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    raw = pd.Series(300 * np.exp(np.cumsum(rng.normal(drift, 0.01, len(DAYS)))), index=DAYS)
    adj = raw * np.exp(np.linspace(-0.04, 0.0, len(DAYS)))  # dividends in the adjusted series
    return adj, raw


def test_expiration_days_are_third_fridays_or_the_session_before() -> None:
    days = expiration_days(date(2025, 1, 1), date(2025, 6, 30))
    assert days[0] == date(2025, 1, 17)
    assert date(2025, 4, 17) in days  # 18 April 2025 was Good Friday: the Thursday
    assert len(days) == 6


def test_flat_market_earns_the_premium() -> None:
    raw = pd.Series(300.0, index=DAYS)
    vix = pd.Series(20.0, index=DAYS)
    out = premium_returns(raw, raw, vix, pd.Series(0.0, index=DAYS), kind="put-write",
                          cfg=VrpConfig(cost=0.0))  # fmt: skip
    rolls = out[out["premium"] > 0]
    assert len(rolls) == 37  # the first day, then 36 monthly expirations
    # nothing moves, so every put expires worthless: the NAV compounds the premiums
    expected = float(np.prod(1 + rolls["premium"].iloc[:-1]))
    assert out["nav"].iloc[-1] == pytest.approx(expected, rel=0.02)
    assert rolls["premium"].mean() == pytest.approx(0.4 * 0.18 * np.sqrt(28 / 365), rel=0.15)


def test_buy_write_and_put_write_are_close_cousins() -> None:
    adj, raw = spy()
    vix = pd.Series(18.0, index=DAYS)
    rate = pd.Series(1.5, index=DAYS)
    bw = premium_returns(adj, raw, vix, rate, kind="buy-write")
    pw = premium_returns(adj, raw, vix, rate, kind="put-write")
    index = adj.pct_change().fillna(0)
    assert bw["returns"].std() < index.std()  # the call caps the upside
    assert bw["returns"].corr(pw["returns"]) > 0.8  # put-call parity
    costly = premium_returns(adj, raw, vix, rate, kind="put-write", cfg=VrpConfig(cost=0.2))
    assert costly["nav"].iloc[-1] < pw["nav"].iloc[-1]
    with pytest.raises(ValueError, match="kind"):
        premium_returns(adj, raw, vix, kind="straddle")
