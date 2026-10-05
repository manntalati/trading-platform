from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_strategies.ma_timing import (
    CASH,
    equal_weight_monthly,
    ma_signals,
    ma_timing,
    month_end_closes,
)

DAYS = pd.DatetimeIndex(sessions(date(2015, 1, 2), date(2016, 12, 30)))
MONTHS = DAYS.to_period("M")


def monthly_steps(levels: list[float]) -> pd.Series:
    """Daily prices that sit flat at ``levels[k]`` throughout the k-th month."""
    month_ids = pd.factorize(MONTHS)[0]
    keep = month_ids < len(levels)
    return pd.Series(np.asarray(levels)[month_ids[keep]], index=DAYS[keep], name="A")


# Months 1-10 flat at 10, month 11 at 12 (above the 10-month SMA), month 12 at 8 (below).
LEVELS = [10.0] * 10 + [12.0, 8.0, 8.0]


def month_end(k: int) -> pd.Timestamp:
    """Last session of the k-th month (1-based) of DAYS."""
    return pd.Timestamp(month_end_closes(DAYS.to_frame()).index[k - 1])


def test_signal_needs_price_strictly_above_full_window_average() -> None:
    signals = ma_signals(monthly_steps(LEVELS).to_frame(), months=10)["A"]
    assert not signals.iloc[:10].any()  # warm-up, then price == SMA is not "above"
    assert signals.iloc[10]  # 12 > mean(9x10, 12) = 10.2
    assert not signals.iloc[11]  # 8 < mean(8x10, 12, 8) = 10.0


def test_lag_one_trades_the_session_after_the_signal() -> None:
    result = ma_timing(monthly_steps(LEVELS).to_frame(), lag_days=1, cost_bps=0)
    weight = result.weights["A"]
    signal_in, signal_out = month_end(11), month_end(12)
    assert weight.loc[signal_in] == 0.0
    assert weight.loc[DAYS[DAYS.get_loc(signal_in) + 1]] == 1.0
    assert weight.loc[signal_out] == 1.0
    assert weight.loc[DAYS[DAYS.get_loc(signal_out) + 1]] == 0.0
    assert list(result.trades["weight_after"]) == [1.0, 0.0]


def test_lag_zero_trades_on_the_signal_close() -> None:
    result = ma_timing(monthly_steps(LEVELS).to_frame(), lag_days=0, cost_bps=0)
    assert result.weights["A"].loc[month_end(11)] == 1.0
    assert result.weights["A"].loc[month_end(12)] == 0.0
    # Holding through month 12 captures its 12 -> 8 drop on the first day of the month.
    first_day_m12 = DAYS[DAYS.get_loc(month_end(11)) + 1]
    assert result.returns.loc[first_day_m12] == pytest.approx(8 / 12 - 1)


def test_series_start_when_the_first_position_is_taken() -> None:
    result = ma_timing(monthly_steps(LEVELS).to_frame(), lag_days=1, cost_bps=0)
    assert result.returns.index[0] == DAYS[DAYS.get_loc(month_end(10)) + 1]
    assert result.weights.iloc[0][CASH] == 1.0


def test_costs_charged_on_traded_notional() -> None:
    prices = monthly_steps(LEVELS).to_frame()
    free = ma_timing(prices, cost_bps=0)
    costly = ma_timing(prices, cost_bps=10)
    diff = (free.returns - costly.returns).loc[lambda s: s.abs() > 0]
    assert len(diff) == 2  # buy and sell
    np.testing.assert_allclose(diff, 0.001)
    assert costly.turnover.sum() == pytest.approx(2.0)


def test_cash_earns_cash_returns_when_out() -> None:
    prices = monthly_steps([10.0] * 11).to_frame()  # never above its SMA
    cash = pd.Series(0.0001, index=DAYS)
    result = ma_timing(prices, cash_returns=cash, cost_bps=0)
    assert result.exposure.max() == 0.0
    np.testing.assert_allclose(result.returns.iloc[1:], 0.0001)


def test_invested_returns_match_the_asset() -> None:
    rng = np.random.default_rng(0)
    trend = pd.Series(100 * np.exp(np.cumsum(0.002 + rng.normal(0, 0.005, len(DAYS)))), DAYS)
    result = ma_timing(trend.to_frame("A"), cost_bps=0)
    invested = result.weights["A"].shift(1) == 1.0
    asset = trend.pct_change().loc[result.returns.index]
    np.testing.assert_allclose(result.returns[invested], asset[invested])


def test_two_assets_split_equally_and_drift() -> None:
    a = monthly_steps(LEVELS)
    b = monthly_steps([10.0] * 13).rename("B")  # never invested
    # Let A rise 1% a day during month 12 so its weight drifts while held.
    in_month_12 = a.index.to_period("M") == month_end(12).to_period("M")
    a[in_month_12] = 12.0 * 1.01 ** np.arange(in_month_12.sum())
    result = ma_timing(pd.concat([a, b], axis=1), lag_days=1, cost_bps=0)
    entry = DAYS[DAYS.get_loc(month_end(11)) + 1]
    assert result.weights.loc[entry, ["A", "B", CASH]].tolist() == [0.5, 0.0, 0.5]
    assert result.weights.loc[month_end(12), "A"] > 0.5  # drifted up with the price


def test_no_look_ahead_truncation_does_not_change_the_past() -> None:
    rng = np.random.default_rng(1)
    walk = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (len(DAYS), 2)), axis=0)),
        index=DAYS,
        columns=["A", "B"],
    )
    full = ma_timing(walk)
    for cut in (300, 333, 400):
        partial = ma_timing(walk.iloc[:cut])
        common = partial.returns.index
        pd.testing.assert_series_equal(partial.returns, full.returns.loc[common])
        pd.testing.assert_frame_equal(partial.weights, full.weights.loc[common])


def test_incomplete_trailing_month_has_no_signal() -> None:
    mid_month = DAYS[DAYS < "2016-06-15"]
    month_ends = month_end_closes(pd.DataFrame({"A": 1.0}, index=mid_month))
    assert month_ends.index[-1] == pd.Timestamp("2016-05-31")


def test_equal_weight_benchmark_single_asset_is_buy_and_hold() -> None:
    rng = np.random.default_rng(2)
    prices = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(DAYS)))), DAYS)
    bench = equal_weight_monthly(prices.to_frame("A"), cost_bps=0)
    asset = prices.pct_change().loc[bench.returns.index[1:]]
    np.testing.assert_allclose(bench.returns.iloc[1:], asset)
    assert bench.exposure.min() == 1.0


def test_fixed_weights_overlay() -> None:
    a = monthly_steps(LEVELS)
    b = monthly_steps([10.0] * 13).rename("B")  # never invested
    weights = pd.Series({"A": 0.7, "B": 0.2})
    timing = ma_timing(pd.concat([a, b], axis=1), cost_bps=0, weights=weights)
    entry = DAYS[DAYS.get_loc(month_end(11)) + 1]
    assert timing.weights.loc[entry, ["A", "B", CASH]].tolist() == pytest.approx([0.7, 0.0, 0.3])
    bench = equal_weight_monthly(pd.concat([a, b], axis=1), cost_bps=0, weights=weights)
    assert bench.weights.iloc[0][["A", "B", CASH]].tolist() == pytest.approx([0.7, 0.2, 0.1])
    with pytest.raises(ValueError, match="sum to at most 1"):
        ma_timing(pd.concat([a, b], axis=1), weights=pd.Series({"A": 0.9, "B": 0.2}))
