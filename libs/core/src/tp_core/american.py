"""American options on a Cox-Ross-Rubinstein binomial tree.

US equity options can be exercised any day, which Black-Scholes ignores. For calls on stocks
with no dividend before expiry the right is worth nothing (never exercise a call early: selling
it is worth more), so ``tp_core.greeks`` is exact there. For puts, and for calls on a stock with
a dividend yield, early exercise can be worth something, most for deep in-the-money puts with
months to run and high rates: the tree prices that **early-exercise premium**.

The tree has ``steps`` time steps of ``years / steps``; each step the price goes up by
``u = exp(vol * sqrt(dt))`` or down by ``1 / u`` with the risk-neutral probability of up
``(exp((rate - div) * dt) - d) / (u - d)``. Working back from expiry, a node is worth the larger of
holding (the discounted expected value of the next step) and exercising now. A binomial price
oscillates as the number of steps changes; averaging ``steps`` and ``steps + 1`` cancels most of
that, so 200 steps is within about a cent of the limit for ordinary contracts.
"""

from __future__ import annotations

import math

import numpy as np

from tp_core import greeks as bs

DEFAULT_STEPS = 200


def _tree(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    call: bool,
    rate: float,
    div: float,
    steps: int,
    american: bool,
) -> float:
    dt = years / steps
    u = math.exp(vol * math.sqrt(dt))
    d = 1.0 / u
    p = (math.exp((rate - div) * dt) - d) / (u - d)
    if not 0.0 < p < 1.0:  # volatility too low for the drift at this step size
        return bs.price(spot, strike, years, vol, call=call, rate=rate, div=div)
    disc = math.exp(-rate * dt)
    j = np.arange(steps + 1)
    prices = spot * u ** (2 * j - steps)
    values = np.maximum(prices - strike, 0.0) if call else np.maximum(strike - prices, 0.0)
    for i in range(steps - 1, -1, -1):
        values = disc * (p * values[1:] + (1.0 - p) * values[:-1])
        if american:
            prices = spot * u ** (2 * np.arange(i + 1) - i)
            exercise = prices - strike if call else strike - prices
            values = np.maximum(values, exercise)
    return float(values[0])


def price(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
    steps: int = DEFAULT_STEPS,
    american: bool = True,
) -> float:
    """Per-share value. ``american=False`` prices the European contract on the same tree (to
    check the tree against Black-Scholes)."""
    if years <= 0:
        return bs.intrinsic(spot, strike, call)
    if vol <= 0:
        euro = bs.price(spot, strike, years, 0.0, call=call, rate=rate, div=div)
        return max(euro, bs.intrinsic(spot, strike, call)) if american else euro
    a = _tree(spot, strike, years, vol, call, rate, div, steps, american)
    b = _tree(spot, strike, years, vol, call, rate, div, steps + 1, american)
    return 0.5 * (a + b)


def early_exercise_premium(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
    steps: int = DEFAULT_STEPS,
) -> float:
    """What the right to exercise early adds to the European (Black-Scholes) value, per share."""
    if years <= 0 or vol <= 0:
        return 0.0
    tree = price(spot, strike, years, vol, call=call, rate=rate, div=div, steps=steps)
    euro = price(
        spot, strike, years, vol, call=call, rate=rate, div=div, steps=steps, american=False
    )
    return max(tree - euro, 0.0)


def implied_vol(
    value: float,
    spot: float,
    strike: float,
    years: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
    steps: int = 100,
) -> float | None:
    """The volatility at which the American price equals ``value`` (bisection on the tree). For
    a deep in-the-money put Black-Scholes has no answer when the quote is below the European
    value's floor; the American one usually does. None when no volatility fits."""
    if years <= 0 or value <= 0 or spot <= 0 or strike <= 0:
        return None

    def f(v: float) -> float:
        return price(spot, strike, years, v, call=call, rate=rate, div=div, steps=steps) - value

    lo, hi = bs.MIN_VOL, bs.MAX_VOL
    if f(lo) >= -1e-9 or f(hi) <= 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)
