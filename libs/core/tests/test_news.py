"""Headlines and their sentiment per session (synthetic XYZ, ABC)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.news import (
    NEWS_SCHEMA,
    RAW_NEWS,
    daily_sentiment,
    load_news,
    load_scores,
    save_scores,
    session_of,
)
from tp_core.schemas import conform
from tp_core.storage import Lake, write_raw


def test_a_headline_counts_for_the_next_close() -> None:
    assert session_of(datetime(2024, 7, 10, 15, 0, tzinfo=UTC)) == date(2024, 7, 10)  # 11:00 NY
    assert session_of(datetime(2024, 7, 10, 21, 0, tzinfo=UTC)) == date(2024, 7, 11)  # 17:00 NY
    assert session_of(datetime(2024, 7, 13, 15, 0, tzinfo=UTC)) == date(2024, 7, 15)  # Saturday


def store(lake: Lake, rows: list[dict[str, object]], run: str) -> None:
    df = pd.DataFrame(rows).assign(
        summary="", source="t", url="", ingested_at=pd.Timestamp("2024-07-12T23:00Z")
    )
    write_raw(lake, RAW_NEWS, conform(df, NEWS_SCHEMA), run_id=run,
              partition={"ingest_date": "2024-07-12"}, schema=NEWS_SCHEMA)  # fmt: skip


def test_daily_sentiment_per_symbol(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    at = pd.Timestamp("2024-07-10T15:00Z")
    store(lake, [
        {"id": "1", "created_at": at, "headline": "XYZ beats", "symbols": "XYZ"},
        {"id": "2", "created_at": at, "headline": "XYZ and ABC partner", "symbols": "XYZ,ABC"},
        {"id": "3", "created_at": pd.Timestamp("2024-07-10T22:00Z"), "headline": "ABC late",
         "symbols": "ABC"},
    ], "r1")  # fmt: skip
    store(lake, [{"id": "1", "created_at": at, "headline": "XYZ beats", "symbols": "XYZ"}], "r2")
    assert len(load_news(lake)) == 3  # fetched twice, stored once
    now = pd.Timestamp("2024-07-12T23:00Z")
    save_scores(lake, pd.DataFrame({"id": ["1", "2", "3"], "score": [0.8, 0.2, -0.6],
                                    "model": "m", "scored_at": now}))  # fmt: skip
    save_scores(lake, pd.DataFrame({"id": ["1"], "score": [0.6], "model": "m", "scored_at": now}))
    assert len(load_scores(lake)) == 3  # rescored: the latest kept
    daily = daily_sentiment(lake).set_index(["date", "symbol"])
    xyz = daily.loc[(pd.Timestamp("2024-07-10"), "XYZ")]
    assert xyz["score"] == pytest.approx((0.6 + 0.2) / 2)
    assert xyz["headlines"] == 2
    assert daily.loc[(pd.Timestamp("2024-07-10"), "ABC"), "score"] == pytest.approx(0.2)
    assert daily.loc[(pd.Timestamp("2024-07-11"), "ABC"), "score"] == pytest.approx(
        -0.6
    )  # after 16:00


def test_nothing_stored_yet(tmp_path: Path) -> None:
    assert daily_sentiment(Lake(tmp_path)).empty
