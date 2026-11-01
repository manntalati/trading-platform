"""Intraday bars (``tp_core.intraday``): the last ``days`` sessions of ``minutes``-minute bars
for intraday research (the opening-range breakout, the first candle against the EMA).

Years of 5-minute bars for many symbols are millions of rows, so the job asks for them a few
symbols and ``WINDOW`` sessions at a time: each request's bars go to their own raw file and into
the clean table before the next, a request that fails is reported and the rest go on, and with
``resume`` the pieces already stored are skipped, so an interrupted download picks up where it
stopped. Re-downloading a window is harmless: the clean table keeps the latest download of each
bar."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tp_core.calendar import last_completed_session, sessions_back
from tp_core.intraday import RAW_DATASET, merge_clean_intraday, regular_hours, stored_sessions
from tp_core.schemas import RAW_BARS_SCHEMA
from tp_core.storage import Lake, new_run_id, write_raw
from tp_ingest.sources.base import IntradaySource

log = logging.getLogger(__name__)
BATCH = 10  # symbols a request
WINDOW = 60  # sessions a request


@dataclass
class IntradayJobResult:
    run_id: str
    bars: int
    sessions: int
    symbols: list[str]
    skipped: int = 0  # requests not made: already stored (resume)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        names = (
            ", ".join(self.symbols) if len(self.symbols) <= 12 else f"{len(self.symbols)} symbols"
        )
        parts = [f"run {self.run_id}: {self.bars} bars, {self.sessions} sessions, {names}"]
        if self.skipped:
            parts.append(f"{self.skipped} pieces already stored")
        if self.errors:
            parts.append(f"{len(self.errors)} requests failed (run it again with --resume)")
        return "; ".join(parts)


def run_intraday(
    lake: Lake,
    source: IntradaySource,
    symbols: Sequence[str],
    *,
    now: datetime,
    days: int = 60,
    minutes: int = 5,
    resume: bool = False,
) -> IntradayJobResult:
    end = last_completed_session(now)
    window = sessions_back(end, days)
    run_id = new_run_id(now)
    result = IntradayJobResult(run_id, 0, len(window), [])
    have = {s: stored_sessions(lake, s, minutes) for s in symbols} if resume else {}
    got: set[str] = set()
    piece = 0
    for i in range(0, len(symbols), BATCH):
        for j in range(0, len(window), WINDOW):
            span = window[j : j + WINDOW]
            names = [s for s in symbols[i : i + BATCH] if not set(span) <= have.get(s, set())]
            if not names:
                result.skipped += 1
                continue
            piece += 1
            try:
                bars = regular_hours(source.intraday_bars(names, span[0], span[-1], minutes))
            except Exception as exc:  # one failed request must not lose the rest
                log.exception("intraday bars for %s, %s to %s failed", names, span[0], span[-1])
                result.errors[f"{','.join(names)} {span[0]}..{span[-1]}"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if bars.empty:
                continue
            out = bars.assign(feed=source.feed, adjustment="raw", ingested_at=now, run_id=run_id)
            out = out[list(RAW_BARS_SCHEMA.names)]
            write_raw(
                lake,
                RAW_DATASET.format(minutes=minutes),
                out,
                run_id=f"{run_id}-{piece:04d}",
                partition={"ingest_date": now.date().isoformat()},
                schema=RAW_BARS_SCHEMA,
            )
            merge_clean_intraday(lake, out, minutes)
            result.bars += len(bars)
            got |= set(bars["symbol"])
            log.info("%s to %s: %d bars for %s", span[0], span[-1], len(bars), ", ".join(names))
    result.symbols = sorted(got)
    return result
