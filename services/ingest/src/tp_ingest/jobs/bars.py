"""Daily bars jobs: backfill history, append the latest sessions, rebuild the clean table."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from tp_core.bars import build_clean_bars, read_raw_bars
from tp_core.calendar import last_completed_session, sessions_back
from tp_core.schemas import (
    RAW_BARS_SCHEMA,
    RAW_CORPORATE_ACTIONS,
    RAW_CORPORATE_ACTIONS_SCHEMA,
    RAW_STOCK_BARS_1D,
    conform,
)
from tp_core.storage import Lake, new_run_id, write_raw
from tp_core.validate import ValidationReport
from tp_ingest.sources.base import MarketDataSource

log = logging.getLogger(__name__)

DEFAULT_BACKFILL_YEARS = 5
# Re-fetch this many recent sessions on every daily run so late vendor corrections land too.
DEFAULT_OVERLAP_SESSIONS = 5


@dataclass
class BarsJobResult:
    run_id: str
    start: date | None
    end: date
    bars_fetched: int = 0
    actions_fetched: int = 0
    files: list[Path] = field(default_factory=list)
    report: ValidationReport = field(default_factory=ValidationReport)


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # Feb 29 → Feb 28
        return day.replace(year=day.year - years, day=28)


def run_backfill(
    lake: Lake,
    source: MarketDataSource,
    symbols: Sequence[str],
    *,
    now: datetime,
    years: int = DEFAULT_BACKFILL_YEARS,
) -> BarsJobResult:
    """Fetch ``years`` of history up to the last completed session, then rebuild clean."""
    end = last_completed_session(now)
    start = years_before(end, years)
    run_id = new_run_id(now)
    result = BarsJobResult(run_id=run_id, start=start, end=end)
    _fetch_and_store(lake, source, symbols, start, end, now=now, result=result)
    result.report = build_clean_bars(lake, now=now)
    return result


def run_daily(
    lake: Lake,
    source: MarketDataSource,
    symbols: Sequence[str],
    *,
    now: datetime,
    overlap_sessions: int = DEFAULT_OVERLAP_SESSIONS,
    new_symbol_years: int = DEFAULT_BACKFILL_YEARS,
) -> BarsJobResult:
    """Fetch recent sessions for every symbol, then rebuild clean.

    The window starts ``overlap_sessions`` back, or earlier if some symbol's last stored bar is
    older (a missed run is caught up automatically). Symbols with no history at all — newly
    added to the universe — are backfilled ``new_symbol_years``.
    """
    end = last_completed_session(now)
    run_id = new_run_id(now)
    result = BarsJobResult(run_id=run_id, start=None, end=end)

    last_stored = _last_stored_session(lake)
    existing = [s for s in symbols if s in last_stored]
    new = [s for s in symbols if s not in last_stored]

    if existing:
        start = sessions_back(end, overlap_sessions)[0]
        oldest_last = min(last_stored[s] for s in existing)
        start = min(start, oldest_last + timedelta(days=1))
        result.start = start
        if start <= end:
            _fetch_and_store(lake, source, existing, start, end, now=now, result=result)
    if new:
        start_new = years_before(end, new_symbol_years)
        log.info("backfilling %d new symbol(s) from %s: %s", len(new), start_new, new)
        result.start = start_new if result.start is None else min(result.start, start_new)
        _fetch_and_store(lake, source, new, start_new, end, now=now, result=result)

    result.report = build_clean_bars(lake, now=now)
    return result


def _last_stored_session(lake: Lake) -> dict[str, date]:
    raw = read_raw_bars(lake)
    if raw.empty:
        return {}
    return {str(k): v for k, v in raw.groupby("symbol")["session"].max().items()}


def _fetch_and_store(
    lake: Lake,
    source: MarketDataSource,
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    now: datetime,
    result: BarsJobResult,
) -> None:
    log.info("fetching %s bars for %d symbols, %s..%s", source.name, len(symbols), start, end)
    bars = source.daily_bars(symbols, start, end)
    actions = source.corporate_actions(symbols, start, end)
    ingest_date = now.date().isoformat()
    partition = {"ingest_date": ingest_date}

    if not bars.empty:
        stamped = bars.assign(
            feed=source.feed, adjustment="raw", ingested_at=pd.Timestamp(now), run_id=result.run_id
        )
        result.files.append(
            write_raw(
                lake,
                RAW_STOCK_BARS_1D,
                conform(stamped, RAW_BARS_SCHEMA),
                run_id=_part_id(result),
                partition=partition,
                schema=RAW_BARS_SCHEMA,
            )
        )
    if not actions.empty:
        stamped = actions.assign(ingested_at=pd.Timestamp(now), run_id=result.run_id)
        result.files.append(
            write_raw(
                lake,
                RAW_CORPORATE_ACTIONS,
                conform(stamped, RAW_CORPORATE_ACTIONS_SCHEMA),
                run_id=_part_id(result),
                partition=partition,
                schema=RAW_CORPORATE_ACTIONS_SCHEMA,
            )
        )
    result.bars_fetched += len(bars)
    result.actions_fetched += len(actions)
    log.info("stored %d bars, %d corporate actions", len(bars), len(actions))


def _part_id(result: BarsJobResult) -> str:
    """One run may write several files per dataset (e.g. daily + new-symbol backfill)."""
    return f"{result.run_id}-{len(result.files):02d}"
