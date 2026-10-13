"""Fundamentals and earnings dates from SEC EDGAR (``tp_core.fundamentals``).

For each symbol with a CIK on EDGAR (funds mostly have none): its company facts (the concepts
in ``CONCEPTS``, every filing) and its filings list (earnings releases and periodic reports).
Each run writes immutable raw files (``raw/sec/company_facts``, ``raw/sec/earnings_events``) and
rebuilds the clean tables. A symbol that fails doesn't stop the others; the run reports it and
exits non-zero.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from tp_core.fundamentals import (
    EVENTS_DATASET,
    EVENTS_SCHEMA,
    FACTS_DATASET,
    FACTS_SCHEMA,
    build_clean,
    parse_company_facts,
    parse_submissions,
)
from tp_core.storage import Lake, new_run_id, write_json, write_raw
from tp_ingest.sources.sec import FundamentalsSource, edgar_ticker

__all__ = ["FundamentalsJobResult", "FundamentalsSource", "run_fundamentals"]

log = logging.getLogger(__name__)


@dataclass
class FundamentalsJobResult:
    run_id: str
    facts: dict[str, int] = field(default_factory=dict)
    releases: dict[str, int] = field(default_factory=dict)
    no_cik: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        with_facts = sum(1 for n in self.facts.values() if n)
        text = (
            f"run {self.run_id}: {with_facts}/{len(self.facts)} companies with facts, "
            f"{sum(self.releases.values())} earnings releases"
        )
        if self.no_cik:
            text += f"; not on EDGAR (funds?): {', '.join(self.no_cik)}"
        if self.errors:
            text += f"; FAILED {', '.join(self.errors)}"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ok": self.ok}


def run_fundamentals(
    lake: Lake, source: FundamentalsSource, symbols: Sequence[str], *, now: datetime
) -> FundamentalsJobResult:
    result = FundamentalsJobResult(run_id=new_run_id(now))
    ciks = source.tickers()
    facts, events = [], []
    for symbol in dict.fromkeys(s.upper() for s in symbols):
        cik = ciks.get(edgar_ticker(symbol))
        if cik is None:
            result.no_cik.append(symbol)
            continue
        try:
            f = parse_company_facts(source.company_facts(cik), symbol)
            doc, extra = source.submissions(cik)
            e = parse_submissions(doc, symbol, extra)
        except Exception as exc:  # one company failing doesn't stop the rest
            log.warning("EDGAR %s (CIK %s) failed: %s", symbol, cik, exc)
            result.errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        result.facts[symbol] = len(f)
        result.releases[symbol] = int((e["form"].str.startswith("8-K")).sum()) if len(e) else 0
        facts.append(f)
        events.append(e)
    stamp = pd.Timestamp(now).tz_convert("UTC")
    partition = {"ingest_date": now.date().isoformat()}
    for frames, dataset, schema in (
        (facts, FACTS_DATASET, FACTS_SCHEMA),
        (events, EVENTS_DATASET, EVENTS_SCHEMA),
    ):
        nonempty = [f for f in frames if len(f)]
        if nonempty:
            df = pd.concat(nonempty, ignore_index=True)
            df["ingested_at"] = stamp
            write_raw(lake, dataset, df[list(schema.names)], run_id=result.run_id,
                      partition=partition, schema=schema)  # fmt: skip
    if facts or events:
        build_clean(lake)
    write_json(
        result.to_dict(), lake.reports_dir("fundamentals") / f"{now.date().isoformat()}.json"
    )
    return result
