"""Portfolio risk: beta, VaR, correlation, option Greeks and the scenario grid."""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tp_core.portfolio_risk import OptionLeg, aggregate_greeks, portfolio_risk, scenario_grid

NOW = datetime(2026, 1, 12, 15, 0, tzinfo=UTC)


def closes() -> pd.DataFrame:
    days = pd.bdate_range(end="2026-01-09", periods=300)
    rng = np.random.default_rng(5)
    spy = rng.normal(0.0004, 0.01, 300)
    tech = 1.5 * spy + rng.normal(0, 0.005, 300)  # beta 1.5
    twin = tech + rng.normal(0, 0.002, 300)  # moves with tech
    bond = rng.normal(0.0001, 0.003, 300)  # unrelated
    r = pd.DataFrame({"SPY": spy, "XYZ": tech, "XYY": twin, "BND": bond}, index=days)
    return 100 * (1 + r).cumprod()


def test_beta_var_and_correlation() -> None:
    risk = portfolio_risk(
        {"XYZ": 50_000.0, "XYY": 30_000.0, "BND": 20_000.0}, closes(), total_value=110_000.0
    )
    assert risk["betas"]["XYZ"] == pytest.approx(1.5, abs=0.1)
    assert risk["beta"] == pytest.approx((50_000 * 1.5 + 30_000 * 1.5) / 110_000, abs=0.1)
    var = risk["var"]
    assert 0 < var["historical_95"] < var["historical_99"]
    assert var["parametric_95"] < var["parametric_99"]
    assert var["expected_shortfall_95"] >= var["historical_95"]
    assert risk["coverage"] == 1.0
    top = risk["correlation"]["most_correlated"][0]
    assert {top["a"], top["b"]} == {"XYZ", "XYY"}
    assert top["correlation"] > 0.9
    assert len(risk["worst_days"]) == 5
    assert risk["scenarios"]["rows"][0]["pnl"][1] < 0  # market -10%: a loss


def test_uncovered_exposure_is_reported() -> None:
    risk = portfolio_risk({"XYZ": 50_000.0, "FUND": 50_000.0}, closes(), total_value=100_000.0)
    assert risk["coverage"] == pytest.approx(0.5)


def test_option_greeks_and_repricing() -> None:
    call = OptionLeg("XYZ", 2, 100.0, date(2026, 3, 20), call=True, iv=0.3, spot=100.0)
    g = aggregate_greeks([call], now=NOW, rate=0.04)
    assert 0 < g["delta_dollars"] < 200 * 100  # less than 200 shares' worth
    assert g["theta_day"] < 0
    assert g["vega_point"] > 0
    assert g["gamma_1pct"] > 0
    grid = scenario_grid({"XYZ": g["delta_dollars"]}, {"XYZ": 1.0}, [call], now=NOW, rate=0.04)
    flat = next(r for r in grid["rows"] if r["move"] == 0.0)
    assert flat["pnl"][1] == pytest.approx(0.0, abs=1e-6)  # nothing moved: no P&L
    assert flat["pnl"][2] > 0 > flat["pnl"][0]  # long vega
    up, down = grid["rows"][-1]["pnl"][1], grid["rows"][0]["pnl"][1]
    assert up > 0 > down
    assert up > -down  # convexity: gains more on +10% than it loses on -10%
