"""Model factories shared by the ML strategies.

Two families, both cheap enough to retrain inside a backtest:

- ``logistic``: median imputation, standardization and an L2-penalised logistic regression. The
  baseline: few parameters, hard to overfit, easy to read.
- ``gbm``: gradient-boosted trees (scikit-learn's histogram GBM) on features cut into quantile
  bins first. Shallow trees and a slow learning rate; missing values are handled natively.

And two regressors for ranking (``make_regressor``): ``ridge`` (the linear baseline) and
``gbm``, on features already ranked across stocks.

The quantile bins are not only regularization: scikit-learn re-sorts every feature once per
bin edge when it bins with sample weights, which made a fit 20 times slower. Features that are
already cut into fewer distinct values than the model's bins skip that step.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODELS = ("logistic", "gbm")
REGRESSORS = ("ridge", "gbm")


class QuantileBins(TransformerMixin, BaseEstimator):  # type: ignore[misc]
    """Each column to the index of its quantile bin (0 to ``n_bins - 1``) on the training data.
    Missing and infinite values stay missing."""

    def __init__(self, n_bins: int = 32) -> None:
        self.n_bins = n_bins

    def fit(self, x: Any, y: Any = None) -> QuantileBins:
        values = np.asarray(x, dtype=float)
        quantiles = np.linspace(0, 1, self.n_bins + 1)[1:-1]
        self.edges_ = []
        for column in values.T:
            finite = column[np.isfinite(column)]
            edges = np.unique(np.quantile(finite, quantiles)) if len(finite) else np.array([])
            self.edges_.append(edges)
        return self

    def transform(self, x: Any) -> np.ndarray:
        values = np.asarray(x, dtype=float)
        out = np.full(values.shape, np.nan)
        for j, edges in enumerate(self.edges_):
            column = values[:, j]
            ok = np.isfinite(column)
            out[ok, j] = np.searchsorted(edges, column[ok], side="right")
        return out


def make_model(kind: str) -> Callable[[], Any]:
    """A factory for a fresh, unfitted classifier of ``kind``."""
    if kind == "logistic":
        return lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(C=0.1, max_iter=1000),
        )
    if kind == "gbm":
        return lambda: make_pipeline(
            QuantileBins(32),
            HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=1.0,
                min_samples_leaf=40, random_state=0,
            ),
        )  # fmt: skip
    raise ValueError(f"unknown model {kind!r}: {', '.join(MODELS)}")


def make_regressor(kind: str) -> Callable[[], Any]:
    """A factory for a fresh regressor of ``kind``, for features already ranked to -0.5..0.5
    (``tp_ml.ranking``): a missing rank is the middle, 0."""
    if kind == "ridge":
        return lambda: make_pipeline(
            SimpleImputer(strategy="constant", fill_value=0.0), Ridge(alpha=10.0)
        )
    if kind == "gbm":
        return lambda: HistGradientBoostingRegressor(
            max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0,
            min_samples_leaf=50, random_state=0,
        )  # fmt: skip
    raise ValueError(f"unknown model {kind!r}: {', '.join(REGRESSORS)}")


def usable_columns(data: pd.DataFrame, columns: tuple[str, ...]) -> tuple[str, ...]:
    """The columns with something to learn from: present, and not all one value or missing
    (no macro data in the lake leaves its columns empty)."""

    def varies(c: str) -> bool:
        values = data[c].dropna().to_numpy()
        return len(values) > 0 and bool((values != values[0]).any())

    return tuple(c for c in columns if c in data.columns and varies(c))
