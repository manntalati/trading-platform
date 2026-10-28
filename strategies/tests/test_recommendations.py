"""Stored recommendations: what's new, the log and the track record (synthetic XYZ, ABC)."""

import json
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


def test_legs_inside_a_playbook_pick_and_its_plan_are_logged() -> None:
    long = leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")
    short = leg(CALL, "sell", 0.8, "2024-01-19", 110.0, "call")
    pick = {
        "kind": "call debit spread",
        "legs": [long, short],
        "credit": None,
        "entry": {"limit": 1.2},
        "model": {"entry": 1.2},
        "exit": {
            "take_profit": 1.8,
            "stop": 0.6,
            "sell_by": "2024-01-05",
            "out_by": "2023-12-29",
            "stock": {"rule": "above_ma5"},
            "rules": {},
        },
    }
    assert [x["side"] for x in recs.legs(pick)] == ["buy", "sell"]
    at = datetime(2023, 12, 1, 21, 0, tzinfo=UTC)
    ideas = [idea("XYZ", "swing", pick) | {"play": {"name": "pullback"}}]
    (row,) = recs.log_rows(run(at, date(2023, 12, 1), swing=ideas)).to_dict("records")
    assert row["setup"] == "pullback"
    assert row["structure"] == "call debit spread"
    plan = json.loads(row["plan"])
    assert plan == {
        "take_profit": 1.8,
        "stop": 0.6,
        "sell_by": "2024-01-05",
        "out_by": "2023-12-29",
        "stock": {"rule": "above_ma5"},
        "credit": False,
    }


def marks(*values: float, start: date = date(2023, 12, 4)) -> pd.Series:
    days = [start + timedelta(days=i) for i in range(len(values))]
    return pd.Series(values, index=days)


def follow(
    found: list[dict[str, Any]],
    plan: dict[str, Any],
    entry: float,
    series: dict[str, pd.Series],
    today: date = date(2023, 12, 20),
) -> dict[str, Any]:
    out = recs.follow_plan(found, plan, entry, series, None, date(2023, 12, 1),
                           pd.DataFrame(), "XYZ", today)  # fmt: skip
    assert out is not None
    return out


def test_following_the_plan_closes_at_the_first_exit() -> None:
    call = [leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")]
    plan = {"take_profit": 3.0, "stop": 1.0, "sell_by": "2023-12-15", "out_by": "2023-12-29",
            "credit": False}  # fmt: skip
    hit = follow(call, plan, 2.0, {CALL: marks(2.2, 2.6, 3.1, 3.5)})
    assert (hit["status"], hit["reason"], hit["exit"]) == ("closed", "target", "2023-12-06")
    assert hit["result"] == pytest.approx(0.55)
    assert hit["sessions"] == 3
    stopped = follow(call, plan, 2.0, {CALL: marks(1.6, 0.9, 2.5)})
    assert (stopped["reason"], stopped["result"]) == ("stop", pytest.approx(-0.55))
    timed = follow(call, plan, 2.0, {CALL: marks(*[2.1] * 14)})
    assert (timed["reason"], timed["exit"]) == ("time", "2023-12-15")
    still = follow(call, plan, 2.0, {CALL: marks(2.1, 2.3)}, today=date(2023, 12, 6))
    assert (still["status"], still["value"]) == ("open", 2.3)


def test_following_a_credit_plan_buys_it_back() -> None:
    put = [leg(PUT, "sell", 1.5, "2023-12-15", 90.0, "put")]
    plan = {"take_profit": 0.75, "stop": 4.5, "sell_by": None, "out_by": None, "credit": True}
    kept = follow(put, plan, -1.5, {PUT: marks(1.2, 0.9, 0.7)})
    assert (kept["reason"], kept["result"]) == ("target", pytest.approx(0.8 / 1.5))
    blown = follow(put, plan, -1.5, {PUT: marks(2.5, 4.8)})
    assert blown["reason"] == "stop"
    assert blown["pnl"] == pytest.approx((1.5 - 4.8) * 100)


def test_the_plan_is_followed_from_the_session_after_the_recommendation(lake: Lake) -> None:
    closes = close_matrix(load_bars(lake))
    day, nxt = closes.index[-2].date(), closes.index[-1].date()
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=19)
    call = leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")
    pick = {"kind": "call", "legs": [call], "credit": None,
            "exit": {"take_profit": 99.0, "stop": 0.01, "sell_by": "2024-01-10",
                     "out_by": "2024-01-10", "stock": None}}  # fmt: skip
    ideas = [idea("XYZ", "swing", pick) | {"play": {"name": "pullback"}}]
    recs.store_run(lake, run(at, closes.index[-3].date(), swing=ideas), now=at)
    spot = float(closes["XYZ"].iloc[-1])
    for d in (day, nxt):  # the snapshot it was priced from, then the next session's
        snap_at = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=20)
        chain = synthetic_chain("XYZ", spot, snap_at, expirations=[date(2024, 1, 19)],
                                strikes=[100.0], half_spread=0.0)  # fmt: skip
        write_raw(lake, RAW_OPTION_CHAIN_SNAPSHOTS, chain, run_id=f"r-{d}",
                  partition={"snapshot_date": d.isoformat(), "underlying": "XYZ"},
                  schema=RAW_OPTION_CHAIN_SCHEMA)  # fmt: skip
    rows = recs.track_record(lake, nxt)["recommendations"]
    (row,) = [r for r in rows if r.get("setup") == "pullback"]
    followed = row["followed"]
    assert followed["status"] == "open"
    assert followed["sessions"] == 1  # only the session after; not the entry's own snapshot
    assert followed["exit"] == nxt.isoformat()


