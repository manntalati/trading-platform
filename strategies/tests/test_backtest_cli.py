import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_strategies import backtest_cli

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lake:
    lake = Lake(tmp_path / "lake")
    run_backfill(lake, FakeSource(), ["SPY", "EFA", "IEF", "VNQ", "DBC"], now=NOW, years=4)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(lake.root))
    monkeypatch.setenv("TP_RISK_FILE", str(REPO / "config" / "risk.toml"))
    monkeypatch.setenv("TP_CLASSIFICATIONS_FILE", str(REPO / "config" / "classifications.toml"))
    return lake


def test_list_shows_strategies_and_parameters() -> None:
    result = CliRunner().invoke(backtest_cli.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "ma-timing: 10-month moving-average timing" in result.output
    assert "assets = SPY,EFA,IEF,VNQ,DBC" in result.output


def test_run_prints_a_tear_sheet_and_saves_the_run(lake: Lake) -> None:
    result = CliRunner().invoke(
        backtest_cli.app, ["run", "ma-timing", "--start", "2021-06-01", "--capital", "50000"]
    )
    assert result.exit_code == 0, result.output
    assert "10-month moving-average timing (ma-timing)" in result.output
    assert "Max drawdown" in result.output
    assert "SPY hold" in result.output

    [run_dir] = (lake.root / "reports" / "backtests" / "ma-timing").iterdir()
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["params"]["assets"] == ["SPY", "EFA", "IEF", "VNQ", "DBC"]
    assert summary["config"]["initial_cash"] == 50000
    assert summary["config"]["execution"]["fill_at"] == "next_open"
    assert summary["data"]["symbols"] == 5
    assert summary["stats"]["start"] == "2021-06-01"
    assert summary["benchmark"]["symbol"] == "SPY"
    assert "commit" in summary["code"]
    assert summary["risk"]["gate"] == "RiskManager"
    assert summary["risk"]["limits"]["max_position"] == 0.10
    assert "risk limits on" in result.output
    equity = pd.read_parquet(run_dir / "equity.parquet")
    assert equity["equity"].iloc[0] == 50000
    fills = pd.read_parquet(run_dir / "fills.parquet")
    assert len(fills) == summary["stats"]["trades"] > 0


def test_run_with_parameters_and_no_save(lake: Lake) -> None:
    result = CliRunner().invoke(
        backtest_cli.app,
        ["run", "ma-timing", "-p", "assets=SPY", "--fill", "next_close", "--no-save", "--no-risk"],
    )
    assert result.exit_code == 0, result.output
    assert "risk limits OFF" in result.output
    assert "'assets': ('SPY',)" in result.output
    assert not (lake.root / "reports" / "backtests").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "nope"], "unknown strategy"),
        (["run", "ma-timing", "-p", "assets=XYZ", "--benchmark", "XYZ"], "no clean bars"),
        (["run", "ma-timing", "-p", "months"], "key=value"),
    ],
)
def test_run_reports_bad_input(lake: Lake, args: list[str], message: str) -> None:
    result = CliRunner().invoke(backtest_cli.app, args)
    assert result.exit_code != 0
    assert message in result.output


def test_missing_risk_file_is_a_clear_error(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TP_RISK_FILE", "nowhere/risk.toml")
    result = CliRunner().invoke(backtest_cli.app, ["run", "ma-timing", "--no-save"])
    assert result.exit_code == 2
    assert "risk limits file nowhere/risk.toml not found" in result.output
