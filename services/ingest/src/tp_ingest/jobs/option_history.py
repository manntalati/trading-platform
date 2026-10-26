"""Real prices for the playbook's backtest: find the listed contract nearest each one the backtest
asked for, and fetch its daily bars (``tp_core.option_history``).

Each request (from ``tp-recs calibrate``) has a target expiry and strike. The contracts endpoint
lists every contract of the underlying and side, expired or not, in one pass per underlying and
side; the match is the nearest expiry within ``EXPIRY_SLACK`` days of the target, then the
nearest strike within ``STRIKE_SLACK`` of it. Requests already matched are skipped, so a run only
fetches what's new; a request with nothing close enough is recorded unmatched and not retried.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from tp_core import option_history
from tp_core.calendar import NEW_YORK
from tp_core.schemas import conform
from tp_core.storage import Lake, new_run_id, write_json, write_raw
from tp_ingest.sources.base import OptionHistorySource

log = logging.getLogger(__name__)
EXPIRY_SLACK = 35  # days
STRIKE_SLACK = 0.10  # of the target strike
BATCH = 100  # contracts per bars request


@dataclass
class HistoryResult:
    run_id: str
    requests: int = 0
    new: int = 0
    matched: int = 0
    unmatched: int = 0
    bars: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"run {self.run_id}: {self.requests} contracts wanted, {self.new} new; "
            f"{self.matched} matched, {self.unmatched} with nothing close; {self.bars} daily "
            f"bars; {len(self.errors)} failed"
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def match(listed: pd.DataFrame, expiration: date, strike: float) -> pd.Series | None:
    """The listed contract nearest the target expiry, then strike; None if none is close."""
    if listed.empty:
        return None
    gap = (pd.to_datetime(listed["expiration"]) - pd.Timestamp(expiration)).dt.days.abs()
    near = listed[gap <= EXPIRY_SLACK]
    if near.empty:
        return None
    gap = gap[near.index]
    near = near[gap == gap.min()]
    off = (near["strike"] - strike).abs()
    best: pd.Series = near.loc[off.idxmin()]  # type: ignore[call-overload]
    return best if abs(float(best["strike"]) - strike) <= STRIKE_SLACK * strike else None


def run_option_history(
    lake: Lake,
    source: OptionHistorySource,
    requests: Sequence[dict[str, Any]],
    *,
    now: datetime,
    limit: int | None = None,
) -> HistoryResult:
    """Match and fetch the ``requests`` not matched yet (at most ``limit`` of them)."""
    result = HistoryResult(new_run_id(now), requests=len(requests))
    known = set(option_history.load_matches(lake)["key"])
    todo = [r for r in {r["key"]: r for r in requests}.values() if r["key"] not in known]
    todo = todo[:limit] if limit is not None else todo
    result.new = len(todo)
    stamp = pd.Timestamp(now)
    matches: list[dict[str, Any]] = []
    windows: dict[str, tuple[date, date]] = {}
    by_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in todo:
        by_side.setdefault((r["underlying"], r["right"]), []).append(r)
    for (underlying, right), group in by_side.items():
        targets = [date.fromisoformat(r["expiration"]) for r in group]
        strikes = [float(r["strike"]) for r in group]
        try:
            listed = source.listed_contracts(
                underlying,
                right=right,
                expiration=(
                    min(targets) - timedelta(days=EXPIRY_SLACK),
                    max(targets) + timedelta(days=EXPIRY_SLACK),
                ),
                strikes=(min(strikes) * (1 - STRIKE_SLACK), max(strikes) * (1 + STRIKE_SLACK)),
            )
        except Exception as exc:  # one underlying failing must not lose the others
            log.exception("listing %s %s contracts failed", underlying, right)
            result.errors[f"{underlying} {right}"] = f"{type(exc).__name__}: {exc}"
            continue
        for r, target, k in zip(group, targets, strikes, strict=True):
            found = match(listed, target, k)
            contract = str(found["contract"]) if found is not None else None
            matches.append(
                {
                    "key": r["key"],
                    "contract": contract,
                    "underlying": underlying,
                    "right": right,
                    "expiration": found["expiration"] if found is not None else None,
                    "strike": float(found["strike"]) if found is not None else None,
                    "matched_at": stamp,
                }
            )
            if contract is None:
                result.unmatched += 1
                continue
            result.matched += 1
            start, end = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
            lo, hi = windows.get(contract, (start, end))
            windows[contract] = (min(lo, start), max(hi, end))
    frames = []
    today = now.astimezone(NEW_YORK).date()
    contracts = sorted(windows)
    for i in range(0, len(contracts), BATCH):
        batch = contracts[i : i + BATCH]
        start = min(windows[c][0] for c in batch)
        end = min(max(windows[c][1] for c in batch), today)
        try:
            frames.append(source.option_bars(batch, start, end))
        except Exception as exc:
            log.exception("option bars for %d contracts failed", len(batch))
            result.errors[f"bars {batch[0]}..{batch[-1]}"] = f"{type(exc).__name__}: {exc}"
    bars = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(bars):
        bars = conform(bars.assign(ingested_at=stamp), option_history.OPTION_BARS_SCHEMA)
        write_raw(
            lake,
            option_history.RAW_OPTION_BARS,
            bars,
            run_id=result.run_id,
            partition={"ingest_date": today.isoformat()},
            schema=option_history.OPTION_BARS_SCHEMA,
        )
        option_history.save_bars(lake, bars)
    result.bars = len(bars)
    if matches:
        option_history.save_matches(
            lake, conform(pd.DataFrame(matches), option_history.MATCHES_SCHEMA)
        )
    write_json(
        result.to_dict(), lake.reports_dir("option_history") / f"{today}-{result.run_id}.json"
    )
    log.info(result.summary())
    return result
