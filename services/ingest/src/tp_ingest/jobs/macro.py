"""Macro series: VIX and Treasury yields (``tp_core.macro``; sources in
``tp_ingest.sources.macro``).

``backfill`` downloads each series from 1990; the daily run the last 30 days (a few days of
overlap catch late publication). Each run writes one immutable raw file with every series it
got, then rebuilds ``clean/macro_1d``. A series that fails doesn't stop the others; the run
reports it and exits non-zero.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core.macro import DATASET, FIRST_DATE, SCHEMA, SERIES, build_clean_macro
from tp_core.storage import Lake, new_run_id, write_json, write_raw
from tp_ingest.sources.macro import MacroSource, MacroUnavailableError

__all__ = ["MacroJobResult", "MacroSource", "run_backfill", "run_macro"]

log = logging.getLogger(__name__)
DAILY_LOOKBACK_DAYS = 30


@dataclass
class MacroJobResult:
    run_id: str
    start: date
    rows: dict[str, int] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # which source served each series
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = []
        for s, n in self.rows.items():
            via = f", from {self.sources[s]}" if s in self.sources else ""
            parts.append(f"{s} {n} (to {self.latest.get(s)}{via})")
        got = ", ".join(parts)
        failed = "".join(f"\n  FAILED {s}: {e}" for s, e in self.errors.items())
        return f"run {self.run_id}: from {self.start}: {got or 'nothing'}{failed}"

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ok": self.ok, "path": str(self.path) if self.path else None}


def run_macro(
    lake: Lake,
    source: MacroSource,
    *,
    now: datetime,
    start: date | None = None,
    series: Sequence[str] = tuple(SERIES),
) -> MacroJobResult:
    """Download ``series`` from ``start`` (default: the last 30 days), store, rebuild clean."""
    start = start or (now.date() - timedelta(days=DAILY_LOOKBACK_DAYS))
    result = MacroJobResult(run_id=new_run_id(now), start=start)
    frames = []
    for s in series:
        try:
            df = source.series(s, start)
        except Exception as exc:  # one series failing doesn't stop the rest
            log.warning("macro series %s failed: %s", s, exc)
            plain = isinstance(exc, MacroUnavailableError)  # already says what each source said
            result.errors[s] = str(exc) if plain else f"{type(exc).__name__}: {exc}"
            continue
        result.rows[s] = len(df)
        used = getattr(source, "used", {})
        if s in used:
            result.sources[s] = used[s]
        if len(df):
            result.latest[s] = str(max(df["date"]))
            frames.append(df)
    if frames:
        raw = pd.concat(frames, ignore_index=True)
        raw["ingested_at"] = pd.Timestamp(now).tz_convert("UTC")
        result.path = write_raw(
            lake,
            DATASET,
            raw[list(SCHEMA.names)],
            run_id=result.run_id,
            partition={"ingest_date": now.date().isoformat()},
            schema=SCHEMA,
        )
        build_clean_macro(lake)
    write_json(result.to_dict(), lake.reports_dir("macro") / f"{now.date().isoformat()}.json")
    return result


def run_backfill(lake: Lake, source: MacroSource, *, now: datetime) -> MacroJobResult:
    return run_macro(lake, source, now=now, start=FIRST_DATE)
