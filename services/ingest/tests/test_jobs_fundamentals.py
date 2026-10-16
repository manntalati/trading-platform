"""SEC EDGAR download (recorded-style JSON, no network), the fundamentals job and its CLI."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import responses
from typer.testing import CliRunner

from tp_core.fundamentals import load_events, load_facts, quarterly
from tp_core.storage import Lake, parquet_files
from tp_ingest import cli
from tp_ingest.jobs.fundamentals import run_fundamentals
from tp_ingest.sources.sec import (
    EXTRA_URL,
    FACTS_URL,
    SUBMISSIONS_URL,
    TICKERS_URL,
    FakeSecSource,
    SecSource,
    edgar_ticker,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
AGENT = "trading-platform test@example.com"


def body(name: str) -> str:
    return (FIXTURES / name).read_text()


@responses.activate
def test_sec_source_sends_the_user_agent_and_reads_every_page() -> None:
    ua = [responses.matchers.header_matcher({"User-Agent": AGENT})]
    responses.get(TICKERS_URL, body=body("sec_company_tickers.json"), match=ua)
    responses.get(
        FACTS_URL.format(cik=42), body=body("sec_companyfacts_CIK0000000042.json"), match=ua
    )
    responses.get(SUBMISSIONS_URL.format(cik=42), body=body("sec_submissions_CIK0000000042.json"))
    responses.get(
        EXTRA_URL.format(name="CIK0000000042-submissions-001.json"),
        body=body("sec_submissions_CIK0000000042-001.json"),
    )
    sec = SecSource(AGENT, pause=0.0)
    assert sec.tickers() == {"XYZ": 42, "BRK-B": 77}
    assert sec.company_facts(42)["cik"] == 42
    _, extra = sec.submissions(42)
    assert len(extra) == 1
    assert extra[0]["form"][0] in ("8-K", "10-K")
    assert edgar_ticker("brk.b") == "BRK-B"


@responses.activate
def test_a_filer_without_financial_data_has_no_facts_rather_than_failing() -> None:
    responses.get(FACTS_URL.format(cik=7), status=404)  # e.g. an ETF's trust
    responses.get(FACTS_URL.format(cik=8), status=500)
    sec = SecSource(AGENT, pause=0.0)
    assert sec.company_facts(7) == {"cik": 7, "facts": {}}
    with pytest.raises(requests.HTTPError):
        sec.company_facts(8)


def test_job_writes_raw_and_clean_and_lists_what_it_couldnt_get(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    source = FakeSecSource(["XYZ", "ABC", "SPY"], missing={"SPY"})
    result = run_fundamentals(lake, source, ["XYZ", "abc", "SPY"], now=NOW)
    assert result.ok
    assert result.no_cik == ["SPY"]
    assert result.facts["XYZ"] > 0
    assert result.releases["ABC"] == 20  # five years of quarters
    facts = load_facts(lake)
    assert set(facts["symbol"]) == {"XYZ", "ABC"}
    assert len(quarterly(facts, "XYZ", "eps_diluted")) == 20
    assert set(load_events(lake)["symbol"]) == {"XYZ", "ABC"}
    assert len(parquet_files(lake.raw_dir("sec/company_facts"))) == 1
    assert (lake.reports_dir("fundamentals") / "2026-10-09.json").exists()


def test_one_company_failing_is_reported(tmp_path: Path) -> None:
    class Flaky(FakeSecSource):
        def company_facts(self, cik: int) -> dict[str, Any]:
            if cik == self.tickers()["ABC"]:
                raise ConnectionError("reset by peer")
            return super().company_facts(cik)

    result = run_fundamentals(Lake(tmp_path), Flaky(["XYZ", "ABC"]), ["XYZ", "ABC"], now=NOW)
    assert not result.ok
    assert "ABC" in result.errors
    assert "FAILED ABC" in result.summary()
    assert set(load_facts(Lake(tmp_path))["symbol"]) == {"XYZ"}


def test_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("TP_SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    runner = CliRunner()
    ok = runner.invoke(cli.app, ["fundamentals", "--source", "fake", "--symbols", "XYZ,ABC"])
    assert ok.exit_code == 0, ok.output
    assert "2/2 companies with facts" in ok.output
    assert runner.invoke(cli.app, ["fundamentals", "--symbols", "XYZ"]).exit_code == 2  # no agent
    assert load_facts(Lake(tmp_path / "lake"))["end"].max() == date(2025, 12, 31)
