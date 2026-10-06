"""Strategy 6: leveraged ETF momentum rotation, the deliberately high-risk sleeve.

Spec: docs/strategies/06-leveraged-momentum.md. After every close, rank a handful of 3x leveraged
ETFs by their short-term (10-session) return. Hold the strongest ``top_n`` that are rising and
above their 20-session average, at equal weight, and rebalance back to those weights every day;
what doesn't qualify sits in cash.

High risk by design: daily-reset 3x funds (a 10% index fall is roughly 30% here, worse in choppy
markets), at most two positions, short signals that whipsaw, and no stops. It exists to see how
an aggressive daily strategy behaves next to the others, on paper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_strategies.library.signals import pct, trailing_return
from tp_trading.strategy import Context, Strategy

# 3x daily leveraged funds: Nasdaq-100, S&P 500, semiconductors, Russell 2000, financials, and
# 20+ year Treasuries (the one that can rise when stocks fall).
LEVERAGED = ("TQQQ", "UPRO", "SOXL", "TNA", "FAS", "TMF")


def leveraged_momentum_targets(
    closes: pd.DataFrame, assets: tuple[str, ...], lookback: int, trend: int, top_n: int
) -> tuple[dict[str, float], dict[str, str]]:
    """Target weights and the reason for each, from history ending at the signal close."""
    prices = closes[list(assets)]
    momentum = trailing_return(prices, lookback)
    window = prices.iloc[-trend:]
    average = window.mean().where(window.notna().all() & (len(window) == trend))
    last = prices.iloc[-1]
    reasons: dict[str, str] = {}
    eligible: list[str] = []
    for a in assets:
        ret, close, avg = float(momentum[a]), float(last[a]), float(average[a])
        if ret != ret or avg != avg or close != close:
            reasons[a] = f"{a}: not enough history for the {lookback}-day signal"
            continue
        label = (
            f"{a} {lookback}-day return {pct(ret)}, close {close:.2f} "
            f"{'above' if close > avg else 'not above'} its {trend}-day average {avg:.2f}"
        )
        reasons[a] = label
        if ret > 0 and close > avg:
            eligible.append(a)
        else:
            reasons[a] = f"{label}: out"
    eligible.sort(key=lambda a: float(momentum[a]), reverse=True)
    weight = 1.0 / top_n
    targets = dict.fromkeys(assets, 0.0)
    for rank, a in enumerate(eligible, start=1):
        if rank <= top_n:
            targets[a] = weight
            reasons[a] = f"{reasons[a]}: rank {rank}, held at {weight:.0%}"
        else:
            reasons[a] = f"{reasons[a]}: rank {rank}, outside the top {top_n}"
    return targets, reasons


@dataclass(frozen=True)
class LeveragedMomentum(Strategy):
    name: ClassVar[str] = "leveraged-momentum"
    title: ClassVar[str] = "Leveraged ETF momentum rotation (daily, high risk)"
    spec: ClassVar[str] = "docs/strategies/06-leveraged-momentum.md"

    assets: tuple[str, ...] = LEVERAGED
    lookback: int = 10  # sessions of momentum
    trend: int = 20  # sessions in the moving average a holding must be above
    top_n: int = 2
    min_change: float = 0.005  # rebalance daily, skipping trims under 0.5% of the sleeve

    def symbols(self) -> list[str]:
        return list(self.assets)

    def on_bar(self, ctx: Context) -> None:
        need = max(self.lookback, self.trend) + 1
        closes = ctx.history("close", lookback=need)
        if len(closes) < need:
            return
        targets, reasons = leveraged_momentum_targets(
            closes, self.assets, self.lookback, self.trend, self.top_n
        )
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)
