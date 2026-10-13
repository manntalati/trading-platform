"""The backtest validation protocol, end to end on synthetic bars (no network)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_strategies.validation import (
    Gate,
    ValidationConfig,
    latest_reports,
    signature,
    stress_tests,
    validate,
    variant_params,
    walk_forward,
)
from tp_trading.engine import EngineConfig

GTAA = ["SPY", "EFA", "IEF", "VNQ", "DBC"]
NOW = datetime(2026, 1, 9, 22, tzinfo=UTC)
CONFIG = ValidationConfig(
    train_end=date(2023, 6, 30),
    validation_end=date(2024, 12, 31),
    stress={"Aug 2024 volatility spike": (date(2024, 7, 16), date(2024, 8, 7)),
            "Before the data": (date(2001, 1, 1), date(2001, 2, 1))},
    wf_train=252,
    wf_test=126,
    variants={"ma-timing": {"months": [8, 12]}},
)  # fmt: skip


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    lake = Lake(tmp_path_factory.mktemp("validation"))
    run_backfill(lake, FakeSource(), GTAA, now=NOW, years=6)
    return lake


def test_config_file_loads() -> None:
    config = ValidationConfig.load(
        Path(__file__).resolve().parents[2] / "config" / "validation.toml"
    )
    assert config.validation_end > config.train_end
    assert "COVID crash" in config.stress
    assert config.variants["ma-timing"]["months"] == [8, 9, 10, 11, 12]
    assert config.gate == Gate(stress_max_drawdown=0.15)


def test_variants_start_with_the_base_and_skip_duplicates() -> None:
    config = ValidationConfig(variants={"ma-timing": {"months": [8, 10, 12]}})
    params = variant_params("ma-timing", {"months": 10}, config)
    assert [p["months"] for p in params] == [10, 8, 12]


def test_validate_runs_the_whole_protocol(lake: Lake) -> None:
    report = validate(lake, "ma-timing", engine=EngineConfig(), config=CONFIG, now=NOW)
    assert report["strategy"] == "ma-timing"
    assert report["params"]["months"] == 10
    assert set(report["periods"]) == {"train", "validation", "test"}
    assert report["periods"]["test"]["start"] > "2024-12-31"
    assert report["periods"]["test"]["benchmark"]["cagr"] is not None
    assert len(report["stability"]["variants"]) == 3
    assert report["deflated"]["trials"] == 3
    assert report["walk_forward"]["windows"]
    assert {w["params"]["months"] for w in report["walk_forward"]["windows"]} <= {8, 10, 12}
    stress = {s["name"]: s for s in report["stress"]}
    assert stress["Before the data"]["passed"] is None
    assert stress["Aug 2024 volatility spike"]["passed"] in (True, False)
    assert set(report["regimes"]) <= {"trend", "volatility", "breadth"}
    assert report["monte_carlo"]["paths"] == 1000
    names = [c["name"] for c in report["gate"]["checks"]]
    assert names == ["Locked test set", "Walk-forward", "Deflated Sharpe", "Stress periods",
                     "Parameter stability"]  # fmt: skip
    assert report["gate"]["passed"] == all(c["passed"] is True for c in report["gate"]["checks"])
    assert report["test_touches"] == 1

    again = validate(lake, "ma-timing", engine=EngineConfig(), config=CONFIG, now=NOW)
    assert again["test_touches"] == 2  # every look at the locked test set is counted
    assert again["deflated"]["trials"] == 3  # the same variants: still three trials
    wider = ValidationConfig(**{**CONFIG.__dict__, "variants": {"ma-timing": {"months": [6, 14]}}})
    third = validate(lake, "ma-timing", engine=EngineConfig(), config=wider, now=NOW)
    assert third["deflated"]["trials"] == 5  # every distinct variant ever tried counts
    assert latest_reports(lake)["ma-timing"]["run_id"] == third["run_id"]
    assert signature({"b": 1, "a": (1, 2)}) == '{"a": [1, 2], "b": 1}'


def test_walk_forward_trades_the_trailing_winner() -> None:
    days = pd.bdate_range("2024-01-01", periods=40)
    good_then_bad = pd.Series([0.01, 0.012] * 10 + [-0.01, -0.012] * 10, index=days)
    steady = pd.Series([0.001, 0.002] * 20, index=days)
    wf = walk_forward([({"v": "a"}, good_then_bad), ({"v": "b"}, steady)], 0, 10, 10)
    chosen = [w["params"]["v"] for w in wf["windows"]]
    # a led over sessions 0-19, so it's traded through 10-29 (and loses in 20-29: the lag
    # walk-forward exposes); by session 30 the trailing winner is b
    assert chosen == ["a", "a", "b"]
    assert wf["sessions"] == 30
    assert wf["windows"][1]["test_return"] < 0


def test_stress_survived_when_shallow_or_better_than_the_market() -> None:
    days = pd.bdate_range("2024-07-15", periods=20)
    strat = pd.Series([-0.01] * 20, index=days)  # about -18%
    spy = pd.Series([-0.02] * 20, index=days)  # about -33%
    config = ValidationConfig(stress={"x": (days[0].date(), days[-1].date())})
    [row] = stress_tests(strat, spy, config)
    assert row["passed"] is True  # deeper than 15%, but better than SPY
    [worse] = stress_tests(spy, strat, config)
    assert worse["passed"] is False
    shallow = pd.Series([-0.002] * 20, index=days)
    [ok] = stress_tests(shallow, pd.Series(0.0, index=days), config)
    assert ok["passed"] is True  # -4%: survived, whatever SPY did
