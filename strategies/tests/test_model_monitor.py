"""Monitoring strategy 13's ranker and strategy 12's filter on a synthetic lake (S00..S11)."""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.storage import Lake
from tp_ml import monitor
from tp_ml.testing import write_lake
from tp_strategies.model_monitor import monitor_all, monitor_model

STOCKS = [f"S{i:02d}" for i in range(12)]
SECTORS = {s: ("Tech" if i < 6 else "Energy") for i, s in enumerate(STOCKS)}
NOW = datetime(2023, 12, 29, 23, 30, tzinfo=UTC)
RANK = {"universe": STOCKS, "model": "ridge", "min_dates": 12, "retrain": 3}
META = {"universe": STOCKS, "model": "logistic", "min_events": 40, "retrain": 3}


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    drifts = [0.0003] + [(-0.0004 + 0.00015 * i) for i in range(12)]
    return write_lake(tmp_path_factory.mktemp("models"), ["SPY", *STOCKS], n=1100, seed=5,
                      drifts=drifts)  # fmt: skip


def test_the_ranker_is_trained_registered_and_watched(lake: Lake) -> None:
    first = monitor_model(lake, "ml-rank", RANK, sectors=SECTORS, now=NOW)
    assert first["retrained"] is True
    assert first["status"] in ("ok", "warn")
    version = first["version"]
    assert version["cutoff"] == "2023-10-01"  # every 3 months: the quarter's first day
    assert version["validation"]["dates"] > 0
    assert first["predictions_today"] == len(STOCKS)
    assert first["drift"]
    assert {d["level"] for d in first["drift"]} <= {"stable", "moving", "shifted"}
    stored = monitor.latest(lake, "ml-rank")
    assert stored["profile"]
    assert stored["columns"]

    again = monitor_model(lake, "ml-rank", RANK, sectors=SECTORS, now=NOW)
    assert again["retrained"] is False  # same retrain date, same parameters
    assert len(monitor.versions(lake, "ml-rank")) == 1
    assert len(monitor.load_predictions(lake, "ml-rank")) == len(STOCKS)  # replaced, not added

    changed = monitor_model(lake, "ml-rank", RANK | {"horizon": 10}, sectors=SECTORS, now=NOW)
    assert changed["retrained"] is True  # new parameters: a new version
    assert len(monitor.versions(lake, "ml-rank")) == 2
    reports = monitor.latest_reports(lake)
    assert reports["ml-rank"]["version"]["version"] == changed["version"]["version"]


def test_the_filter_is_watched_through_its_signals(lake: Lake) -> None:
    report = monitor_model(lake, "rsi2-meta", META, sectors=SECTORS, now=NOW)
    assert report["status"] in ("ok", "warn"), report
    assert report["version"]["validation"]["scored"] > 0
    assert report["predictions_today"] >= 0  # only the day's RSI(2) signals get a score


def test_no_bars_no_model_and_one_failure_does_not_hide_the_other(tmp_path: Path) -> None:
    empty = Lake(tmp_path)
    (skipped,) = monitor_all(empty, ["ml-rank"], {}, sectors={}, now=NOW)
    assert skipped["status"] == "skipped"
    reports = monitor_all(empty, ["ml-rank", "rsi2-meta"], {"ml-rank": {"no_such": 1}},
                          sectors={}, now=NOW)  # fmt: skip
    assert [r["status"] for r in reports] == ["error", "skipped"]
    assert "no parameter" in reports[0]["reasons"][0]
    assert pd.Timestamp(reports[1]["checked_at"]) == pd.Timestamp(NOW)
