"""The paper cycle end to end against the simulated broker, with a scripted clock."""

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from tp_paper import jobs
from tp_paper.broker import OrderRequest
from tp_paper.jobs import PaperError
from tp_paper.store import PaperStore


def propose_and_approve(env: Any) -> list[str]:
    jobs.propose(env.paper, env.at(env.month_end_evening))
    pending = env.paper.store.proposals(status="pending")
    for p in pending:
        jobs.decide(env.paper.store, p.id, approve=True)
    return [p.id for p in pending]


def test_month_end_proposals_wait_for_approval_with_reasons_and_checks(env: Any) -> None:
    report = jobs.propose(env.paper, env.at(env.month_end_evening))
    assert report.session == "2024-07-31"
    assert report.proposed["ma-timing"] > 0
    proposals = env.paper.store.proposals(strategy="ma-timing")
    assert {p.status for p in proposals} == {"pending"}  # manual approval
    first = proposals[0]
    assert "10-month average" in first.reason
    assert {c["check"] for c in first.checks} >= {"kill_switch", "gross_exposure", "liquidity"}
    assert all(c["passed"] for c in first.checks)
    assert first.notional <= 20_000 * 0.2 + 1  # sized from the 20k sleeve, not the account
    again = jobs.propose(env.paper, env.month_end_evening)
    assert again.skipped["ma-timing"] == "already proposed for this session"


def test_full_cycle_fills_at_the_open_reconciles_and_marks_sleeves(env: Any) -> None:
    approved = propose_and_approve(env)
    result = jobs.submit(env.paper, env.at(env.next_morning))
    assert sorted(result.submitted) == sorted(approved)
    assert {p.status for p in env.paper.store.proposals(strategy="ma-timing")} == {"submitted"}

    report = jobs.sync(env.paper, env.at(env.after_open))
    assert report.fills == len(approved)
    assert report.breaks == {}
    assert report.recorded_session == "2024-07-31"
    filled = env.paper.store.proposals(status="filled")
    assert len(filled) == len(approved)
    fill = env.paper.store.fills()[0]
    assert fill.session == "2024-08-01"
    assert fill.price > 0

    status = jobs.status(env.paper, env.at(env.next_evening))
    ma = next(s for s in status["sleeves"] if s["strategy"] == "ma-timing")
    assert ma["trades"] == len(approved)
    assert ma["slippage_bps"] == pytest.approx(0.0)  # the simulator fills at the official open
    assert ma["gate"] == {"days": [1, 60], "trades": [len(approved), 30]}
    assert set(ma["positions"]) == {env.paper.store.proposal(i).symbol for i in approved}
    assert status["reconciliation"]["ok"] is True
    assert status["account"]["number"] == "…FAKE"


def test_unapproved_proposals_expire_at_submission(env: Any) -> None:
    jobs.propose(env.paper, env.at(env.month_end_evening))
    result = jobs.submit(env.paper, env.at(env.next_morning))
    assert result.submitted == []
    assert len(result.expired) == len(env.paper.store.proposals(strategy="ma-timing"))
    assert {p.note for p in env.paper.store.proposals(status="expired")} == {
        "not approved before submission"
    }


def test_decisions_reject_or_approve_fewer_shares_never_more(env: Any) -> None:
    jobs.propose(env.paper, env.at(env.month_end_evening))
    first, second, *_ = env.paper.store.proposals(status="pending")
    with pytest.raises(PaperError, match="whole shares"):
        jobs.decide(env.paper.store, first.id, approve=True, quantity=first.quantity + 1)
    with pytest.raises(PaperError, match="whole shares"):
        jobs.decide(env.paper.store, first.id, approve=True, quantity=1.5)
    smaller = jobs.decide(env.paper.store, first.id, approve=True, quantity=1)
    assert (smaller.status, smaller.order_quantity) == ("approved", 1)
    rejected = jobs.decide(env.paper.store, second.id, approve=False, note="not today")
    assert (rejected.status, rejected.note) == ("rejected", "not today")
    with pytest.raises(PaperError, match="is rejected, not pending"):
        jobs.decide(env.paper.store, second.id, approve=True)
    vetoed = jobs.decide(env.paper.store, smaller.id, approve=False, note="changed my mind")
    assert vetoed.status == "rejected"  # an approved, unsent proposal can still be stopped


