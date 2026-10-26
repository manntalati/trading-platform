"""Real prices for the playbook's backtest: find the listed contract nearest each one the backtest
asked for, and fetch its daily bars (``tp_core.option_history``).

Each request (from ``tp-recs calibrate``) has a target expiry and strike. The contracts endpoint
lists every contract of the underlying and side, expired or not, in one pass per underlying and
side; the match is the nearest expiry within ``EXPIRY_SLACK`` days of the target, then the
nearest strike within ``STRIKE_SLACK`` of it, among standard contracts only: after a split or
a special dividend the old contracts are renamed (``1RIVN…``, ``AAPL1…``) and deliver something
other than 100 shares, so their prices don't compare, and the bars endpoint refuses them.

Requests already matched are skipped, so a run only fetches what's new; a request with nothing
close enough is recorded unmatched and not retried. A contract whose bars request failed is
retried on the next run, and a failing batch is split until the one contract at fault is found,
so it can't take the others down with it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
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
    retried: int = 0  # matched before, bars fetched again
    bars: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"run {self.run_id}: {self.requests} contracts wanted, {self.new} new; "
            f"{self.matched} matched, {self.unmatched} with nothing close, {self.retried} "
            f"retried; {self.bars} daily bars; {len(self.errors)} failed"
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def standard(contract: str, underlying: str) -> bool:
    """An ordinary contract on ``underlying`` (OCC root = the ticker without its dot), not one
    adjusted for a corporate action."""
    root = re.escape(underlying.replace(".", ""))
    return re.fullmatch(rf"{root}\d{{6}}[CP]\d{{8}}", contract) is not None


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


def _sort_old(old: pd.DataFrame, bars: pd.DataFrame) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """The requests settled by earlier runs (unmatched, or matched to a standard contract whose
    bars came), and those matched to a contract whose bars never did (their match rows). A
    match to a non-standard contract (before those were left out) is neither: matched again."""
    have = set(bars["contract"]) if len(bars) else set()
    settled: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    for row in old.to_dict("records"):
        contract = row["contract"]
        if contract is None or pd.isna(contract):
            settled.add(str(row["key"]))
        elif not standard(str(contract), str(row["underlying"])):
            continue
        elif row.get("bars_fetched") is True or contract in have:
            settled.add(str(row["key"]))
        else:
            pending[str(row["key"])] = {str(k): v for k, v in row.items()}
    return settled, pending


def _widen(windows: dict[str, tuple[date, date]], contract: str, r: dict[str, Any]) -> None:
    start, end = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
    lo, hi = windows.get(contract, (start, end))
    windows[contract] = (min(lo, start), max(hi, end))


def _fetch_splitting(
    batch: list[str], fetch: Callable[[list[str]], None], errors: dict[str, str]
) -> None:
    """Fetch ``batch``; if it fails, each half, down to the one contract at fault."""
    try:
        fetch(batch)
    except Exception as exc:
        if len(batch) == 1:
            log.warning("option bars for %s failed: %s", batch[0], exc)
            errors[f"bars {batch[0]}"] = f"{type(exc).__name__}: {exc}"
            return
        half = len(batch) // 2
        _fetch_splitting(batch[:half], fetch, errors)
        _fetch_splitting(batch[half:], fetch, errors)


def run_option_history(
    lake: Lake,
    source: OptionHistorySource,
    requests: Sequence[dict[str, Any]],
    *,
    now: datetime,
    limit: int | None = None,
) -> HistoryResult:
    """Match and fetch the ``requests`` not matched yet (at most ``limit`` of them), and the bars
    of contracts matched before whose bars never arrived."""
    result = HistoryResult(new_run_id(now), requests=len(requests))
    old = option_history.load_matches(lake)
    unique = {r["key"]: r for r in requests}
    settled, pending = _sort_old(old, option_history.load_bars(lake))
    todo = [r for k, r in unique.items() if k not in settled and k not in pending]
    todo = todo[:limit] if limit is not None else todo
    result.new = len(todo)
    stamp = pd.Timestamp(now)
    matches: list[dict[str, Any]] = []
    windows: dict[str, tuple[date, date]] = {}
    for key, row in pending.items():  # matched before; their bars still to fetch
        if key in unique:
            matches.append(row)
            _widen(windows, str(row["contract"]), unique[key])
            result.retried += 1
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
        listed = listed[[standard(str(c), underlying) for c in listed["contract"]]]
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
                    "bars_fetched": None,
                }
            )
            if contract is None:
                result.unmatched += 1
                continue
            result.matched += 1
            _widen(windows, contract, r)
    frames: list[pd.DataFrame] = []
    fetched: set[str] = set()
    today = now.astimezone(NEW_YORK).date()
    contracts = sorted(windows)

    def fetch(batch: list[str]) -> None:
        start = min(windows[c][0] for c in batch)
        end = min(max(windows[c][1] for c in batch), today)
        frames.append(source.option_bars(batch, start, end))
        fetched.update(batch)

    for i in range(0, len(contracts), BATCH):
        _fetch_splitting(contracts[i : i + BATCH], fetch, result.errors)
    for m in matches:
        if m["contract"] in fetched:
            m["bars_fetched"] = True
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
