"""Cross-validation that doesn't leak through overlapping labels (López de Prado, ch. 7).

A triple-barrier label spans days: from the trade's entry to the barrier that ended it. Ordinary
k-fold puts a sample in the training set whose trade overlaps one in the test set, so the model
trains on the very price moves it's tested on. Two fixes:

- **Purging**: drop from training every sample whose span overlaps the test fold's span.
- **Embargo**: also drop the training samples that start just after the test fold (serial
  correlation leaks forward too): those starting within ``embargo`` of the fold's end, as a share
  of the whole sample's time span.

Folds are contiguous blocks of time (not shuffled). ``walk_forward`` gives the stricter version
where the model only ever trains on the past, as it would live.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


def _times(values: Any) -> np.ndarray:
    return pd.to_datetime(pd.Series(values)).to_numpy(dtype="datetime64[ns]")


@dataclass(frozen=True)
class PurgedKFold:
    n_splits: int = 5
    embargo: float = 0.01  # share of the sample's time span

    def split(self, start: Any, end: Any) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Positional (train, test) indices; ``start`` and ``end`` are each sample's label span
        (entry and the end of its trade), in any order."""
        s, e = _times(start), _times(end)
        if len(s) < self.n_splits:
            raise ValueError(f"{len(s)} samples can't make {self.n_splits} folds")
        order = np.argsort(s, kind="stable")
        gap = (e.max() - s.min()) * self.embargo
        for test in np.array_split(order, self.n_splits):
            t0, t1 = s[test].min(), e[test].max()
            overlaps = (s <= t1) & (e >= t0)
            embargoed = (s > t1) & (s <= t1 + gap)
            train = np.flatnonzero(~(overlaps | embargoed))
            yield train, np.sort(test)


def walk_forward(
    start: Any, end: Any, *, n_splits: int = 5, min_train: float = 0.4, embargo: float = 0.01
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Expanding window: the first ``min_train`` of the time-ordered samples train the first
    model; the rest is cut into ``n_splits`` test blocks, each trained on everything that ended
    before it started (less the embargo)."""
    s, e = _times(start), _times(end)
    order = np.argsort(s, kind="stable")
    first = int(len(order) * min_train)
    gap = (e.max() - s.min()) * embargo
    for test in np.array_split(order[first:], n_splits):
        if len(test) == 0:
            continue
        t0 = s[test].min()
        train = np.flatnonzero(e < t0 - gap)
        if len(train):
            yield train, np.sort(test)


def oof_predict(
    make_model: Callable[[], Any],
    x: pd.DataFrame,
    y: pd.Series,
    splits: Iterator[tuple[np.ndarray, np.ndarray]],
    sample_weight: pd.Series | None = None,
) -> pd.Series:
    """Out-of-fold probabilities of class 1: each sample predicted by a model that never saw it
    (or anything overlapping it). Samples in no test fold stay NaN."""
    out = pd.Series(np.nan, index=x.index, dtype=float)
    for train, test in splits:
        if len(np.unique(y.iloc[train])) < 2:
            continue  # one class only: nothing to learn
        model = make_model()
        kw = {}
        if sample_weight is not None:
            kw = {weight_arg(model): sample_weight.iloc[train].to_numpy()}
        model.fit(x.iloc[train], y.iloc[train], **kw)
        out.iloc[test] = model.predict_proba(x.iloc[test])[:, 1]
    return out


def weight_arg(model: Any) -> str:
    """``sample_weight`` for a plain estimator, ``<last step>__sample_weight`` in a pipeline."""
    steps = getattr(model, "steps", None)
    return f"{steps[-1][0]}__sample_weight" if steps else "sample_weight"


def uniqueness(start: Any, end: Any, index: pd.DatetimeIndex) -> pd.Series:
    """Average uniqueness of each label (López de Prado, ch. 4): on each session of its span,
    one over the number of labels alive that day, averaged. Overlapping labels share their
    information, so they weigh less."""
    s, e = _times(start), _times(end)
    days = index.to_numpy(dtype="datetime64[ns]")
    a = np.searchsorted(days, s, side="left")
    b = np.searchsorted(days, e, side="right")
    concurrent = np.zeros(len(days) + 1)
    np.add.at(concurrent, a, 1)
    np.add.at(concurrent, b, -1)
    alive = np.cumsum(concurrent)[:-1]
    out = np.array(
        [float(np.mean(1.0 / alive[i:j])) if j > i else np.nan for i, j in zip(a, b, strict=True)]
    )
    return pd.Series(out)
