"""Black-Scholes against textbook values, finite differences and its own inverse."""

import math

import pytest

from tp_core import greeks as bs


def test_textbook_prices_and_put_call_parity() -> None:
    # Hull, Options, Futures and Other Derivatives: S=42, K=40, r=10%, vol=20%, 6 months.
    call = bs.price(42, 40, 0.5, 0.2, call=True, rate=0.1)
    put = bs.price(42, 40, 0.5, 0.2, call=False, rate=0.1)
    assert call == pytest.approx(4.76, abs=0.005)
    assert put == pytest.approx(0.81, abs=0.005)
    assert call - put == pytest.approx(42 - 40 * math.exp(-0.1 * 0.5))


def test_expired_or_zero_vol_is_worth_its_intrinsic_value() -> None:
    assert bs.price(105, 100, 0.0, 0.3, call=True) == 5.0
    assert bs.price(95, 100, 0.0, 0.3, call=True) == 0.0
    assert bs.price(95, 100, -1.0, 0.3, call=False) == 5.0
    g = bs.greeks(105, 100, 0.0, 0.3, call=True)
    assert (g.price, g.delta, g.theta) == (5.0, 1.0, 0.0)


@pytest.mark.parametrize("call", [True, False])
def test_greeks_match_finite_differences(call: bool) -> None:
    s, k, t, v, r = 100.0, 105.0, 30 / 365, 0.35, 0.04
    g = bs.greeks(s, k, t, v, call=call, rate=r)

    def p(spot: float = s, years: float = t, vol: float = v) -> float:
        return bs.price(spot, k, years, vol, call=call, rate=r)

    h = 0.01
    assert g.delta == pytest.approx((p(s + h) - p(s - h)) / (2 * h), rel=1e-4)
    assert g.gamma == pytest.approx((p(s + h) - 2 * p() + p(s - h)) / h**2, rel=1e-3)
    day = 1 / 365
    assert g.theta == pytest.approx(p(years=t - day) - p(), rel=0.02)  # per calendar day
    assert g.vega == pytest.approx(p(vol=v + 0.01) - p(), rel=0.02)  # per vol point
    assert g.theta < 0  # a long option loses value with time


@pytest.mark.parametrize(("strike", "call"), [(90, True), (100, True), (115, False), (80, False)])
def test_implied_vol_recovers_the_vol_that_priced_it(strike: float, call: bool) -> None:
    value = bs.price(100, strike, 20 / 365, 0.42, call=call, rate=0.04)
    assert bs.implied_vol(value, 100, strike, 20 / 365, call=call, rate=0.04) == pytest.approx(
        0.42, abs=1e-5
    )


def test_implied_vol_has_no_answer_outside_the_price_bounds() -> None:
    assert bs.implied_vol(4.0, 105, 100, 0.1, call=True) is None  # below intrinsic
    assert bs.implied_vol(150.0, 100, 100, 0.1, call=True) is None  # more than the stock
    assert bs.implied_vol(1.0, 100, 100, 0.0, call=True) is None  # expired


def test_probabilities_and_expected_move() -> None:
    # At the money with no drift, the chance of ending above is just under a half (the
    # lognormal's median sits below its mean).
    p = bs.prob_above(100, 100, 0.25, 0.3)
    assert 0.45 < p < 0.5
    assert bs.prob_above(100, 120, 0.25, 0.3) < bs.prob_above(100, 110, 0.25, 0.3)
    assert bs.prob_above(100, 90, 0.0, 0.3) == 1.0
    assert bs.expected_move(100, 0.25, 0.3) == pytest.approx(15.0)
    assert bs.expected_move(100, 0.0, 0.3) == 0.0
