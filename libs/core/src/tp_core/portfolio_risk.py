"""Portfolio-level risk: how the whole book moves, not each position alone.

Built from today's holdings and the daily bars:

- **Exposure** per underlying in dollars: shares at market value, options by delta (shares-
  equivalent x price), so a call and the stock on the same name add up.
- **Beta to the S&P 500** (one year of daily returns) and the book's volatility.
- **Value at Risk**: the one-day loss exceeded on 5% (and 1%) of days, from replaying the last
  year's daily returns on today's exposures (historical) and from the book's volatility
  (parametric, normal); and the expected shortfall, the average loss beyond the 95% VaR.
- **Correlation** between the largest exposures, the average pairwise correlation, and the most
  correlated pairs: diversification that isn't there.
- **Aggregate option Greeks** and a **scenario grid**: the book's P&L if the S&P 500 moved
  -10% to +10% (each name by its beta) with implied volatility down or up 5 points, options
  fully repriced (Black-Scholes), not just by delta.

The options part is first order in VaR (delta) and exact in the grid, at the current implied
volatility. History-based figures cover only what has daily bars (funds without bars and cash
are left out; ``coverage`` says how much that is).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from tp_core import greeks as bs
from tp_core.positions import years_until

MARKET_MOVES = (-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10)
VOL_SHIFTS = (-0.05, 0.0, 0.05)
Z = {0.95: 1.6449, 0.99: 2.3263}


@dataclass(frozen=True)
class OptionLeg:
    underlying: str
    quantity: float  # contracts (negative: short)
    strike: float
    expiration: date
    call: bool
    iv: float
    spot: float
    multiplier: float = 100.0


def betas(returns: pd.DataFrame, market: pd.Series) -> dict[str, float]:
    out = {}
    m = market.dropna()
    var = float(m.var())
    for s in returns.columns:
        joined = pd.concat([returns[s], m], axis=1, join="inner").dropna()
        if len(joined) > 20 and var > 0:
            out[str(s)] = float(joined.iloc[:, 0].cov(joined.iloc[:, 1]) / var)
    return out


def portfolio_risk(
    exposures: dict[str, float],
    closes: pd.DataFrame,
    *,
    total_value: float,
    options: list[OptionLeg] | None = None,
    now: datetime | None = None,
    market: str = "SPY",
    lookback: int = 252,
    rate: float = 0.04,
) -> dict[str, Any]:
    """``exposures``: dollars per underlying (stock value plus option delta-dollars).
    ``closes``: adjusted daily closes with a column per underlying and ``market``."""
    exposures = {s: v for s, v in exposures.items() if abs(v) > 0}
    returns = closes.sort_index().pct_change(fill_method=None).iloc[-lookback:]
    priced = {
        s: v for s, v in exposures.items() if s in returns.columns and returns[s].count() > 20
    }
    covered = sum(abs(v) for v in priced.values())
    out: dict[str, Any] = {
        "exposure": sum(exposures.values()),
        "gross_exposure": sum(abs(v) for v in exposures.values()),
        "coverage": covered / sum(abs(v) for v in exposures.values()) if exposures else None,
        "value": total_value,
    }
    if not priced or market not in returns.columns:
        return out
    r = returns[list(priced)].fillna(0.0)
    vec = np.array(list(priced.values()))
    pnl = pd.Series(r.to_numpy() @ vec, index=r.index)
    b = betas(returns[list(priced)], returns[market])
    beta_dollars = sum(v * b.get(s, 1.0) for s, v in priced.items())
    sd = float(pnl.std())
    var95 = -float(np.percentile(pnl, 5))
    tail = pnl[pnl <= -var95]
    out |= {
        "sessions": len(pnl),
        "beta": beta_dollars / total_value if total_value else None,
        "beta_dollars": beta_dollars,  # dollars the book moves per 1.00 (100%) S&P 500 move
        "volatility": sd * math.sqrt(252) / total_value if total_value else None,
        "var": {
            "historical_95": var95,
            "historical_99": -float(np.percentile(pnl, 1)),
            "parametric_95": Z[0.95] * sd,
            "parametric_99": Z[0.99] * sd,
            "expected_shortfall_95": -float(tail.mean()) if len(tail) else None,
        },
        "worst_days": [
            {"date": str(pd.Timestamp(str(d)).date()), "pnl": float(v)}
            for d, v in pnl.nsmallest(5).items()
        ],
        "betas": b,
    }
    out |= _correlation(r, priced)
    if options:
        out["greeks"] = aggregate_greeks(options, now=now, rate=rate)
    out["scenarios"] = scenario_grid(priced, b, options or [], now=now, rate=rate)
    return out


def _correlation(
    returns: pd.DataFrame, exposures: dict[str, float], top: int = 10
) -> dict[str, Any]:
    largest = sorted(exposures, key=lambda s: -abs(exposures[s]))[:top]
    if len(largest) < 2:
        return {"correlation": None}
    corr = returns[largest].corr()
    values = corr.to_numpy(dtype=float)
    pairs = []
    for i, a in enumerate(largest):
        for j in range(i + 1, len(largest)):
            v = float(values[i, j])
            if v == v:
                pairs.append((v, a, largest[j]))
    pairs.sort(reverse=True)
    avg = float(np.mean([p[0] for p in pairs])) if pairs else None
    return {
        "correlation": {
            "symbols": largest,
            "matrix": [[_r(v) for v in row] for row in values],
            "average": avg,
            "most_correlated": [{"a": a, "b": c, "correlation": v} for v, a, c in pairs[:5]],
        }
    }


def _r(value: Any) -> float | None:
    v = float(value)
    return round(v, 3) if math.isfinite(v) else None


def aggregate_greeks(
    options: list[OptionLeg], *, now: datetime | None, rate: float
) -> dict[str, float]:
    """Totals across the option book, in dollars: delta (per 1.00 move in every underlying
    together, i.e. shares-equivalent value), gamma (change in delta-dollars for a 1% move),
    theta per day, vega per volatility point."""
    when = now or datetime.now().astimezone()
    out = {"delta_dollars": 0.0, "gamma_1pct": 0.0, "theta_day": 0.0, "vega_point": 0.0}
    for o in options:
        t = years_until(o.expiration, when)
        g = bs.greeks(o.spot, o.strike, t, o.iv, call=o.call, rate=rate)
        size = o.quantity * o.multiplier
        out["delta_dollars"] += g.delta * size * o.spot
        out["gamma_1pct"] += g.gamma * size * o.spot * (0.01 * o.spot)
        out["theta_day"] += g.theta * size
        out["vega_point"] += g.vega * size
    return out


def scenario_grid(
    exposures: dict[str, float],
    beta: dict[str, float],
    options: list[OptionLeg],
    *,
    now: datetime | None,
    rate: float,
) -> dict[str, Any]:
    """P&L of the book if the S&P 500 moved ``MARKET_MOVES`` (each name by its beta) and implied
    volatility shifted ``VOL_SHIFTS``. Stocks move linearly; options are repriced. The option
    part of ``exposures`` (their delta-dollars) is taken out first so it isn't counted twice."""
    when = now or datetime.now().astimezone()
    option_delta: dict[str, float] = {}
    base: list[float] = []
    for o in options:
        t = years_until(o.expiration, when)
        g = bs.greeks(o.spot, o.strike, t, o.iv, call=o.call, rate=rate)
        option_delta[o.underlying] = (
            option_delta.get(o.underlying, 0.0) + g.delta * o.quantity * o.multiplier * o.spot
        )
        base.append(bs.price(o.spot, o.strike, t, o.iv, call=o.call, rate=rate))
    stocks = {s: v - option_delta.get(s, 0.0) for s, v in exposures.items()}
    rows = []
    for m in MARKET_MOVES:
        cells = []
        for dv in VOL_SHIFTS:
            pnl = sum(v * beta.get(s, 1.0) * m for s, v in stocks.items())
            for o, p0 in zip(options, base, strict=True):
                t = years_until(o.expiration, when)
                spot = o.spot * (1 + beta.get(o.underlying, 1.0) * m)
                p1 = bs.price(spot, o.strike, t, max(o.iv + dv, 0.01), call=o.call, rate=rate)
                pnl += (p1 - p0) * o.quantity * o.multiplier
            cells.append(pnl)
        rows.append({"move": m, "pnl": cells})
    return {"vol_shifts": list(VOL_SHIFTS), "rows": rows}
