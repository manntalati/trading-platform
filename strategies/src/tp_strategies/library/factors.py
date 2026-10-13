"""Strategy 8: factor portfolios: value, quality, low volatility.

Spec: docs/strategies/08-factors.md. On the last session of each month, rank the universe on
the chosen factor and hold the best ``top`` names in equal weight, at most ``max_per_sector``
per sector:

- ``value``: earnings yield and book to market (higher is cheaper), from SEC filings as known
  that day (``ctx.reference("valuation")``);
- ``quality``: gross profit over assets (Novy-Marx), or return on equity where a company reports
  no gross profit;
- ``low-vol``: the lowest volatility of daily returns over the last year (prices only);
- ``composite``: the average of the three ranks.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_trading.strategy import Context, Strategy

FACTORS = ("value", "quality", "low-vol", "composite")


def factor_scores(
    factor: str,
    closes: pd.DataFrame,
    valuation: pd.DataFrame | None,
    lookback: int = 252,
) -> pd.Series:
    """A score per symbol, higher is better: the percentile rank on ``factor`` (names without
    the data are left out)."""
    if factor not in FACTORS:
        raise ValueError(f"unknown factor {factor!r}: {', '.join(FACTORS)}")
    ranks: list[pd.Series] = []
    if factor in ("low-vol", "composite"):
        rets = closes.tail(lookback + 1).pct_change(fill_method=None).iloc[1:]
        enough = rets.count().astype(float) >= lookback * 0.9
        vol = rets.std().where(enough)
        ranks.append((-vol).rank(pct=True))
    if factor in ("value", "quality", "composite"):
        if valuation is None:
            raise LookupError(f"{factor} needs fundamentals (ctx.reference('valuation'))")
        cols = ["earnings_yield", "book_to_market", "gross_profitability", "roe"]
        # before the first filings there is nothing to rank: no names that month
        v = valuation.reindex(index=closes.columns, columns=cols)
        if factor in ("value", "composite"):
            ey = v["earnings_yield"].astype(float).rank(pct=True)
            bm = v["book_to_market"].astype(float).rank(pct=True)
            ranks.append(pd.concat([ey, bm], axis=1).mean(axis=1, skipna=True))
        if factor in ("quality", "composite"):
            gp = v["gross_profitability"].astype(float)
            roe = v["roe"].astype(float)
            # gross profitability where reported, else return on equity, each ranked on its own
            q = gp.rank(pct=True).where(gp.notna(), roe.rank(pct=True))
            ranks.append(q)
    score = pd.concat(ranks, axis=1).mean(axis=1, skipna=factor != "composite")
    return score.dropna().sort_values(ascending=False)


def pick(
    scores: pd.Series, top: int, max_per_sector: int, sector: Callable[[str], str | None]
) -> list[str]:
    chosen: list[str] = []
    per_sector: Counter[str] = Counter()
    for symbol in scores.index:
        s = str(symbol)
        bucket = sector(s) or s
        if per_sector[bucket] >= max_per_sector:
            continue
        per_sector[bucket] += 1
        chosen.append(s)
        if len(chosen) >= top:
            break
    return chosen


@dataclass(frozen=True)
class FactorPortfolio(Strategy):
    name: ClassVar[str] = "factor"
    title: ClassVar[str] = "Factor portfolios: value, quality, low volatility"
    spec: ClassVar[str] = "docs/strategies/08-factors.md"

    universe: tuple[str, ...] = LARGE_CAPS
    factor: str = "low-vol"  # value, quality, low-vol or composite
    top: int = 10  # equal weight, 1/top each: keep it at or under the 10% position cap
    max_per_sector: int = 3
    lookback: int = 252

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_rebalance_day():
            return
        closes = ctx.history("close", lookback=self.lookback + 1)
        if len(closes) < self.lookback + 1:
            return
        valuation = ctx.reference("valuation") if self.factor != "low-vol" else None
        scores = factor_scores(self.factor, closes, valuation, self.lookback)
        names = pick(scores, self.top, self.max_per_sector, ctx.sector)
        if not names:
            return
        weight = 1.0 / max(self.top, len(names))
        reasons = {
            s: f"{s} ranks {i + 1} of {len(scores)} on {self.factor} "
            f"(score {scores[s]:.2f}): hold {weight:.0%}"
            for i, s in enumerate(names)
            if math.isfinite(float(scores[s]))
        }
        ctx.order_target_weights(dict.fromkeys(names, weight), reason=reasons, min_change=0.02)
