"""Regime gates from the lake: when a strategy should sit in cash (``tp_trading.regime``).

    hmm     the learned volatility regime (``tp_ml.regime``, SPY): risk off once the filtered
            probability of the stressed state reaches 0.7, back on when it falls to 0.3. Refit
            each January on the years before, so every day's value is out of sample. Needs
            five years of SPY bars.
    trend   SPY's close against its 200-day average: off 1% below it, on again 1% above it
            (the classic trend filter, Faber 2007).
    vix     the VIX close: off above 30, on again below 25 (needs ``tp-data macro``).

Each has a band (hysteresis) so that a reading hovering at the line doesn't flip the strategy
in and out of the market every few days. Every value uses data up to that day's close only.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from tp_core.bars import load_bars
from tp_core.macro import load_macro
from tp_core.storage import Lake
from tp_ml.regime import out_of_sample_probabilities
from tp_trading.regime import SeriesGate

METHODS = ("off", "hmm", "trend", "vix")
HMM_ENTER, HMM_LEAVE = 0.7, 0.3
TREND_BAND = 0.01
VIX_ENTER, VIX_LEAVE = 30.0, 25.0


def hysteresis(x: pd.Series, enter: float, leave: float) -> pd.Series:
    """True (risk off) from the day ``x`` reaches ``enter`` until it falls back to ``leave``
    (``enter > leave``). Missing values keep the previous state."""
    out, off = [], False
    for v in x.to_numpy(dtype=float):
        if v == v:  # not NaN
            if not off and v >= enter:
                off = True
            elif off and v <= leave:
                off = False
        out.append(off)
    return pd.Series(out, index=x.index, dtype=bool)


def spy_ohlc(lake: Lake, end: date | None = None) -> pd.DataFrame:
    raw = load_bars(lake, ["SPY"], end=end)
    if raw.empty:
        return raw
    bars = raw[["adj_open", "adj_high", "adj_low", "adj_close"]].astype(float)
    bars.columns = ["open", "high", "low", "close"]
    bars.index = pd.DatetimeIndex(raw["session"])
    return bars.sort_index()


def regime_series(lake: Lake, method: str, end: date | None = None) -> tuple[pd.Series, str]:
    """``(risk off per session, what the rule is)`` for ``method``."""
    if method == "hmm":
        probs = out_of_sample_probabilities(spy_ohlc(lake, end))
        if probs.empty:
            raise ValueError("the hmm gate needs five years of SPY bars: tp-data bars backfill")
        off = hysteresis(probs["stressed"], HMM_ENTER, HMM_LEAVE)
        return off, f"stressed probability {HMM_ENTER:.0%} on, {HMM_LEAVE:.0%} off"
    if method == "trend":
        bars = spy_ohlc(lake, end)
        close = bars["close"] if len(bars) else pd.Series(dtype=float)
        if len(close) < 200:
            raise ValueError("the trend gate needs 200 sessions of SPY bars")
        below = 1 - close / close.rolling(200).mean()  # > 0 under the average
        off = hysteresis(below, TREND_BAND, -TREND_BAND)
        return off, f"SPY {TREND_BAND:.0%} below its 200-day average"
    if method == "vix":
        macro = load_macro(lake, end)
        if "VIXCLS" not in macro.columns:
            raise ValueError("the vix gate needs the VIX: tp-data macro backfill")
        off = hysteresis(macro["VIXCLS"].dropna(), VIX_ENTER, VIX_LEAVE)
        return off, f"VIX above {VIX_ENTER:g} (back below {VIX_LEAVE:g})"
    raise ValueError(f"unknown regime gate {method!r}: {', '.join(METHODS)}")


def make_gate(lake: Lake, method: str | None, end: date | None = None) -> SeriesGate | None:
    """The gate for ``method`` (``None`` or "off": no gate)."""
    if method in (None, "", "off"):
        return None
    off, detail = regime_series(lake, method, end)
    return SeriesGate(method, off, detail)
