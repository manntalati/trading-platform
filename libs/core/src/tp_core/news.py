"""News headlines (Alpaca's news feed) and their sentiment, per symbol per session.

    raw/alpaca/news/ingest_date=<day>/run-<ts>.parquet   the headlines (tp-data news fetch)
    clean/news_sentiment/part.parquet                     a score per headline (tp-data news score)

A headline counts for the session whose close comes after it: one published after 16:00 New
York time, or on a weekend or holiday, belongs to the next session, so a feature built from a
session's headlines uses nothing its close hadn't seen.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pyarrow as pa

from tp_core.calendar import NEW_YORK, is_session, next_session
from tp_core.storage import Lake, read_parquet_dir, write_parquet

RAW_NEWS = "alpaca/news"
SCORES = "news_sentiment"
_TS = pa.timestamp("us", tz="UTC")
NEWS_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("created_at", _TS),
        ("headline", pa.string()),
        ("summary", pa.string()),
        ("source", pa.string()),
        ("url", pa.string()),
        ("symbols", pa.string()),  # comma-separated tickers the item is tagged with
        ("ingested_at", _TS),
    ]
)
SCORES_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("score", pa.float64()),  # -1 (clearly bad for the companies named) .. +1 (clearly good)
        ("model", pa.string()),
        ("scored_at", _TS),
    ]
)
CLOSE = time(16, 0)


def load_news(lake: Lake, since: date | None = None) -> pd.DataFrame:
    """Every stored headline once (the latest fetch of each), oldest first."""
    df = read_parquet_dir(lake.raw_dir(RAW_NEWS), NEWS_SCHEMA)
    if df.empty:
        return df
    df = df.sort_values("ingested_at").drop_duplicates("id", keep="last")
    if since is not None:
        df = df[df["created_at"] >= pd.Timestamp(since, tz="UTC")]
    return df.sort_values("created_at").reset_index(drop=True)


def load_scores(lake: Lake) -> pd.DataFrame:
    path = lake.clean_dir(SCORES)
    if not (path / "part.parquet").exists():
        return pd.DataFrame(columns=[f.name for f in SCORES_SCHEMA])
    return read_parquet_dir(path, SCORES_SCHEMA)


def save_scores(lake: Lake, scores: pd.DataFrame) -> int:
    """Add ``scores`` (``id``, ``score``, ``model``, ``scored_at``); a headline scored again keeps
    its latest score."""
    if scores.empty:
        return 0
    old = load_scores(lake)
    frames = [f for f in (old, scores[[f.name for f in SCORES_SCHEMA]]) if len(f)]
    out = pd.concat(frames, ignore_index=True).drop_duplicates("id", keep="last")
    write_parquet(out, lake.clean_dir(SCORES) / "part.parquet", SCORES_SCHEMA)
    return len(scores)


def session_of(at: datetime | pd.Timestamp) -> date:
    """The session whose close is the first after ``at``."""
    local = pd.Timestamp(at).tz_convert(NEW_YORK)
    day = local.date()
    if is_session(day) and local.time() < CLOSE:
        return day
    return next_session(day)


def daily_sentiment(lake: Lake) -> pd.DataFrame:
    """Per (session, symbol): the mean score of its scored headlines and how many there were
    (``date``, ``symbol``, ``score``, ``headlines``)."""
    news, scores = load_news(lake), load_scores(lake)
    columns = ["date", "symbol", "score", "headlines"]
    if news.empty or scores.empty:
        return pd.DataFrame(columns=columns)
    scored = news.merge(scores[["id", "score"]], on="id")
    if scored.empty:
        return pd.DataFrame(columns=columns)
    scored["date"] = pd.to_datetime([session_of(t) for t in scored["created_at"]])
    scored["symbol"] = scored["symbols"].str.split(",")
    rows = scored.explode("symbol")
    rows["symbol"] = rows["symbol"].str.strip().str.upper()
    rows = rows[rows["symbol"] != ""]
    out = rows.groupby(["date", "symbol"]).agg(score=("score", "mean"), headlines=("id", "nunique"))
    return out.reset_index()[columns]
