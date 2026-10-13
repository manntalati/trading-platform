"""Intraday bars (5-minute by default), regular hours only.

``tp-data bars intraday`` stores Alpaca's minute bars like the daily ones: an immutable raw file
per run (``raw/alpaca/stock_bars_5m``, ``RAW_BARS_SCHEMA``, timestamps are each bar's start in
UTC), and ``clean/stock_bars_5m/symbol=<SYM>/part.parquet`` rebuilt from all of them, the latest
download winning per symbol and bar. Only bars that start between 9:30 and 16:00 New York are
kept. Prices are raw (not adjusted for splits or dividends): fine within a day, which is all
intraday research here needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time

import pandas as pd

from tp_core.calendar import NEW_YORK
from tp_core.schemas import RAW_BARS_SCHEMA
from tp_core.storage import Lake, read_parquet_dir, write_parquet

RAW_DATASET = "alpaca/stock_bars_{minutes}m"
CLEAN_DATASET = "stock_bars_{minutes}m"
OPEN, CLOSE = time(9, 30), time(16, 0)
COLUMNS = ("symbol", "timestamp", "session", "open", "high", "low", "close", "volume", "vwap")


def regular_hours(bars: pd.DataFrame) -> pd.DataFrame:
    """Only bars starting from 9:30 up to (not including) 16:00 New York."""
    local = pd.to_datetime(bars["timestamp"], utc=True).dt.tz_convert(NEW_YORK)
    clock = local.dt.time
    out: pd.DataFrame = bars[(clock >= OPEN) & (clock < CLOSE)]
    return out


def build_clean_intraday(lake: Lake, minutes: int = 5) -> pd.DataFrame:
    raw = read_parquet_dir(lake.raw_dir(RAW_DATASET.format(minutes=minutes)), RAW_BARS_SCHEMA)
    if raw.empty:
        return raw
    clean = (
        regular_hours(raw)
        .sort_values("ingested_at")
        .drop_duplicates(["symbol", "timestamp"], keep="last")
        .sort_values(["symbol", "timestamp"])
        .reset_index(drop=True)
    )
    base = lake.clean_dir(CLEAN_DATASET.format(minutes=minutes))
    for symbol, group in clean.groupby("symbol"):
        write_parquet(group, base / f"symbol={symbol}" / "part.parquet", RAW_BARS_SCHEMA)
    return clean


def load_intraday(
    lake: Lake,
    symbols: Iterable[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    minutes: int = 5,
) -> pd.DataFrame:
    """Regular-hours bars with ``session`` (the New York date), sorted by symbol and time."""
    base = lake.clean_dir(CLEAN_DATASET.format(minutes=minutes))
    if symbols is None:
        df = read_parquet_dir(base, RAW_BARS_SCHEMA)
    else:
        parts = [read_parquet_dir(base / f"symbol={s}", RAW_BARS_SCHEMA) for s in symbols]
        parts = [p for p in parts if len(p)]
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if df.empty:
        return pd.DataFrame(columns=list(COLUMNS))
    stamps = pd.to_datetime(df["timestamp"], utc=True)
    df = df.assign(timestamp=stamps, session=stamps.dt.tz_convert(NEW_YORK).dt.date)
    if start is not None:
        df = df[df["session"] >= start]
    if end is not None:
        df = df[df["session"] <= end]
    out: pd.DataFrame = df[list(COLUMNS)].sort_values(["symbol", "timestamp"])
    return out.reset_index(drop=True)
