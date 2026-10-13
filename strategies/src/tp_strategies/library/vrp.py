"""Strategy 10: the volatility risk premium: buy-write (BXM-like) and put-write (PUT-like).

Spec: docs/strategies/10-vrp.md. A vectorized research backtest on SPY with option prices from
Black-Scholes at the VIX (``tp_core.macro``): each month on the expiration day (the third Friday,
or the session before), the expiring option settles at its intrinsic value and a new one-month
option is sold at the close; in between it is marked daily at that day's VIX.

- **Buy-write**: long SPY (total return, from the adjusted closes), short a call at
  ``moneyness`` above the price.
- **Put-write**: T-bills (the 3-month yield) as collateral for the strike, short a put at
  ``moneyness`` below the price.

At-the-money volatility is the VIX times ``iv_scale`` (the VIX is a variance-swap rate, which
includes the skew, so it sits above at-the-money volatility); strikes away from the money get a
linear skew in log-moneyness (``skew_slope``: puts below the money dearer, calls above cheaper).
Selling pays ``cost`` of the premium (the bid/ask spread).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from tp_core import greeks as bs
from tp_core.calendar import sessions


@dataclass(frozen=True)
class VrpConfig:
    moneyness: float = 0.0  # 0: at the money; 0.02: 2% out of the money
    iv_scale: float = 0.9
    skew_slope: float = 1.5  # relative IV change per unit of -ln(strike / spot)
    cost: float = 0.03  # of the premium, per sale
    dividend_yield: float = 0.013  # for pricing SPY options


def expiration_days(start: date, end: date) -> list[date]:
    """Monthly expiration sessions between ``start`` and ``end``: the third Friday, or the
    session before it when the exchange is closed."""
    out = []
    month = date(start.year, start.month, 1)
    while month <= end:
        first_friday = month + timedelta(days=(4 - month.weekday()) % 7)
        third = first_friday + timedelta(days=14)
        open_days = sessions(third - timedelta(days=6), third)
        if open_days and start <= open_days[-1] <= end:
            out.append(open_days[-1])
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return out


def _iv(vix: float, spot: float, strike: float, cfg: VrpConfig) -> float:
    atm = vix / 100.0 * cfg.iv_scale
    return max(atm * (1.0 - cfg.skew_slope * math.log(strike / spot)), 0.03)


def premium_returns(
    adj_close: pd.Series,
    close: pd.Series,
    vix: pd.Series,
    rate: pd.Series | None = None,
    *,
    kind: str = "buy-write",
    cfg: VrpConfig | None = None,
) -> pd.DataFrame:
    """Daily ``nav`` and ``returns`` of the buy-write (``kind="buy-write"``) or the put-write
    (``"put-write"``), with the premium sold on each roll day (as a share of the price) and the
    option's value per share. ``adj_close`` and ``close`` are SPY's adjusted and raw closes;
    ``vix`` in points and ``rate`` (3-month yield) in percent, carried forward over gaps.

    Buy-write: ``shares`` of SPY, short one call per share, ``cash`` (the premium, earning the
    T-bill rate); the call is valued in adjusted terms (times adjusted over raw close). At each
    roll the whole value goes back into SPY, as in the BXM.
    Put-write: ``cash`` (the collateral, earning the T-bill rate), short ``puts`` with
    puts x strike = the NAV at each roll."""
    cfg = cfg or VrpConfig()
    if kind not in ("buy-write", "put-write"):
        raise ValueError(f"kind must be buy-write or put-write, not {kind!r}")
    call = kind == "buy-write"
    idx = pd.DatetimeIndex(adj_close.dropna().index)
    adj = adj_close.reindex(idx).astype(float).to_numpy()
    raw = close.reindex(idx).astype(float).to_numpy()
    vol = _carry(vix, idx)
    rf = (_carry(rate, idx) / 100.0) if rate is not None else np.full(len(idx), 0.04)
    rf = np.where(np.isfinite(rf), rf, 0.04)
    days = [d.date() for d in idx]
    rolls = expiration_days(days[0], days[-1])
    roll_set = set(rolls)

    def price(i: int, k: float, expiry: date) -> float:
        years = max((expiry - days[i]).days, 0) / 365.0
        if years <= 0 or not math.isfinite(vol[i]):
            return bs.intrinsic(raw[i], k, call)
        return bs.price(raw[i], k, years, _iv(vol[i], raw[i], k, cfg), call=call,
                        rate=float(rf[i]), div=cfg.dividend_yield)  # fmt: skip

    nav = np.full(len(idx), np.nan)
    sold = np.zeros(len(idx))
    marks = np.zeros(len(idx))
    shares = cash = units = 0.0
    strike: float | None = None
    expiry: date | None = None
    for i, day in enumerate(days):
        if strike is not None and expiry is not None:
            cash *= 1 + rf[i - 1] / 252
            marks[i] = price(i, strike, expiry)
        conv = adj[i] / raw[i]
        if strike is not None:
            if call:
                nav[i] = shares * adj[i] - shares * marks[i] * conv + cash
            else:
                nav[i] = cash - units * marks[i]
        if (strike is None or day in roll_set) and math.isfinite(vol[i]):
            later = [d for d in rolls if d > day]
            new_expiry = later[0] if later else day + timedelta(days=30)
            value = nav[i] if strike is not None else 1.0
            if call:
                # the old call settles (its value is already in the NAV); everything goes back
                # into the index, as the BXM does, and one call is sold per share
                shares = value / adj[i]
                k = raw[i] * (1 + cfg.moneyness)
                sale = price(i, k, new_expiry)
                cash = shares * sale * conv * (1 - cfg.cost)
                nav[i] = shares * adj[i] - shares * sale * conv + cash
            else:
                cash = value  # the old put settles: the NAV is all collateral again
                k = raw[i] * (1 - cfg.moneyness)
                sale = price(i, k, new_expiry)
                units = cash / k
                cash += units * sale * (1 - cfg.cost)
                nav[i] = cash - units * sale
            strike, expiry = k, new_expiry
            sold[i] = sale / raw[i]
            marks[i] = sale
    out = pd.DataFrame({"nav": nav, "premium": sold, "option": marks}, index=idx)
    out = out.dropna(subset=["nav"])
    out["returns"] = out["nav"].pct_change().fillna(0.0)
    return out


def _carry(series: pd.Series, idx: pd.DatetimeIndex) -> np.ndarray:
    s = series.copy()
    s.index = pd.DatetimeIndex(s.index)
    both = s.reindex(idx.union(s.index)).sort_index().ffill()
    return both.reindex(idx).to_numpy(dtype=float)
