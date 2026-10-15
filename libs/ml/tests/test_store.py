from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_ml import tracking
from tp_ml.features import FEATURES
from tp_ml.store import build_feature_set, features_dir, load_feature_set, registry
from tp_ml.testing import write_lake

NOW = datetime(2024, 1, 3, 12, 0, tzinfo=UTC)
SYMBOLS = ["SPY", "AAA", "BBB"]


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    return write_lake(tmp_path_factory.mktemp("lake"), SYMBOLS)


def test_build_stores_a_versioned_registered_feature_set(lake: Lake) -> None:
    classifier = Classifier(sectors={"AAA": "Tech", "BBB": "Tech"}, funds={})
    entry = build_feature_set(lake, classifier=classifier, now=NOW)
    assert entry["symbols"] == 3
    assert entry["columns"] == list(FEATURES)
    assert Path(entry["path"]).exists()
    assert Path(entry["path"]).is_relative_to(features_dir(lake) / "daily" / entry["version"])
    assert entry["data"]["rows"] == entry["rows"]

    # same code, same data: the same version, registered once
    again = build_feature_set(lake, classifier=classifier, now=NOW)
    assert again["version"] == entry["version"]
    assert len(registry(lake)) == 1

    panel = load_feature_set(lake)
    assert panel.index.names == ["date", "symbol"]
    assert len(panel) == entry["rows"]
    assert panel["rel_sector_63"].notna().any()

    subset = build_feature_set(lake, name="two", symbols=["SPY", "AAA"], now=NOW)
    assert subset["version"] != entry["version"]
    assert {e["name"] for e in registry(lake)} == {"daily", "two"}
    assert load_feature_set(lake, "two", subset["version"]).index.get_level_values(
        "symbol"
    ).unique().tolist() == ["AAA", "SPY"]


def test_missing_feature_sets_and_bars_say_what_to_run(tmp_path: Path) -> None:
    empty = Lake(tmp_path)
    with pytest.raises(LookupError, match="tp-ml features"):
        load_feature_set(empty)
    with pytest.raises(LookupError, match="tp-data bars backfill"):
        build_feature_set(empty, now=NOW)


def test_tracking_records_runs_in_the_data_root(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    with tracking.run(lake, "tests", name="first", params={"a": 1}, tags={"t": "x"}):
        tracking.log_metrics({"auc": 0.6, "skipped": None, "nan": float("nan")})
        tracking.log_text("hello", "note.txt")
    frame = tracking.runs(lake, "tests")
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["params.a"] == "1"
    assert row["metrics.auc"] == pytest.approx(0.6)
    assert "metrics.skipped" not in frame.columns
    assert "metrics.nan" not in frame.columns
    assert row["tags.t"] == "x"
    assert "tags.git_commit" in frame.columns
    assert (tmp_path / "mlflow" / "mlflow.db").exists()
    assert list((tmp_path / "mlflow" / "artifacts" / "tests").rglob("note.txt"))
