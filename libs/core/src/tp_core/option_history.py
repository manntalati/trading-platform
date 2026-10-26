"""Daily bars of past option contracts: real prices to check the playbook's backtest against.

The backtest (``tp-recs calibrate``) lists the contracts its trades since February 2024 would
have used (a target expiry and strike per trade); ``tp-data options history`` finds the listed
contract nearest each and fetches its daily bars from Alpaca:

    reports/playbook/wanted_contracts.json        what the backtest asked for
    raw/alpaca/option_bars_1d/ingest_date=.../    the bars as fetched (immutable)
    clean/option_bars_1d/part.parquet             one row per contract and session
    clean/option_history_contracts/part.parquet   which contract each request was matched to

Bars are trades, not quotes: a session with no trade has no bar, and the close is the last
trade, which can sit anywhere inside the spread.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pyarrow as pa

from tp_core.schemas import conform
from tp_core.storage import Lake, read_parquet_dir, write_parquet

WANTED = ("playbook", "wanted_contracts.json")  # under reports/
RAW_OPTION_BARS = "alpaca/option_bars_1d"
OPTION_BARS = "option_bars_1d"
MATCHES = "option_history_contracts"
OPTION_BARS_SCHEMA = pa.schema(
    [
        ("contract", pa.string()),
        ("session", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("trade_count", pa.float64()),
        ("vwap", pa.float64()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)
MATCHES_SCHEMA = pa.schema(
    [
        ("key", pa.string()),  # underlying|right|target expiration|target strike
        ("contract", pa.string()),  # the listed contract (OCC symbol); null: none close enough
        ("underlying", pa.string()),
        ("right", pa.string()),
        ("expiration", pa.date32()),
        ("strike", pa.float64()),
        ("matched_at", pa.timestamp("us", tz="UTC")),
        ("bars_fetched", pa.bool_()),  # its bars were asked for (and answered); else retried
    ]
)


def wanted(lake: Lake) -> list[dict[str, Any]]:
    """The requests the last calibration wrote (``key``, ``underlying``, ``right``,
    ``expiration``, ``strike``, ``start``, ``end``)."""
    path = lake.reports_dir(WANTED[0]) / WANTED[1]
    if not path.exists():
        return []
    found: list[dict[str, Any]] = json.loads(path.read_text()).get("contracts") or []
    return found


def load_bars(lake: Lake) -> pd.DataFrame:
    return read_parquet_dir(lake.clean_dir(OPTION_BARS), OPTION_BARS_SCHEMA)


def load_matches(lake: Lake) -> pd.DataFrame:
    return read_parquet_dir(lake.clean_dir(MATCHES), MATCHES_SCHEMA)


def save_bars(lake: Lake, bars: pd.DataFrame) -> None:
    """Merge ``bars`` into the clean table (a contract and session fetched again: the latest)."""
    old = load_bars(lake)
    frames = [f for f in (old, bars[[f.name for f in OPTION_BARS_SCHEMA]]) if len(f)]
    if not frames:
        return
    out = pd.concat(frames, ignore_index=True).sort_values("ingested_at")
    out = out.drop_duplicates(["contract", "session"], keep="last")
    out = out.sort_values(["contract", "session"]).reset_index(drop=True)
    write_parquet(out, lake.clean_dir(OPTION_BARS) / "part.parquet", OPTION_BARS_SCHEMA)


def save_matches(lake: Lake, matches: pd.DataFrame) -> None:
    """Merge ``matches`` in (a request matched again keeps the latest); missing columns are
    null."""
    old = load_matches(lake)
    frames = [f for f in (old, conform(matches, MATCHES_SCHEMA)) if len(f)]
    if not frames:
        return
    out = pd.concat(frames, ignore_index=True).drop_duplicates("key", keep="last")
    write_parquet(
        out.reset_index(drop=True), lake.clean_dir(MATCHES) / "part.parquet", MATCHES_SCHEMA
    )
