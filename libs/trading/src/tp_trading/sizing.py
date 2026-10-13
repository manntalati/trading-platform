"""Position sizing: how much of each position a strategy's signal turns into.

A strategy says what to hold and, through its target weights, the most it wants in each. A
``Sizing`` policy then decides how much, from the trailing prices only (no look-ahead). The three
the plan asks to compare:

- **Fixed fractional**: risk a fixed share of equity per position. With a stop
  ``stop_atr`` average true ranges below the price, a position of weight ``w`` loses
  ``w * stop distance`` at the stop, so ``w = risk / stop distance``.
- **Volatility targeting**: each position contributes about the same risk (weights inverse to
  each one's volatility), and the whole book is scaled down when its estimated volatility is
  above ``target``.
- **Fractional Kelly**: the Kelly bet ``mu / sigma^2`` from each asset's trailing returns, times
  ``fraction`` (a quarter at most, because estimated edges are noisy); nothing when the trailing
  edge is negative.

Every method only reallocates the strategy's weights: no name above the strategy's largest single
weight, no more in total than the strategy's total, never leverage. ``None`` (the default) keeps
the strategy's weights as they are.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from tp_trading.strategy import Context

TRADING_DAYS = 252
Method = Literal["strategy", "fixed-fractional", "vol-target", "kelly"]


class Sizing(Protocol):
    @property
    def name(self) -> str: ...

    def resize(self, targets: Mapping[str, float], ctx: Context) -> dict[str, float]: ...


def _atr(ctx: Context, symbols: list[str], days: int) -> dict[str, float]:
    hist = days + 1
    high, low, close = (ctx.history(f, hist) for f in ("high", "low", "close"))
    out = {}
    for s in symbols:
        if s not in close.columns:
            continue
        prev = close[s].shift(1)
        tr = pd.concat([high[s] - low[s], (high[s] - prev).abs(), (low[s] - prev).abs()], axis=1)
        atr = tr.max(axis=1).iloc[1:].mean()
        if atr == atr and atr > 0:
            out[s] = float(atr)
    return out


def _returns(ctx: Context, symbols: list[str], days: int) -> pd.DataFrame:
    closes = ctx.history("close", days + 1)
    cols = [s for s in symbols if s in closes.columns]
    return closes[cols].pct_change(fill_method=None).iloc[1:]


@dataclass(frozen=True)
class FixedFractional:
    risk: float = 0.01  # of equity, lost if the stop is hit
    stop_atr: float = 2.0  # stop this many ATRs below the price
    atr_days: int = 14
    name: str = "fixed-fractional"

    def resize(self, targets: Mapping[str, float], ctx: Context) -> dict[str, float]:
        held = [s for s, w in targets.items() if w > 0]
        atr = _atr(ctx, held, self.atr_days)
        raw: dict[str, float] = {}
        for s in held:
            price = ctx.price(s)
            if s in atr and price and math.isfinite(price):
                raw[s] = self.risk / (self.stop_atr * atr[s] / price)
            else:
                raw[s] = targets[s]  # no range to size by: the strategy's weight
        return _fit(raw, targets)


@dataclass(frozen=True)
class VolatilityTarget:
    target: float = 0.12  # annualized volatility of the whole book, at most
    lookback: int = 60
    name: str = "vol-target"

    def resize(self, targets: Mapping[str, float], ctx: Context) -> dict[str, float]:
        held = [s for s, w in targets.items() if w > 0]
        if not held:
            return dict(targets)
        rets = _returns(ctx, held, self.lookback).dropna(axis=1, how="all")
        vols = rets.std() * math.sqrt(TRADING_DAYS)
        usable = [s for s in held if s in vols.index and vols[s] > 0]
        if not usable:
            return dict(targets)
        inverse = {s: 1.0 / float(vols[s]) for s in usable}
        norm = sum(inverse.values())
        total = sum(targets[s] for s in usable)
        raw = {s: total * v / norm for s, v in inverse.items()}
        raw |= {s: targets[s] for s in held if s not in raw}  # no history: as the strategy says
        weights = _fit(raw, targets)
        cov = rets[usable].cov().to_numpy() * TRADING_DAYS
        vec = np.array([weights[s] for s in usable])
        port_vol = float(np.sqrt(max(vec @ cov @ vec, 0.0)))
        scale = min(1.0, self.target / port_vol) if port_vol > 0 else 1.0
        return {s: weights[s] * scale for s in targets}


@dataclass(frozen=True)
class FractionalKelly:
    fraction: float = 0.25
    lookback: int = 252
    name: str = "kelly"

    def resize(self, targets: Mapping[str, float], ctx: Context) -> dict[str, float]:
        held = [s for s, w in targets.items() if w > 0]
        rets = _returns(ctx, held, self.lookback)
        out = {}
        for s, w in targets.items():
            if w <= 0 or s not in rets.columns or rets[s].count() < 20:
                out[s] = 0.0 if w <= 0 else w
                continue
            mu = float(rets[s].mean()) * TRADING_DAYS
            var = float(rets[s].var()) * TRADING_DAYS
            kelly = mu / var if var > 0 else 0.0
            out[s] = min(w, max(0.0, self.fraction * kelly))
        return out


def _fit(raw: Mapping[str, float], targets: Mapping[str, float]) -> dict[str, float]:
    """Cap each weight at the strategy's largest single weight, then scale the whole set down
    if it adds up to more than the strategy's total: sizing reallocates risk, it never adds
    exposure or concentration the strategy didn't ask for."""
    largest = max((w for w in targets.values() if w > 0), default=0.0)
    total = sum(w for w in targets.values() if w > 0)
    capped = {s: min(max(w, 0.0), largest) for s, w in raw.items()}
    gross = sum(capped.values())
    scale = min(1.0, total / gross) if gross > 0 else 1.0
    return {s: capped.get(s, 0.0) * scale for s in targets}


def make(method: str | None, **options: float) -> Sizing | None:
    """A sizing policy by name; ``None`` or ``"strategy"`` keeps the strategy's own weights."""
    if method in (None, "", "strategy"):
        return None
    if method == "fixed-fractional":
        return FixedFractional(**options)  # type: ignore[arg-type]
    if method == "vol-target":
        return VolatilityTarget(**options)  # type: ignore[arg-type]
    if method == "kelly":
        return FractionalKelly(**options)  # type: ignore[arg-type]
    raise ValueError(f"unknown sizing {method!r}: strategy, fixed-fractional, vol-target, kelly")


METHODS: tuple[Method, ...] = ("strategy", "fixed-fractional", "vol-target", "kelly")
