"""Black-Scholes against textbook values, finite differences and its own inverse."""

import math

import numpy as np
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


@pytest.mark.parametrize("strike", [40.0, 70.0, 95.0, 100.0, 110.0, 160.0, 250.0])
@pytest.mark.parametrize("call", [True, False])
@pytest.mark.parametrize("years", [3 / 365, 0.25, 2.0])
def test_newton_implied_vol_recovers_the_volatility(
    strike: float, call: bool, years: float
) -> None:
    for vol in (0.08, 0.3, 1.2):
        value = bs.price(100.0, strike, years, vol, call=call, rate=0.04)
        floor = bs.price(100.0, strike, years, bs.MIN_VOL, call=call, rate=0.04)
        if value - floor < 1e-6:
            continue  # no time value left: the volatility can't be read from the price
        iv = bs.implied_vol(value, 100.0, strike, years, call=call, rate=0.04)
        assert iv is not None
        back = bs.price(100.0, strike, years, iv, call=call, rate=0.04)
        assert back == pytest.approx(value, abs=1e-7)


def test_higher_greeks_match_finite_differences() -> None:
    s, k, t, v, r = 100.0, 105.0, 0.4, 0.3, 0.05
    for call in (True, False):
        h = bs.higher_greeks(s, k, t, v, call=call, rate=r, div=0.01)

        def delta(vol: float = v, years: float = t, call: bool = call) -> float:
            return bs.greeks(s, k, years, vol, call=call, rate=r, div=0.01).delta

        def vega(vol: float, call: bool = call) -> float:
            return bs.greeks(s, k, t, vol, call=call, rate=r, div=0.01).vega

        up = bs.price(s, k, t, v, call=call, rate=r + 1e-4, div=0.01)
        down = bs.price(s, k, t, v, call=call, rate=r - 1e-4, div=0.01)
        assert h.rho == pytest.approx((up - down) / 2e-4 / 100, rel=1e-4)
        assert h.vanna == pytest.approx((delta(v + 1e-4) - delta(v - 1e-4)) / 2e-4 / 100, rel=1e-4)
        day = 1 / bs.YEAR_DAYS
        assert h.charm == pytest.approx(delta(years=t - day) - delta(), rel=1e-2)
        assert h.vomma == pytest.approx((vega(v + 1e-4) - vega(v - 1e-4)) / 2e-4 / 100, rel=1e-4)
    assert bs.higher_greeks(s, k, 0.0, v, call=True) == bs.HigherGreeks(0.0, 0.0, 0.0, 0.0)


def test_the_array_functions_agree_with_the_scalar_ones() -> None:
    rng = np.random.default_rng(0)
    n = 2000
    strike = rng.uniform(30, 250, n)
    years = np.concatenate([[0.0, 0.5], rng.uniform(0.005, 2.5, n - 2)])
    vol = np.concatenate([[0.3, 0.0], rng.uniform(0.05, 1.5, n - 2)])
    call = rng.random(n) < 0.5
    prices = bs.price_array(100.0, strike, years, vol, call, rate=0.04)
    g = bs.greeks_array(100.0, strike, years, vol, call, rate=0.04)
    for i in range(n):
        one = bs.greeks(100.0, strike[i], years[i], vol[i], call=bool(call[i]), rate=0.04)
        assert prices[i] == pytest.approx(one.price, abs=1e-9)
        for name in ("delta", "gamma", "theta", "vega"):
            assert g[name][i] == pytest.approx(getattr(one, name), abs=1e-9)
    quoted = prices * rng.uniform(0.9, 1.1, n)  # some fall outside what any volatility gives
    iv = bs.implied_vol_array(quoted, 100.0, strike, years, call, rate=0.04)
    for i in range(n):
        one_iv = bs.implied_vol(
            quoted[i], 100.0, strike[i], years[i], call=bool(call[i]), rate=0.04
        )
        if one_iv is None:
            assert math.isnan(iv[i])
        else:
            assert iv[i] == pytest.approx(one_iv, abs=1e-7)
    assert np.isnan(bs.greeks_array(100.0, 100.0, 0.5, np.nan, True)["delta"])
