"""FRED download (recorded CSV, no network), the macro job and its CLI."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses
from typer.testing import CliRunner

from tp_core.macro import load_macro
from tp_core.storage import Lake, parquet_files
from tp_ingest import cli
from tp_ingest.jobs.macro import run_backfill, run_macro
from tp_ingest.sources.fred import URL, FakeMacroSource, FredSource

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)


@responses.activate
def test_fred_source_parses_the_csv_download() -> None:
    responses.get(
        URL,
        body=(FIXTURES / "fred_vixcls.csv").read_text(),
        match=[responses.matchers.query_param_matcher({"id": "VIXCLS", "cosd": "2026-10-01"})],
    )
    df = FredSource().series("VIXCLS", date(2026, 10, 1))
    assert len(df) == 4  # the "." day is dropped
    assert df["value"].iloc[-1] == 17.9
    assert df["date"].iloc[0] == date(2026, 10, 1)


@responses.activate
def test_http_errors_surface() -> None:
    responses.get(URL, status=500)
    with pytest.raises(Exception, match="500"):
        FredSource().series("DGS10", date(2026, 10, 1))


def test_job_stores_raw_and_clean_and_reports_failures(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    first = run_macro(lake, FakeMacroSource(end=NOW.date()), now=NOW)
    assert first.ok  # 30 calendar days of weekdays
    assert first.rows["VIXCLS"] > 15
    assert len(parquet_files(lake.raw_dir("fred/macro_1d"))) == 1
    wide = load_macro(lake)
    assert {"VIXCLS", "VXVCLS", "DGS3MO", "DGS2", "DGS10"} <= set(wide.columns)

    later = datetime(2026, 10, 10, 22, tzinfo=UTC)
    second = run_macro(lake, FakeMacroSource(end=NOW.date(), fail={"DGS2"}), now=later)
    assert not second.ok
    assert "DGS2" in second.errors
    assert "FAILED DGS2" in second.summary()
    assert len(parquet_files(lake.raw_dir("fred/macro_1d"))) == 2  # raw is append-only
    assert (lake.reports_dir("macro") / "2026-10-10.json").exists()


def test_backfill_goes_back_to_1990(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    result = run_backfill(lake, FakeMacroSource(end=date(1991, 1, 4)), now=NOW)
    assert result.start == date(1990, 1, 2)
    assert load_macro(lake).index.min().year == 1990


def test_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    out = CliRunner().invoke(cli.app, ["macro", "daily", "--source", "fake"])
    assert out.exit_code == 0, out.output
    assert "VIXCLS" in out.output
    assert list((tmp_path / "lake/clean/macro_1d").iterdir())
