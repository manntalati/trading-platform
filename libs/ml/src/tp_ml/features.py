"""Point-in-time features: one row per (session, symbol), each from data up to that close.

The plan's feature families, from what the lake has:

- **Returns** over 1, 5, 21, 63, 126 and 252 sessions (log), and against the market and the
  sector (the stock's 63-day return less SPY's, less its sector's median).
- **Volatility and range**: 21- and 63-day realized volatility, their ratio, the 14-day average
  true range and today's range as a share of the price.
- **Volume**: today's volume against its 20-day average (z-score), and log dollar volume.
- **Trend and oscillators**: distance from the 20-, 50- and 200-day averages; RSI(2) and RSI(14).
- **Macro** (with ``tp-data macro``): VIX, VIX over its 3-month version, the 10-year minus
  3-month curve and the 10-year's 63-day change; the same for every symbol on a day.
- **Earnings** (with ``tp-data fundamentals``): sessions since the latest results reached the
  data, and their standardized surprise (SUE).

``FEATURES`` describes each column; ``feature_spec`` hashes the list and the parameters so a
stored feature set can say exactly what it contains.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from collections.abc import Mapping

import numpy as np
import pandas as pd

from tp_core.indicators import wilder_rsi

TRADING_DAYS = 252
RETURN_WINDOWS = (1, 5, 21, 63, 126, 252)
LOOKBACK = 260  # sessions of bars the latest features need (the 252-day return)

FEATURES: dict[str, str] = {
    **{f"ret_{n}": f"log return over the last {n} sessions" for n in RETURN_WINDOWS},
    "rel_market_63": "63-day return less the market's (SPY)",
    "rel_sector_63": "63-day return less its sector's median",
    "vol_21": "21-day realized volatility, annualized",
    "vol_63": "63-day realized volatility, annualized",
    "vol_ratio": "vol_21 over vol_63: volatility rising (>1) or falling",
    "atr_pct": "14-day average true range over the close",
    "range_pct": "today's high minus low over the close",
    "volume_z": "today's volume against its 20-day mean, in standard deviations",
    "dollar_volume": "log of the 20-day average dollar volume",
    "dist_ma20": "close over its 20-day average, less 1",
    "dist_ma50": "close over its 50-day average, less 1",
    "dist_ma200": "close over its 200-day average, less 1",
    "rsi_2": "2-period RSI (Wilder)",
    "rsi_14": "14-period RSI (Wilder)",
    "vix": "VIX close",
    "vix_ratio": "VIX over the 3-month VIX (above 1: an inverted volatility curve)",
    "curve_10y_3m": "10-year less 3-month Treasury yield, percentage points",
    "rate_10y_chg_63": "change in the 10-year yield over 63 sessions, percentage points",
    "days_since_results": "sessions since the latest quarterly results reached the data",
    "last_sue": "standardized unexpected earnings of the latest results",
}
MACRO = ("vix", "vix_ratio", "curve_10y_3m", "rate_10y_chg_63")
EARNINGS = ("days_since_results", "last_sue")


def feature_spec(**params: object) -> str:
    """A short hash of the feature definitions and parameters: changes when either does."""
    payload = json.dumps({"features": FEATURES, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _stack(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    # wide (sessions x symbols) to long (session, symbol): melt would lose the index
    parts = {name: f.stack(future_stack=True) for name, f in frames.items()}  # noqa: PD013
    out = pd.DataFrame(parts)
    out.index.names = ["date", "symbol"]
    return out


def _rolling(frame: pd.DataFrame, n: int, min_periods: int) -> tuple[np.ndarray, ...]:
    """Windowed count, sum and sum of squares of every column at once (NaN skipped)."""
    values = frame.to_numpy(dtype=float)
    ok = np.isfinite(values)
    zero = np.where(ok, values, 0.0)
    pad = np.zeros((1, values.shape[1]))

    def window(a: np.ndarray) -> np.ndarray:
        c = np.concatenate([pad, np.cumsum(a, axis=0)])
        return c[n:] - c[:-n] if len(a) >= n else np.zeros((0, a.shape[1]))

    def full(a: np.ndarray) -> np.ndarray:
        # the first n - 1 rows have partial windows: cumulative sums from the start
        head = np.cumsum(a[: n - 1], axis=0)
        return np.concatenate([head, window(a)])[: len(a)]

    count, total, squares = full(ok.astype(float)), full(zero), full(zero * zero)
    enough = count >= min_periods
    return count, total, squares, enough


def rolling_mean(frame: pd.DataFrame, n: int, min_periods: int) -> pd.DataFrame:
    """``frame.rolling(n, min_periods).mean()``, computed on all columns at once."""
    count, total, _, enough = _rolling(frame, n, min_periods)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(enough, total / count, np.nan)
    return pd.DataFrame(mean, index=frame.index, columns=frame.columns)


def rolling_std(frame: pd.DataFrame, n: int, min_periods: int) -> pd.DataFrame:
    """``frame.rolling(n, min_periods).std()`` (sample, ddof 1), on all columns at once."""
    count, total, squares, enough = _rolling(frame, n, min_periods)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (squares - total * total / count) / (count - 1)
        std = np.sqrt(np.clip(var, 0.0, None))
    std = np.where(enough & (count > 1), std, np.nan)
    return pd.DataFrame(std, index=frame.index, columns=frame.columns)


def price_features(
    bars: Mapping[str, pd.DataFrame],
    *,
    market: str = "SPY",
    sectors: Mapping[str, str] | None = None,
) -> dict[str, pd.DataFrame]:
    """The features computed from bars alone, each a wide frame (sessions x symbols)."""
    close = bars["close"].sort_index()
    high, low = bars["high"].reindex_like(close), bars["low"].reindex_like(close)
    volume = bars["volume"].reindex_like(close)
    logp = pd.DataFrame(np.log(close), index=close.index, columns=close.columns)
    daily = logp.diff()
    f: dict[str, pd.DataFrame] = {}
    for n in RETURN_WINDOWS:
        f[f"ret_{n}"] = logp - logp.shift(n)
    if market in close.columns:
        f["rel_market_63"] = f["ret_63"].sub(f["ret_63"][market], axis=0)
    else:
        f["rel_market_63"] = f["ret_63"] * np.nan
    ret63 = f["ret_63"].to_numpy(dtype=float)
    rel = np.full(ret63.shape, np.nan)
    groups: dict[str, list[int]] = {}
    for j, symbol in enumerate(close.columns):
        if sectors and sectors.get(str(symbol)):
            groups.setdefault(sectors[str(symbol)], []).append(j)
    for members in groups.values():
        block = ret63[:, members]
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows: NaN median
            median = np.nanmedian(block, axis=1, keepdims=True)
        rel[:, members] = block - median
    f["rel_sector_63"] = pd.DataFrame(rel, index=close.index, columns=close.columns)
    f["vol_21"] = rolling_std(daily, 21, 15) * math.sqrt(TRADING_DAYS)
    f["vol_63"] = rolling_std(daily, 63, 45) * math.sqrt(TRADING_DAYS)
    f["vol_ratio"] = f["vol_21"] / f["vol_63"]
    prev = close.shift(1)
    ranges = np.stack([(high - low).to_numpy(), (high - prev).abs().to_numpy(),
                       (low - prev).abs().to_numpy()])  # fmt: skip
    with np.errstate(invalid="ignore"):
        widest = np.where(np.isnan(ranges).all(axis=0), np.nan, np.nanmax(ranges, axis=0))
    true_range = pd.DataFrame(widest, index=close.index, columns=close.columns)
    f["atr_pct"] = rolling_mean(true_range, 14, 10) / close
    f["range_pct"] = (high - low) / close
    vmean, vstd = rolling_mean(volume, 20, 10), rolling_std(volume, 20, 10)
    f["volume_z"] = (volume - vmean) / vstd.replace(0, np.nan)
    dollar = rolling_mean(close * volume, 20, 10)
    f["dollar_volume"] = pd.DataFrame(np.log(dollar), index=close.index, columns=close.columns)
    for n in (20, 50, 200):
        f[f"dist_ma{n}"] = close / rolling_mean(close, n, n) - 1
    f["rsi_2"] = wilder_rsi(close, 2)
    f["rsi_14"] = wilder_rsi(close, 14)
    return f


def latest_price_features(
    bars: Mapping[str, pd.DataFrame],
    *,
    market: str = "SPY",
    sectors: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """The bar features of the last session only, one row per symbol: what a strategy scores
    each day (``LOOKBACK`` sessions of bars are enough). Macro and earnings columns are NaN."""
    wide = price_features(bars, market=market, sectors=sectors)
    out = pd.DataFrame({name: frame.iloc[-1] for name, frame in wide.items()})
    out.index = out.index.map(str)
    for name in (*MACRO, *EARNINGS):
        out[name] = np.nan
    return out[list(FEATURES)]


def compute_features(
    bars: Mapping[str, pd.DataFrame],
    *,
    market: str = "SPY",
    sectors: Mapping[str, str] | None = None,
    macro: pd.DataFrame | None = None,
    earnings: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """``bars``: wide adjusted ``open``, ``high``, ``low``, ``close``, ``volume`` (sessions x
    symbols). ``macro``: ``tp_core.macro.load_macro``'s table. ``earnings``: the reference
    ``earnings`` table (``symbol``, ``session``, ``sue``). Returns the panel indexed by
    ``(date, symbol)`` with a column per ``FEATURES`` entry (missing data is NaN)."""
    close = bars["close"].sort_index()
    panel = _stack(price_features(bars, market=market, sectors=sectors))

    days = pd.DatetimeIndex(close.index)
    if macro is not None and not macro.empty:
        m = macro.sort_index()
        m = m.reindex(days.union(pd.DatetimeIndex(m.index))).ffill(limit=5).reindex(days)

        def col(name: str) -> pd.Series:
            return m[name] if name in m.columns else pd.Series(np.nan, index=days)

        by_day = pd.DataFrame(
            {
                "vix": col("VIXCLS"),
                "vix_ratio": col("VIXCLS") / col("VXVCLS"),
                "curve_10y_3m": col("DGS10") - col("DGS3MO"),
                "rate_10y_chg_63": col("DGS10") - col("DGS10").shift(63),
            },
            index=days,
        )
        panel = panel.join(by_day, on="date")
    else:
        for name in MACRO:
            panel[name] = np.nan

    if earnings is not None and not earnings.empty:
        panel = panel.join(_earnings_features(earnings, days, list(close.columns)))
    else:
        for name in EARNINGS:
            panel[name] = np.nan
    return panel[list(FEATURES)]


def _earnings_features(
    earnings: pd.DataFrame, days: pd.DatetimeIndex, symbols: list[str]
) -> pd.DataFrame:
    """Per (date, symbol): sessions since the latest results reached the data, and their SUE."""
    since = pd.DataFrame(np.nan, index=days, columns=symbols)
    sue = pd.DataFrame(np.nan, index=days, columns=symbols)
    position = pd.Series(np.arange(len(days)), index=days)
    for symbol, g in earnings.dropna(subset=["session"]).groupby("symbol"):
        s = str(symbol)
        if s not in since.columns:
            continue
        g = g.sort_values("session")
        at = pd.DatetimeIndex(pd.to_datetime(g["session"]))
        loc = days.searchsorted(at)
        mark = pd.Series(np.nan, index=days)
        value_at: dict[float, float] = {}  # the latest result's SUE, even when it is NaN
        for i, v in zip(loc.tolist(), g["sue"].to_numpy(dtype=float).tolist(), strict=True):
            if i < len(days):
                mark.iloc[i] = float(i)
                value_at[float(i)] = v
        mark = mark.ffill()
        since[s] = position - mark
        sue[s] = mark.map(value_at)
    out = _stack({"days_since_results": since, "last_sue": sue})
    return out
