"""Fetching headlines (fake feed) and scoring them (a stand-in for Claude)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tp_core.news import daily_sentiment, load_news, load_scores
from tp_core.storage import Lake
from tp_ingest import cli
from tp_ingest.jobs.news import run_news, run_scoring
from tp_ingest.sentiment import ClaudeScorer, parse_scores
from tp_ingest.sources.fake import FakeSource

NOW = datetime(2024, 7, 12, 23, 0, tzinfo=UTC)


class Scorer:
    model = "stand-in"

    def __init__(self, fail_on: int | None = None) -> None:
        self.batches: list[int] = []
        self.fail_on = fail_on

    def score(self, items: list[dict[str, str]]) -> dict[str, float]:
        self.batches.append(len(items))
        if self.fail_on is not None and len(self.batches) == self.fail_on:
            raise RuntimeError("overloaded")
        return {i["id"]: (0.7 if "beats" in i["headline"] else -0.7) for i in items}


def test_fetch_stores_new_headlines_once(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    first = run_news(lake, FakeSource(), ["XYZ", "ABC"], now=NOW, days=3)
    assert first.fetched == first.new == 6  # one a day each, three days
    again = run_news(lake, FakeSource(), ["XYZ", "ABC"], now=NOW, days=3)
    assert again.new == 0
    assert len(load_news(lake)) == 6
    assert list(lake.reports_dir("news").glob("*.json"))


def test_scoring_in_batches_survives_a_failed_one(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    run_news(lake, FakeSource(), [f"S{i:02d}" for i in range(30)], now=NOW, days=3)  # 90
    scorer = Scorer(fail_on=2)
    result = run_scoring(lake, scorer, now=NOW)
    assert scorer.batches == [40, 40, 10]
    assert result.failed_batches == 1
    assert result.scored == 50
    retry = run_scoring(lake, Scorer(), now=NOW)  # the failed batch is still unscored
    assert retry.scored == 40
    assert len(load_scores(lake)) == 90
    assert set(daily_sentiment(lake)["score"].round(1)) <= {0.7, -0.7}
    assert "nothing to score" in run_scoring(lake, Scorer(), now=NOW).summary()


def test_replies_are_checked_and_clipped() -> None:
    scores = [{"id": "a", "score": 3}, {"id": "b", "score": -0.4},
              {"id": "zzz", "score": 0.5}, {"id": "c", "score": "x"}]  # fmt: skip
    text = json.dumps({"scores": scores})
    assert parse_scores(text, {"a", "b", "c"}) == {"a": 1.0, "b": -0.4}


def test_claude_scorer_asks_for_structured_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        sent.update(kwargs)
        body = json.dumps({"scores": [{"id": "1", "score": 0.5}]})
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=body)]
        )

    scorer = ClaudeScorer("key", "claude-opus-5-5")
    monkeypatch.setattr(scorer, "_client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = scorer.score([{"id": "1", "headline": "XYZ beats", "summary": "", "symbols": "XYZ"}])
    assert out == {"1": 0.5}
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["effort"] == "low"
    assert "XYZ beats" in sent["messages"][0]["content"]

    def refusing(**kwargs: Any) -> Any:
        return SimpleNamespace(stop_reason="refusal", content=[])

    monkeypatch.setattr(
        scorer, "_client", SimpleNamespace(messages=SimpleNamespace(create=refusing))
    )
    with pytest.raises(RuntimeError, match="refusal"):
        scorer.score([{"id": "1", "headline": "x", "summary": "", "symbols": "XYZ"}])


def test_cli_fetch_show_and_score_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    runner = CliRunner()
    fetched = runner.invoke(cli.app, ["news", "fetch", "--source", "fake", "--symbols", "XYZ"])
    assert fetched.exit_code == 0, fetched.output
    assert "3 new" in fetched.output
    shown = runner.invoke(cli.app, ["news", "show", "XYZ"])
    assert "estimates" in shown.output
    assert "?" in shown.output  # not scored yet
    scored = runner.invoke(cli.app, ["news", "score"])
    assert scored.exit_code == 2  # no API key: a configuration error
