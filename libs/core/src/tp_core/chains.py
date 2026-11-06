"""Read API for stored option chain snapshots (written by ``tp-data options snapshot``)."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from tp_core.calendar import NEW_YORK, is_session, session_close, session_open
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, read_parquet_dir

MIDDAY_FROM = time(11, 0)  # New York; a snapshot before this (after the open) is the morning one
CLOSE_FROM = timedelta(minutes=20)  # before the close: from here it is the closing one ...
CLOSE_UNTIL = timedelta(minutes=30)  # ... until this long after it


def infer_kind(at: datetime) -> str:
    """Which of the day's snapshots one taken at ``at`` is, from the clock alone (a snapshot
    written before ``snapshot_kind`` was stored, or run by hand). It can't tell the late
    snapshot from the closing one: both are taken 15 minutes before the close."""
    local = at.astimezone(NEW_YORK)
    day = local.date()
    if not is_session(day):
        return "after_close"
    if at < session_open(day):
        return "before_open"
    close = session_close(day)
    if at >= close + CLOSE_UNTIL:
        return "after_close"
    if at >= close - CLOSE_FROM:
        return "close"
    return "morning" if local.time() < MIDDAY_FROM else "midday"


def late_runs(lake: Lake) -> set[str]:
    """Run ids of the late snapshots (``tp-data options late``), from their reports: the files
    are ``<snapshot date>-<run id>.json``."""
    return {p.stem[11:] for p in lake.reports_dir("options_late").glob("*.json")}


def load_chain_snapshots(lake: Lake, underlying: str | None = None) -> pd.DataFrame:
    """Every stored snapshot row, optionally for one underlying, sorted by time and contract.

    Snapshots are raw (one file per underlying per run); if a day was snapshotted twice, both
    are returned and ``snapshot_at`` and ``snapshot_kind`` tell them apart (the kind is worked
    out for files written before it was stored).
    """
    base = lake.raw_dir(RAW_OPTION_CHAIN_SNAPSHOTS)
    pattern = f"snapshot_date=*/underlying={underlying or '*'}"
    frames = [read_parquet_dir(d, RAW_OPTION_CHAIN_SCHEMA) for d in sorted(base.glob(pattern))]
    if not frames:
        frames = [read_parquet_dir(base / "__none__", RAW_OPTION_CHAIN_SCHEMA)]
    df = pd.concat(frames, ignore_index=True)
    df = fill_kinds(df, late_runs(lake))
    return df.sort_values(["snapshot_at", "underlying", "contract"]).reset_index(drop=True)


def fill_kinds(df: pd.DataFrame, late: set[str]) -> pd.DataFrame:
    """``snapshot_kind`` where it's missing: ``late`` for the late runs, else from the clock."""
    missing = df["snapshot_kind"].isna()
    if not missing.any():
        return df
    out = df.copy()
    stamps = out.loc[missing, ["snapshot_at", "run_id"]].drop_duplicates()
    kinds = {
        (at, run): "late" if run in late else infer_kind(pd.Timestamp(at).to_pydatetime())
        for at, run in zip(stamps["snapshot_at"], stamps["run_id"], strict=True)
    }
    out.loc[missing, "snapshot_kind"] = [
        kinds[(at, run)]
        for at, run in zip(out.loc[missing, "snapshot_at"], out.loc[missing, "run_id"], strict=True)
    ]
    return out
