"""Triple-barrier labels (López de Prado, *Advances in Financial Machine Learning*, ch. 3).

A trade entered on a session is labelled by whichever of three barriers its price path touches
first after entry:

- the **target**: ``profit`` daily volatilities in the trade's favour;
- the **stop**: ``stop`` daily volatilities against it;
- the **vertical** barrier: ``horizon`` sessions later (time's up).

Volatility is the exponentially weighted standard deviation of daily returns known on the entry
day, so a quiet stock gets tight barriers and a wild one wide barriers. The entry is the next
session's open (signals are decided at a close, filled at the next open, as in the engine).

``label`` is 1 when the target is hit first (or, at the vertical barrier, the trade is up),
else 0; if one bar reaches both, the stop counts first. ``ret`` is the trade's return to the
barrier that ended it; ``end`` is the session it ended, which is what purged cross-validation
needs (a label is only known once its trade has ended).

With ``side`` (1 long, -1 short) these are **meta-labels**: not "which way", but "was this
trade from the primary strategy worth taking".

``rule_labels`` is the alternative for a primary strategy that has exits of its own (RSI(2)
sells on the first close above its 5-day average): it labels each signal by the trade those
exits would have made, so the model learns about the trades the strategy actually takes.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping
from typing import Any

import numpy as np
import pandas as pd


def daily_vol(closes: pd.Series | pd.DataFrame, span: int = 50) -> pd.Series | pd.DataFrame:
    """Exponentially weighted volatility of daily returns, known at each close."""
    rets = closes.pct_change(fill_method=None)
    return rets.ewm(span=span, min_periods=max(span // 2, 10)).std()


class _Panel:
    """Numpy views of wide bars, for per-event loops without pandas indexing."""

    def __init__(self, closes: pd.DataFrame, **frames: pd.DataFrame) -> None:
        self.index = pd.DatetimeIndex(closes.index)
        self.symbols = [str(c) for c in closes.columns]
        self.column = {s: j for j, s in enumerate(self.symbols)}
        self.close = closes.to_numpy(dtype=float)
        self.frames = {
            k: f.reindex(index=closes.index, columns=closes.columns).to_numpy(dtype=float)
            for k, f in frames.items()
        }

    def locate(self, ev: Mapping[Hashable, Any]) -> tuple[int, int] | None:
        """(row, column) of an event's signal session and symbol, or None."""
        j = self.column.get(str(ev["symbol"]))
        when = pd.Timestamp(ev["date"])
        i = int(self.index.searchsorted(when))
        if j is None or i >= len(self.index) or self.index[i] != when:
            return None
        return i, j


def _row(
    panel: _Panel, i: int, j: int, side: int, entry_i: int, k: int, barrier: str, ret: float,
    entry: float, vol: np.ndarray, label: int,
) -> dict[str, object]:  # fmt: skip
    return {
        "symbol": panel.symbols[j],
        "date": panel.index[i],
        "side": side,
        "start": panel.index[entry_i],
        "end": panel.index[k],
        "entry": entry,
        "barrier": barrier,
        "ret": ret,
        "label": label,
        "vol": float(vol[i, j]),
    }


COLUMNS = ["symbol", "date", "side", "start", "end", "entry", "barrier", "ret", "label", "vol"]


