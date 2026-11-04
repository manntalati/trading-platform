"""Intraday bars (5-minute by default), regular hours only.

``tp-data bars intraday`` stores Alpaca's minute bars like the daily ones: an immutable raw file
per run (``raw/alpaca/stock_bars_5m``, ``RAW_BARS_SCHEMA``, timestamps are each bar's start in
UTC), and ``clean/stock_bars_5m/symbol=<SYM>/part.parquet`` rebuilt from all of them, the latest
download winning per symbol and bar. Only bars that start between 9:30 and 16:00 New York are
kept. Prices are raw (not adjusted for splits or dividends): fine within a day, which is all
intraday research here needs.

``provisional_bars`` reads today's bar so far from the underlyings' snapshots the option chain
snapshots store (``raw/alpaca/stock_snapshots``: Alpaca's dailyBar and last trade), for rules
that act before the close on the session they fire (strategy 16 at 15:45).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time

import pandas as pd

from tp_core.calendar import NEW_YORK
from tp_core.schemas import RAW_BARS_SCHEMA, RAW_STOCK_SNAPSHOT_SCHEMA, RAW_STOCK_SNAPSHOTS
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


def merge_clean_intraday(lake: Lake, bars: pd.DataFrame, minutes: int = 5) -> None:
    """Add freshly downloaded raw ``bars`` (``RAW_BARS_SCHEMA``) to the clean table, one symbol
    at a time, the newer download winning per bar: the same table ``build_clean_intraday``
    makes, without reading every raw file (years of bars for many symbols don't fit in
    memory at once)."""
    base = lake.clean_dir(CLEAN_DATASET.format(minutes=minutes))
    for symbol, fresh in regular_hours(bars).groupby("symbol"):
        path = base / f"symbol={symbol}"
        old = read_parquet_dir(path, RAW_BARS_SCHEMA)
        both = pd.concat([f for f in (old, fresh) if len(f)], ignore_index=True)
        out = (
            both.sort_values("ingested_at", kind="stable")
            .drop_duplicates(["symbol", "timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        write_parquet(out, path / "part.parquet", RAW_BARS_SCHEMA)


def stored_sessions(lake: Lake, symbol: str, minutes: int = 5) -> set[date]:
    """The New York dates the clean table has bars of ``symbol`` for."""
    path = lake.clean_dir(CLEAN_DATASET.format(minutes=minutes)) / f"symbol={symbol}"
    df = read_parquet_dir(path, RAW_BARS_SCHEMA)
    if df.empty:
        return set()
    stamps = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NEW_YORK)
    return set(stamps.dt.date)


def intraday_symbols(lake: Lake, minutes: int = 5) -> list[str]:
    """The symbols the clean table has bars for."""
    base = lake.clean_dir(CLEAN_DATASET.format(minutes=minutes))
    return sorted(d.name.split("=", 1)[1] for d in base.glob("symbol=*")) if base.exists() else []


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


def provisional_bars(lake: Lake, day: date, symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """Today's bar so far per symbol (index), from the latest underlying snapshot of ``day``:
    ``open``, ``high``, ``low`` (the session's trades so far on the quotes feed), ``close`` (the
    last trade, else the bid-ask middle), ``prev_close`` and ``at`` (when the
    snapshot was taken). Only snapshots whose daily bar is ``day``'s session; empty when there
    are none."""
    path = lake.raw_dir(RAW_STOCK_SNAPSHOTS) / f"snapshot_date={day.isoformat()}"
    df = read_parquet_dir(path, RAW_STOCK_SNAPSHOT_SCHEMA)
    columns = ["open", "high", "low", "close", "prev_close", "at"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    if symbols is not None:
        df = df[df["symbol"].isin(list(symbols))]
    bar_day = pd.to_datetime(df["day_at"], utc=True).dt.tz_convert(NEW_YORK).dt.date
    df = df[bar_day == day]
    df = df.sort_values("snapshot_at").drop_duplicates("symbol", keep="last")
    close = df["price"].fillna((df["bid"] + df["ask"]) / 2)
    out = pd.DataFrame(
        {
            "open": df["day_open"].to_numpy(),
            "high": pd.concat([df["day_high"], close], axis=1).max(axis=1).to_numpy(),
            "low": pd.concat([df["day_low"], close], axis=1).min(axis=1).to_numpy(),
            "close": close.to_numpy(),
            "prev_close": df["prev_close"].to_numpy(),
            "at": df["snapshot_at"].to_numpy(),
        },
        index=pd.Index(df["symbol"], name="symbol"),
    )
    return out[out[["open", "high", "low", "close"]].notna().all(axis=1)]
