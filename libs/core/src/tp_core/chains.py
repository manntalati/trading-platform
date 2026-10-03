"""Read API for stored option chain snapshots (written by ``tp-data options snapshot``)."""

from __future__ import annotations

import pandas as pd

from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, read_parquet_dir


def load_chain_snapshots(lake: Lake, underlying: str | None = None) -> pd.DataFrame:
    """Every stored snapshot row, optionally for one underlying, sorted by time and contract.

    Snapshots are raw (one file per underlying per run); if a day was snapshotted twice, both
    are returned and ``snapshot_at`` tells them apart.
    """
    base = lake.raw_dir(RAW_OPTION_CHAIN_SNAPSHOTS)
    pattern = f"snapshot_date=*/underlying={underlying or '*'}"
    frames = [read_parquet_dir(d, RAW_OPTION_CHAIN_SCHEMA) for d in sorted(base.glob(pattern))]
    if not frames:
        frames = [read_parquet_dir(base / "__none__", RAW_OPTION_CHAIN_SCHEMA)]
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["snapshot_at", "underlying", "contract"]).reset_index(drop=True)
