"""The model registry, drift (PSI) and live accuracy, on synthetic data."""

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_core.storage import Lake
from tp_ml import monitor

DAYS = pd.DatetimeIndex(sessions(date(2023, 1, 3), date(2023, 12, 29)))


def test_psi_is_small_for_the_same_distribution_and_large_for_a_shift() -> None:
    rng = np.random.default_rng(0)
    train = pd.DataFrame({"x": rng.normal(0, 1, 5000), "y": rng.normal(0, 1, 5000)})
    prof = monitor.profile(train, ["x", "y", "missing"])
    assert set(prof) == {"x", "y"}
    same = pd.DataFrame({"x": rng.normal(0, 1, 1000), "y": rng.normal(1.5, 1, 1000)})
    rows = {r["feature"]: r for r in monitor.drift(prof, same)}
    assert rows["x"]["psi"] < monitor.STABLE
    assert rows["x"]["level"] == "stable"
    assert rows["y"]["level"] == "shifted"
    assert monitor.drift(prof, same)[0]["feature"] == "y"  # most shifted first
    assert monitor.psi(prof["x"]["expected"], prof["x"]["edges"], np.array([1.0, 2.0])) is None


def test_registry_keeps_versions_and_loads_them(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    assert monitor.latest(lake, "m") is None
    july = {"cutoff": date(2023, 7, 1), "trained_at": "2023-07-01T22:00:00"}
    october = {"cutoff": date(2023, 10, 1), "trained_at": "2023-10-01T22:00:00", "score": math.nan}
    first = monitor.register(lake, "m", {"w": [1, 2]}, july)
    second = monitor.register(lake, "m", {"w": [3]}, october)
    assert first["version"].startswith("20230701-")
    assert [v["version"] for v in monitor.versions(lake, "m")] == [
        first["version"],
        second["version"],
    ]
    assert monitor.latest(lake, "m")["score"] is None  # NaN stored as null
    assert monitor.load_model(lake, "m", first["version"]) == {"w": [1, 2]}


def test_predictions_replace_the_same_day_and_score_live(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    rng = np.random.default_rng(1)
    symbols = [f"S{i}" for i in range(12)]
    closes = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (len(DAYS), 12)), axis=0)),
                          index=DAYS, columns=symbols)  # fmt: skip
    horizon = 21
    for i in range(0, len(DAYS) - horizon, 5):  # a prediction every week
        fwd = closes.iloc[i + horizon] / closes.iloc[i] - 1
        monitor.record_predictions(lake, "r", "v1", DAYS[i].date(), fwd)  # perfect foresight
    monitor.record_predictions(lake, "r", "v1", DAYS[0].date(), closes.iloc[0] * 0 + 1)
    preds = monitor.load_predictions(lake, "r")
    assert len(preds[preds["date"] == DAYS[0].date()]) == 12  # replaced, not appended
    live = monitor.live_rank_ic(preds[preds["date"] != DAYS[0].date()], closes, horizon)
    assert live["dates"] >= 8
    assert live["mean_ic"] == pytest.approx(1.0)
    # dates a holding period apart: none of the periods overlap
    used = pd.DatetimeIndex([r["date"] for r in live["series"]])
    gaps = np.diff([DAYS.get_loc(d) for d in used])
    assert (gaps >= horizon).all()


def test_live_classification_against_the_base_rate() -> None:
    scored = pd.DataFrame({"score": [0.9, 0.8, 0.7, 0.3, 0.2, 0.1], "label": [1, 1, 0, 0, 1, 0]})
    out = monitor.live_classification(scored, threshold=0.55)
    assert out["taken"] == 3
    assert out["taken_hit_rate"] == pytest.approx(2 / 3)
    assert out["base_rate"] == pytest.approx(0.5)
    assert out["auc"] is not None
    assert monitor.live_classification(scored.iloc[:0], 0.5) == {"predictions": 0}
