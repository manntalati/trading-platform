"""Source protocols.

Each method returns a plain DataFrame with the columns listed below, so jobs, tests and future
vendors (Massive/Polygon, IBKR, Databento) are interchangeable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

import pandas as pd

BAR_COLUMNS = (
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
)

CORPORATE_ACTION_COLUMNS = (
    "id",
    "symbol",
    "action_type",
    "ex_date",
    "process_date",
    "record_date",
    "payable_date",
    "new_rate",
    "old_rate",
    "rate",
    "special",
    "foreign",
)


class BarsSource(Protocol):
    name: str
    feed: str

    def daily_bars(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        """Unadjusted daily bars for sessions in ``[start, end]``; columns ``BAR_COLUMNS``.

        ``timestamp`` is tz-aware UTC.
        """
        ...


class CorporateActionsSource(Protocol):
    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        """Splits, reverse splits, stock and cash dividends with ex-dates in ``[start, end]``.

        Columns ``CORPORATE_ACTION_COLUMNS``; ``action_type`` is one of
        ``tp_core.schemas.CORPORATE_ACTION_TYPES``.
        """
        ...


class MarketDataSource(BarsSource, CorporateActionsSource, Protocol):
    """Everything the bars jobs need."""
