"""Intraday bars: the job (fake source), regular hours, the clean table and the CLI."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.intraday import load_intraday, regular_hours
from tp_core.storage import Lake, parquet_files
from tp_ingest import cli
from tp_ingest.jobs.intraday import run_intraday
from tp_ingest.sources.fake import FakeSource

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)  # Friday evening


def test_job_keeps_regular_hours_and_the_latest_download(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    first = run_intraday(lake, FakeSource(), ["SPY", "QQQ"], now=NOW, days=5)
    assert first.bars == 2 * 5 * 78
    again = run_intraday(lake, FakeSource(), ["SPY"], now=NOW, days=3)
    assert again.sessions == 3
    assert len(parquet_files(lake.raw_dir("alpaca/stock_bars_5m"))) == 2
    bars = load_intraday(lake, ["SPY"])
    assert len(bars) == 5 * 78  # re-downloaded bars replace, not duplicate
    assert sorted(set(bars["session"]))[-1] == date(2024, 7, 12)
    first_bar = bars["timestamp"].iloc[0].tz_convert("America/New_York")
    assert (first_bar.hour, first_bar.minute) == (9, 30)
    assert set(load_intraday(lake, ["SPY"], start=date(2024, 7, 12))["session"]) == {
        date(2024, 7, 12)
    }
    assert load_intraday(lake, ["NONE"]).empty


def test_regular_hours_drops_the_extended_session() -> None:
    stamps = pd.to_datetime(
        ["2024-07-12 13:25", "2024-07-12 13:30", "2024-07-12 19:55", "2024-07-12 20:00"], utc=True
    )
    bars = pd.DataFrame({"timestamp": stamps, "close": [1.0, 2.0, 3.0, 4.0]})
    assert list(regular_hours(bars)["close"]) == [2.0, 3.0]  # 9:30 to 15:55 New York (EDT)


def test_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    out = CliRunner().invoke(
        cli.app, ["bars", "intraday", "--symbols", "spy", "--days", "2", "--source", "fake"]
    )
    assert out.exit_code == 0, out.output
    assert "156 bars" in out.output
