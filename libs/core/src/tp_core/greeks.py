"""Black-Scholes option maths: price, implied volatility, greeks and probabilities.

US equity options are American, but for calls on stocks that pay no dividend before expiry,
and for any contract a few weeks from expiry, the European formula is within cents of the real
value; good enough to read a position's risk, not to make a market. Time is calendar years
(days / 365) and theta is per calendar day.

The probabilities are *risk-neutral*: what the option's price implies, not a forecast. They are
how the market prices the odds, which is the right yardstick for "is holding worth it": an
option is fairly priced exactly when its expected payoff under these odds equals its price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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
    """The volatility at which the model price equals ``value`` (bisection: slow but it never
    diverges). None when no volatility fits: a price at or below intrinsic value, or above the
    most an option can be worth."""
    if years <= 0 or value <= 0 or spot <= 0 or strike <= 0:
        return None
    lo, hi = MIN_VOL, MAX_VOL
    if value <= price(spot, strike, years, lo, call=call, rate=rate, div=div) + 1e-9:
        return None
    if value >= price(spot, strike, years, hi, call=call, rate=rate, div=div):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if price(spot, strike, years, mid, call=call, rate=rate, div=div) < value:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


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
