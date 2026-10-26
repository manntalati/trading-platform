"""Black-Scholes option maths: price, implied volatility, greeks and probabilities.

US equity options are American, but for calls on stocks that pay no dividend before expiry,
and for any contract a few weeks from expiry, the European formula is within cents of the real
value; good enough to read a position's risk, not to make a market. Time is calendar years
(days / 365) and theta is per calendar day.

The probabilities are *risk-neutral*: what the option's price implies, not a forecast. They are
how the market prices the odds, which is the right yardstick for "is holding worth it": an
option is fairly priced exactly when its expected payoff under these odds equals its price.

The ``*_array`` functions do the same over numpy arrays (a whole chain at once), element by
element; they agree with the scalar ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import special

YEAR_DAYS = 365.0
MIN_VOL, MAX_VOL = 1e-4, 5.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(
    spot: float, strike: float, years: float, vol: float, rate: float, div: float
) -> tuple[float, float]:
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * vol * vol) * years) / sd
    return d1, d1 - sd


def intrinsic(spot: float, strike: float, call: bool) -> float:
    return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)


def price(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
) -> float:
    """Per-share value of a call or put. At or past expiry (or zero vol) it's the intrinsic
    value (of the forward, for zero vol)."""
    if years <= 0 or vol <= 0:
        if years <= 0:
            return intrinsic(spot, strike, call)
        forward = spot * math.exp((rate - div) * years)
        return math.exp(-rate * years) * intrinsic(forward, strike, call)
    d1, d2 = _d1_d2(spot, strike, years, vol, rate, div)
    df, qf = math.exp(-rate * years), math.exp(-div * years)
    if call:
        return spot * qf * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * qf * norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    """Per share of underlying (multiply by 100 for a standard contract)."""

    price: float
    delta: float  # value change per $1 move in the underlying
    gamma: float  # delta change per $1 move
    theta: float  # value change per calendar day, all else equal (negative for long options)
    vega: float  # value change per 1 point (0.01) of implied volatility


def greeks(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
) -> Greeks:
    if years <= 0 or vol <= 0:
        itm = (spot > strike) if call else (spot < strike)
        value = price(spot, strike, years, vol, call=call, rate=rate, div=div)
        return Greeks(value, (1.0 if call else -1.0) if itm else 0.0, 0.0, 0.0, 0.0)
    d1, d2 = _d1_d2(spot, strike, years, vol, rate, div)
    df, qf = math.exp(-rate * years), math.exp(-div * years)
    root = math.sqrt(years)
    value = price(spot, strike, years, vol, call=call, rate=rate, div=div)
    gamma = qf * norm_pdf(d1) / (spot * vol * root)
    vega = spot * qf * norm_pdf(d1) * root / 100.0
    decay = -spot * qf * norm_pdf(d1) * vol / (2.0 * root)
    if call:
        delta = qf * norm_cdf(d1)
        theta = decay - rate * strike * df * norm_cdf(d2) + div * spot * qf * norm_cdf(d1)
    else:
        delta = -qf * norm_cdf(-d1)
        theta = decay + rate * strike * df * norm_cdf(-d2) - div * spot * qf * norm_cdf(-d1)
    return Greeks(value, delta, gamma, theta / YEAR_DAYS, vega)


def implied_vol(
    value: float,
    spot: float,
    strike: float,
    years: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
) -> float | None:
    """The volatility at which the model price equals ``value``. Newton's method on vega from a
    Brenner-Subrahmanyam first guess, kept inside a bracket that shrinks every step and falling
    back to bisection when a Newton step would leave it (deep in or out of the money, where vega
    is tiny), so it is fast and never diverges. None when no volatility fits: a price at or below
    intrinsic value, or above the most an option can be worth."""
    if years <= 0 or value <= 0 or spot <= 0 or strike <= 0:
        return None
    lo, hi = MIN_VOL, MAX_VOL
    if value <= price(spot, strike, years, lo, call=call, rate=rate, div=div) + 1e-9:
        return None
    if value >= price(spot, strike, years, hi, call=call, rate=rate, div=div):
        return None
    vol = min(max(math.sqrt(2.0 * math.pi / years) * value / spot, 0.05), 2.0)
    for _ in range(100):
        diff = price(spot, strike, years, vol, call=call, rate=rate, div=div) - value
        if abs(diff) < 1e-10:
            return vol
        if diff > 0:
            hi = vol
        else:
            lo = vol
        vega = _vega(spot, strike, years, vol, rate, div)
        step = vol - diff / vega if vega > 1e-12 else math.nan
        nxt = step if lo < step < hi else 0.5 * (lo + hi)
        if abs(nxt - vol) < 1e-10 or hi - lo < 1e-10:
            return nxt
        vol = nxt
    return vol


def _vega(spot: float, strike: float, years: float, vol: float, rate: float, div: float) -> float:
    """Per unit (1.00) of volatility."""
    d1, _ = _d1_d2(spot, strike, years, vol, rate, div)
    return spot * math.exp(-div * years) * norm_pdf(d1) * math.sqrt(years)


@dataclass(frozen=True)
class HigherGreeks:
    """Per share. Second-order effects that matter for holding options through time and
    volatility changes."""

    rho: float  # value change per 1 point (0.01) of the interest rate
    vanna: float  # delta change per 1 point of implied volatility
    charm: float  # delta change per calendar day, all else equal
    vomma: float  # vega change per 1 point of implied volatility


def higher_greeks(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    *,
    call: bool,
    rate: float = 0.0,
    div: float = 0.0,
) -> HigherGreeks:
    if years <= 0 or vol <= 0:
        return HigherGreeks(0.0, 0.0, 0.0, 0.0)
    d1, d2 = _d1_d2(spot, strike, years, vol, rate, div)
    df, qf = math.exp(-rate * years), math.exp(-div * years)
    root = math.sqrt(years)
    if call:
        rho = strike * years * df * norm_cdf(d2) / 100.0
    else:
        rho = -strike * years * df * norm_cdf(-d2) / 100.0
    vanna = -qf * norm_pdf(d1) * d2 / vol / 100.0
    drift = qf * norm_pdf(d1) * (2.0 * (rate - div) * years - d2 * vol * root)
    drift /= 2.0 * years * vol * root
    # delta's change as a day passes: minus its derivative in time to expiry
    charm = (div * qf * norm_cdf(d1) - drift) if call else (-div * qf * norm_cdf(-d1) - drift)
    vomma = _vega(spot, strike, years, vol, rate, div) * d1 * d2 / vol / 10_000.0
    return HigherGreeks(rho, vanna, charm / YEAR_DAYS, vomma)


def prob_above(
    spot: float, level: float, years: float, vol: float, *, rate: float = 0.0, div: float = 0.0
) -> float:
    """Risk-neutral probability that the underlying ends above ``level`` (N(d2))."""
    if level <= 0:
        return 1.0
    if years <= 0 or vol <= 0:
        return 1.0 if spot > level else 0.0
    return norm_cdf(_d1_d2(spot, level, years, vol, rate, div)[1])


def expected_move(spot: float, years: float, vol: float) -> float:
    """One standard deviation of the underlying's price by then, in dollars (about a 68% range,
    as the option market prices it)."""
    if years <= 0 or vol <= 0:
        return 0.0
    return spot * vol * math.sqrt(years)


# -- whole chains at once -------------------------------------------------------------------------

Array = np.ndarray[Any, np.dtype[np.float64]]


def _arrays(*values: Any) -> list[Array]:
    return [np.asarray(v, dtype=float) for v in np.broadcast_arrays(*values)]


def price_array(
    spot: Any, strike: Any, years: Any, vol: Any, call: Any, rate: float = 0.0, div: float = 0.0
) -> Array:
    """``price`` element by element (``call``: a boolean array or one bool)."""
    s, k, t, v = _arrays(spot, strike, years, vol)
    c = np.broadcast_to(np.asarray(call, dtype=bool), s.shape)
    live = (t > 0) & (v > 0)
    tt, vv = np.where(live, t, 1.0), np.where(live, v, 1.0)
    sd = vv * np.sqrt(tt)
    d1 = (np.log(s / k) + (rate - div + 0.5 * vv * vv) * tt) / sd
    d2 = d1 - sd
    df, qf = np.exp(-rate * tt), np.exp(-div * tt)
    calls = s * qf * special.ndtr(d1) - k * df * special.ndtr(d2)
    puts = k * df * special.ndtr(-d2) - s * qf * special.ndtr(-d1)
    value = np.where(c, calls, puts)
    # expired: intrinsic; no volatility: the discounted intrinsic value of the forward
    forward = s * np.exp((rate - div) * np.maximum(t, 0.0))
    flat = np.exp(-rate * np.maximum(t, 0.0)) * np.where(
        c, np.maximum(forward - k, 0.0), np.maximum(k - forward, 0.0)
    )
    spent = np.where(c, np.maximum(s - k, 0.0), np.maximum(k - s, 0.0))
    out: Array = np.where(live, value, np.where(t <= 0, spent, flat))
    return out


def greeks_array(
    spot: Any, strike: Any, years: Any, vol: Any, call: Any, rate: float = 0.0, div: float = 0.0
) -> dict[str, Array]:
    """``greeks`` element by element: ``price``, ``delta``, ``gamma``, ``theta`` (per calendar
    day) and ``vega`` (per volatility point). NaN where the volatility is NaN."""
    s, k, t, v = _arrays(spot, strike, years, vol)
    c = np.broadcast_to(np.asarray(call, dtype=bool), s.shape)
    live = (t > 0) & (v > 0)
    tt, vv = np.where(live, t, 1.0), np.where(live, v, 1.0)
    root = np.sqrt(tt)
    sd = vv * root
    d1 = (np.log(s / k) + (rate - div + 0.5 * vv * vv) * tt) / sd
    d2 = d1 - sd
    df, qf = np.exp(-rate * tt), np.exp(-div * tt)
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = qf * pdf / (s * sd)
    vega = s * qf * pdf * root / 100.0
    decay = -s * qf * pdf * vv / (2.0 * root)
    call_theta = decay - rate * k * df * special.ndtr(d2) + div * s * qf * special.ndtr(d1)
    put_theta = decay + rate * k * df * special.ndtr(-d2) - div * s * qf * special.ndtr(-d1)
    delta = np.where(c, qf * special.ndtr(d1), -qf * special.ndtr(-d1))
    itm = np.where(c, s > k, s < k)
    dead = np.where(itm, np.where(c, 1.0, -1.0), 0.0)
    out = {
        "price": price_array(s, k, t, v, c, rate, div),
        "delta": np.where(live, delta, dead),
        "gamma": np.where(live, gamma, 0.0),
        "theta": np.where(live, np.where(c, call_theta, put_theta) / YEAR_DAYS, 0.0),
        "vega": np.where(live, vega, 0.0),
    }
    missing = np.isnan(v)
    return {name: np.where(missing, np.nan, values) for name, values in out.items()}


def implied_vol_array(
    value: Any,
    spot: Any,
    strike: Any,
    years: Any,
    call: Any,
    rate: float = 0.0,
    div: float = 0.0,
    *,
    iterations: int = 100,
) -> Array:
    """``implied_vol`` element by element, NaN where no volatility fits. The same safeguarded
    Newton iteration, run on every element at once until all have converged."""
    p, s, k, t = _arrays(value, spot, strike, years)
    c = np.broadcast_to(np.asarray(call, dtype=bool), s.shape)
    ok = (t > 0) & (p > 0) & (s > 0) & (k > 0)
    lo = np.full(s.shape, MIN_VOL)
    hi = np.full(s.shape, MAX_VOL)
    tt = np.where(ok, t, 1.0)
    ok &= p > price_array(s, k, tt, lo, c, rate, div) + 1e-9
    ok &= p < price_array(s, k, tt, hi, c, rate, div)
    vol = np.clip(np.sqrt(2.0 * math.pi / tt) * p / np.where(s > 0, s, 1.0), 0.05, 2.0)
    done = ~ok
    with np.errstate(all="ignore"):  # far from the money, vega underflows: bisection takes over
        vol = _solve(p, s, k, tt, c, rate, div, vol, lo, hi, done, iterations)
    out: Array = np.where(ok, vol, np.nan)
    return out


def _solve(
    p: Array,
    s: Array,
    k: Array,
    tt: Array,
    c: Any,
    rate: float,
    div: float,
    vol: Array,
    lo: Array,
    hi: Array,
    done: Any,
    iterations: int,
) -> Array:
    for _ in range(iterations):
        if done.all():
            break
        diff = price_array(s, k, tt, vol, c, rate, div) - p
        settled = np.abs(diff) < 1e-10
        hi = np.where(~done & (diff > 0), vol, hi)
        lo = np.where(~done & (diff <= 0), vol, lo)
        sd = vol * np.sqrt(tt)
        d1 = (np.log(s / k) + (rate - div + 0.5 * vol * vol) * tt) / sd
        vega = s * np.exp(-div * tt) * np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * np.sqrt(tt)
        step = np.where(vega > 1e-12, vol - diff / vega, np.nan)
        inside = (step > lo) & (step < hi)
        nxt = np.where(inside, step, 0.5 * (lo + hi))
        settled |= (np.abs(nxt - vol) < 1e-10) | (hi - lo < 1e-10)
        vol = np.where(done | (np.abs(diff) < 1e-10), vol, nxt)
        done |= settled
    return vol
