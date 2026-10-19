"""Stored recommendations: what's new, the log and the track record (synthetic XYZ, ABC)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from tp_core.bars import close_matrix, load_bars
from tp_core.occ import format_occ
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, write_raw
from tp_core.testing import synthetic_chain
from tp_ml.testing import write_lake
from tp_strategies import recommendations as recs

CALL = format_occ("XYZ", date(2024, 1, 19), "C", 100.0)
PUT = format_occ("ABC", date(2023, 12, 15), "P", 90.0)  # expires before the record's date


def leg(contract: str, side: str, price: float, expiration: str, strike: float,
        right: str) -> dict[str, Any]:  # fmt: skip
    return {"contract": contract, "side": side, "price": price, "expiration": expiration,
            "strike": strike, "right": right, "bid": price, "ask": price}  # fmt: skip


def idea(
    symbol: str, kind: str, pick: dict[str, Any] | None, spot: float = 100.0
) -> dict[str, Any]:
    return {"symbol": symbol, "kind": kind, "score": 0.8, "spot": spot, "pick": pick}


def run(at: datetime, session: date, **lists: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": f"{at:%Y%m%dT%H%M%SZ}-abcd1234",
        "as_of": at.isoformat(),
        "bars_session": session.isoformat(),
        "research": {k: lists.get(k, []) for k in recs.LISTS},
        "portfolio": {"ideas": []},
    }


def test_legs_of_every_structure() -> None:
    single = leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")
    spread = {"long": single, "short": leg(CALL, "sell", 0.8, "2024-01-19", 110.0, "call"),
              "debit": 1.2}  # fmt: skip
    condor = {"kind": "iron condor", "put_spread": {"short": single, "long": single},
              "call_spread": {"short": single, "long": single}}  # fmt: skip
    assert len(recs.legs(single)) == 1
    assert [x["side"] for x in recs.legs(spread)] == ["buy", "sell"]
    assert recs._entry(recs.legs(spread)) == pytest.approx(1.2)  # debit
    assert len(recs.legs(condor)) == 4
    assert recs.legs(None) == []


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return write_lake(tmp_path, ["SPY", "XYZ", "ABC"], n=300, seed=3)


def test_new_ideas_are_measured_against_the_previous_session(lake: Lake) -> None:
    day1 = datetime(2023, 11, 1, 20, 0, tzinfo=UTC)
    first = run(day1, date(2023, 11, 1), swing=[idea("XYZ", "swing", None)])
    recs.store_run(lake, first, now=day1)
    assert first["research"]["swing"][0]["new"] is False  # nothing to compare the first run with

    later = day1 + timedelta(hours=2)  # same day: the morning's run isn't "previous"
    second = run(
        later, date(2023, 11, 1), swing=[idea("XYZ", "swing", None), idea("ABC", "swing", None)]
    )
    recs.store_run(lake, second, now=later)
    assert second["previous_run"] is None

    day2 = day1 + timedelta(days=1)
    third = run(day2, date(2023, 11, 2), swing=[idea("ABC", "swing", None)],
                income=[idea("XYZ", "income", None)])  # fmt: skip
    result = recs.store_run(lake, third, now=day2)
    assert third["previous_run"] == second["run_id"]
    assert [i["new"] for i in third["research"]["swing"]] == [False]  # ABC was there yesterday
    assert third["research"]["income"][0]["new"] is True
    assert third["dropped"]["swing"] == ["XYZ"]
    assert result.new == 1
    assert recs.latest_run(lake)["run_id"] == third["run_id"]


def test_track_record_marks_positions_and_moves(lake: Lake) -> None:
    closes = close_matrix(load_bars(lake))
    start = closes.index[-40].date()
    at = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
    call = leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")
    put = leg(PUT, "sell", 1.5, "2023-12-15", 90.0, "put")
    recs.store_run(lake, run(at, start, swing=[idea("XYZ", "swing", call)],
                             income=[idea("ABC", "income", put)]), now=at)  # fmt: skip
    # the same idea the next day is still one recommendation, seen in two runs
    nxt = at + timedelta(days=1)
    recs.store_run(lake, run(nxt, closes.index[-39].date(),
                             swing=[idea("XYZ", "swing", call)]), now=nxt)  # fmt: skip

    end = closes.index[-1].date()
    spot = float(closes["XYZ"].iloc[-1])
    snap_at = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(hours=20)
    chain = synthetic_chain("XYZ", spot, snap_at, expirations=[date(2024, 1, 19)],
                            strikes=[100.0], half_spread=0.0)  # fmt: skip
    write_raw(lake, RAW_OPTION_CHAIN_SNAPSHOTS, chain, run_id="r-xyz",
              partition={"snapshot_date": end.isoformat(), "underlying": "XYZ"},
              schema=RAW_OPTION_CHAIN_SCHEMA)  # fmt: skip
    mid = float(chain.loc[chain["contract"] == CALL, "bid"].iloc[0])

    record = recs.track_record(lake, end)
    rows = {r["list"]: r for r in record["recommendations"]}
    swing = rows["swing"]
    assert swing["runs"] == 2
    assert swing["structure"] == "long call"
    assert swing["result"] == pytest.approx(mid / 2.0 - 1)
    assert swing["pnl"] == pytest.approx((mid - 2.0) * 100)
    assert swing["move"] == pytest.approx(spot / float(closes["XYZ"].loc[pd.Timestamp(start)]) - 1)
    assert swing["move_5d"] is not None and swing["move_21d"] is not None  # noqa: PT018

    income = rows["income"]  # the put expired: worth its intrinsic value at expiry
    abc = close_matrix(load_bars(lake, ["ABC"]), adjusted=False)["ABC"]
    at_expiry = float(abc[abc.index <= pd.Timestamp(date(2023, 12, 15))].iloc[-1])
    owed = max(90.0 - at_expiry, 0.0)
    assert income["status"] == "expired"
    assert income["structure"] == "cash-secured put"
    assert income["result"] == pytest.approx((1.5 - owed) / 1.5)  # the share of the premium kept

    summary = {s["list"]: s for s in record["summary"]}
    assert summary["swing"]["recommendations"] == 1
    assert summary["swing"]["profitable"] in (0.0, 1.0)


def test_nothing_is_scored_before_a_session_has_passed(lake: Lake) -> None:
    end = close_matrix(load_bars(lake)).index[-1].date()
    at = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
    recs.store_run(lake, run(at, end, swing=[idea("XYZ", "swing", None)]), now=at)
    (row,) = recs.track_record(lake, end)["recommendations"]
    assert row["structure"] == "no contract"
    assert row["move"] is None
    assert row["move_vs_spy"] is None
    assert recs.track_record(lake, end)["summary"][0]["beat_spy"] is None


def test_old_full_runs_are_pruned_but_the_log_stays(lake: Lake) -> None:
    old = datetime(2023, 9, 1, 20, 0, tzinfo=UTC)
    recs.store_run(lake, run(old, date(2023, 9, 1), swing=[idea("XYZ", "swing", None)]), now=old)
    now = old + timedelta(days=recs.KEEP_DAYS + 5)
    recs.store_run(lake, run(now, date(2023, 10, 6), swing=[idea("ABC", "swing", None)]), now=now)
    assert len(recs.run_paths(lake)) == 1
    assert set(recs.load_log(lake)["symbol"]) == {"XYZ", "ABC"}


def test_input_stamp_changes_when_a_job_writes(lake: Lake) -> None:
    before = recs.input_stamp(lake)
    (lake.reports_dir("options_snapshot")).mkdir(parents=True, exist_ok=True)
    (lake.reports_dir("options_snapshot") / "2023-12-29-r1.json").write_text("{}")
    assert recs.input_stamp(lake) != before
