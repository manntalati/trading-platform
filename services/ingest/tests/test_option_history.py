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


def test_only_standard_contracts_are_matched() -> None:
    from tp_ingest.jobs.option_history import standard

    assert standard("SPY240621C00500000", "SPY")
    assert standard("BRKB240621C00400000", "BRK.B")
    assert not standard("1RIVN260116C00017400", "RIVN")  # adjusted after a corporate action
    assert not standard("AAPL1240621C00100000", "AAPL")
    assert not standard("SPYX240621C00500000", "SPY")


def test_an_adjusted_contract_in_a_batch_costs_nothing_and_misses_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What happened on a real run: Alpaca listed an adjusted contract (``1SPY…``) and refused
    the whole bars request for its batch. It isn't matched any more; and the contracts whose
    bars an earlier run lost are fetched on the next one."""
    lake = Lake(tmp_path)
    source = FakeSource(as_of=date(2024, 7, 12))
    k = round(source._spot("SPY") / 5) * 5.0
    listed, bars = source.listed_contracts, source.option_bars

    def with_adjusted(underlying: str, **kwargs: object) -> pd.DataFrame:
        out = listed(underlying, **kwargs)  # type: ignore[arg-type]
        odd = out.iloc[:1].assign(contract=lambda d: "1" + d["contract"], strike=k)
        return pd.concat([odd, out], ignore_index=True)

    def strict_bars(contracts: list[str], start: date, end: date) -> pd.DataFrame:
        if any(c[0].isdigit() for c in contracts):
            raise ValueError(f"invalid symbol: {next(c for c in contracts if c[0].isdigit())}")
        return bars(contracts, start, end)

    monkeypatch.setattr(source, "listed_contracts", with_adjusted)
    monkeypatch.setattr(source, "option_bars", strict_bars)
    wanted = [request("SPY", "C", e, k + d, "2024-04-01", "2024-05-01")
              for e in ("2024-05-17", "2024-06-21") for d in (0.0, 5.0, 10.0)]  # fmt: skip
    result = run_option_history(lake, source, wanted, now=NOW)
    assert result.ok, result.errors
    matches = option_history.load_matches(lake)
    assert not matches["contract"].str.startswith("1").any()
    assert matches["bars_fetched"].all()

    # an earlier run's lost batch: matched, no bars, never marked fetched
    lost = matches.assign(bars_fetched=None)
    option_history.save_matches(lake, lost)
    (lake.clean_dir(option_history.OPTION_BARS) / "part.parquet").unlink()
    again = run_option_history(lake, source, wanted, now=NOW)
    assert (again.new, again.retried) == (0, len(wanted))
    assert set(option_history.load_bars(lake)["contract"]) == set(matches["contract"])
    third = run_option_history(lake, source, wanted, now=NOW)
    assert (third.new, third.retried, third.bars) == (0, 0, 0)


def test_a_failing_batch_is_split_down_to_the_contract_at_fault(tmp_path: Path) -> None:
    from tp_ingest.jobs.option_history import _fetch_splitting

    asked: list[list[str]] = []

    def fetch(batch: list[str]) -> None:
        asked.append(batch)
        if "bad" in batch:
            raise ValueError("invalid symbol: bad")

    errors: dict[str, str] = {}
    _fetch_splitting(["a", "b", "bad", "c"], fetch, errors)
    assert list(errors) == ["bars bad"]
    assert ["a"] in asked or ["a", "b"] in asked
    assert ["c"] in asked