def test_an_idea_listed_run_after_run_is_tracked_once(lake: Lake) -> None:
    closes = close_matrix(load_bars(lake))
    start = closes.index[-30].date()
    at = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(hours=15)
    first = leg(CALL, "buy", 2.0, "2024-01-19", 100.0, "call")
    other = leg(format_occ("XYZ", date(2024, 1, 19), "C", 105.0), "buy", 1.1, "2024-01-19",
                105.0, "call")  # fmt: skip

    def listed(when: datetime, pick: dict[str, Any], setup: str = "pullback") -> None:
        ideas = [idea("XYZ", "swing", pick) | {"play": {"name": setup}}]
        recs.store_run(lake, run(when, start, swing=ideas), now=when)

    for hours, pick in ((0, first), (1, other), (2, first), (26, other), (50, other)):
        listed(at + timedelta(hours=hours), pick)  # hourly runs; the contract picked shifts
    listed(at + timedelta(hours=27), first, setup="breakout")  # another setup: another idea
    listed(at + timedelta(days=12), other)  # back after more than a week: a new idea

    rows = recs.track_record(lake, closes.index[-1].date())["recommendations"]
    pullbacks = [r for r in rows if r["setup"] == "pullback"]
    assert len(pullbacks) == 2
    later, earlier = pullbacks  # newest first
    assert (earlier["runs"], earlier["contracts"]) == (5, 2)
    assert [x["contract"] for x in earlier["legs"]] == [CALL]  # tracked as the first run picked
    assert earlier["entry"] == pytest.approx(2.0)
    assert earlier["last_seen"] == (at + timedelta(hours=50)).isoformat()
    assert later["runs"] == 1
    assert [r["setup"] for r in rows].count("breakout") == 1
    assert {r["state"] for r in rows} <= {"open", "closed"}
    record = recs.track_record(lake, closes.index[-1].date())
    assert record["total"] == 3
    assert {s["list"]: s["recommendations"] for s in record["summary"]} == {"swing": 3}


def shares_pick(price: float, stop: float, target: float | None, sell_by: str) -> dict[str, Any]:
    exit_plan = {"stop": stop, "target": target, "sell_by": sell_by, "stock": None}
    return {"kind": "shares", "structure": "shares", "price": price,
            "entry": {"side": "debit", "limit": price}, "exit": exit_plan}  # fmt: skip


