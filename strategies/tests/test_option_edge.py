"""Valuing positions at the horizon under our forecast, on a synthetic chain (XYZ)."""

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tp_core.testing import synthetic_chain
from tp_strategies import option_edge as edge
from tp_strategies.option_edge import Leg, Outlook, Surface
from tp_strategies.options_research import price_chain

NOW = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)
DAYS = (18, 46, 60, 88, 151, 340)


def priced(iv: float | list[float] = 0.3, skew: float = 0.0) -> pd.DataFrame:
    expirations = [NOW.date() + timedelta(days=d) for d in DAYS]
    strikes = [float(k) for k in range(50, 155, 5)]
    chain = synthetic_chain("XYZ", 100.0, NOW, expirations=expirations, iv=iv, skew=skew,
                            strikes=strikes, half_spread=0.01)  # fmt: skip
    return price_chain(chain, 100.0, NOW, 0.0)


def leg(p: pd.DataFrame, right: str, dte: int, strike: float, qty: int = 1) -> Leg:
    row = p[(p["right"] == right) & (p["dte"] == dte) & (p["strike"] == strike)].iloc[0]
    return Leg(str(row["contract"]), strike, float(row["years"]), right == "C", qty,
               float(row["bid"]), float(row["ask"]))  # fmt: skip


def outlook(vol: float, drift: float = 0.0, horizon: int = 20) -> Outlook:
    return Outlook(spot=100.0, horizon=horizon, drift=drift, vol=vol, rate=0.0)


def test_the_surface_reads_back_the_smile_and_term_structure() -> None:
    s = Surface(priced(iv=[0.40, 0.35, 0.33, 0.30, 0.28, 0.25], skew=-0.2), 100.0)
    assert s.atm == pytest.approx([0.40, 0.35, 0.33, 0.30, 0.28, 0.25], abs=0.01)
    assert s.slope == pytest.approx([-0.2] * 6, abs=0.05)
    between = float(s.atm_at((60 + 88) / 2 / 365))
    assert 0.30 < between < 0.33  # interpolated in total variance
    assert float(s.atm_at(5.0)) == pytest.approx(0.25, abs=0.01)  # flat past the last expiry
    assert float(s.iv(88 / 365, math.log(0.9))) == pytest.approx(0.30 + 0.2 * 0.105, abs=0.01)


def test_at_fair_volatility_buying_loses_about_the_costs() -> None:
    p = priced()
    call = leg(p, "C", 60, 100.0)
    fair = edge.evaluate([call], outlook(0.3), Surface(p, 100.0))
    assert -0.08 < fair["expected_return"] < 0  # the spread and the fees, both ways
    assert 0.3 < fair["prob_profit"] < 0.5
    assert fair["q10"] < -0.4
    assert fair["entry"] == pytest.approx(call.ask + edge.FEE)


def test_more_movement_or_drift_than_priced_is_an_edge() -> None:
    p = priced()
    s = Surface(p, 100.0)
    call, put = leg(p, "C", 60, 100.0), leg(p, "P", 60, 100.0)
    fair = edge.evaluate([call], outlook(0.3), s)["expected_return"]
    assert edge.evaluate([call], outlook(0.45), s)["expected_return"] > fair
    assert edge.evaluate([call], outlook(0.3, drift=0.05), s)["expected_return"] > fair
    assert (
        edge.evaluate([put], outlook(0.3, drift=0.05), s)["expected_return"]
        < (edge.evaluate([put], outlook(0.3), s)["expected_return"])
    )
    straddle = [call, put]
    assert edge.evaluate(straddle, outlook(0.5), s)["expected_return"] > 0.05
    assert edge.evaluate(straddle, outlook(0.2), s)["expected_return"] < -0.05


def test_selling_rich_premium_has_the_edge_when_movement_falls_short() -> None:
    p = priced(iv=0.4)
    short, wing = leg(p, "P", 46, 90.0, qty=-1), leg(p, "P", 46, 85.0)
    credit = short.bid - wing.ask
    assert credit > 0
    risk = 5.0 - credit
    calm = edge.evaluate([short, wing], outlook(0.2, horizon=17), Surface(p, 100.0), capital=risk)
    assert calm["entry"] < 0  # a credit
    assert calm["capital"] == pytest.approx(risk)
    assert calm["expected_return"] > 0
    assert calm["prob_profit"] > 0.7
    wild = edge.evaluate([short, wing], outlook(0.6, horizon=17), Surface(p, 100.0), capital=risk)
    assert wild["expected_return"] < calm["expected_return"]


def test_implied_volatility_drifts_back_to_its_usual_level() -> None:
    p = priced(iv=0.5)
    call = leg(p, "C", 151, 100.0)
    rich_now = edge.evaluate([call], outlook(0.5, horizon=40), Surface(p, 100.0))
    usual = Outlook(spot=100.0, horizon=40, drift=0.0, vol=0.5, rate=0.0, iv_level=0.3)
    crushed = edge.evaluate([call], usual, Surface(p, 100.0))
    assert crushed["expected_return"] < rich_now["expected_return"] - 0.05


def test_gamma_against_theta_and_the_horizon_volatility() -> None:
    p = priced()
    row = p[(p["right"] == "C") & (p["dte"] == 46) & (p["strike"] == 100.0)].iloc[0]
    move = edge.breakeven_move(float(row["theta"]), float(row["gamma"]), 100.0)
    assert move is not None
    # an at-the-money option's decay is paid by moves near its implied daily volatility
    assert move == pytest.approx(0.3 / math.sqrt(252), rel=0.15)
    assert edge.breakeven_move(0.05, 0.02, 100.0) is None  # earns theta: nothing to pay
    assert edge.horizon_vol(0.2, 0.3, 0.4, 10) == 0.2
    assert edge.horizon_vol(None, 0.3, 0.4, 10) == 0.3
    longer = edge.horizon_vol(0.2, None, 0.4, 126)
    assert longer is not None
    assert 0.2 < longer < 0.4
    assert edge.default_drift(252) == pytest.approx(edge.EQUITY_DRIFT)


def test_an_expired_leg_is_worth_its_intrinsic_value() -> None:
    p = priced()
    near = leg(p, "C", 18, 95.0)
    out = edge.leg_values([near], outlook(0.3, horizon=30), Surface(p, 100.0))
    spots = edge.terminal(outlook(0.3, horizon=30))
    assert out[0] == pytest.approx((spots - 95.0).clip(min=0))
