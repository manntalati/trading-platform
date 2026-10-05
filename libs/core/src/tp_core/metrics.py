"""Return and risk statistics (Phase 0 module 3, and the tear sheet for every backtest).

Conventions:

- Inputs are **simple periodic returns** (0.01 = +1%) as a ``pd.Series`` indexed by time, unless
  a function says it takes prices. Missing values are dropped.
- ``periods_per_year`` annualizes: 252 for daily bars, 52 weekly, 12 monthly.
- ``rf`` is an **annual** risk-free rate (0.04 = 4%), converted to a per-period rate by
  compounding: ``(1 + rf) ** (1 / periods_per_year) - 1``.
- Drawdowns are negative numbers (-0.25 = 25% below the prior peak).

Simple vs log returns: simple returns aggregate across assets (a portfolio's return is the
weighted sum of its assets' simple returns); log returns aggregate across time (a period's log
return is the sum of its sub-periods'). ``log_r = log(1 + simple_r)``.
"""

from __future__ import annotations

import math
from typing import cast

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def simple_returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """Period-over-period simple returns from a price series; the first row is dropped."""
    return prices.pct_change(fill_method=None).iloc[1:]


def log_returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    ratio = prices / prices.shift(1)
    return ratio.apply(np.log).iloc[1:]


def per_period_rate(rf: float, periods_per_year: int) -> float:
    return float((1.0 + rf) ** (1.0 / periods_per_year) - 1.0)


def _clean(returns: pd.Series) -> pd.Series:
    return returns.dropna().astype(float)


def _values(returns: pd.Series) -> np.ndarray:
    return _clean(returns).to_numpy(dtype=float)


def total_return(returns: pd.Series) -> float:
    return float(np.prod(1.0 + _values(returns)) - 1.0)


