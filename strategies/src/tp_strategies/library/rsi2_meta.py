"""Strategy 12: RSI(2) with a meta-labeling filter (Phase 4).

Spec: docs/strategies/12-rsi2-meta.md. Strategy 5's rules propose, a model decides. Every session
RSI(2)'s exits run unchanged; then each of its buy signals is scored by a classifier trained on
the strategy's own past signals (``tp_ml.meta``: point-in-time features on the signal day, each
signal labelled by whether the trade RSI(2)'s exits made returned money), and only those the
model gives at least ``threshold`` are bought, the most likely first.

The model is retrained every ``retrain`` months on the bars before that month: it has never seen
the trades it scores, nor anything after them. Until there are ``min_events`` labelled signals
there is no model and the strategy trades as plain RSI(2) (its orders say so).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_ml.features import LOOKBACK, latest_price_features
from tp_ml.meta import MetaConfig, MetaModel, bet_size, train
from tp_strategies.library.ml import ModelCache, latest_bars, sectors_of
from tp_strategies.library.rsi2 import INDEX_ETFS, rsi2_decisions
from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_trading.strategy import Context, Strategy

_MODELS = ModelCache()


@dataclass(frozen=True)
class Rsi2Meta(Strategy):
    name: ClassVar[str] = "rsi2-meta"
    title: ClassVar[str] = "RSI(2) with a meta-labeling filter"
    spec: ClassVar[str] = "docs/strategies/12-rsi2-meta.md"

    universe: tuple[str, ...] = INDEX_ETFS + LARGE_CAPS
    entry: float = 10.0  # RSI(2) below this is a signal
    trend: int = 200  # only above the 200-day average
    exit_ma: int = 5  # sell on a close above the 5-day average...
    max_hold: int = 10  # ...or after this many sessions
    slots: int = 10
    max_per_sector: int = 3
    model: str = "gbm"  # or "logistic"
    threshold: float = 0.55  # buy a signal the model gives at least this
    retrain: int = 3  # months between retrains
    min_events: int = 200  # labelled signals before the model is trusted
    sized: bool = False  # scale each position by the model's confidence (tp_ml.meta.bet_size)

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def config(self) -> MetaConfig:
        return MetaConfig(
            entry=self.entry, trend=self.trend, label="trade", exit_ma=self.exit_ma,
            max_hold=self.max_hold, model=self.model, threshold=self.threshold,
        )  # fmt: skip

    def on_bar(self, ctx: Context) -> None:
        closes = ctx.history("close", lookback=self.trend + 1)
        if len(closes) < self.trend:
            return
        # every RSI(2) signal (no slot or sector limit yet: the model ranks them first)
        wide = len(closes.columns) + len(ctx.positions)
        decision = rsi2_decisions(
            closes, ctx.positions, period=2, entry=self.entry, trend=self.trend,
            exit_ma=self.exit_ma, slots=wide, max_hold=self.max_hold, max_per_sector=wide,
            sector=ctx.sector,
        )  # fmt: skip
        cash = ctx.cash
        for symbol, why in decision.sells.items():
            quantity = ctx.quantity(symbol)
            cash += quantity * ctx.price(symbol)
            ctx.order(symbol, -quantity, reason=why, target_weight=0.0)
        if not decision.buys:
            return

        model = self._model(ctx)
        picks: list[tuple[str, str, float]] = []  # symbol, reason, share of a slot
        if model is None:
            note = f"no model yet (fewer than {self.min_events} labelled signals): plain RSI(2)"
            picks = [(s, f"{why}; {note}", 1.0) for s, why in decision.buys]
        else:
            scores = self._score(ctx, model, [s for s, _ in decision.buys])
            ranked = sorted(
                ((scores[s], s, why) for s, why in decision.buys if scores[s] >= self.threshold),
                reverse=True,
            )
            for p, symbol, why in ranked:
                size = float(bet_size(p)) if self.sized else 1.0
                picks.append(
                    (
                        symbol,
                        f"{why}; the model gives it {p:.0%} (threshold {self.threshold:.0%}, "
                        f"trained on {model.events} signals to {model.trained_to:%Y-%m-%d})",
                        size,
                    )
                )

        kept = [s for s, p in ctx.positions.items() if p.quantity > 0 and s not in decision.sells]
        free = self.slots - len(kept)
        per_sector: Counter[str] = Counter(ctx.sector(s) or s for s in kept)
        slot = ctx.equity / self.slots
        for symbol, why, size in picks:
            if free <= 0:
                break
            bucket = ctx.sector(symbol) or symbol
            if per_sector[bucket] >= self.max_per_sector:
                continue
            price = ctx.price(symbol)
            budget = min(slot * size, cash)
            quantity = budget / price if ctx.fractional else math.floor(budget / price)
            if quantity <= 0:
                continue
            free -= 1
            per_sector[bucket] += 1
            cash -= quantity * price
            ctx.order(symbol, quantity, reason=why, target_weight=size / self.slots)

    def _model(self, ctx: Context) -> MetaModel | None:
        """The model for today: trained on the bars before the current retrain date (cached)."""

        def fit(past: dict[str, pd.DataFrame]) -> MetaModel | None:
            sectors = sectors_of(ctx, past["close"].columns)
            try:
                return train(past, self.config(), sectors=sectors, min_events=self.min_events)
            except ValueError:
                return None

        params = (self.universe, self.entry, self.trend, self.exit_ma, self.max_hold,
                  self.model, self.min_events)  # fmt: skip
        model: MetaModel | None = _MODELS.get(
            ctx, params, months=self.retrain, min_sessions=self.trend, train=fit
        )
        return model

    def _score(self, ctx: Context, model: MetaModel, symbols: list[str]) -> dict[str, float]:
        """Today's probability for each signal, from features on the bars up to today."""
        bars = latest_bars(ctx, LOOKBACK)
        sectors = sectors_of(ctx, bars["close"].columns)
        today = latest_price_features(bars, sectors=sectors).reindex(symbols)
        return dict(zip(symbols, model.predict(today).tolist(), strict=True))
