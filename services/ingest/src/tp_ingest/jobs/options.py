"""Daily option chain snapshots: build a proprietary options history from day one.

Free historical chains are rare and incomplete, so every trading day this job stores the full
chain (quotes, last trade, IV, greeks, open interest) for each underlying in the options
universe. One immutable raw file per underlying per day.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core.calendar import NEW_YORK, is_session
from tp_core.occ import parse_occ
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS, conform
from tp_core.storage import Lake, new_run_id, write_json, write_raw
from tp_ingest.sources.base import OptionsSource

log = logging.getLogger(__name__)


@dataclass
class UnderlyingSnapshot:
    underlying: str
    contracts: int = 0
    with_quote: int = 0
    crossed: int = 0  # bid > ask: a broken quote
    missing_iv: int = 0
    path: Path | None = None
    error: str | None = None


@dataclass
class OptionsJobResult:
    run_id: str
    snapshot_date: date
    skipped: str | None = None
    underlyings: list[UnderlyingSnapshot] = field(default_factory=list)

    @property
    def failures(self) -> list[UnderlyingSnapshot]:
        return [u for u in self.underlyings if u.error is not None]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        if self.skipped:
            return f"skipped: {self.skipped}"
        stored = [u for u in self.underlyings if u.error is None]
        return (
            f"{len(stored)}/{len(self.underlyings)} underlyings stored, "
            f"{sum(u.contracts for u in stored)} contracts, "
            f"{sum(u.crossed for u in stored)} crossed quotes, "
            f"{len(self.failures)} failed"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "skipped": self.skipped,
            "ok": self.ok,
            "underlyings": [
                {**u.__dict__, "path": str(u.path) if u.path else None} for u in self.underlyings
            ],
        }


def run_snapshot(
    lake: Lake,
    source: OptionsSource,
    underlyings: Sequence[str],
    *,
    now: datetime,
    max_dte: int = 365,
) -> OptionsJobResult:
    snapshot_date = now.astimezone(NEW_YORK).date()
    result = OptionsJobResult(run_id=new_run_id(now), snapshot_date=snapshot_date)
    if not is_session(snapshot_date):
        result.skipped = f"{snapshot_date} is not an XNYS session"
        log.info(result.skipped)
        return result

    expiration_lte = snapshot_date + timedelta(days=max_dte)
    spots = source.underlying_snapshots(underlyings).set_index("symbol")

    for underlying in underlyings:
        snap = UnderlyingSnapshot(underlying)
        result.underlyings.append(snap)
        try:
            chain = _snapshot_one(source, underlying, expiration_lte, spots, now, result.run_id)
            snap.contracts = len(chain)
            snap.with_quote = int(chain["bid"].notna().sum())
            snap.crossed = int((chain["bid"] > chain["ask"]).sum())
            snap.missing_iv = int(chain["implied_volatility"].isna().sum())
            if chain.empty:
                raise ValueError("empty chain")
            snap.path = write_raw(
                lake,
                RAW_OPTION_CHAIN_SNAPSHOTS,
                chain,
                run_id=result.run_id,
                partition={
                    "snapshot_date": snapshot_date.isoformat(),
                    "underlying": underlying,
                },
                schema=RAW_OPTION_CHAIN_SCHEMA,
            )
            log.info("%s: %d contracts (%d crossed)", underlying, snap.contracts, snap.crossed)
        except Exception as exc:  # one bad underlying must not lose the others
            snap.error = f"{type(exc).__name__}: {exc}"
            log.exception("%s: snapshot failed", underlying)

    write_json(
        {"generated_at": now.isoformat(), **result.to_dict()},
        lake.reports_dir("options_snapshot") / f"{snapshot_date}-{result.run_id}.json",
    )
    log.info(result.summary())
    return result


def _snapshot_one(
    source: OptionsSource,
    underlying: str,
    expiration_lte: date,
    spots: pd.DataFrame,
    now: datetime,
    run_id: str,
) -> pd.DataFrame:
    quotes = source.option_chain(underlying, expiration_lte=expiration_lte)
    contracts = source.option_contracts(underlying, expiration_lte=expiration_lte)
    chain = quotes.merge(contracts, on="contract", how="outer")

    parsed = [parse_occ(c) for c in chain["contract"]]
    chain["expiration"] = [p.expiration for p in parsed]
    chain["right"] = [p.right for p in parsed]
    chain["strike"] = [p.strike for p in parsed]
    snapshot_date = now.astimezone(NEW_YORK).date()
    chain["dte"] = [(p.expiration - snapshot_date).days for p in parsed]

    spot = spots.loc[underlying] if underlying in spots.index else None
    chain["underlying"] = underlying
    chain["underlying_price"] = None if spot is None else spot["price"]
    chain["underlying_bid"] = None if spot is None else spot["bid"]
    chain["underlying_ask"] = None if spot is None else spot["ask"]
    chain["snapshot_at"] = pd.Timestamp(now)
    chain["ingested_at"] = pd.Timestamp(now)
    chain["feed"] = source.options_feed
    chain["run_id"] = run_id
    chain = chain.sort_values(["expiration", "strike", "right"]).reset_index(drop=True)
    return conform(chain, RAW_OPTION_CHAIN_SCHEMA)
