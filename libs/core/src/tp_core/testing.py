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


def synthetic_chain(
    underlying: str,
    spot: float,
    snapshot_at: datetime,
    *,
    expirations: Sequence[date],
    iv: float | Sequence[float] = 0.3,
    strikes: Sequence[float] | None = None,
    half_spread: float = 0.02,
    open_interest: float = 1000.0,
) -> pd.DataFrame:
    """A chain snapshot priced by Black-Scholes at ``iv`` (one per expiration, or the same for
    all), with a bid/ask ``half_spread`` (a share of the price) around the model price."""
    from tp_core import greeks as bs
    from tp_core.occ import format_occ
    from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA

    vols = [iv] * len(expirations) if isinstance(iv, float | int) else list(iv)
    strikes = strikes or [round(spot * k / 20) * 1.0 for k in range(10, 31)]  # 50%..150%
    day = snapshot_at.date()
    rows = []
    for expiration, vol in zip(expirations, vols, strict=True):
        dte = (expiration - day).days
        for strike in strikes:
            for right in ("C", "P"):
                call = right == "C"
                price = bs.price(spot, strike, dte / 365, float(vol), call=call)
                g = bs.greeks(spot, strike, dte / 365, float(vol), call=call)
                rows.append(
                    {
                        "snapshot_at": pd.Timestamp(snapshot_at),
                        "underlying": underlying,
                        "contract": format_occ(underlying, expiration, right, strike),
                        "expiration": expiration,
                        "right": right,
                        "strike": strike,
                        "dte": dte,
                        "underlying_price": spot,
                        "bid": round(max(price * (1 - half_spread), 0.0), 2),
                        "ask": round(max(price * (1 + half_spread), 0.01), 2),
                        "implied_volatility": float(vol),
                        "delta": g.delta,
                        "open_interest": open_interest,
                        "feed": "synthetic",
                        "ingested_at": pd.Timestamp(snapshot_at),
                        "run_id": f"syn-{day}",
                    }
                )
    return conform(pd.DataFrame(rows), RAW_OPTION_CHAIN_SCHEMA)
