"""Technical indicators shared by strategies and machine-learning features."""

from __future__ import annotations

import pandas as pd


def wilder_rsi(closes: pd.DataFrame, period: int) -> pd.DataFrame:
    """Relative strength index with Wilder's smoothing (an EMA with alpha = 1 / period).

    100 when a window has no down moves; NaN until ``period`` changes exist.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi.where(avg_loss > 0, 100.0).where(avg_gain.notna())
