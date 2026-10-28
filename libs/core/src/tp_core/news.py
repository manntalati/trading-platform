"""News headlines (Alpaca's news feed) and their sentiment, per symbol per session.

    raw/alpaca/news/ingest_date=<day>/run-<ts>.parquet   the headlines (tp-data news fetch)
    clean/news_sentiment/part.parquet                     a score per headline (tp-data news score)

A headline counts for the session whose close comes after it: one published after 16:00 New
York time, or on a weekend or holiday, belongs to the next session, so a feature built from a
session's headlines uses nothing its close hadn't seen.

The recommendations read each name's last few sessions of headlines (``recent_news``): Claude's
score where it scored them (``TP_NEWS_SCORING``), else a rough one from finance words in the
headline (``keyword_score``: "beats", "raises guidance", "downgrade", "probe", ...), worked out
when read and never stored, so there is a sentiment even with scoring off while the ML
features see Claude's scores only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
import pyarrow as pa

from tp_core.calendar import NEW_YORK, is_session, next_session, sessions_back
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
    """Every stored headline once (the latest fetch of each), oldest first; from ``since`` on,
    reading only the fetches made since (a headline can't be fetched before it's published)."""
    root = lake.raw_dir(RAW_NEWS)
    if since is None:
        df = read_parquet_dir(root, NEWS_SCHEMA)
    else:
        parts = sorted(
            d for d in root.glob("ingest_date=*") if d.name.split("=", 1)[1] >= since.isoformat()
        )
        frames = [f for f in (read_parquet_dir(d, NEWS_SCHEMA) for d in parts) if len(f)]
        empty: pd.DataFrame = NEWS_SCHEMA.empty_table().to_pandas()
        df = pd.concat(frames, ignore_index=True) if frames else empty
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


# -- a rough score for headlines Claude hasn't scored ---------------------------------------------

KEYWORDS = "keywords"  # the ``scored_by`` of a keyword score
_GOOD = (
    "beat", "beats", "tops", "top estimates", "tops estimates", "surpasses", "exceeds",
    "record revenue", "record profit", "record quarter", "raises guidance", "raised guidance",
    "raises outlook", "raises forecast", "boosts guidance", "lifts guidance", "hikes guidance",
    "upgrade", "upgrades", "upgraded", "outperform", "buy rating", "overweight",
    "raises price target", "price target raised", "fda approval", "fda approves", "approved",
    "wins contract", "awarded", "contract win", "buyback", "share repurchase",
    "raises dividend", "dividend increase", "surge", "surges", "soar", "soars", "jumps",
    "rallies", "climbs", "strong demand", "profit rises", "profit jumps", "breakthrough",
    "positive results", "meets primary endpoint", "beats expectations",
)  # fmt: skip
_BAD = (
    "miss", "misses", "missed", "falls short", "below estimates", "cuts guidance",
    "lowers guidance", "cuts outlook", "lowers outlook", "cuts forecast", "lowers forecast",
    "warns", "profit warning", "downgrade", "downgrades", "downgraded", "underperform",
    "sell rating", "underweight", "cuts price target", "lowers price target",
    "price target cut", "lawsuit", "sued", "probe", "investigation", "subpoena", "fraud",
    "recall", "fined", "penalty", "layoffs", "job cuts", "bankruptcy", "chapter 11",
    "default", "delisted", "delisting", "plunge", "plunges", "tumble", "tumbles", "slump",
    "slumps", "sinks", "crash", "halted", "fails", "failed", "rejects", "rejected",
    "complete response letter", "dilution", "secondary offering", "short seller", "resigns",
    "steps down", "data breach", "outage", "weak demand", "loss widens", "misses expectations",
)  # fmt: skip


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted({w.lower() for w in words}, key=len, reverse=True)  # longest phrase first
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in ordered) + r")\b", re.IGNORECASE)


_GOOD_RE, _BAD_RE = _pattern(_GOOD), _pattern(_BAD)


def keyword_score(headline: str) -> float:
    """-1..+1 from the good and bad finance words in ``headline`` (each counted once): one good
    word is +0.5, two +0.67, a good and a bad one 0, none 0 (routine coverage). Rough: it
    can't read who the news is good for, or sarcasm; Claude's scores replace it."""
    good = {m.lower() for m in _GOOD_RE.findall(headline or "")}
    bad = {m.lower() for m in _BAD_RE.findall(headline or "")}
    return (len(good) - len(bad)) / (len(good) + len(bad) + 1)


def recent_news(
    lake: Lake,
    symbols: Iterable[str],
    now: datetime,
    *,
    sessions: int = 5,
    latest: int = 3,
) -> dict[str, dict[str, Any]]:
    """Per symbol, the headlines naming it over the last ``sessions`` sessions to ``now`` (the
    session a headline counts for, as ``session_of``): their mean score (Claude's where it
    scored them, else ``keyword_score``), how many there were and how many Claude scored, and
    the ``latest`` newest. Symbols without headlines are left out."""
    wanted = {s.upper() for s in symbols}
    days = sessions_back(session_of(now), sessions)
    if not days or not wanted:
        return {}
    news = load_news(lake, since=days[0] - timedelta(days=5))
    if news.empty:
        return {}
    news = news[news["created_at"] <= pd.Timestamp(now)].copy()
    news["session"] = [session_of(t) for t in news["created_at"]]
    news = news[news["session"] >= days[0]]
    if news.empty:
        return {}
    claude = load_scores(lake).drop_duplicates("id", keep="last").set_index("id")["score"]
    news["scored_by"] = ["claude" if i in claude.index else KEYWORDS for i in news["id"]]
    news["score"] = [
        float(claude[i]) if by == "claude" else keyword_score(h)
        for i, by, h in zip(news["id"], news["scored_by"], news["headline"], strict=True)
    ]
    news["symbol"] = news["symbols"].fillna("").str.upper().str.split(",")
    rows = news.explode("symbol")
    rows["symbol"] = rows["symbol"].str.strip()
    rows = rows[rows["symbol"].isin(wanted)]
    out: dict[str, dict[str, Any]] = {}
    for symbol, g in rows.groupby("symbol"):
        g = g.sort_values("created_at", ascending=False)
        out[str(symbol)] = {
            "sentiment": float(g["score"].mean()),
            "headlines": len(g),
            "claude": int((g["scored_by"] == "claude").sum()),
            "sessions": sessions,
            "since": days[0].isoformat(),
            "latest": [
                {
                    "at": pd.Timestamp(r["created_at"]).isoformat(),
                    "headline": str(r["headline"]),
                    "source": str(r["source"]),
                    "url": str(r["url"]),
                    "score": float(r["score"]),
                    "scored_by": str(r["scored_by"]),
                }
                for r in g.head(latest).to_dict("records")
            ],
        }
    return out
