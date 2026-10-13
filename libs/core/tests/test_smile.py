"""SVI fits, arbitrage checks, skew and the term structure."""

import math
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tp_core import smile
from tp_core.testing import synthetic_chain

NOW = datetime(2026, 10, 9, 19, 45, tzinfo=UTC)
EXPIRATIONS = [date(2026, 11, 20), date(2026, 12, 18), date(2027, 3, 19)]
STRIKES = [float(k) for k in range(60, 141, 5)]


def chain(skew: float = -0.25, iv: list[float] | None = None) -> pd.DataFrame:
    return synthetic_chain(
        "XYZ", 100.0, NOW, expirations=EXPIRATIONS, iv=iv or [0.32, 0.30, 0.28],
        skew=skew, curvature=0.4, rate=0.04, half_spread=0.01, strikes=STRIKES,
    )  # fmt: skip


def test_fit_recovers_a_known_svi() -> None:
    truth = smile.SVI(a=0.02, b=0.12, rho=-0.5, m=0.03, sigma=0.15)
    k = np.linspace(-0.5, 0.4, 25)
    fitted = smile.fit_svi(k, truth.w(k))
    assert fitted is not None
    svi, rmse = fitted
    assert rmse < 1e-4
    assert np.allclose(svi.w(k), truth.w(k), atol=2e-4)
    assert svi.rho < 0
    assert smile.fit_svi(k[:4], truth.w(k[:4])) is None  # too few points


def test_chain_fits_skew_and_term() -> None:
    fits = smile.fit_chain(chain(), 100.0, NOW, 0.04)
    assert [f.expiration for f in fits] == EXPIRATIONS
    for f in fits:
        assert f.rmse_iv < 0.002  # a fifth of a volatility point
        assert f.skew_25d is not None
        assert f.skew_25d < 0  # puts dearer
        assert not f.butterfly_arbitrage
        assert not f.calendar_arbitrage
    assert fits[0].atm_iv == pytest.approx(0.32, abs=0.01)
    flat = smile.fit_chain(chain(skew=0.0), 100.0, NOW, 0.04)[0]
    assert flat.skew_25d is not None
    assert abs(flat.skew_25d) < 0.005
    curve = smile.smile_curve(fits[0], 100.0, 0.04)
    assert len(curve) == 41
    assert curve[0]["strike"] < 100 < curve[-1]["strike"]


def test_arbitrage_flags() -> None:
    # a later expiry with less total variance than the earlier one
    fits = smile.fit_chain(chain(iv=[0.40, 0.20, 0.30]), 100.0, NOW, 0.04)
    assert [f.calendar_arbitrage for f in fits] == [False, True, False]
    # wings too steep for a positive density
    bad = smile.SVI(a=0.001, b=1.5, rho=0.0, m=0.0, sigma=0.01)
    assert float(np.min(bad.g(np.linspace(-0.3, 0.3, 61)))) < 0
    good = smile.SVI(a=0.02, b=0.1, rho=-0.4, m=0.0, sigma=0.2)
    assert float(np.min(good.g(np.linspace(-0.5, 0.5, 61)))) > 0


def test_quote_arbitrage_finds_inconsistent_calls() -> None:
    c = chain()
    exp = EXPIRATIONS[0]
    assert smile.quote_arbitrage(c, exp) == {"vertical": 0, "butterfly": 0}
    calls = (c["expiration"] == exp) & (c["right"] == "C")
    k105 = calls & (c["strike"] == 105.0)
    k100 = calls & (c["strike"] == 100.0)
    c.loc[k105, ["bid", "ask"]] = [float(c.loc[k100, "ask"].iloc[0]) + 0.5, 30.0]
    assert smile.quote_arbitrage(c, exp)["vertical"] == 1


def test_delta_strikes_and_term_shape() -> None:
    svi = smile.SVI(a=0.005, b=0.05, rho=-0.6, m=0.0, sigma=0.1)
    years = 30 / 365
    kc, kp = smile.delta_strike(svi, years, 0.25), smile.delta_strike(svi, years, -0.25)
    assert kc is not None
    assert kp is not None
    assert kp < 0 < kc
    w = float(svi.w(kc))
    assert smile.bs.norm_cdf((-kc + w / 2) / math.sqrt(w)) == pytest.approx(0.25, abs=1e-6)
    assert smile.term_shape(0.30, 0.33) == {"ratio": pytest.approx(1.1), "shape": "contango"}
    assert smile.term_shape(0.30, 0.25)["shape"] == "backwardation"
    assert smile.term_shape(None, 0.25) == {"ratio": None, "shape": None}
