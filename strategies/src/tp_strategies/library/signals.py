"""Small signal helpers shared by library strategies. All take point-in-time history (rows up to
"now", oldest first) and never look past its last row."""

from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS = 252


def trailing_return(closes: pd.DataFrame, lookback: int, skip: int = 0) -> pd.Series:
    """Return from ``lookback`` sessions ago to ``skip`` sessions ago, per column.

    ``skip=21`` gives the classic "12-1" momentum (the last month left out). NaN where the
    history is too short or a price is missing.
    """
    if lookback <= skip:
        raise ValueError("lookback must be longer than skip")
    if len(closes) < lookback + 1:
        return pd.Series(math.nan, index=closes.columns)
    return closes.iloc[-1 - skip] / closes.iloc[-1 - lookback] - 1.0


def annualized_volatility(closes: pd.DataFrame, lookback: int) -> pd.Series:
    """Standard deviation of the last ``lookback`` daily returns, annualized."""
    returns = closes.pct_change(fill_method=None).iloc[-lookback:]
    vol = returns.std() * math.sqrt(TRADING_DAYS)
    return vol.where(returns.notna().sum() >= max(2, lookback // 2))


def pct(value: float) -> str:
    return "n/a" if value != value else f"{value:+.1%}"
