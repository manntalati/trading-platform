"""What an options position is expected to be worth when the move it bets on has had time to
happen, under our own forecast rather than the market's.

The option price says what the market expects; buying is worth it only when we expect more.
``evaluate`` values a position (one or more legs) at the setup's horizon:

- **The stock** ends the horizon lognormally distributed around today's price with the setup's
  drift (what the stock did after the same signal in the backtest; with no calibration, an
  ordinary equity drift of 7% a year, so a put starts with the wind against it) and the
  volatility forecast for the horizon (HAR-RV for the next month, blended toward the year's
  realized volatility for longer horizons), on ``NODES`` equally likely outcomes (the
  midpoints of as many quantiles), so the odds and the worst tenth are read directly.
- **Implied volatility** at the horizon comes off today's surface: each leg is valued at the
  implied volatility today of an option with the time it will have left and the moneyness it
  will have (the surface "rolls down": an option a month from expiry is priced like today's
  month options). With a history of implied volatility, its level also drifts back toward
  the stock's usual level with a half-life of ``IV_HALFLIFE`` sessions.
- **Costs**: legs bought at the ask and sold at the bid, closed across half the spread, and
  $0.65 a contract each way.

Returns are on the capital at risk: the debit paid, or for a credit the most it can lose.
Valued at the horizon, before any stop or profit target; the backtest tests those.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import special

from tp_core import greeks as bs

NODES = 400
FEE = 0.65 / 100  # per share of a contract, each way
EQUITY_DRIFT = 0.07  # a year: the drift assumed without a calibrated one
IV_HALFLIFE = 30.0  # sessions
TRADING_DAYS = 252.0
_X = special.ndtri((np.arange(NODES) + 0.5) / NODES)
_X = _X / _X.std()  # exactly unit variance, so the drift and volatility come out as given


@dataclass(frozen=True)
class Outlook:
    """What we expect of the stock over the horizon."""

    spot: float
    horizon: int  # sessions
    drift: float  # expected log return over the horizon
    vol: float  # annualized volatility expected over it
    rate: float
    iv_level: float | None = None  # the stock's usual 30-day implied volatility, when known


@dataclass(frozen=True)
class Leg:
    contract: str
    strike: float
    years: float
    call: bool
    qty: int  # +1 bought, -1 sold
    bid: float
    ask: float

    @property
    def price(self) -> float:
        return self.ask if self.qty > 0 else self.bid

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid > 0 else 1.0


class Surface:
    """Today's implied volatility by expiry and moneyness, from a priced chain: the
    at-the-money level of each expiry (interpolated in total variance) and a straight-line skew
    in log-moneyness, fitted to the out-of-the-money options (puts below spot, calls above)
    within 25% of spot, weighted by vega: the quotes that say most about volatility."""

    def __init__(self, priced: pd.DataFrame, spot: float) -> None:
        rows = []
        for _, g in priced.groupby("expiration"):
            otm = np.where(g["right"] == "C", g["strike"] >= spot, g["strike"] <= spot)
            g = g[otm & g["iv"].notna() & (g["iv"] > 0) & (g["vega"] > 0)]
            if g.empty:
                continue
            x = np.log(g["strike"].to_numpy(dtype=float) / spot)
            iv = g["iv"].to_numpy(dtype=float)
            near = np.abs(x) <= 0.25
            if near.sum() >= 3 and np.ptp(x[near]) > 0:
                weights = np.sqrt(g["vega"].to_numpy(dtype=float)[near])
                slope, level = np.polyfit(x[near], iv[near], 1, w=weights)
            else:
                slope, level = 0.0, float(iv[np.argmin(np.abs(x))])
            rows.append((float(g["years"].iloc[0]), max(float(level), 0.01), float(slope)))
        rows.sort()
        self.years = np.array([r[0] for r in rows])
        self.atm = np.array([r[1] for r in rows])
        self.slope = np.array([r[2] for r in rows])

    def __bool__(self) -> bool:
        return len(self.years) > 0

    def atm_at(self, years: Any) -> Any:
        """At-the-money implied volatility for ``years`` to expiry (flat beyond the ends)."""
        t = np.clip(np.asarray(years, dtype=float), self.years[0], self.years[-1])
        if len(self.years) == 1:
            return np.full(t.shape, self.atm[0])
        total = np.interp(t, self.years, self.atm**2 * self.years)
        return np.sqrt(np.maximum(total, 1e-8) / np.maximum(t, 1e-8))

    def iv(self, years: Any, log_moneyness: Any) -> Any:
        t = np.asarray(years, dtype=float)
        slope = np.interp(np.clip(t, self.years[0], self.years[-1]), self.years, self.slope)
        x = np.clip(np.asarray(log_moneyness, dtype=float), -0.5, 0.5)
        return np.maximum(self.atm_at(t) + slope * x, 0.03)


def terminal(outlook: Outlook) -> np.ndarray[Any, np.dtype[np.float64]]:
    """The stock at the horizon, one value per quadrature node."""
    h = outlook.horizon / TRADING_DAYS
    sd = outlook.vol * math.sqrt(h)
    out: np.ndarray[Any, np.dtype[np.float64]] = outlook.spot * np.exp(
        outlook.drift - 0.5 * sd * sd + sd * _X
    )
    return out


def leg_values(
    legs: Sequence[Leg], outlook: Outlook, surface: Surface
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Mid value of each leg at the horizon per node: shape (legs, nodes)."""
    spots = terminal(outlook)
    h = outlook.horizon / TRADING_DAYS
    rows = []
    for leg in legs:
        left = max(leg.years - h, 0.0)
        if left <= 0:
            rows.append(
                np.where(
                    leg.call,
                    np.maximum(spots - leg.strike, 0.0),
                    np.maximum(leg.strike - spots, 0.0),
                )
            )
            continue
        iv = surface.iv(np.full(spots.shape, left), np.log(leg.strike / spots))
        if outlook.iv_level is not None:
            now_atm = float(surface.atm_at(left))
            iv = iv + (outlook.iv_level - now_atm) * (1 - 0.5 ** (outlook.horizon / IV_HALFLIFE))
        iv = np.maximum(iv, 0.03)
        rows.append(bs.price_array(spots, leg.strike, left, iv, leg.call, outlook.rate))
    return np.array(rows)