def test_a_stock_idea_is_logged_and_followed_on_the_bars() -> None:
    at = datetime(2023, 12, 1, 21, 0, tzinfo=UTC)
    ideas = [idea("XYZ", "stocks", shares_pick(100.0, 95.0, 110.0, "2023-12-15"))]
    (row,) = recs.log_rows(run(at, date(2023, 12, 1), stocks=ideas)).to_dict("records")
    assert (row["structure"], row["entry"], row["legs"]) == ("shares", 100.0, "[]")
    assert json.loads(row["plan"]) == {"stop": 95.0, "target": 110.0, "sell_by": "2023-12-15",
                                       "stock": None, "shares": True}  # fmt: skip

    days = pd.DatetimeIndex(pd.bdate_range("2023-12-01", periods=6))
    bars = pd.DataFrame(  # adjusted, half the price: the run saw 50 for the 100 it was sold at
        {"open": [50, 50.5, 49.0, 47.0, 51, 52], "high": [50, 51, 49.5, 48.0, 56, 57],
         "low": [50, 50, 47.4, 46.0, 50, 51], "close": [50, 50.6, 48.5, 47.5, 55, 56]},
        index=days,
    )  # fmt: skip
    plan = json.loads(row["plan"])
    stopped = recs.follow_shares(plan, 100.0, bars, None, days[0], date(2023, 12, 1))
    assert stopped is not None
    assert (stopped["reason"], stopped["exit"]) == ("stop", "2023-12-05")  # the low reached 95
    assert stopped["value"] == pytest.approx(95.0)
    assert stopped["pnl"] == pytest.approx(-500.0)  # 100 shares
    gapped = recs.follow_shares(plan | {"stop": 98.5}, 100.0, bars, None, days[0],
                                date(2023, 12, 1))  # fmt: skip
    assert gapped is not None
    assert (gapped["reason"], gapped["value"]) == (
        "stop",
        pytest.approx(98.0),
    )  # filled at the open
    won = recs.follow_shares(plan | {"stop": 80.0}, 100.0, bars, None, days[0], date(2023, 12, 1))
    assert won is not None
    assert (won["reason"], won["value"]) == ("target", pytest.approx(110.0))
    still = recs.follow_shares(plan | {"stop": 80.0, "target": None, "sell_by": None}, 100.0,
                               bars, None, days[0], date(2023, 12, 1))  # fmt: skip
    assert still is not None
    assert (still["status"], still["value"]) == ("open", pytest.approx(112.0))


def test_the_track_record_marks_and_follows_stock_ideas(lake: Lake) -> None:
    closes = close_matrix(load_bars(lake))
    start = closes.index[-30]
    at = datetime.combine(start.date(), datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
    price = float(closes["XYZ"].loc[start])
    pick = shares_pick(price, price * 0.5, None, closes.index[-10].date().isoformat())
    recs.store_run(lake, run(at, start.date(), stocks=[idea("XYZ", "stocks", pick, spot=price)]),
                   now=at)  # fmt: skip
    record = recs.track_record(lake, closes.index[-1].date())
    (row,) = [r for r in record["recommendations"] if r["list"] == "stocks"]
    assert row["structure"] == "shares"
    move = float(closes["XYZ"].iloc[-1]) / price - 1
    assert row["result"] == pytest.approx(move)
    assert row["pnl"] == pytest.approx(price * move * 100)
    followed = row["followed"]
    assert (followed["status"], followed["reason"]) == ("closed", "time")
    assert followed["exit"] == closes.index[-10].date().isoformat()
    assert row["state"] == "closed"
    assert {s["list"] for s in record["summary"]} == {"stocks"}


def test_the_record_by_how_the_news_stood(lake: Lake) -> None:
    closes = close_matrix(load_bars(lake))
    start = closes.index[-30].date()
    at = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
    with_news = [
        idea("XYZ", "swing", None) | {"news": {"sentiment": 0.4, "headlines": 3}},
        idea("ABC", "swing", None) | {"news": None},  # no headlines that run
    ]
    against = [idea("XYZ", "bearish", None) | {"news": {"sentiment": 0.5, "headlines": 4}}]
    recs.store_run(lake, run(at, start, swing=with_news, bearish=against), now=at)
    before = at - timedelta(days=20)  # a run from before the news was kept
    recs.store_run(lake, run(before, closes.index[-45].date(), leaps=[idea("ABC", "leaps", None)]),
                   now=before)  # fmt: skip
    log = recs.load_log(lake)
    assert log.loc[log["symbol"].eq("ABC") & log["list"].eq("swing"), "headlines"].tolist() == [0]
    record = recs.track_record(lake, closes.index[-1].date())
    groups = {g["group"]: g["recommendations"] for g in record["by_news"]}
    assert groups == {"agreed": 1, "against": 1, "quiet": 1}  # the old LEAPS idea isn't counted
    row = next(r for r in record["recommendations"] if r["list"] == "bearish")
    assert row["news"] == {"sentiment": 0.5, "headlines": 4}
    assert row["news_group"] == "against"
