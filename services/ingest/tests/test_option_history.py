"""Fetching past contracts' daily bars for the playbook's backtest (fake source, SPY and AAPL)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core import option_history
from tp_core.storage import Lake
from tp_ingest.jobs.option_history import match, run_option_history
from tp_ingest.sources.fake import FakeSource

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)


def request(underlying: str, right: str, expiration: str, strike: float, start: str,
            end: str) -> dict[str, object]:  # fmt: skip
    return {"key": f"{underlying}|{right}|{expiration}|{strike:.2f}", "underlying": underlying,
            "right": right, "expiration": expiration, "strike": strike, "start": start,
            "end": end}  # fmt: skip


def test_match_the_nearest_listed_contract() -> None:
    source = FakeSource(as_of=date(2024, 7, 12))
    k = round(source._spot("SPY") / 5) * 5.0  # a listed $5 strike near the price
    listed = source.listed_contracts(
        "SPY", right="C", expiration=(date(2024, 5, 1), date(2024, 8, 31)),
        strikes=(k * 0.8, k * 1.2),
    )  # fmt: skip
    best = match(listed, date(2024, 6, 14), k + 1.0)
    assert best is not None
    assert (best["expiration"], best["strike"]) == (date(2024, 6, 21), k)  # the third Friday
    assert match(listed, date(2024, 6, 14), k * 1.5) is None  # no strike within 10%
    assert match(listed, date(2025, 6, 14), k) is None  # no expiry within 35 days
    assert match(listed.iloc[:0], date(2024, 6, 14), k) is None


def test_fetch_once_and_skip_what_is_known(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    source = FakeSource(as_of=date(2024, 7, 12))
    spot = source._spot("SPY")
    wanted = [
        request("SPY", "C", "2024-06-21", round(spot, 2), "2024-04-01", "2024-05-15"),
        request("SPY", "P", "2024-06-21", round(spot, 2), "2024-04-10", "2024-05-01"),
        request("AAPL", "C", "2024-06-21", 10_000.0, "2024-04-01", "2024-05-15"),  # nothing near
    ]
    result = run_option_history(lake, source, wanted, now=NOW)
    assert result.ok
    assert (result.new, result.matched, result.unmatched) == (3, 2, 1)
    bars = option_history.load_bars(lake)
    assert set(bars["contract"]) == set(option_history.load_matches(lake)["contract"].dropna())
    assert bars["session"].min() >= date(2024, 4, 1)
    assert bars["close"].gt(0).all()
    matches = option_history.load_matches(lake).set_index("key")
    assert pd.isna(matches.loc[wanted[2]["key"], "contract"])
    assert result.bars == len(bars)

    again = run_option_history(lake, source, wanted, now=NOW)
    assert (again.new, again.bars) == (0, 0)  # every request already matched
    assert len(option_history.load_bars(lake)) == len(bars)


def test_one_failing_underlying_does_not_stop_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = Lake(tmp_path)
    source = FakeSource(as_of=date(2024, 7, 12))
    real = source.listed_contracts

    def flaky(underlying: str, **kwargs: object) -> object:
        if underlying == "AAPL":
            raise ConnectionError("synthetic")
        return real(underlying, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source, "listed_contracts", flaky)
    spot = source._spot("SPY")
    wanted = [
        request("AAPL", "C", "2024-06-21", 100.0, "2024-04-01", "2024-05-01"),
        request("SPY", "C", "2024-06-21", round(spot, 2), "2024-04-01", "2024-05-01"),
    ]
    result = run_option_history(lake, source, wanted, now=NOW)
    assert not result.ok
    assert "AAPL C" in result.errors
    assert result.matched == 1


def test_nothing_wanted_is_still_reported(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    result = run_option_history(lake, FakeSource(as_of=date(2024, 7, 12)), [], now=NOW)
    assert (result.requests, result.bars) == (0, 0)
    assert list(lake.reports_dir("option_history").glob("*.json"))
