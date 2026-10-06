"""Strategy 2: time-series momentum (Moskowitz, Ooi & Pedersen 2012), long/flat ETF version.

Spec: docs/strategies/02-ts-momentum.md. On the last session of each month, each asset whose
trailing 12-month return beats T-bills (BIL) over the same period is held; the rest of its slice
sits in cash. Slices are inverse-volatility weights across the whole universe, so each holding
carries a similar share of risk and the strategy is fully invested only when every asset trends
up. No shorts and no leverage, unlike the paper's futures version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_strategies.library.signals import annualized_volatility, pct, trailing_return
from tp_trading.strategy import Context, Strategy

DIVERSIFIED = ("SPY", "EFA", "EEM", "IEF", "TLT", "HYG", "VNQ", "DBC", "GLD")


def ts_momentum_targets(
    closes: pd.DataFrame,
    assets: tuple[str, ...],
    cash: str | None,
    lookback: int,
    vol_lookback: int,
) -> tuple[dict[str, float], dict[str, str]]:
    """Target weights and the reason for each, from history ending at the signal close."""
    momentum = trailing_return(closes, lookback)
    hurdle = float(momentum.get(cash, float("nan"))) if cash else 0.0
    hurdle_name = f"T-bills ({cash})" if cash else "zero"
    if hurdle != hurdle:  # cash proxy has no history yet: fall back to a zero hurdle
        hurdle, hurdle_name = 0.0, "zero"
    vol = annualized_volatility(closes[list(assets)], vol_lookback)
    usable = [a for a in assets if momentum[a] == momentum[a] and vol[a] == vol[a] and vol[a] > 0]
    inverse = pd.Series({a: 1.0 / vol[a] for a in usable})
    slices = inverse / inverse.sum() if len(inverse) else inverse
    targets: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for a in assets:
        if a not in usable:
            targets[a] = 0.0
            reasons[a] = f"{a}: not enough history for a 12-month signal"
            continue
        trending = bool(momentum[a] > hurdle)
        targets[a] = float(slices[a]) if trending else 0.0
        reasons[a] = f"{a} 12-month return {pct(momentum[a])} vs {hurdle_name} {pct(hurdle)}: " + (
            f"long, {targets[a]:.1%} slice at {vol[a]:.0%} volatility" if trending else "flat"
        )
    return targets, reasons


@dataclass(frozen=True)
class TimeSeriesMomentum(Strategy):
    name: ClassVar[str] = "ts-momentum"
    title: ClassVar[str] = "Time-series momentum (12-month, volatility-weighted)"
    spec: ClassVar[str] = "docs/strategies/02-ts-momentum.md"

    assets: tuple[str, ...] = DIVERSIFIED
    cash: str | None = "BIL"  # the hurdle; None = beat zero
    lookback: int = 252
    vol_lookback: int = 63
    min_change: float = 0.01  # skip drift trims smaller than this weight

    def symbols(self) -> list[str]:
        return [*self.assets, *([self.cash] if self.cash else [])]

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_last_session_of_month():
            return
        closes = ctx.history("close", lookback=max(self.lookback, self.vol_lookback) + 1)
        if len(closes) < self.lookback + 1:
            return
        targets, reasons = ts_momentum_targets(
            closes, self.assets, self.cash, self.lookback, self.vol_lookback
        )
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)