def test_market_on_open_orders_need_the_overnight_window(env: Any) -> None:
    propose_and_approve(env)
    with pytest.raises(PaperError, match="7:00pm to 9:28am ET"):
        jobs.submit(env.paper, env.at(env.after_open))


def test_kill_switch_cancels_open_orders_and_blocks_submission(env: Any) -> None:
    propose_and_approve(env)
    jobs.submit(env.paper, env.at(env.next_morning))
    canceled = jobs.kill(env.paper, "testing", env.next_morning)
    assert canceled > 0
    assert {p.status for p in env.paper.store.proposals(strategy="ma-timing")} == {"canceled"}
    with pytest.raises(PaperError, match="kill switch engaged"):
        jobs.submit(env.paper, env.next_morning)
    assert any(e["kind"] == "kill" for e in env.paper.store.events())


def test_a_disabled_strategy_cannot_buy_at_submission(env: Any) -> None:
    propose_and_approve(env)
    env.paper.risk.state.set_disabled("ma-timing", "drawdown review")
    result = jobs.submit(env.paper, env.at(env.next_morning))
    assert result.submitted == []
    notes = {p.note for p in env.paper.store.proposals(strategy="ma-timing")}
    assert notes == {"strategy disabled before submission: drawdown review"}


def test_resubmitting_after_a_crash_does_not_duplicate_orders(env: Any) -> None:
    [first, *_] = propose_and_approve(env)
    p = env.paper.store.proposal(first)
    env.at(env.next_morning)
    env.broker.submit(OrderRequest(p.id, p.symbol, "buy", p.quantity))  # sent, then we crashed
    result = jobs.submit(env.paper, env.next_morning)
    assert first in result.submitted
    orders = [
        o
        for o in env.broker.orders_since(datetime(2024, 1, 1, tzinfo=UTC))
        if o.client_order_id == first
    ]
    assert len(orders) == 1


def test_reconciliation_flags_positions_the_ledger_does_not_know(env: Any) -> None:
    env.broker.account()  # create the simulated account
    state = json.loads(env.broker.path.read_text())
    state["positions"]["TSLA"] = 3
    env.broker.path.write_text(json.dumps(state))
    report = jobs.sync(env.paper, env.at(env.after_open))
    assert report.breaks == {"TSLA": {"ledger": 0.0, "broker": 3}}
    assert env.paper.store.get("reconciliation")["ok"] is False


def test_forced_rerun_replaces_pending_proposals_with_new_ids(env: Any) -> None:
    jobs.propose(env.paper, env.at(env.month_end_evening))
    before = {p.id for p in env.paper.store.proposals(strategy="ma-timing")}
    jobs.propose(env.paper, env.month_end_evening, force=True)
    after = env.paper.store.proposals(strategy="ma-timing")
    assert {p.id for p in after if p.status == "expired"} == before
    fresh = [p for p in after if p.status == "pending"]
    assert len(fresh) == len(before)
    assert not {p.id for p in fresh} & before


def test_stale_bars_stop_proposals(env: Any) -> None:
    with pytest.raises(PaperError, match="run `tp-data bars daily`"):
        jobs.propose(env.paper, env.at(datetime(2024, 8, 7, 23, tzinfo=UTC)))


