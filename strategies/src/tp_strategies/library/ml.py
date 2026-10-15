"""What the machine-learning strategies share: models retrained on a schedule from the bars before
each retrain date, cached so a backtest trains once per period, not once per session."""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any

import numpy as np
import pandas as pd

from tp_ml.cv import retrain_date
from tp_trading.data import FIELDS
from tp_trading.strategy import Context


class ModelCache:
    """Fitted models by (training parameters, retrain date, data fingerprint). Validation's
    variants that only differ in how a model is used (a threshold, the number of names) reuse
    the same models; a paper run (a fresh process each day) trains once."""

    def __init__(self, size: int = 64) -> None:
        self.size = size
        self._models: dict[tuple[Any, ...], Any] = {}

    def get(
        self,
        ctx: Context,
        params: tuple[Hashable, ...],
        *,
        months: int,
        min_sessions: int,
        train: Callable[[dict[str, pd.DataFrame]], Any],
    ) -> Any:
        """The model for ``ctx.now``: ``train`` on the bars before the current retrain date
        (every ``months`` months), or None with fewer than ``min_sessions`` of them."""
        cutoff = retrain_date(pd.Timestamp(ctx.now), months)
        history = ctx.history("close")
        close = history[history.index < cutoff]
        if len(close) <= min_sessions:
            return None
        key = (
            params,
            cutoff,
            tuple(map(str, close.columns)),
            close.shape,
            float(np.nansum(close.to_numpy(dtype=float))),
        )
        if key not in self._models:
            past: dict[str, pd.DataFrame] = {"close": close}
            for field in FIELDS:
                frame = ctx.history(field)
                past[field] = frame[frame.index < cutoff]
            if len(self._models) >= self.size:
                self._models.pop(next(iter(self._models)))
            self._models[key] = train(past)
        return self._models[key]

    def clear(self) -> None:
        self._models.clear()


def sectors_of(ctx: Context, symbols: pd.Index) -> dict[str, str]:
    return {str(s): sector for s in symbols if (sector := ctx.sector(str(s)))}


def latest_bars(ctx: Context, lookback: int) -> dict[str, pd.DataFrame]:
    return {field: ctx.history(field, lookback=lookback) for field in FIELDS}
