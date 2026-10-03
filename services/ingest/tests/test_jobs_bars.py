from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.bars import load_bars
from tp_core.calendar import sessions
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill, run_daily, years_before
from tp_ingest.sources.fake import FakeDividend, FakeSource, FakeSplit

# Friday 2024-07-12 after the close.
FRI = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)
# Following Wednesday after the close.
WED = datetime(2024, 7, 17, 22, 0, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path)


def test_backfill_writes_raw_and_clean(lake: Lake) -> None:
    result = run_backfill(lake, FakeSource(), ["AAA", "BBB"], now=FRI, years=1)
    assert (result.start, result.end) == (date(2023, 7, 12), date(2024, 7, 12))
    expected_sessions = len(sessions(result.start, result.end))
    assert result.bars_fetched == 2 * expected_sessions
    assert result.report.ok
    assert len(load_bars(lake)) == 2 * expected_sessions
    assert all(p.exists() for p in result.files)


def test_split_and_dividend_flow_through_to_adjusted_prices(lake: Lake) -> None:
    ex = date(2024, 6, 10)
    source = FakeSource(
        splits={"AAA": [FakeSplit(ex, 10.0)]},
        dividends={"AAA": [FakeDividend(date(2024, 5, 10), 0.5)]},
    )
    result = run_backfill(lake, source, ["AAA"], now=FRI, years=1)
    assert result.actions_fetched == 2
    assert result.report.ok
    assert result.report.warnings == []  # no false 90% "jump" on the split date

    bars = load_bars(lake, ["AAA"]).set_index("session")
    raw_move = bars["close"].pct_change().loc[ex]
    adj_move = bars["adj_close"].pct_change().loc[ex]
    assert raw_move < -0.85  # the raw series shows the split
    assert abs(adj_move) < 0.1  # the adjusted series does not
    assert bars.loc[date(2024, 7, 12), "adj_close"] == bars.loc[date(2024, 7, 12), "close"]


def test_daily_refetches_overlap_and_appends(lake: Lake) -> None:
    run_backfill(lake, FakeSource(), ["AAA"], now=FRI, years=1)
    source = FakeSource()
    result = run_daily(lake, source, ["AAA"], now=WED, overlap_sessions=5)
    # Five sessions back from Wed 17th: Thu 11th .. Wed 17th.
    assert (result.start, result.end) == (date(2024, 7, 11), date(2024, 7, 17))
    assert source.calls[0] == ("daily_bars", ("AAA",), date(2024, 7, 11), date(2024, 7, 17))
    assert result.report.ok
    assert load_bars(lake)["session"].max() == date(2024, 7, 17)
    assert not load_bars(lake).duplicated(["symbol", "session"]).any()


def test_daily_catches_up_after_missed_runs(lake: Lake) -> None:
    run_backfill(lake, FakeSource(), ["AAA"], now=datetime(2024, 6, 14, 22, tzinfo=UTC), years=1)
    result = run_daily(lake, FakeSource(), ["AAA"], now=WED, overlap_sessions=5)
    assert result.start == date(2024, 6, 15)  # day after the last stored session
    assert result.report.warnings == []  # no gap left behind


def test_daily_backfills_new_symbols(lake: Lake) -> None:
    run_backfill(lake, FakeSource(), ["AAA"], now=FRI, years=1)
    source = FakeSource()
    result = run_daily(lake, source, ["AAA", "NEW"], now=WED, new_symbol_years=2)
    assert (
        "daily_bars",
        ("NEW",),
        years_before(date(2024, 7, 17), 2),
        date(2024, 7, 17),
    ) in source.calls
    assert result.report.ok
    assert set(load_bars(lake)["symbol"]) == {"AAA", "NEW"}


def test_corrupt_vendor_rows_are_quarantined(lake: Lake) -> None:
    def corrupt(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.loc[df.index[3], "high"] = df.loc[df.index[3], "low"] / 2
        return df

    result = run_backfill(lake, FakeSource(mutate=corrupt), ["AAA"], now=FRI, years=1)
    assert not result.report.ok
    assert [i.check for i in result.report.errors] == ["ohlc_inconsistent"]
    assert result.report.rows_quarantined == 1
    # The hole left by the quarantined row is surfaced as a gap warning.
    assert [i.check for i in result.report.warnings] == ["missing_sessions"]


def test_fake_prices_are_stable_across_windows() -> None:
    source = FakeSource()
    a = source.daily_bars(["AAA"], date(2024, 1, 2), date(2024, 3, 1))
    b = source.daily_bars(["AAA"], date(2024, 2, 1), date(2024, 7, 1))
    merged = a.merge(b, on="timestamp", suffixes=("_a", "_b"))
    assert len(merged) > 10
    np.testing.assert_allclose(merged["close_a"], merged["close_b"])


@pytest.mark.parametrize(("day", "expected"), [(date(2024, 2, 29), date(2023, 2, 28))])
def test_years_before_leap_day(day: date, expected: date) -> None:
    assert years_before(day, 1) == expected
