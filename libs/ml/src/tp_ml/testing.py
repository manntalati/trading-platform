"""Builders for synthetic bars and lakes (used by tests): random walks, never real prices."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tp_core.bars import build_clean_bars
from tp_core.schemas import RAW_BARS_SCHEMA, RAW_STOCK_BARS_1D, conform
from tp_core.storage import Lake, write_raw
from tp_core.testing import raw_bars

NOW = datetime(2024, 1, 2, 23, 0, tzinfo=UTC)


def random_bars(
    symbols: list[str], n: int = 600, seed: int = 0, start: str = "2020-01-02"
) -> dict[str, pd.DataFrame]:
    """Wide OHLCV random walks (daily vol ~1.5%) on business days."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(start, periods=n)
    rets = rng.normal(0.0003, 0.015, size=(n, len(symbols)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=index, columns=symbols)
    gap = pd.DataFrame(rng.normal(0, 0.004, size=close.shape), index=index, columns=symbols)
    open_ = close.shift(1).fillna(close) * (1 + gap)
    spread = pd.DataFrame(rng.uniform(0.002, 0.02, size=close.shape), index=index, columns=symbols)
    high = open_.where(open_ >= close, close) * (1 + spread / 2)
    low = open_.where(open_ <= close, close) * (1 - spread / 2)
    volume = pd.DataFrame(
        rng.lognormal(14, 0.3, size=close.shape), index=index, columns=symbols
    ).round()
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def write_lake(root: Path, symbols: list[str], n: int = 420, seed: int = 0) -> Lake:
    """A lake with clean daily bars: random walks for ``symbols`` from 2022."""
    lake = Lake(root)
    rng = np.random.default_rng(seed)
    frames = []
    for symbol in symbols:
        closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, size=n)))
        frames.append(
            raw_bars(symbol, [round(float(c), 4) for c in closes], start=date(2022, 1, 3))
        )
    write_raw(
        lake,
        RAW_STOCK_BARS_1D,
        conform(pd.concat(frames), RAW_BARS_SCHEMA),
        run_id="r1",
        partition={"ingest_date": "x"},
        schema=RAW_BARS_SCHEMA,
    )
    build_clean_bars(lake, now=NOW)
    return lake
