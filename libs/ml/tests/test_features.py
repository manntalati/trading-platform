from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tp_ml.features import (
    FEATURES,
    LOOKBACK,
    compute_features,
    feature_spec,
    latest_price_features,
    rolling_mean,
    rolling_std,
)

SECTORS = {"AAA": "Tech", "BBB": "Tech", "CCC": "Tech", "DDD": "Energy", "EEE": "Energy"}


def test_panel_shape_and_columns(bars: dict[str, pd.DataFrame]) -> None:
    panel = compute_features(bars, sectors=SECTORS)
    assert list(panel.columns) == list(FEATURES)
    assert panel.index.names == ["date", "symbol"]
    assert len(panel) == bars["close"].size
    last = panel.loc[bars["close"].index[-1]]
    assert last[["ret_252", "dist_ma200", "vol_63", "rsi_14"]].notna().all().all()


def test_returns_and_relative_returns(bars: dict[str, pd.DataFrame]) -> None:
    panel = compute_features(bars, sectors=SECTORS)
    close = bars["close"]
    day = close.index[300]
    assert panel.loc[(day, "AAA"), "ret_5"] == pytest.approx(
        math.log(close.loc[day, "AAA"] / close.iloc[295]["AAA"])
    )
    assert panel.loc[(day, "SPY"), "rel_market_63"] == pytest.approx(0.0)
    tech = panel.loc[day].loc[["AAA", "BBB", "CCC"], "rel_sector_63"]
    assert sorted(tech.round(12).tolist())[1] == 0.0  # three members: the middle one is the median
    assert math.isnan(panel.loc[(day, "SPY"), "rel_sector_63"])  # no sector


def test_features_are_point_in_time(bars: dict[str, pd.DataFrame]) -> None:
    """Changing the future never changes the past: features on bars cut at a day equal the
    full-history features up to that day."""
    full = compute_features(bars, sectors=SECTORS)
    cut = bars["close"].index[400]
    shocked = {k: v.copy() for k, v in bars.items()}
    for frame in shocked.values():
        frame.loc[frame.index > cut] *= 3.0
    later = compute_features(shocked, sectors=SECTORS)
    upto = full.index.get_level_values("date") <= cut
    pd.testing.assert_frame_equal(full[upto], later[upto])


def test_latest_features_match_the_panel(bars: dict[str, pd.DataFrame]) -> None:
    """What a strategy scores each day is exactly what the model was trained on."""
    window = {k: v.iloc[-LOOKBACK:] for k, v in bars.items()}
    latest = latest_price_features(window, sectors=SECTORS)
    panel = compute_features(bars, sectors=SECTORS).loc[bars["close"].index[-1]]
    price = [c for c in FEATURES if c not in ("vix", "vix_ratio", "curve_10y_3m",
                                              "rate_10y_chg_63", "days_since_results",
                                              "last_sue")]  # fmt: skip
    # RSI is an exponential average: started LOOKBACK sessions back instead of at the first bar,
    # RSI(14) differs by (13/14)^245, about 1e-8 relative
    pd.testing.assert_frame_equal(
        latest[price], panel[price].rename_axis(None), check_names=False, rtol=1e-6
    )
    assert latest["vix"].isna().all()


@pytest.mark.parametrize(("n", "min_periods"), [(5, 5), (20, 10), (63, 45), (200, 200)])
def test_rolling_helpers_match_pandas(n: int, min_periods: int) -> None:
    rng = np.random.default_rng(1)
    frame = pd.DataFrame(rng.normal(100, 5, size=(300, 6)))
    frame = frame.mask(rng.random(frame.shape) < 0.1)
    frame.iloc[:40, 2] = np.nan
    expected_mean = frame.rolling(n, min_periods=min_periods).mean()
    expected_std = frame.rolling(n, min_periods=min_periods).std()
    pd.testing.assert_frame_equal(rolling_mean(frame, n, min_periods), expected_mean, rtol=1e-9)
    pd.testing.assert_frame_equal(rolling_std(frame, n, min_periods), expected_std, rtol=1e-6)


def test_macro_features_are_the_same_for_every_symbol(bars: dict[str, pd.DataFrame]) -> None:
    days = bars["close"].index
    macro = pd.DataFrame(
        {"VIXCLS": 20.0, "VXVCLS": 22.0, "DGS10": 4.0, "DGS3MO": 5.0}, index=days[::2]
    )  # every other day: the gaps are forward-filled
    panel = compute_features(bars, macro=macro)
    day = days[301]
    row = panel.loc[day]
    assert (row["vix"] == 20.0).all()
    assert row["vix_ratio"].iloc[0] == pytest.approx(20 / 22)
    assert row["curve_10y_3m"].iloc[0] == pytest.approx(-1.0)
    assert row["rate_10y_chg_63"].iloc[0] == pytest.approx(0.0)


def test_earnings_features_count_sessions_and_do_not_carry_old_surprises(
    bars: dict[str, pd.DataFrame],
) -> None:
    days = bars["close"].index
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB"],
            "session": [days[100], days[200], days[150]],
            "sue": [1.5, np.nan, -2.0],
        }
    )
    panel = compute_features(bars, earnings=earnings)
    assert panel.loc[(days[105], "AAA"), "days_since_results"] == 5
    assert panel.loc[(days[105], "AAA"), "last_sue"] == 1.5
    # the latest results had no surprise estimate: NaN, not the older 1.5
    assert panel.loc[(days[210], "AAA"), "days_since_results"] == 10
    assert math.isnan(panel.loc[(days[210], "AAA"), "last_sue"])
    assert panel.loc[(days[150], "BBB"), "last_sue"] == -2.0
    assert math.isnan(panel.loc[(days[99], "AAA"), "days_since_results"])
    assert math.isnan(panel.loc[(days[300], "CCC"), "last_sue"])


def test_feature_spec_changes_with_the_parameters() -> None:
    assert feature_spec(macro=True) == feature_spec(macro=True)
    assert feature_spec(macro=True) != feature_spec(macro=False)
    assert len(feature_spec()) == 12


def test_news_features_over_five_sessions() -> None:
    import numpy as np

    from tp_ml.features import compute_features
    from tp_ml.testing import random_bars

    bars = random_bars(["AAA", "BBB"], n=40)
    days = bars["close"].index
    news = pd.DataFrame(
        {"date": [days[10], days[12], days[30]], "symbol": ["AAA", "AAA", "BBB"],
         "score": [0.5, -0.1, 0.9], "headlines": [2, 1, 1]}
    )  # fmt: skip
    f = compute_features(bars, news=news)
    aaa = f.xs("AAA", level="symbol")
    assert np.isnan(aaa["news_count_5d"].iloc[9])  # before the first headline: unknown
    assert aaa["news_count_5d"].iloc[11] == 2
    assert aaa["news_sentiment_5d"].iloc[12] == pytest.approx((0.5 * 2 - 0.1) / 3)
    assert aaa["news_count_5d"].iloc[20] == 0  # known, and no news
    assert np.isnan(aaa["news_sentiment_5d"].iloc[20])
    assert compute_features(bars)["news_count_5d"].isna().all()
