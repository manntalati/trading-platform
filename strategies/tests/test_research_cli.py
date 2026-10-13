"""``tp-backtest research`` on a small synthetic lake."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.jobs.intraday import run_intraday
from tp_ingest.jobs.macro import run_backfill as macro_backfill
from tp_ingest.sources.fake import FakeSource
from tp_ingest.sources.fred import FakeMacroSource
from tp_strategies.backtest_cli import app
from tp_strategies.library.xs_momentum import LARGE_CAPS

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("research")
    lake = Lake(path)
    run_backfill(lake, FakeSource(), [*LARGE_CAPS[:6], "SPY"], now=NOW, years=3)
    macro_backfill(lake, FakeMacroSource(end=NOW.date()), now=NOW)
    run_intraday(lake, FakeSource(), ["SPY", "QQQ"], now=NOW, days=20)
    return path


@pytest.fixture(autouse=True)
def env(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(root))
    monkeypatch.setenv(
        "TP_CLASSIFICATIONS_FILE", str(Path(__file__).parents[2] / "config/classifications.toml")
    )


def test_vrp() -> None:
    out = runner.invoke(app, ["research", "vrp"])
    assert out.exit_code == 0, out.output
    assert "buy-write" in out.output
    assert "put-write" in out.output
    assert "rolls, average premium" in out.output


def test_orb_and_pairs() -> None:
    orb = runner.invoke(app, ["research", "orb", "--direction", "both"])
    assert orb.exit_code == 0, orb.output
    assert "20 sessions" in orb.output
    pairs = runner.invoke(app, ["research", "pairs"])
    assert pairs.exit_code == 0, pairs.output
    assert "Correlation with SPY" in pairs.output


def test_missing_data_says_what_to_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "empty"))
    out = runner.invoke(app, ["research", "orb"])
    assert out.exit_code == 2
    assert "tp-data bars intraday" in out.output
