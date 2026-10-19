"""News: fetch the day's headlines, and score the ones not scored yet (``tp_core.news``).

Fetching is free with the Alpaca keys. Scoring calls Claude (``tp_ingest.sentiment``) and costs
API tokens, so it only runs when asked (``TP_NEWS_SCORING=on`` for the scheduler).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from tp_core.news import NEWS_SCHEMA, RAW_NEWS, load_news, load_scores, save_scores
from tp_core.schemas import conform
from tp_core.storage import Lake, new_run_id, write_json, write_raw
from tp_ingest.sources.base import NewsSource

log = logging.getLogger(__name__)
BATCH = 40


@dataclass
class NewsResult:
    run_id: str
    fetched: int = 0
    new: int = 0
    scored: int = 0
    failed_batches: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"run {self.run_id}: {self.fetched} headlines fetched, {self.new} new"]
        if self.scored or self.failed_batches:
            parts.append(f"{self.scored} scored ({self.failed_batches} batches failed)")
        return "; ".join(parts + self.notes)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def run_news(
    lake: Lake, source: NewsSource, symbols: Sequence[str], *, now: datetime, days: int = 3
) -> NewsResult:
    """Fetch the last ``days`` of headlines for ``symbols``; store the ones not stored yet."""
    result = NewsResult(new_run_id(now))
    df = source.news(list(symbols), now - timedelta(days=days), now)
    result.fetched = len(df)
    known = set(load_news(lake, since=(now - timedelta(days=days + 2)).date())["id"])
    fresh = df[~df["id"].isin(known)]
    result.new = len(fresh)
    if len(fresh):
        write_raw(
            lake, RAW_NEWS, conform(fresh.assign(ingested_at=pd.Timestamp(now)), NEWS_SCHEMA),
            run_id=result.run_id, partition={"ingest_date": now.date().isoformat()},
            schema=NEWS_SCHEMA,
        )  # fmt: skip
    write_json(result.to_dict(), lake.reports_dir("news") / f"{now.date()}-{result.run_id}.json")
    log.info(result.summary())
    return result


class Scorer(Protocol):
    model: str

    def score(self, items: list[dict[str, str]]) -> dict[str, float]:
        """``{id: score}`` for items with ``id``, ``headline``, ``summary`` and ``symbols``."""
        ...


def run_scoring(
    lake: Lake, scorer: Scorer, *, now: datetime, days: int = 10, limit: int = 400
) -> NewsResult:
    """Score up to ``limit`` unscored headlines from the last ``days``, in batches."""
    result = NewsResult(new_run_id(now))
    news = load_news(lake, since=(now - timedelta(days=days)).date())
    done = set(load_scores(lake)["id"])
    todo = news[~news["id"].isin(done)].tail(limit)
    for k in range(0, len(todo), BATCH):
        chunk = todo.iloc[k : k + BATCH]
        items = [
            {"id": str(r.id), "headline": str(r.headline), "summary": str(r.summary)[:300],
             "symbols": str(r.symbols)}
            for r in chunk.itertuples()
        ]  # fmt: skip
        try:
            scores = scorer.score(items)
        except Exception as exc:  # one failed batch: the rest still get scored, and it's retried
            log.warning("scoring a batch failed: %s", exc)
            result.failed_batches += 1
            continue
        rows = pd.DataFrame(
            {"id": list(scores), "score": list(scores.values()), "model": scorer.model,
             "scored_at": pd.Timestamp(now)}
        )  # fmt: skip
        result.scored += save_scores(lake, rows)
    if not len(todo):
        result.notes.append("nothing to score")
    write_json(
        result.to_dict(), lake.reports_dir("news_scores") / f"{now.date()}-{result.run_id}.json"
    )
    log.info(result.summary())
    return result
