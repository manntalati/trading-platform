"""Strategy 1: 10-month moving-average timing (Faber, "A Quantitative Approach to Tactical Asset
Allocation", 2007). Spec: docs/strategies/01-ma-timing.md.

Rule, evaluated on the last trading day of each month, per asset:

- **in** if the month-end close is above the average of the last ``months`` month-end closes
  (10 by default, including the current one);
- **out** (that asset's slice goes to cash) otherwise.

With several assets each gets an equal ``1/N`` slice, so the portfolio holds between 0% and 100%
risk assets. Positions are rebalanced back to target at every month end and drift with prices in
between.

Execution timing (look-ahead control):

- ``lag_days=1`` (default): trade at the close ``1`` session after the signal. The signal bar is
  never the fill bar, per the project's backtest rules.
- ``lag_days=0``: trade at the signal month-end's own close, as in Faber's paper. Use it to
  replicate the paper, not to estimate what you could have earned.

This is the vectorised research version that works on a price matrix. The event-driven version
(``on_bar`` etc.) comes with the backtest engine and must reproduce these numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from tp_core.calendar import sessions

CASH = "cash"


@dataclass(frozen=True)
class BacktestResult:
    """Daily results. All series start on the first day a position could be held."""

    returns: pd.Series  # net of costs
    gross_returns: pd.Series
    weights: pd.DataFrame  # end-of-day holdings after trading, one column per asset + "cash"
    turnover: pd.Series  # sum of |weight change| over risk assets, on trade days
    trades: pd.DataFrame  # one row per asset weight change
    signals: pd.DataFrame  # month-end signal per asset (True = invested), indexed by signal date

    @property
    def exposure(self) -> pd.Series:
        """Fraction of the portfolio in risk assets at each close."""
        return self.weights.drop(columns=CASH).sum(axis=1)

    def trades_per_year(self) -> float:
        years = len(self.returns) / 252
        return len(self.trades) / years if years else float("nan")


def month_end_closes(prices: pd.DataFrame) -> pd.DataFrame:
    """The last close of each calendar month, indexed by that trading day.

    A trailing month that is still in progress (its last row is not the month's final NYSE
    session) is dropped: its "month-end" signal doesn't exist yet.
    """
    month = pd.DatetimeIndex(prices.index).to_period("M")
    month_end = prices.groupby(month).tail(1)
    if len(month_end) and not _is_last_session_of_month(month_end.index[-1].date()):
        month_end = month_end.iloc[:-1]
    return month_end


def _is_last_session_of_month(day: date) -> bool:
    upcoming = sessions(day + timedelta(days=1), day + timedelta(days=10))
    return bool(upcoming) and upcoming[0].month != day.month


def ma_signals(prices: pd.DataFrame, months: int = 10) -> pd.DataFrame:
    """True where the month-end close is above its ``months``-month simple moving average.

    Months before the average exists are False (not invested). Only data up to and including
    each month end is used.
    """
    month_end = month_end_closes(prices)
    sma = month_end.rolling(months, min_periods=months).mean()
    return (month_end > sma) & sma.notna()


def ma_timing(
    prices: pd.DataFrame,
    *,
    months: int = 10,
    lag_days: int = 1,
    cost_bps: float = 5.0,
    cash_returns: pd.Series | None = None,
) -> BacktestResult:
    """Backtest MA timing on daily (adjusted) closes; one column per asset.

    ``cost_bps`` is charged on traded notional (each 100% of the portfolio bought or sold costs
    ``cost_bps`` basis points; it approximates half-spread plus impact for liquid ETFs).
    ``cash_returns`` are daily returns earned on the uninvested part (e.g. T-bills); 0 if omitted.
    """
    signals = ma_signals(prices, months)
    warm = signals.index[months - 1 :] if len(signals) >= months else signals.index[:0]
    targets = _equal_weight_targets(signals.loc[warm], invested=signals.loc[warm])
    return _simulate(prices, targets, signals, lag_days, cost_bps, cash_returns)


def equal_weight_monthly(
    prices: pd.DataFrame,
    *,
    start_after: pd.Timestamp | None = None,
    lag_days: int = 1,
    cost_bps: float = 5.0,
) -> BacktestResult:
    """Benchmark: always invested, equal weights, rebalanced at month ends (buy and hold for a
    single asset). ``start_after`` aligns the first rebalance with a strategy's first signal."""
    month_end = month_end_closes(prices)
    if start_after is not None:
        month_end = month_end[month_end.index >= start_after]
    invested = month_end.notna()
    targets = _equal_weight_targets(month_end, invested=invested)
    return _simulate(prices, targets, invested, lag_days, cost_bps, None)


