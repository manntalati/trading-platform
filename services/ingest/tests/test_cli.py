from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.config import Settings
from tp_ingest import cli
from tp_ingest.sources.fake import FakeSource

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env
    universes = tmp_path / "universes.toml"
    universes.write_text('[bars]\netfs = ["SPY"]\nstocks = ["AAPL"]\n')
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("TP_UNIVERSES_FILE", str(universes))
    for var in ("ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    return tmp_path


def test_backfill_daily_rebuild_with_fake_source(env: Path) -> None:
    result = runner.invoke(cli.app, ["bars", "backfill", "--source", "fake", "--years", "1"])
    assert result.exit_code == 0, result.output
    assert "0 errors" in result.output
    assert list((env / "lake/clean/stock_bars_1d").iterdir())

    assert runner.invoke(cli.app, ["bars", "daily", "--source", "fake"]).exit_code == 0
    assert runner.invoke(cli.app, ["bars", "rebuild"]).exit_code == 0


def test_symbols_option_overrides_universe(env: Path) -> None:
    result = runner.invoke(
        cli.app, ["bars", "backfill", "--source", "fake", "--years", "1", "--symbols", "qqq, iwm"]
    )
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in (env / "lake/clean/stock_bars_1d").iterdir())
    assert names == ["symbol=IWM", "symbol=QQQ"]


def test_missing_keys_exit_2() -> None:
    result = runner.invoke(cli.app, ["bars", "daily"])
    assert result.exit_code == 2
    assert "ALPACA_API_KEY" in result.output


def test_missing_universe_file_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TP_UNIVERSES_FILE", "does/not/exist.toml")
    assert runner.invoke(cli.app, ["bars", "daily", "--source", "fake"]).exit_code == 2


def test_validation_errors_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def corrupt(df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(close=-1.0)

    def make_source(name: cli.SourceName, settings: Settings) -> FakeSource:
        return FakeSource(mutate=corrupt)

    monkeypatch.setattr(cli, "make_source", make_source)
    result = runner.invoke(cli.app, ["bars", "backfill", "--years", "1"])
    assert result.exit_code == 1
    assert "ERROR SPY" in result.output


def test_check_with_fake_source() -> None:
    result = runner.invoke(cli.app, ["check", "--source", "fake"])
    assert result.exit_code == 0
    assert "no network" in result.output
