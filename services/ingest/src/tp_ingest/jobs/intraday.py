"""Intraday bars (``tp_core.intraday``): the last ``days`` sessions of ``minutes``-minute bars
for a few symbols, for intraday research (the opening-range breakout). Re-downloading a window
is harmless: the clean table keeps the latest download of each bar."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tp_core.calendar import last_completed_session, sessions_back
from tp_core.intraday import RAW_DATASET, build_clean_intraday, regular_hours
from tp_core.schemas import RAW_BARS_SCHEMA
from tp_core.storage import Lake, new_run_id, write_raw
from tp_ingest.sources.base import IntradaySource


@dataclass
class IntradayJobResult:
    run_id: str
    bars: int
    sessions: int
    symbols: list[str]

    def summary(self) -> str:
        return (
            f"run {self.run_id}: {self.bars} bars, {self.sessions} sessions, "
            f"{', '.join(self.symbols)}"
        )


def run_intraday(
    lake: Lake,
    source: IntradaySource,
    symbols: Sequence[str],
    *,
    now: datetime,
    days: int = 60,
    minutes: int = 5,
) -> IntradayJobResult:
    end = last_completed_session(now)
    window = sessions_back(end, days)
    run_id = new_run_id(now)
    bars = regular_hours(source.intraday_bars(list(symbols), window[0], end, minutes))
    if len(bars):
        out = bars.assign(
            feed=source.feed,
            adjustment="raw",
            ingested_at=now,
            run_id=run_id,
        )
        write_raw(lake, RAW_DATASET.format(minutes=minutes), out[list(RAW_BARS_SCHEMA.names)],
                  run_id=run_id, partition={"ingest_date": now.date().isoformat()},
                  schema=RAW_BARS_SCHEMA)  # fmt: skip
        build_clean_intraday(lake, minutes)
    return IntradayJobResult(run_id, len(bars), len(window), sorted(set(bars["symbol"])))
