import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.chains import load_chain_snapshots
from tp_core.storage import Lake
from tp_ingest.jobs.options import run_snapshot
from tp_ingest.sources.fake import FakeSource

# Friday 2024-07-12, 15:45 New York.
NOW = datetime(2024, 7, 12, 19, 45, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path)


def test_snapshot_stores_one_file_per_underlying(lake: Lake) -> None:
    source = FakeSource(as_of=date(2024, 7, 12))
    result = run_snapshot(lake, source, ["SPY", "AAPL"], now=NOW, max_dte=60)

    assert result.ok
    assert [u.underlying for u in result.underlyings] == ["SPY", "AAPL"]
    paths = [u.path for u in result.underlyings]
    assert all(p is not None and p.exists() for p in paths)
    assert "snapshot_date=2024-07-12/underlying=SPY" in str(paths[0])

    chain = load_chain_snapshots(lake, "SPY")
    assert len(chain) == result.underlyings[0].contracts > 100
    assert set(chain["right"]) == {"C", "P"}
    assert chain["dte"].between(0, 60).all()
    assert chain["expiration"].min() == date(2024, 7, 12)  # same-day Friday expiry included
    assert chain["underlying_price"].notna().all()
    assert (chain["open_interest"] == 1000).all()  # merged from the contracts endpoint
    assert (chain["bid"] <= chain["ask"]).all()
    assert str(chain["snapshot_at"].dt.tz) == "UTC"

    report = json.loads(next(lake.reports_dir("options_snapshot").glob("*.json")).read_text())
    assert report["ok"] is True
    assert len(load_chain_snapshots(lake)) == sum(u.contracts for u in result.underlyings)


def test_max_dte_is_passed_to_the_source(lake: Lake) -> None:
    source = FakeSource(as_of=date(2024, 7, 12))
    run_snapshot(lake, source, ["SPY"], now=NOW, max_dte=10)
    assert ("option_chain", ("SPY",), date(2024, 7, 12), date(2024, 7, 22)) in source.calls
    assert load_chain_snapshots(lake)["dte"].max() <= 10


def test_holiday_is_skipped(lake: Lake) -> None:
    july_4th = datetime(2024, 7, 4, 19, 45, tzinfo=UTC)
    result = run_snapshot(lake, FakeSource(), ["SPY"], now=july_4th)
    assert result.skipped is not None
    assert result.ok
    assert load_chain_snapshots(lake).empty


def test_one_failure_does_not_lose_the_rest(lake: Lake) -> None:
    source = FakeSource(as_of=date(2024, 7, 12), failing_underlyings=frozenset({"QQQ"}))
    result = run_snapshot(lake, source, ["SPY", "QQQ", "AAPL"], now=NOW, max_dte=30)
    assert not result.ok
    assert [f.underlying for f in result.failures] == ["QQQ"]
    assert "ConnectionError" in (result.failures[0].error or "")
    assert set(load_chain_snapshots(lake)["underlying"]) == {"SPY", "AAPL"}
    assert "2/3 underlyings stored" in result.summary()


def test_two_runs_same_day_both_kept(lake: Lake) -> None:
    source = FakeSource(as_of=date(2024, 7, 12))
    run_snapshot(lake, source, ["SPY"], now=NOW, max_dte=10)
    run_snapshot(lake, source, ["SPY"], now=NOW + pd.Timedelta(minutes=5), max_dte=10)
    chain = load_chain_snapshots(lake, "SPY")
    assert chain["snapshot_at"].nunique() == 2


def test_underlying_quote_failure_still_stores_chains(
    lake: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource(as_of=date(2024, 7, 12))

    def broken(symbols: object) -> pd.DataFrame:
        raise ConnectionError("quotes endpoint down")

    monkeypatch.setattr(source, "underlying_snapshots", broken)
    result = run_snapshot(lake, source, ["SPY"], now=NOW, max_dte=10)
    assert result.ok
    chain = load_chain_snapshots(lake, "SPY")
    assert len(chain) > 0
    assert chain["underlying_price"].isna().all()
