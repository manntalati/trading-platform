"""Market regime: a label for each day, so strategies can be judged by the conditions they ran in.

From daily closes only, each label using data up to that day:

- **Trend**: the S&P 500 (SPY) above or below its 200-day average.
- **Volatility**: the VIX when it has been downloaded from FRED (``tp-data macro``): calm under
  15, stressed over 25. Without it, SPY's 20-day realized volatility, annualized: calm under
  12%, stressed over 25% (realized volatility runs a few points below the VIX).
- **Breadth**: the share of the universe above its own 50-day average: broad at 60%+, narrow at
  40% or less.
- **Rates** and **curve** (with the FRED data only): the 10-year Treasury yield rising, steady or
  falling against its 200-day average, and the 10-year minus 3-month curve normal, flat or
  inverted (``tp_core.macro``).

A strategy's return on a day is attributed to the regime known at the previous close, so the
breakdown never uses information the strategy couldn't have had.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from tp_core import metrics
from tp_core.macro import macro_regimes

CALM, STRESSED = 0.12, 0.25
BROAD, NARROW = 0.60, 0.40
DIMENSIONS = ("trend", "volatility", "breadth", "rates", "curve")
MACRO_NUMBERS = ("vix", "vix_ratio", "rate_10y", "rate_3m", "curve_10y_3m")


def market_regimes(
    closes: pd.DataFrame, benchmark: str = "SPY", macro: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Per session: ``trend`` (up/down), ``volatility`` (calm/normal/stressed), ``breadth``
    (broad/mixed/narrow), ``rates`` (rising/steady/falling) and ``curve`` (normal/flat/inverted),
    and the numbers behind them. ``macro`` is ``tp_core.macro.load_macro``'s table; without it
    volatility comes from realized volatility and rates and curve are missing. Labels are missing
    until there's enough history (200 sessions for the trend)."""
    closes = closes.sort_index()
    spy = closes[benchmark].astype(float)
    ma200 = spy.rolling(200, min_periods=200).mean()
    vs_ma = spy / ma200 - 1.0
    vol = pd.Series(np.log(spy / spy.shift(1)), index=spy.index).rolling(20, min_periods=20).std()
    vol = vol * math.sqrt(252)
    others = closes.drop(columns=[benchmark])
    ma50 = others.rolling(50, min_periods=50).mean()
    above = (others > ma50).where(ma50.notna())
    breadth = above.sum(axis=1) / above.notna().sum(axis=1).replace(0, np.nan)
    out = pd.DataFrame(
        {
            "trend": [_trend(v) for v in vs_ma],
            "volatility": [_volatility(v) for v in vol],
            "breadth": [_breadth(v) for v in breadth],
            "spy_vs_ma200": vs_ma,
            "realized_vol": vol,
            "breadth_pct": breadth,
        },
        index=closes.index,
    )
    m = macro_regimes(macro, closes.index) if macro is not None and not macro.empty else None
    if m is not None:
        vix_label = m["vix_volatility"].where(m["vix_volatility"].notna(), out["volatility"])
        out["volatility"] = vix_label
        out["volatility_source"] = np.where(m["vix"].notna(), "vix", "realized")
    for k in ("rates", "curve", *MACRO_NUMBERS):
        out[k] = m[k] if m is not None else None
    return out


def _trend(v: float) -> str | None:
    return None if v != v else ("up" if v > 0 else "down")


def _volatility(v: float) -> str | None:
    if v != v:
        return None
    return "calm" if v < CALM else "stressed" if v > STRESSED else "normal"


def _breadth(v: float) -> str | None:
    if v != v:
        return None
    return "broad" if v >= BROAD else "narrow" if v <= NARROW else "mixed"


def describe(row: pd.Series) -> str:
    """Plain words for one day's labels."""
    labels = {k: row.get(k) for k in DIMENSIONS}
    parts = []
    if isinstance(labels["trend"], str):
        parts.append(f"S&P 500 in an {labels['trend']}trend".replace("an down", "a down"))
    if isinstance(labels["volatility"], str):
        parts.append(f"{labels['volatility']} volatility")
    breadth = {
        "broad": "most stocks rising",
        "mixed": "mixed breadth",
        "narrow": "few stocks rising",
    }
    if isinstance(labels["breadth"], str):
        parts.append(breadth[labels["breadth"]])
    if isinstance(labels["rates"], str):
        parts.append(f"rates {labels['rates']}")
    if labels["curve"] == "inverted":
        parts.append("yield curve inverted")
    return ", ".join(parts)


def current(regimes: pd.DataFrame) -> dict[str, Any]:
    """The latest day's labels and numbers."""
    if regimes.empty:
        return {}
    row = regimes.iloc[-1]
    return {
        "date": pd.Timestamp(regimes.index[-1]).date().isoformat(),
        **{k: row[k] if isinstance(row[k], str) else None for k in DIMENSIONS},
        **{k: _num(row.get(k)) for k in ("spy_vs_ma200", "realized_vol", "breadth_pct")},
        **{k: _num(row.get(k)) for k in MACRO_NUMBERS},
        "volatility_source": row.get("volatility_source") or "realized",
        "summary": describe(row),
    }


def performance_by_regime(
    returns: pd.Series, regimes: pd.DataFrame, benchmark: pd.Series | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Per dimension and label: days, the strategy's annualized return, volatility and Sharpe,
    and the benchmark's annualized return over the same days."""
    known = regimes[list(DIMENSIONS)].shift(1).reindex(returns.index)
    out: dict[str, list[dict[str, Any]]] = {}
    for dim in DIMENSIONS:
        rows = []
        for label, days in known.groupby(dim).groups.items():
            r = returns.loc[days].dropna()
            if len(r) < 5:
                continue
            row: dict[str, Any] = {
                "label": label,
                "days": len(r),
                "share": len(r) / len(returns.dropna()),
                "ann_return": float(r.mean() * metrics.TRADING_DAYS),
                "ann_volatility": metrics.annualized_volatility(r),
                "sharpe": metrics.sharpe_ratio(r),
            }
            if benchmark is not None:
                b = benchmark.reindex(r.index).dropna()
                row["benchmark_ann_return"] = (
                    float(b.mean() * metrics.TRADING_DAYS) if len(b) else None
                )
            rows.append(row)
        if rows:  # rates and curve only with the FRED data
            out[dim] = rows
    return out


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
