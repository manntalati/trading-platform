"""Builders for small, hand-checkable bar and corporate-action frames (used by tests)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pandas as pd

from tp_core.calendar import session_midnight_utc, sessions
from tp_core.schemas import RAW_CORPORATE_ACTIONS_SCHEMA, conform

T0 = datetime(2024, 7, 1, 22, 0, tzinfo=UTC)


def raw_bars(
    symbol: str,
    closes: Sequence[float],
    *,
    start: date = date(2024, 6, 3),
    run_id: str = "run-a",
    ingested_at: datetime = T0,
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """One bar per XNYS session from ``start`` with flat-ish OHLC around each close."""
    days = sessions(start, date(start.year + 1, 12, 31))[: len(closes)]
    df = pd.DataFrame(
        {
            "symbol": symbol,
            "session": days,
            "timestamp": [session_midnight_utc(d) for d in days],
            "open": list(closes),
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": list(closes),
            "volume": volume,
            "trade_count": 100.0,
            "vwap": list(closes),
            "feed": "sip",
            "adjustment": "raw",
            "ingested_at": pd.Timestamp(ingested_at),
            "run_id": run_id,
        }
    )
    return df


def actions(*rows: dict[str, object]) -> pd.DataFrame:
    base = {"ingested_at": pd.Timestamp(T0), "run_id": "run-a"}
    return conform(pd.DataFrame([{**base, **r} for r in rows]), RAW_CORPORATE_ACTIONS_SCHEMA)
