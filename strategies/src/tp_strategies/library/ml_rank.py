"""Strategy 13: cross-sectional ranking model, long the top names (Phase 4).

Spec: docs/strategies/13-ml-rank.md. Strategy 3's portfolio (the top ``top_n`` large caps at equal
weight, at most ``max_per_sector`` per sector, rebalanced at each month end) with the 12-1
momentum score replaced by a model's (``tp_ml.ranking``): trained on every past month end to
order the stocks by their next month's return from their point-in-time features, each ranked
against the other stocks that day.

The model is retrained every ``retrain`` months on the bars before that month, so it has never
seen the months it ranks. Until there are ``min_dates`` month ends to learn from, the strategy
ranks by 12-1 momentum, i.e. it is strategy 3 (its orders say so).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_ml.features import LOOKBACK, latest_price_features
from tp_ml.ranking import RankConfig, RankModel, train_ranker
from tp_strategies.library.ml import ModelCache, latest_bars, sectors_of
from tp_strategies.library.xs_momentum import LARGE_CAPS, xs_momentum_targets
from tp_trading.strategy import Context, Strategy

_MODELS = ModelCache()
YEAR = 252


def top_by_score(
    scores: pd.Series, *, top_n: int, max_per_sector: int, sector: Callable[[str], str | None]
) -> tuple[dict[str, float], dict[str, str]]:
    """The ``top_n`` highest scores at equal weight, at most ``max_per_sector`` per sector."""
    ranked = scores.dropna().sort_values(ascending=False)
    picks: list[str] = []
    per_sector: Counter[str] = Counter()
    reasons: dict[str, str] = {}
    for rank, (key, value) in enumerate(ranked.items(), start=1):
        symbol = str(key)
        bucket = sector(symbol) or symbol
        label = f"{symbol} model score {value:+.3f}, rank {rank} of {len(ranked)}"
        if len(picks) >= top_n:
            reasons[symbol] = f"{label}: outside the top {top_n}"
        elif per_sector[bucket] >= max_per_sector:
            reasons[symbol] = f"{label}: {bucket} already has {max_per_sector} names"
        else:
            picks.append(symbol)
            per_sector[bucket] += 1
            reasons[symbol] = f"{label}: held at {1 / top_n:.0%}"
    return dict.fromkeys(picks, 1.0 / top_n), reasons


@dataclass(frozen=True)
class MlRank(Strategy):
    name: ClassVar[str] = "ml-rank"
    title: ClassVar[str] = "Cross-sectional ranking model (top names)"
    spec: ClassVar[str] = "docs/strategies/13-ml-rank.md"

    universe: tuple[str, ...] = LARGE_CAPS
    model: str = "ridge"  # or "gbm"
    horizon: int = 21  # the return the model ranks: the next month's
    top_n: int = 10
    max_per_sector: int = 3
    retrain: int = 3  # months between retrains
    min_dates: int = 24  # month ends to learn from before the model is used
    min_change: float = 0.0

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_rebalance_day():
            return
        closes = ctx.history("close", lookback=YEAR + 1)
        if len(closes) < YEAR + 1:
            return
        model = self._model(ctx)
        if model is None:
            targets, reasons = xs_momentum_targets(
                closes, lookback=YEAR, skip=21, top_n=self.top_n,
                max_per_sector=self.max_per_sector, require_positive=False, sector=ctx.sector,
            )  # fmt: skip
            note = f"no model yet (fewer than {self.min_dates} month ends): ranked by 12-1 momentum"
            reasons = {s: f"{r}; {note}" for s, r in reasons.items()}
        else:
            bars = latest_bars(ctx, LOOKBACK)
            latest = latest_price_features(bars, sectors=sectors_of(ctx, bars["close"].columns))
            latest = latest[latest["ret_21"].notna()]  # priced today, with a month of history
            scores = model.score(latest) if len(latest) else pd.Series(dtype=float)
            targets, reasons = top_by_score(
                scores, top_n=self.top_n, max_per_sector=self.max_per_sector, sector=ctx.sector
            )
            trained = (
                f"{self.model} trained on {model.dates} month ends to {model.trained_to:%Y-%m-%d}"
            )
            reasons = {s: f"{r} ({trained})" for s, r in reasons.items()}
        for symbol in self.symbols():
            reasons.setdefault(symbol, f"{symbol}: not enough history to rank")
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)

    def _model(self, ctx: Context) -> RankModel | None:
        def fit(past: dict[str, pd.DataFrame]) -> RankModel | None:
            cfg = RankConfig(model=self.model, horizon=self.horizon)
            sectors = sectors_of(ctx, past["close"].columns)
            return train_ranker(past, cfg, sectors=sectors, min_dates=self.min_dates)

        params = (self.universe, self.model, self.horizon, self.min_dates)
        model: RankModel | None = _MODELS.get(
            ctx, params, months=self.retrain, min_sessions=YEAR, train=fit
        )
        return model