def triple_barrier(
    events: pd.DataFrame,
    opens: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    profit: float = 2.0,
    stop: float = 2.0,
    horizon: int = 10,
    vol_span: int = 50,
) -> pd.DataFrame:
    """Label ``events`` (columns ``symbol``, ``date``: the signal session, and optional
    ``side``, 1 or -1). Adds ``entry``, ``start`` (the entry session), ``end``, ``barrier``
    (``target``/``stop``/``vertical``), ``ret``, ``label`` and ``vol``. Events without enough
    data after them (the trade hasn't ended yet) are dropped: their label isn't known."""
    panel = _Panel(closes, open=opens, high=highs, low=lows)
    vol = daily_vol(closes, vol_span).to_numpy(dtype=float)
    op, hi_all, lo_all = panel.frames["open"], panel.frames["high"], panel.frames["low"]
    n = len(panel.index)
    rows = []
    for ev in events.to_dict("records"):
        at = panel.locate(ev)
        if at is None:
            continue
        i, j = at
        side = int(ev.get("side", 1) or 1)
        sigma = float(vol[i, j])
        entry_i = i + 1
        last_i = entry_i + horizon - 1
        if not math.isfinite(sigma) or sigma <= 0 or last_i >= n:
            continue
        entry = float(op[entry_i, j])
        if not math.isfinite(entry) or entry <= 0:
            continue
        target = entry * (1 + side * profit * sigma)
        stop_at = entry * (1 - side * stop * sigma)
        hi = hi_all[entry_i : last_i + 1, j]
        lo = lo_all[entry_i : last_i + 1, j]
        opens_j = op[entry_i : last_i + 1, j]
        hits_t = np.flatnonzero(hi >= target) if side > 0 else np.flatnonzero(lo <= target)
        hits_s = np.flatnonzero(lo <= stop_at) if side > 0 else np.flatnonzero(hi >= stop_at)
        first_t = int(hits_t[0]) if len(hits_t) and profit > 0 else None
        first_s = int(hits_s[0]) if len(hits_s) and stop > 0 else None
        if first_t is None and first_s is None:
            k, barrier = horizon - 1, "vertical"
            exit_price = float(panel.close[last_i, j])
        elif first_s is not None and (first_t is None or first_s <= first_t):
            # both on one bar: assume the stop came first (the worse case)
            k, barrier = first_s, "stop"
            # a gap through the stop fills at the open, worse than the stop
            gap = opens_j[k] if k > 0 else stop_at
            exit_price = min(gap, stop_at) if side > 0 else max(gap, stop_at)
        else:
            assert first_t is not None
            k, barrier = first_t, "target"
            gap = opens_j[k] if k > 0 else target
            exit_price = max(gap, target) if side > 0 else min(gap, target)
        ret = side * (exit_price / entry - 1)
        label = int(barrier == "target" or (barrier == "vertical" and ret > 0))
        rows.append(_row(panel, i, j, side, entry_i, entry_i + k, barrier, ret, entry, vol, label))
    return pd.DataFrame(rows, columns=COLUMNS)


def rule_labels(
    events: pd.DataFrame,
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    exit_signal: pd.DataFrame,
    *,
    max_hold: int = 10,
    vol_span: int = 50,
) -> pd.DataFrame:
    """Label ``events`` by the primary strategy's own exits: enter at the next open, sell at the
    open after the first close where ``exit_signal`` is true (the entry session's close counts),
    or after ``max_hold`` sessions. Same columns as ``triple_barrier``; ``barrier`` is ``rule``
    or ``time`` and ``label`` is 1 when the trade made money."""
    signal = exit_signal.reindex(index=closes.index, columns=closes.columns)
    panel = _Panel(closes, open=opens, signal=signal.astype(float))
    flags = panel.frames["signal"] > 0  # NaN (no signal yet) is False
    vol = daily_vol(closes, vol_span).to_numpy(dtype=float)
    op = panel.frames["open"]
    n = len(panel.index)
    rows = []
    for ev in events.to_dict("records"):
        at = panel.locate(ev)
        if at is None:
            continue
        i, j = at
        side = int(ev.get("side", 1) or 1)
        entry_i = i + 1
        if entry_i + max_hold + 1 >= n:
            continue  # the trade may not have ended yet: its label isn't known
        entry = float(op[entry_i, j])
        if not math.isfinite(entry) or entry <= 0:
            continue
        hits = np.flatnonzero(flags[entry_i : entry_i + max_hold, j])
        if len(hits):
            exit_t, barrier = entry_i + int(hits[0]), "rule"
        else:
            exit_t, barrier = entry_i + max_hold, "time"
        exit_price = float(op[exit_t + 1, j])
        if not math.isfinite(exit_price) or exit_price <= 0:
            exit_price = float(panel.close[exit_t, j])
        ret = side * (exit_price / entry - 1)
        rows.append(
            _row(panel, i, j, side, entry_i, exit_t + 1, barrier, ret, entry, vol, int(ret > 0))
        )
    return pd.DataFrame(rows, columns=COLUMNS)