def test_status_and_decisions_work_without_a_broker(env: Any) -> None:
    from tp_paper.broker import UnavailableBroker

    jobs.propose(env.paper, env.at(env.month_end_evening))
    env.paper.broker = UnavailableBroker("no keys")
    status = jobs.status(env.paper, env.month_end_evening)
    assert status["account"] is None
    assert "no keys" in status["broker_error"]
    first = env.paper.store.proposals(status="pending")[0]
    assert jobs.decide(env.paper.store, first.id, approve=True).status == "approved"
    with pytest.raises(PaperError, match="canceling open orders failed"):
        jobs.kill(env.paper, "drill", env.month_end_evening)
    assert env.paper.risk.state.kill_switch() is not None  # engaged anyway


def test_a_new_sleeve_catches_up_instead_of_waiting_for_month_end(env: Any) -> None:
    # 1 August is mid-month: a sleeve that has traded waits for 30 August...
    approved = propose_and_approve(env)
    jobs.submit(env.paper, env.at(env.next_morning))
    jobs.sync(env.paper, env.at(env.after_open))
    assert env.paper.store.fills(strategy="ma-timing")
    traded = jobs.propose(env.paper, env.at(env.next_evening))
    assert traded.proposed["ma-timing"] == 0

    # ... while one that never has takes the positions its last month-end signal calls for.
    fresh = PaperStore.under(env.root / "fresh")
    env.paper.store = fresh
    env.paper.broker = type(env.broker)(env.root / "fresh" / "broker.json", env.broker.prices)
    report = jobs.propose(env.paper, env.next_evening)
    assert report.proposed["ma-timing"] == len(approved)
    reasons = [p.reason for p in fresh.proposals(strategy="ma-timing")]
    assert all("month-end close" in r for r in reasons)


def test_older_databases_gain_the_new_columns(tmp_path: Any) -> None:
    import sqlite3

    from tp_paper.store import SCHEMA

    path = tmp_path / "paper.sqlite"
    with sqlite3.connect(path) as db:  # the schema before time_in_force existed
        db.executescript(SCHEMA.replace(",\n    time_in_force TEXT", ""))
        db.execute(
            "INSERT INTO proposals (id, strategy, session, symbol, side, quantity, order_type, "
            "reference_price, reason, checks, status, created_at, updated_at) "
            "VALUES ('a', 'ma-timing', '2024-07-31', 'SPY', 'buy', 1, 'market', 500, '', '[]', "
            "'approved', 'x', 'x')"
        )
        assert "time_in_force" not in {r[1] for r in db.execute("PRAGMA table_info(proposals)")}
    store = PaperStore(path)
    assert store.proposal("a").time_in_force is None
    assert store.update("a", time_in_force="day")
    assert store.proposal("a").time_in_force == "day"
    PaperStore(path)  # opening again is a no-op


def _fill_month_end_orders(env: Any) -> None:
    propose_and_approve(env)
    jobs.submit(env.paper, env.at(env.next_morning))
    jobs.sync(env.paper, env.at(env.after_open))
    jobs.sync(env.paper, env.at(env.next_evening))  # records 1 August


def _with_capital(env: Any, name: str, capital: float) -> None:
    from dataclasses import replace

    from tp_paper.config import PaperBook

    sleeves = [replace(s, capital=capital) if s.name == name else s for s in env.paper.book.sleeves]
    env.paper.book = PaperBook(tuple(sleeves))


def test_changing_a_sleeves_capital_is_money_moved_not_a_loss(env: Any) -> None:
    _fill_month_end_orders(env)
    friday = datetime(2024, 8, 2, 23, tzinfo=UTC)
    unchanged = jobs.status(env.paper, env.at(friday))
    before = next(s for s in unchanged["sleeves"] if s["strategy"] == "ma-timing")

    _with_capital(env, "ma-timing", 16_000)  # config/paper.toml edited: $4k taken out
    report = jobs.propose(env.paper, friday)
    assert env.paper.risk.state.disabled("ma-timing") is None  # not a 20% "drawdown"
    days = env.paper.store.sleeve_days("ma-timing")
    assert [d.capital for d in days] == [20_000, 20_000, 16_000]
    assert any(
        "ma-timing: capital 20,000 -> 16,000" in e["message"] for e in env.paper.store.events()
    )
    # The smaller sleeve rebalances at once (mid-month) instead of carrying $4k of overdraft.
    trades = env.paper.store.proposals(strategy="ma-timing", session="2024-08-02")
    assert report.proposed["ma-timing"] == len(trades) > 0
    assert {p.side for p in trades} == {"sell"}

    after = next(
        s for s in jobs.status(env.paper, friday)["sleeves"] if s["strategy"] == "ma-timing"
    )
    assert after["equity"] == pytest.approx(before["equity"] - 4_000)
    assert after["return"] == pytest.approx(before["return"])  # performance unchanged
    assert after["max_drawdown"] == pytest.approx(before["max_drawdown"], abs=0.01)