def evaluate(
    legs: Sequence[Leg], outlook: Outlook, surface: Surface, *, capital: float | None = None
) -> dict[str, Any]:
    """Expected return and its spread for a position, per share. ``capital``: what is at risk
    (the default: the debit; give it for a credit)."""
    entry = sum(leg.qty * leg.price for leg in legs) + FEE * len(legs)
    values = leg_values(legs, outlook, surface)
    exit_value = np.zeros(values.shape[1])
    for leg, v in zip(legs, values, strict=True):
        half = leg.spread_pct / 2
        exit_value += leg.qty * v * (1 - half if leg.qty > 0 else 1 + half)
    exit_value -= FEE * len(legs)
    pnl = exit_value - entry
    at_risk = capital if capital is not None else max(entry, 1e-9)
    ret = pnl / at_risk
    mean = float(ret.mean())
    sd = float(ret.std())
    return {
        "expected_return": mean,
        "sd": sd,
        "sharpe": mean / sd if sd > 0 else 0.0,
        "prob_profit": float((pnl > 0).mean()),
        "q10": float(np.quantile(ret, 0.10)),
        "entry": float(entry),  # per share: + paid, - received
        "expected_exit": float(exit_value.mean()),
        "capital": float(at_risk),
        "horizon": outlook.horizon,
        "drift": outlook.drift,
        "vol": outlook.vol,
    }


def breakeven_move(theta_day: float, gamma: float, spot: float) -> float | None:
    """The daily move (a share of spot) whose gamma gain pays a day's time decay: a long option
    earns its theta back only when the stock moves more than this most days. Theta is per
    calendar day; a trading day carries 365/252 of them (weekends decay too)."""
    if gamma <= 0 or theta_day >= 0 or spot <= 0:
        return None
    move = math.sqrt(2 * -theta_day * (365 / TRADING_DAYS) / gamma)
    return move / spot


def fair_value(leg: Leg, spot: float, vol: float, rate: float) -> float:
    """Black-Scholes value at the forecast volatility instead of the implied: what the option is
    worth if the stock moves as forecast (no drift)."""
    return bs.price(spot, leg.strike, leg.years, vol, call=leg.call, rate=rate)


def horizon_vol(
    forecast: float | None, short: float | None, year: float | None, horizon: int
) -> float | None:
    """The volatility to expect over ``horizon`` sessions: the next month's forecast (else the
    last 20 days'), blended toward the year's realized volatility for longer horizons."""
    near = forecast or short
    if near is None:
        return year
    if year is None or horizon <= 21:
        return near
    w = 21 / horizon
    return math.sqrt(w * near * near + (1 - w) * year * year)


def default_drift(horizon: int) -> float:
    return EQUITY_DRIFT * horizon / TRADING_DAYS
