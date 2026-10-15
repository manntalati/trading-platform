from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tp_ml import vol
from tp_ml.testing import ohlc_from_returns, simulate_garch

# a persistent GARCH: calm and wild stretches that a good forecaster can see coming
OMEGA, ALPHA, BETA = 0.02, 0.08, 0.90


@pytest.fixture(scope="module")
def garch_bars() -> pd.DataFrame:
    returns, sigma = simulate_garch(2500, OMEGA, ALPHA, BETA, seed=1)
    return ohlc_from_returns(returns, sigma, seed=1)


def test_range_variance_by_hand() -> None:
    bars = pd.DataFrame(
        {"open": [100.0, 101.0], "high": [101.0, 103.0], "low": [99.0, 100.0],
         "close": [100.0, 102.0]},
        index=pd.bdate_range("2024-01-02", periods=2),
    )  # fmt: skip
    rv = vol.range_variance(bars)
    gap = math.log(101 / 100) ** 2
    session = 0.5 * math.log(103 / 100) ** 2 - (2 * math.log(2) - 1) * math.log(102 / 101) ** 2
    assert rv.iloc[1] == pytest.approx(gap + session)


def test_realized_vol_is_aligned_at_the_day_before() -> None:
    returns = pd.Series([0.0] + [0.01, -0.01] * 30, index=pd.bdate_range("2024-01-02", periods=61))
    rv = vol.realized_vol(returns, 20)
    assert rv.iloc[0] == pytest.approx(0.01 * math.sqrt(252))  # days 1..20, all ±1%
    assert rv.iloc[-20:].isna().all()  # the future isn't known yet


def test_garch_fit_recovers_the_parameters(garch_bars: pd.DataFrame) -> None:
    returns = np.log(garch_bars["close"]).diff().to_numpy()[1:]
    fit = vol.fit_garch(returns)
    assert fit.alpha == pytest.approx(ALPHA, abs=0.04)
    assert fit.beta == pytest.approx(BETA, abs=0.05)
    assert fit.persistence == pytest.approx(ALPHA + BETA, abs=0.03)
    long_run = math.sqrt(OMEGA / (1 - ALPHA - BETA) * 252) / 100
    assert fit.long_run_vol == pytest.approx(long_run, rel=0.25)
    # the forecast reverts from the next day's variance to the long-run level
    high = fit.forecast(next_variance=9.0, horizon=21)
    low = fit.forecast(next_variance=0.2, horizon=21)
    assert low < fit.long_run_vol < high


def test_forecasters_that_model_clustering_beat_the_trailing_one(
    garch_bars: pd.DataFrame,
) -> None:
    report = vol.evaluate_vol(garch_bars, vol.VolConfig(min_train=750, refit=126, step=5))
    s = report["scores"]
    assert s["garch"]["qlike"] < s["naive"]["qlike"]
    assert s["har"]["qlike"] < s["naive"]["qlike"]
    assert s["garch"]["mz_r2"] > 0.15
    assert vol.best_model(s) in ("garch", "har", "gbm", "ewma")
    text = vol.report_text(report, vol.forecast_now(garch_bars))
    for phrase in ("GARCH(1,1)", "HAR-RV", "Trailing realized", "QLIKE", "Now ("):
        assert phrase in text


def test_forecasts_are_point_in_time(garch_bars: pd.DataFrame) -> None:
    cfg = vol.VolConfig(min_train=750, refit=126, step=10)
    cut = garch_bars.index[1500]
    shocked = garch_bars.copy()
    later = shocked.index > cut
    shocked.loc[later] = shocked.loc[later] * np.linspace(1, 3, later.sum())[:, None]
    a = vol.evaluate_vol(garch_bars, cfg)["_forecasts"]
    b = vol.evaluate_vol(shocked, cfg)["_forecasts"]
    before = a.index <= cut
    models = list(vol.MODELS)
    pd.testing.assert_frame_equal(a.loc[before, models], b.loc[before, models])


def test_implied_volatility_is_scored_as_a_forecast(garch_bars: pd.DataFrame) -> None:
    implied = pd.Series(0.25, index=garch_bars.index[::3])
    report = vol.evaluate_vol(garch_bars, vol.VolConfig(min_train=750, refit=252), implied=implied)
    assert report["scores"]["implied"]["n"] > 100
    assert "Implied volatility" in vol.report_text(report)


def test_now_and_har_alone(garch_bars: pd.DataFrame) -> None:
    now = vol.forecast_now(garch_bars, horizon=21)
    assert set(now["forecasts"]) == set(vol.MODELS)
    assert all(0.02 < v < 1.5 for v in now["forecasts"].values() if v is not None)
    har = vol.har_forecast(garch_bars)
    assert har == pytest.approx(now["forecasts"]["har"], rel=0.05)
    assert vol.har_forecast(garch_bars.iloc[:100]) is None


def test_too_little_history_says_so() -> None:
    returns, sigma = simulate_garch(300, OMEGA, ALPHA, BETA)
    with pytest.raises(ValueError, match="sessions of bars"):
        vol.evaluate_vol(ohlc_from_returns(returns, sigma), vol.VolConfig())
