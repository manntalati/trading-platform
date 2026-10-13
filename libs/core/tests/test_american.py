"""American options on the binomial tree."""

import pytest

from tp_core import american as am
from tp_core import greeks as bs


@pytest.mark.parametrize("strike", [80.0, 100.0, 120.0])
@pytest.mark.parametrize("call", [True, False])
def test_the_tree_converges_to_black_scholes_for_european_options(
    strike: float, call: bool
) -> None:
    tree = am.price(100.0, strike, 1.0, 0.3, call=call, rate=0.05, american=False)
    assert tree == pytest.approx(bs.price(100.0, strike, 1.0, 0.3, call=call, rate=0.05), abs=0.01)


def test_early_exercise() -> None:
    # Hull's textbook American put: S=K=50, r=10%, vol 40%, 5 months: 4.28 (European 4.08)
    assert am.price(50.0, 50.0, 5 / 12, 0.4, call=False, rate=0.1, steps=500) == pytest.approx(
        4.284, abs=0.005
    )
    # a call on a stock without dividends is never worth exercising early
    call = am.price(100.0, 90.0, 1.0, 0.25, call=True, rate=0.05)
    assert call == pytest.approx(bs.price(100.0, 90.0, 1.0, 0.25, call=True, rate=0.05), abs=0.01)
    assert am.early_exercise_premium(100.0, 90.0, 1.0, 0.25, call=True, rate=0.05) < 0.01
    # a deep in-the-money put is worth more than its European twin; a dividend makes calls so too
    assert am.early_exercise_premium(100.0, 130.0, 1.0, 0.25, call=False, rate=0.05) > 0.5
    assert am.early_exercise_premium(100.0, 80.0, 1.0, 0.2, call=True, rate=0.02, div=0.06) > 0.1
    # never below what exercising now pays
    assert am.price(100.0, 150.0, 1.0, 0.3, call=False, rate=0.05) >= 50.0


def test_edges() -> None:
    assert am.price(100.0, 110.0, 0.0, 0.3, call=False) == 10.0
    assert am.price(100.0, 110.0, 0.5, 0.0, call=False, rate=0.05) == pytest.approx(10.0)
    assert am.early_exercise_premium(100.0, 110.0, 0.0, 0.3, call=False) == 0.0


def test_american_implied_vol() -> None:
    value = am.price(100.0, 115.0, 0.75, 0.35, call=False, rate=0.05)
    iv = am.implied_vol(value, 100.0, 115.0, 0.75, call=False, rate=0.05)
    assert iv == pytest.approx(0.35, abs=2e-3)
    assert am.implied_vol(14.0, 100.0, 115.0, 0.75, call=False) is None  # below intrinsic
    assert am.implied_vol(1.0, 100.0, 115.0, 0.0, call=False) is None