def _equal_weight_targets(month_end: pd.DataFrame, *, invested: pd.DataFrame) -> pd.DataFrame:
    """Target weights at each signal date: 1/N per invested asset, N = assets with data."""
    available = month_end.notna().sum(axis=1).replace(0, np.nan)
    weights = invested.astype(float).div(available, axis=0).fillna(0.0)
    weights[CASH] = (1.0 - weights.sum(axis=1)).clip(lower=0.0)  # no -2e-16 cash from rounding
    return weights


def _simulate(
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    signals: pd.DataFrame,
    lag_days: int,
    cost_bps: float,
    cash_returns: pd.Series | None,
) -> BacktestResult:
    if lag_days < 0:
        raise ValueError("lag_days must be >= 0")
    days = pd.DatetimeIndex(prices.index)
    if not days.is_unique or not days.is_monotonic_increasing:
        raise ValueError("prices must have a unique, increasing DatetimeIndex")
    assets = list(prices.columns)
    asset_returns = prices.pct_change(fill_method=None).fillna(0.0).to_numpy()
    cash = (
        cash_returns.reindex(days).fillna(0.0).to_numpy()
        if cash_returns is not None
        else np.zeros(len(days))
    )

    # Map each signal date to the session it is executed on.
    positions = days.searchsorted(pd.DatetimeIndex(targets.index)) + lag_days
    values = targets[[*assets, CASH]].to_numpy(dtype=float)
    schedule = {int(p): values[i] for i, p in enumerate(positions) if p < len(days)}

    n = len(days)
    held = np.full((n, len(assets) + 1), np.nan)
    gross = np.full(n, np.nan)
    net = np.full(n, np.nan)
    turnover = np.zeros(n)
    trade_rows: list[dict[str, object]] = []
    current: np.ndarray | None = None

    for t in range(n):
        if current is not None:
            growth = np.append(1.0 + asset_returns[t], 1.0 + cash[t])
            value = float(current @ growth)
            gross[t] = net[t] = value - 1.0
            current = current * growth / value
        if t in schedule:
            target = schedule[t]
            before = current if current is not None else np.append(np.zeros(len(assets)), 1.0)
            change = target[:-1] - before[:-1]
            turnover[t] = float(np.abs(change).sum())
            cost = turnover[t] * cost_bps / 10_000
            gross[t] = 0.0 if np.isnan(gross[t]) else gross[t]
            net[t] = gross[t] - cost
            for i, asset in enumerate(assets):
                if abs(change[i]) > 1e-12:
                    trade_rows.append(
                        {
                            "date": days[t],
                            "asset": asset,
                            "weight_before": float(before[i]),
                            "weight_after": float(target[i]),
                        }
                    )
            current = target.copy()
        if current is not None:
            held[t] = current

    started = ~np.isnan(net)
    index = days[started]
    columns = [*assets, CASH]
    return BacktestResult(
        returns=pd.Series(net[started], index=index, name="strategy"),
        gross_returns=pd.Series(gross[started], index=index, name="strategy_gross"),
        weights=pd.DataFrame(held[started], index=index, columns=columns),
        turnover=pd.Series(turnover[started], index=index, name="turnover"),
        trades=pd.DataFrame(trade_rows, columns=["date", "asset", "weight_before", "weight_after"]),
        signals=signals,
    )
