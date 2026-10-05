"""Portfolio sync: store a snapshot of balances/positions and new transactions in the lake."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tp_broker.base import BrokerSource
from tp_core.calendar import NEW_YORK
from tp_core.portfolio import read_activities
from tp_core.schemas import (
    RAW_BROKER_ACCOUNTS,
    RAW_BROKER_ACCOUNTS_SCHEMA,
    RAW_BROKER_ACTIVITIES,
    RAW_BROKER_ACTIVITIES_SCHEMA,
    RAW_BROKER_HOLDINGS,
    RAW_BROKER_HOLDINGS_SCHEMA,
    conform,
)
from tp_core.storage import Lake, new_run_id, write_raw

log = logging.getLogger(__name__)

# Re-fetch this much transaction history each sync so late-posting activity is picked up.
ACTIVITY_OVERLAP = timedelta(days=30)


@dataclass
class SyncResult:
    run_id: str
    source: str
    accounts: int = 0
    holdings: int = 0
    activities: int = 0
    files: list[Path] = field(default_factory=list)


def run_sync(lake: Lake, source: BrokerSource, *, now: datetime) -> SyncResult:
    run_id = new_run_id(now)
    result = SyncResult(run_id=run_id, source=source.name)
    snapshot_date = now.astimezone(NEW_YORK).date()
    stamp: dict[str, Any] = {
        "taken_at": pd.Timestamp(now),
        "snapshot_date": snapshot_date,
        "source": source.name,
        "ingested_at": pd.Timestamp(now),
        "run_id": run_id,
    }
    partition = {"source": source.name, "snapshot_date": snapshot_date.isoformat()}

    snap = source.snapshot()
    result.accounts, result.holdings = len(snap.accounts), len(snap.holdings)
    if not snap.accounts.empty:
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_ACCOUNTS,
                conform(snap.accounts.assign(**stamp), RAW_BROKER_ACCOUNTS_SCHEMA),
                run_id=run_id,
                partition=partition,
                schema=RAW_BROKER_ACCOUNTS_SCHEMA,
            )
        )
    if not snap.holdings.empty:
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_HOLDINGS,
                conform(snap.holdings.assign(**stamp), RAW_BROKER_HOLDINGS_SCHEMA),
                run_id=run_id,
                partition=partition,
                schema=RAW_BROKER_HOLDINGS_SCHEMA,
            )
        )

    acts = source.activities(_activities_since(lake, source.name))
    result.activities = len(acts)
    if not acts.empty:
        stamped = acts.assign(source=source.name, ingested_at=pd.Timestamp(now), run_id=run_id)
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_ACTIVITIES,
                conform(stamped, RAW_BROKER_ACTIVITIES_SCHEMA),
                run_id=run_id,
                partition={"source": source.name, "ingest_date": snapshot_date.isoformat()},
                schema=RAW_BROKER_ACTIVITIES_SCHEMA,
            )
        )
    log.info(
        "%s sync: %d accounts, %d holdings, %d activities",
        source.name,
        result.accounts,
        result.holdings,
        result.activities,
    )
    return result


def _activities_since(lake: Lake, source: str) -> date | None:
    acts = read_activities(lake)
    acts = acts[acts["source"] == source] if not acts.empty else acts
    if acts.empty or acts["trade_date"].isna().all():
        return None
    latest: date = acts["trade_date"].dropna().max()
    return latest - ACTIVITY_OVERLAP