def cagr(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Compound annual growth rate."""
    r = _values(returns)
    if r.size == 0:
        return math.nan
    growth = float(np.prod(1.0 + r))
    if growth <= 0:
        return -1.0
    return float(growth ** (periods_per_year / r.size) - 1.0)


def annualized_return(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Arithmetic mean return x periods per year (what Sharpe ratios are built from)."""
    return float(np.mean(_values(returns)) * periods_per_year)


def annualized_volatility(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    return float(_clean(returns).std(ddof=1) * math.sqrt(periods_per_year))


def sharpe_ratio(
    returns: pd.Series, rf: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """Annualized mean excess return over annualized volatility of excess returns."""
    excess = _clean(returns) - per_period_rate(rf, periods_per_year)
    sd = float(excess.std(ddof=1))
    if len(excess) < 2 or sd == 0 or math.isnan(sd):
        return math.nan
    return float(excess.mean() / sd * math.sqrt(periods_per_year))


def downside_deviation(
    returns: pd.Series, mar: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """Annualized root-mean-square of returns below the per-period target ``mar``.

    Uses every observation in the mean (Sortino & Price), so a series with few small losses has
    a small downside deviation rather than one computed only from the losing periods.
    """
    shortfall = np.minimum(_values(returns) - mar, 0.0)
    return float(math.sqrt(float(np.mean(shortfall**2))) * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: pd.Series, rf: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """Like Sharpe, but only penalizes volatility below the risk-free rate."""
    target = per_period_rate(rf, periods_per_year)
    r = _clean(returns)
    if r.empty:
        return math.nan
    dd = downside_deviation(r, target, periods_per_year)
    excess = float((r - target).mean() * periods_per_year)
    if dd == 0:
        return math.inf if excess > 0 else math.nan
    return excess / dd


def wealth_index(returns: pd.Series, start: float = 1.0) -> pd.Series:
    return start * (1.0 + _clean(returns)).cumprod()


def drawdowns(returns: pd.Series) -> pd.Series:
    """Fractional distance below the running peak of the wealth index (0 at new highs)."""
    wealth = wealth_index(returns)
    peak = wealth.cummax().clip(lower=1.0)  # starting capital counts as the first peak
    return wealth / peak - 1.0


def max_drawdown(returns: pd.Series) -> float:
    dd = drawdowns(returns)
    return float(dd.min()) if len(dd) else math.nan


def max_drawdown_duration(returns: pd.Series) -> int:
    """Longest run of consecutive periods spent below a prior peak (recovered or not)."""
    underwater = (drawdowns(returns) < 0).to_numpy()
    longest = current = 0
    for below in underwater:
        current = current + 1 if below else 0
        longest = max(longest, current)
    return longest


def calmar_ratio(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    mdd = max_drawdown(returns)
    if not mdd or math.isnan(mdd):
        return math.nan
    return cagr(returns, periods_per_year) / abs(mdd)


def hit_rate(returns: pd.Series) -> float:
    """Share of non-zero periods that were positive (flat periods are ignored)."""
    r = _clean(returns)
    active = r[r != 0]
    return float((active > 0).mean()) if len(active) else math.nan


def average_win(returns: pd.Series) -> float:
    r = _values(returns)
    wins = r[r > 0]
    return float(np.mean(wins)) if wins.size else math.nan


def average_loss(returns: pd.Series) -> float:
    r = _values(returns)
    losses = r[r < 0]
    return float(np.mean(losses)) if losses.size else math.nan


def autocorrelation(returns: pd.Series, lag: int = 1) -> float:
    """Correlation of returns with themselves ``lag`` periods earlier.

    Near 0 for liquid assets' daily returns; clearly positive suggests momentum or stale prices,
    clearly negative suggests mean reversion or bid-ask bounce.
    """
    r = _clean(returns)
    if len(r) < lag + 3:  # too few pairs for a meaningful (or warning-free) correlation
        return math.nan
    return float(r.autocorr(lag=lag))


def skewness(returns: pd.Series) -> float:
    # pandas-stubs types Series reductions as "any scalar"; skew of floats is a float.
    return float(cast(float, _clean(returns).skew()))


def excess_kurtosis(returns: pd.Series) -> float:
    """0 for a normal distribution; positive means fat tails (big moves more common)."""
    return float(cast(float, _clean(returns).kurt()))


def tail_ratio_vs_normal(returns: pd.Series, sigmas: float = 3.0) -> float:
    """How many times more often |z| > ``sigmas`` happens than a normal distribution predicts."""
    r = _clean(returns)
    if len(r) < 2:
        return math.nan
    z = (r - r.mean()) / r.std(ddof=1)
    observed = float((z.abs() > sigmas).mean())
    expected = math.erfc(sigmas / math.sqrt(2))  # two-sided normal tail probability
    return observed / expected


def beta(returns: pd.Series, benchmark: pd.Series) -> float:
    """Sensitivity to the benchmark: cov(r, b) / var(b) over overlapping periods."""
    joined = pd.concat([returns, benchmark], axis=1, join="inner").dropna()
    if len(joined) < 3:
        return math.nan
    b = joined.iloc[:, 1]
    var = float(b.var(ddof=1))
    return float(joined.iloc[:, 0].cov(b) / var) if var else math.nan


def tear_sheet(
    returns: pd.Series | pd.DataFrame,
    *,
    rf: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
    benchmark: pd.Series | None = None,
) -> pd.Series | pd.DataFrame:
    """Standard statistics for one return series (a Series) or several (one column each)."""
    if isinstance(returns, pd.DataFrame):
        return pd.DataFrame(
            {
                col: tear_sheet(
                    returns[col], rf=rf, periods_per_year=periods_per_year, benchmark=benchmark
                )
                for col in returns.columns
            }
        )
    r = _clean(returns)
    stats: dict[str, float | int | str] = {
        "start": str(r.index[0].date()) if len(r) and hasattr(r.index[0], "date") else "",
        "end": str(r.index[-1].date()) if len(r) and hasattr(r.index[-1], "date") else "",
        "periods": len(r),
        "total_return": total_return(r),
        "cagr": cagr(r, periods_per_year),
        "ann_return": annualized_return(r, periods_per_year),
        "ann_volatility": annualized_volatility(r, periods_per_year),
        "sharpe": sharpe_ratio(r, rf, periods_per_year),
        "sortino": sortino_ratio(r, rf, periods_per_year),
        "max_drawdown": max_drawdown(r),
        "max_dd_duration": max_drawdown_duration(r),
        "calmar": calmar_ratio(r, periods_per_year),
        "hit_rate": hit_rate(r),
        "avg_win": average_win(r),
        "avg_loss": average_loss(r),
        "autocorr_1": autocorrelation(r, 1),
        "skew": skewness(r),
        "excess_kurtosis": excess_kurtosis(r),
        "tail_ratio_3sd": tail_ratio_vs_normal(r, 3.0),
    }
    if benchmark is not None:
        stats["beta"] = beta(r, benchmark)
    return pd.Series(stats, name=returns.name)
