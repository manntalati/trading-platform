import json
from datetime import UTC, date, datetime

import pandas as pd

from tp_core.testing import raw_bars
from tp_core.validate import (
    Severity,
    ValidationReport,
    check_missing_sessions,
    check_price_jumps,
    check_structure,
)

NOW = datetime(2024, 7, 1, 22, 0, tzinfo=UTC)


def _checks(issues: list) -> list[str]:  # type: ignore[type-arg]
    return [i.check for i in issues]


def test_clean_bars_pass() -> None:
    good, quarantined, issues = check_structure(raw_bars("A", [10, 11, 12]), now=NOW)
    assert len(good) == 3
    assert quarantined.empty
    assert issues == []


def test_timestamp_not_midnight_new_york() -> None:
    bars = raw_bars("A", [10, 11, 12])
    bars.loc[1, "timestamp"] += pd.Timedelta(hours=4)  # e.g. vendor sent UTC midnight-ish
    good, quarantined, issues = check_structure(bars, now=NOW)
    assert _checks(issues) == ["bad_timestamp"]
    assert list(quarantined["reason"]) == ["bad_timestamp"]
    assert len(good) == 2


def test_non_session_and_future_timestamps() -> None:
    bars = raw_bars("A", [10, 11])
    bars.loc[0, "session"] = date(2024, 6, 1)  # a Saturday
    bars.loc[0, "timestamp"] = pd.Timestamp("2024-06-01 04:00", tz="UTC")
    _, _, issues = check_structure(bars, now=NOW)
    assert _checks(issues) == ["bad_timestamp"]

    _, quarantined, _ = check_structure(
        raw_bars("A", [10, 11]), now=datetime(2024, 6, 3, 12, tzinfo=UTC)
    )
    assert len(quarantined) == 1  # the 2024-06-04 bar is in the future


def test_duplicates_within_a_run_are_errors() -> None:
    bars = raw_bars("A", [10, 11])
    doubled = pd.concat([bars, bars.iloc[[1]]], ignore_index=True)
    good, quarantined, issues = check_structure(doubled, now=NOW)
    assert _checks(issues) == ["duplicate", "duplicate"]
    assert len(good) == 1
    assert len(quarantined) == 2


def test_bad_prices_and_inconsistent_ohlc() -> None:
    bars = raw_bars("A", [10, 11, 12, 13])
    bars.loc[0, "close"] = 0.0
    bars.loc[1, "open"] = float("nan")
    bars.loc[2, "high"] = 11.0  # below close of 12
    bars.loc[3, "volume"] = -5.0
    good, quarantined, issues = check_structure(bars, now=NOW)
    assert good.empty
    assert sorted(quarantined["reason"]) == [
        "bad_price",
        "bad_price",
        "negative_volume",
        "ohlc_inconsistent",
    ]
    assert all(i.severity is Severity.ERROR for i in issues)


def test_zero_volume_is_a_warning_and_kept() -> None:
    bars = raw_bars("A", [10, 11])
    bars.loc[1, "volume"] = 0.0
    good, quarantined, issues = check_structure(bars, now=NOW)
    assert len(good) == 2
    assert quarantined.empty
    assert [(i.check, i.severity) for i in issues] == [("zero_volume", Severity.WARNING)]


def test_missing_sessions_grouped_into_runs() -> None:
    bars = raw_bars("A", [10.0] * 10)
    dropped = bars.drop(index=[2, 3, 6])  # 2024-06-05, 06-06 and 06-11
    issues = check_missing_sessions(dropped)
    assert [i.detail for i in issues] == [
        "2 session(s): 2024-06-05..2024-06-06",
        "1 session(s): 2024-06-11",
    ]


def test_price_jumps_on_adjusted_close() -> None:
    bars = raw_bars("A", [100, 105, 140, 141]).assign(adj_close=[100, 105, 140, 141])
    issues = check_price_jumps(bars, threshold=0.20)
    assert len(issues) == 1
    assert issues[0].session == date(2024, 6, 5)
    assert "+33.3%" in issues[0].detail


def test_report_is_json_serialisable() -> None:
    _, _, issues = check_structure(raw_bars("A", [10, 11]).assign(volume=0.0), now=NOW)
    report = ValidationReport(rows_in=2, rows_clean=2, symbols=1, issues=issues)
    assert report.ok
    payload = json.loads(json.dumps(report.to_dict(), default=str))
    assert payload["counts"] == {"warning:zero_volume": 2}
