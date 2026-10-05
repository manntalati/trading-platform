import math

import numpy as np
import pandas as pd
import pytest

from tp_core import metrics as m


def series(values: list[float], start: str = "2024-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), name="r")


def test_simple_and_log_returns() -> None:
    prices = pd.Series([100.0, 110.0, 99.0])
    np.testing.assert_allclose(m.simple_returns(prices), [0.10, -0.10])
    np.testing.assert_allclose(m.log_returns(prices), np.log1p([0.10, -0.10]))
    # Log returns add over time; simple returns don't.
    assert m.log_returns(prices).sum() == pytest.approx(math.log(99 / 100))


def test_cagr_and_total_return() -> None:
    r = series([0.10, -0.05, 0.02])
    growth = 1.10 * 0.95 * 1.02
    assert m.total_return(r) == pytest.approx(growth - 1)
    assert m.cagr(r, periods_per_year=3) == pytest.approx(growth - 1)  # exactly one "year"
    assert m.cagr(r, periods_per_year=12) == pytest.approx(growth**4 - 1)
    assert m.cagr(series([-1.0, 0.5])) == -1.0  # wiped out


def test_volatility_and_sharpe_match_textbook_formula() -> None:
    rng = np.random.default_rng(0)
    r = series(list(rng.normal(0.0005, 0.01, 500)))
    assert m.annualized_volatility(r) == pytest.approx(r.std(ddof=1) * math.sqrt(252))
    assert m.sharpe_ratio(r) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    rf_daily = 1.04 ** (1 / 252) - 1
    expected = (r - rf_daily).mean() / (r - rf_daily).std(ddof=1) * math.sqrt(252)
    assert m.sharpe_ratio(r, rf=0.04) == pytest.approx(expected)


def test_sharpe_undefined_for_constant_returns() -> None:
    assert math.isnan(m.sharpe_ratio(series([0.01, 0.01, 0.01])))


def test_sortino_only_penalizes_downside() -> None:
    r = series([0.02, -0.01, 0.03, -0.02])
    downside = math.sqrt((0.01**2 + 0.02**2) / 4) * math.sqrt(252)
    assert m.downside_deviation(r) == pytest.approx(downside)
    assert m.sortino_ratio(r) == pytest.approx(r.mean() * 252 / downside)
    assert m.sortino_ratio(series([0.01, 0.02])) == math.inf


def test_drawdowns() -> None:
    # Wealth: 1.10, 0.55, 0.66, 1.21 (new high).
    r = series([0.10, -0.50, 0.20, 0.8333333333333333])
    np.testing.assert_allclose(m.drawdowns(r), [0.0, -0.5, -0.4, 0.0], atol=1e-12)
    assert m.max_drawdown(r) == pytest.approx(-0.5)
    assert m.max_drawdown_duration(r) == 2
    assert m.calmar_ratio(r, periods_per_year=4) == pytest.approx((1.21 - 1) / 0.5)


def test_drawdown_counts_starting_capital_as_peak() -> None:
    r = series([-0.10, 0.05])
    assert m.max_drawdown(r) == pytest.approx(-0.10)
    assert m.max_drawdown_duration(r) == 2  # never got back to 1.0


def test_hit_rate_and_win_loss() -> None:
    r = series([0.02, 0.0, -0.01, 0.04, -0.03])
    assert m.hit_rate(r) == pytest.approx(0.5)  # flat period ignored
    assert m.average_win(r) == pytest.approx(0.03)
    assert m.average_loss(r) == pytest.approx(-0.02)


def test_autocorrelation_sign() -> None:
    assert m.autocorrelation(series([0.01, -0.01] * 50)) == pytest.approx(-1.0)
    assert m.autocorrelation(series([0.01] * 10 + [-0.01] * 10)) > 0.5


def test_fat_tails() -> None:
    rng = np.random.default_rng(1)
    normal = series(list(rng.normal(0, 0.01, 50_000)))
    fat = series(list(rng.standard_t(3, 50_000) * 0.01))
    assert abs(m.excess_kurtosis(normal)) < 0.1
    assert m.excess_kurtosis(fat) > 3
    assert m.tail_ratio_vs_normal(normal) == pytest.approx(1.0, abs=0.25)
    assert m.tail_ratio_vs_normal(fat) > 2


def test_beta() -> None:
    rng = np.random.default_rng(2)
    bench = series(list(rng.normal(0, 0.01, 1000)))
    stock = 1.5 * bench + series(list(rng.normal(0, 0.002, 1000)))
    assert m.beta(stock, bench) == pytest.approx(1.5, abs=0.05)


def test_tear_sheet_series_and_frame() -> None:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame(
        rng.normal(0.0004, 0.01, (300, 2)),
        index=pd.bdate_range("2024-01-01", periods=300),
        columns=["A", "B"],
    )
    one = m.tear_sheet(frame["A"], benchmark=frame["B"])
    assert isinstance(one, pd.Series)
    assert one["periods"] == 300
    assert one["start"] == "2024-01-01"
    assert one["sharpe"] == pytest.approx(m.sharpe_ratio(frame["A"]))
    assert "beta" in one.index

    table = m.tear_sheet(frame)
    assert isinstance(table, pd.DataFrame)
    assert list(table.columns) == ["A", "B"]
    assert table.loc["max_drawdown", "B"] == pytest.approx(m.max_drawdown(frame["B"]))
