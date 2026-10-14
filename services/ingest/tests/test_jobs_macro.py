"""Macro series: each source (recorded-format files, no network), the fallbacks, the job and
its CLI."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import requests
import responses
from typer.testing import CliRunner

from tp_core.macro import load_macro
from tp_core.storage import Lake, parquet_files
from tp_ingest import cli
from tp_ingest.jobs.macro import run_backfill, run_macro
from tp_ingest.sources.macro import (
    CBOE,
    FRED_API,
    FRED_CSV,
    TREASURY,
    AutoMacroSource,
    CboeSource,
    FakeMacroSource,
    FredApiSource,
    FredCsvSource,
    MacroUnavailableError,
    TreasurySource,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)


def body(name: str) -> str:
    return (FIXTURES / name).read_text()


@responses.activate
def test_fred_csv() -> None:
    responses.get(
        FRED_CSV,
        body=body("fred_vixcls.csv"),
        match=[responses.matchers.query_param_matcher({"id": "VIXCLS", "cosd": "2026-10-01"})],
    )
    df = FredCsvSource().series("VIXCLS", date(2026, 10, 1))
    assert len(df) == 4  # the "." day is dropped
    assert df["value"].iloc[-1] == 17.9


@responses.activate
def test_fred_api() -> None:
    responses.get(
        FRED_API,
        body=body("fred_api_DGS10.json"),
        match=[responses.matchers.query_param_matcher(
            {"series_id": "DGS10", "api_key": "k", "file_type": "json",
             "observation_start": "2026-10-01"})],
    )  # fmt: skip
    df = FredApiSource("k").series("DGS10", date(2026, 10, 1))
    assert df["value"].tolist() == [4.12, 4.10, 4.15]
    responses.get(FRED_API, json={"error_code": 400, "error_message": "Bad Request. The value"})
    with pytest.raises(ValueError, match="no observations"):
        FredApiSource("k").series("DGS10", date(2026, 10, 1))


@responses.activate
def test_cboe_and_treasury_are_the_publishers() -> None:
    responses.get(CBOE.format(name="VIX"), body=body("cboe_VIX_History.csv"))
    vix = CboeSource().series("VIXCLS", date(2026, 1, 1))
    assert vix["date"].tolist() == [date(2026, 10, 9), date(2026, 10, 12)]
    assert vix["value"].iloc[-1] == 17.94
    with pytest.raises(KeyError):
        CboeSource().series("DGS10", date(2026, 1, 1))

    responses.get(TREASURY.format(year=2025), body=body("treasury_2025.csv"))
    responses.get(TREASURY.format(year=2026), body=body("treasury_2026.csv"))
    treasury = TreasurySource(today=date(2026, 10, 13))
    ten = treasury.series("DGS10", date(2025, 12, 31))
    three = treasury.series("DGS3MO", date(2025, 12, 31))
    assert ten["value"].tolist() == [4.25, 4.06, 4.05]  # sorted by date, from the start
    assert three["value"].iloc[-1] == 3.88
    assert len(responses.calls) == 1 + 2  # each year downloaded once for all maturities


class Dead:
    name = "dead"

    def __init__(self) -> None:
        self.calls = 0

    def series(self, series_id: str, start: date) -> object:
        self.calls += 1
        raise requests.ConnectTimeout("timed out")


def test_auto_falls_back_and_stops_trying_a_dead_host() -> None:
    dead, fake = Dead(), FakeMacroSource(end=date(2026, 10, 9))
    auto = AutoMacroSource(
        {"fred-api": dead, "cboe": fake, "treasury": fake, "fred-csv": dead},  # type: ignore[dict-item]
        ["fred-api", "publisher", "fred-csv"],
    )
    for s in ("VIXCLS", "DGS10", "DGS2"):
        assert len(auto.series(s, date(2026, 10, 1))) > 0
    assert dead.calls == 1  # unreachable once: skipped for the rest of the run
    assert auto.used == {"VIXCLS": "cboe", "DGS10": "treasury", "DGS2": "treasury"}

    other = Dead()
    nothing = AutoMacroSource(
        {"cboe": other, "fred-csv": other},  # type: ignore[dict-item]
        ["publisher", "fred-csv"],
    )
    with pytest.raises(
        MacroUnavailableError, match=r"cboe: ConnectTimeout.*fred-csv: ConnectTimeout"
    ):
        nothing.series("VIXCLS", date(2026, 10, 1))
    with pytest.raises(MacroUnavailableError, match=r"cboe: skipped.*fred-csv: skipped"):
        nothing.series("VXVCLS", date(2026, 10, 1))
    assert other.calls == 2
    default = AutoMacroSource.default("key")
    assert default.order == ["fred-api", "publisher", "fred-csv"]
    assert AutoMacroSource.default(None).order == ["publisher", "fred-csv"]


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
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    no_key = CliRunner().invoke(cli.app, ["macro", "daily", "--source", "fred-api"])
    assert no_key.exit_code == 2
    assert "FRED_API_KEY" in no_key.output


def test_summary_names_the_source_and_each_failure(tmp_path: Path) -> None:
    fake = FakeMacroSource(end=NOW.date(), fail={"DGS2"})
    auto = AutoMacroSource({"cboe": fake, "treasury": fake}, ["publisher"])
    result = run_macro(Lake(tmp_path), auto, now=NOW)
    assert "VIXCLS" in result.summary()
    assert "from cboe" in result.summary()
    assert result.sources["DGS10"] == "treasury"
    assert "\n  FAILED DGS2: treasury: HTTPError" in result.summary()
