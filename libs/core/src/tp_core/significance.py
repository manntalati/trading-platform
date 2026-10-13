"""Is a backtest's Sharpe ratio real? Corrections for luck, short samples and fat tails.

- **Probabilistic Sharpe ratio (PSR)**: the probability that the true Sharpe exceeds a
  threshold, given the sample's length, skew and kurtosis (Bailey and López de Prado, 2012).
- **Deflated Sharpe ratio (DSR)**: the PSR against the Sharpe the best of N tries would show by
  luck alone (Bailey and López de Prado, 2014). Every variant tried counts, which is why the
  validation keeps a log of them: test 20 versions of a strategy and the best one's Sharpe has to
  clear a much higher bar than if you had tried one.
- **Block bootstrap**: resample the daily returns in blocks (keeping short-run dependence) to
  see the spread of outcomes the same strategy could have produced.

Sharpe ratios here are per period (daily), not annualized, as the formulas require.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

from tp_core import metrics

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def _moments(returns: pd.Series) -> tuple[float, int, float, float]:
    r = returns.dropna().astype(float)
    n = len(r)
    sd = float(r.std(ddof=1)) if n > 1 else math.nan
    sr = float(r.mean()) / sd if sd and sd == sd else math.nan
    skew = float(r.skew()) if n > 2 else 0.0  # type: ignore[arg-type]
    kurt = float(r.kurt()) + 3.0 if n > 3 else 3.0  # type: ignore[arg-type]  # excess -> raw
    return sr, n, skew, kurt


def probabilistic_sharpe(returns: pd.Series, threshold: float = 0.0) -> float:
    """P(true per-period Sharpe > ``threshold``)."""
    sr, n, skew, kurt = _moments(returns)
    if n < 3 or sr != sr:
        return math.nan
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return math.nan
    return _N.cdf((sr - threshold) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(trials: int, variance: float) -> float:
    """The Sharpe the best of ``trials`` independent strategies with true Sharpe 0 would show,
    when trial Sharpes vary with ``variance``."""
    if trials <= 1 or variance <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / trials)
    b = _N.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return math.sqrt(variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


@dataclass(frozen=True)
class Deflated:
    sharpe: float  # annualized, for reading
    trials: int
    threshold: float  # annualized Sharpe the best of the trials would reach by luck
    probability: float  # DSR: P(true Sharpe > threshold)

    @property
    def excess(self) -> float:
        """The deflated Sharpe: what's left after the luck of trying ``trials`` variants."""
        return self.sharpe - self.threshold

    def to_dict(self) -> dict[str, float | int]:
        return {
            "sharpe": self.sharpe,
            "trials": self.trials,
            "threshold": self.threshold,
            "probability": self.probability,
            "excess": self.excess,
        }


def deflated_sharpe(
    returns: pd.Series,
    trial_sharpes: list[float],
    periods_per_year: int = metrics.TRADING_DAYS,
) -> Deflated:
    """DSR of ``returns`` given the annualized Sharpe of every variant tried (``returns``' own
    variant included)."""
    sr, _, _, _ = _moments(returns)
    trials = max(len(trial_sharpes), 1)
    per_period = [s / math.sqrt(periods_per_year) for s in trial_sharpes if s == s]
    variance = float(np.var(per_period, ddof=1)) if len(per_period) > 1 else 0.0
    threshold = expected_max_sharpe(trials, variance)
    scale = math.sqrt(periods_per_year)
    return Deflated(
        sharpe=sr * scale if sr == sr else math.nan,
        trials=trials,
        threshold=threshold * scale,
        probability=probabilistic_sharpe(returns, threshold),
    )


def block_bootstrap(
    returns: pd.Series,
    *,
    paths: int = 1000,
    block: int = 21,
    seed: int = 7,
    periods_per_year: int = metrics.TRADING_DAYS,
) -> dict[str, float]:
    """Resample ``returns`` in blocks of ``block`` days into ``paths`` alternative histories of
    the same length; percentiles of their CAGR, max drawdown and Sharpe, and the share that lost
    money."""
    r = returns.dropna().astype(float).to_numpy()
    n = len(r)
    if n < block * 2:
        return {}
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(paths, math.ceil(n / block)))
    idx = (starts[:, :, None] + np.arange(block)).reshape(paths, -1)[:, :n]
    sims = r[idx]
    growth = np.cumprod(1.0 + sims, axis=1)
    years = n / periods_per_year
    cagr = growth[:, -1] ** (1.0 / years) - 1.0
    peaks = np.maximum.accumulate(growth, axis=1)
    max_dd = (growth / peaks - 1.0).min(axis=1)
    sd = sims.std(axis=1, ddof=1)
    sharpe = np.where(sd > 0, sims.mean(axis=1) / np.where(sd > 0, sd, 1.0), 0.0)
    sharpe = sharpe * math.sqrt(periods_per_year)
    return {
        "paths": paths,
        "block_days": block,
        "cagr_p5": float(np.percentile(cagr, 5)),
        "cagr_p50": float(np.percentile(cagr, 50)),
        "cagr_p95": float(np.percentile(cagr, 95)),
        "max_drawdown_p5": float(np.percentile(max_dd, 5)),  # the bad tail
        "max_drawdown_p50": float(np.percentile(max_dd, 50)),
        "sharpe_p5": float(np.percentile(sharpe, 5)),
        "sharpe_p50": float(np.percentile(sharpe, 50)),
        "prob_loss": float((growth[:, -1] < 1.0).mean()),
    }