def test_records_from_before_capital_was_stored_get_it_back(env: Any) -> None:
    import sqlite3

    _fill_month_end_orders(env)
    with sqlite3.connect(env.paper.store.path) as db:
        db.execute("UPDATE sleeve_days SET capital = NULL")
    jobs.sync(env.paper, env.at(datetime(2024, 8, 2, 23, tzinfo=UTC)))
    assert {d.capital for d in env.paper.store.sleeve_days("ma-timing")} == {20_000}
    assert not any(e["kind"] == "capital" for e in env.paper.store.events())


def test_a_sleeve_without_bars_yet_waits_and_the_others_still_trade(env: Any) -> None:
    from tp_paper.config import PaperBook, Sleeve
    from tp_strategies.library import build

    risky = Sleeve(build("leveraged-momentum"), 10_000, "auto")
    env.paper.book = PaperBook((*env.paper.book.sleeves, risky))
    assert set(jobs.missing_bars(env.paper)) == set(risky.strategy.symbols())
    report = jobs.propose(env.paper, env.at(env.month_end_evening))
    assert report.skipped["leveraged-momentum"].startswith("no bars yet for FAS, SOXL")
    assert report.proposed["ma-timing"] > 0


def test_bars_behind_lists_only_the_symbols_missing_the_last_close(env: Any) -> None:
    from tp_ingest.jobs.bars import run_daily
    from tp_ingest.sources.fake import FakeSource
    from tp_paper.config import PaperBook, Sleeve
    from tp_strategies.library import build

    assert jobs.bars_behind(env.paper, env.at(env.next_evening)) == []
    risky = Sleeve(build("leveraged-momentum"), 10_000, "auto")
    env.paper.book = PaperBook((*env.paper.book.sleeves, risky))
    funds = sorted(risky.strategy.symbols())
    assert sorted(jobs.bars_behind(env.paper, env.next_evening)) == funds  # none at all

    class UntilJuly31(FakeSource):  # a feed that is a day behind for these funds
        def daily_bars(self, symbols: Any, start: date, end: date) -> Any:
            return super().daily_bars(symbols, start, min(end, date(2024, 7, 31)))

    run_daily(env.paper.lake, UntilJuly31(), funds, now=datetime(2024, 8, 2, 22, 5, tzinfo=UTC))
    assert jobs.missing_bars(env.paper) == []
    assert sorted(jobs.bars_behind(env.paper, env.next_evening)) == funds  # a day behind
    assert jobs.bars_behind(env.paper, env.month_end_evening) == []


def test_the_simulator_waits_for_the_days_bars_before_expiring_an_order(env: Any) -> None:
    friday_evening = datetime(2024, 8, 3, 0, tzinfo=UTC)  # 8pm ET Friday: queued for Monday
    env.at(friday_evening)
    env.broker.submit(OrderRequest("x-1", "SPY", "buy", 1, "opg"))
    env.at(datetime(2024, 8, 5, 21, tzinfo=UTC))  # Monday 5pm: its bar isn't in the lake yet
    assert env.broker.order_by_client_id("x-1").status == "submitted"
    env.at(datetime(2024, 8, 6, 14, tzinfo=UTC))  # Tuesday: still no bar, so it never traded
    assert env.broker.order_by_client_id("x-1").status != "submitted"
