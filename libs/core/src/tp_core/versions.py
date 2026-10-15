"""What a result was computed from: the code (git commit) and the data (rows, sessions, the
latest ingest). Every saved backtest, validation and model run records both."""

from __future__ import annotations

import subprocess
from typing import Any

import pandas as pd


def data_version(bars: pd.DataFrame) -> dict[str, Any]:
    """Enough to tell whether two runs saw the same data."""
    if bars.empty:
        return {"rows": 0}
    return {
        "rows": len(bars),
        "symbols": int(bars["symbol"].nunique()),
        "first_session": str(bars["session"].min()),
        "last_session": str(bars["session"].max()),
        "latest_ingest": str(bars["ingested_at"].max()),
    }


def git_version() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(dirty)}
