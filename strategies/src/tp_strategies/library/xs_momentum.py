"""Strategy 3: cross-sectional momentum, "12-1" (Jegadeesh & Titman 1993), long-only.

Spec: docs/strategies/03-xs-momentum.md. On the last session of each month, rank the stock
universe by the return from 12 months ago to 1 month ago (the most recent month is skipped: it
tends to reverse). Hold the top ``top_n`` at equal weight, at most ``max_per_sector`` names per
GICS sector so the book stays inside the plan's 30% sector cap; the rest is cash.

The universe is today's large caps (see the survivorship warning in config/universes.toml), so
backtests flatter this strategy more than most.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_strategies.library.signals import pct, trailing_return
from tp_trading.strategy import Context, Strategy

# The [bars] stocks in config/universes.toml (a test keeps the two in step).
LARGE_CAPS = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "INTC",
    "ORCL", "CRM", "ADBE", "CSCO", "NFLX",
    "BRK.B", "JPM", "BAC", "V", "MA",
    "UNH", "LLY", "JNJ", "MRK", "ABBV",
    "XOM", "CVX",
    "PG", "KO", "PEP", "COST", "WMT", "HD", "MCD", "NKE", "DIS",
    "CAT", "BA",
)  # fmt: skip


def xs_momentum_targets(
    closes: pd.DataFrame,
    *,
    lookback: int,
    skip: int,
    top_n: int,
    max_per_sector: int,
    require_positive: bool,
    sector: Callable[[str], str | None],
) -> tuple[dict[str, float], dict[str, str]]:
    score = trailing_return(closes, lookback, skip).dropna().sort_values(ascending=False)
    ranked = len(score)
    picks: list[str] = []
    per_sector: Counter[str] = Counter()
    reasons: dict[str, str] = {}
    for rank, (symbol, value) in enumerate(score.items(), start=1):
        bucket = sector(str(symbol)) or str(symbol)
        label = f"{symbol} 12-1 return {pct(value)}, rank {rank} of {ranked}"
        if len(picks) >= top_n:
            reasons[str(symbol)] = f"{label}: outside the top {top_n}"
        elif require_positive and value <= 0:
            reasons[str(symbol)] = f"{label}: not positive"
        elif per_sector[bucket] >= max_per_sector:
            reasons[str(symbol)] = f"{label}: {bucket} already has {max_per_sector} names"
        else:
            picks.append(str(symbol))
            per_sector[bucket] += 1
            reasons[str(symbol)] = f"{label}: held at {1 / top_n:.0%}"
    for symbol in closes.columns:
        reasons.setdefault(str(symbol), f"{symbol}: not enough history to rank")
    return dict.fromkeys(picks, 1.0 / top_n), reasons


@dataclass(frozen=True)
class CrossSectionalMomentum(Strategy):
    name: ClassVar[str] = "xs-momentum"
    title: ClassVar[str] = "Cross-sectional momentum (12-1, top names)"
    spec: ClassVar[str] = "docs/strategies/03-xs-momentum.md"

    universe: tuple[str, ...] = LARGE_CAPS
    lookback: int = 252
    skip: int = 21
    top_n: int = 10
    max_per_sector: int = 3
    require_positive: bool = False  # True adds an absolute-momentum filter (cash instead)
    # Full monthly rebalance: trimming every holding back to 1/top_n is what keeps three names
    # in one sector at or under the 30% sector cap after a month of drift.
    min_change: float = 0.0

    def symbols(self) -> list[str]:
        return list(self.universe)

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_rebalance_day():
            return
        closes = ctx.history("close", lookback=self.lookback + 1)
        if len(closes) < self.lookback + 1:
            return
        targets, reasons = xs_momentum_targets(
            closes,
            lookback=self.lookback,
            skip=self.skip,
            top_n=self.top_n,
            max_per_sector=self.max_per_sector,
            require_positive=self.require_positive,
            sector=ctx.sector,
        )
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)
