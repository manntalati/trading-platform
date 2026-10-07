"""The unattended bot, driven through a scripted clock against the simulated broker."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tp_core.calendar import NEW_YORK
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_daily
from tp_ingest.sources.fake import FakeSource
from tp_paper import jobs
from tp_paper.bot import Bot, schedule, tasks_for
from tp_paper.config import PaperBook


def ny(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


JUL31, AUG1, AUG2 = date(2024, 7, 31), date(2024, 8, 1), date(2024, 8, 2)


@pytest.fixture
def auto(env: Any) -> Any:
    """The test book with every sleeve on automatic approval."""
    env.paper.book = PaperBook(tuple(replace(s, approval="auto") for s in env.paper.book.sleeves))
    return env


def outcome(env: Any, key: str) -> str | None:
    record = env.paper.store.get(f"bot:task:{key}")
    return record["outcome"] if record else None


def test_each_session_has_four_tasks_on_exchange_hours() -> None:
    names = {t.name: t for t in tasks_for(AUG1)}
    assert names["submit"].due == ny(AUG1, 9, 10)
    assert names["submit"].deadline == ny(AUG1, 15, 45)  # late: market orders until then
    assert names["sync_open"].due == ny(AUG1, 9, 45)
    assert names["sync_close"].due == ny(AUG1, 16, 30)
    assert names["propose"].due == ny(AUG1, 18, 45)
    assert names["propose"].deadline == ny(AUG2, 15, 30)  # a morning start still proposes
    half_day = {t.name: t for t in tasks_for(date(2024, 7, 3))}
    assert half_day["submit"].deadline == ny(date(2024, 7, 3), 12, 45)
    assert half_day["sync_close"].due == ny(date(2024, 7, 3), 13, 30)
    upcoming = schedule(ny(AUG1, 12))
    assert [t.due for t in upcoming] == sorted(t.due for t in upcoming)
    assert "submit:2024-08-02" in {t.key for t in upcoming}


def test_a_day_runs_itself_without_any_approval(auto: Any) -> None:
    bot = Bot(auto.paper, clock=auto.clock)
    auto.at(ny(JUL31, 17))
    nxt = bot.step()
    assert outcome(auto, "sync_close:2024-07-31") == "done"
    assert nxt is not None
    assert nxt.key == "propose:2024-07-31"

    auto.at(ny(JUL31, 19))
    bot.step()
    assert outcome(auto, "propose:2024-07-31") == "done"
    store = auto.paper.store
    assert store.proposals(status="pending") == []  # nothing waits for a person
    approved = store.proposals(status="approved")
    assert approved
    assert {p.decided_by for p in approved} == {"auto"}

    auto.at(ny(AUG1, 9, 12))
    bot.step()
    assert outcome(auto, "submit:2024-08-01") == "done"
    assert {p.time_in_force for p in store.proposals(status="submitted")} == {"opg"}
    assert store.get("bot:sync_after") is None
    auto.at(ny(AUG1, 10))
    bot.step()
    assert outcome(auto, "sync_open:2024-08-01") == "done"
    assert len(store.fills()) == len(approved)
    assert {p.status for p in store.proposals(session="2024-07-31")} == {"filled"}

    status = jobs.status(auto.paper, auto.clock.now)
    assert status["bot"]["alive"] is True
    assert status["bot"]["next"]["task"] == "sync_close"
    assert status["bot"]["last"]["task"] == "sync_open"
    later = jobs.status(auto.paper, auto.clock.now + timedelta(minutes=30))
    assert later["bot"]["alive"] is False  # no heartbeat for 30 minutes


def test_a_restart_neither_repeats_nor_reports_old_windows(auto: Any) -> None:
    first = Bot(auto.paper, clock=auto.clock)
    auto.at(ny(JUL31, 19))
    first.step()
    proposals = len(auto.paper.store.proposals())
    second = Bot(auto.paper, clock=auto.clock)  # e.g. after a crash
    second.step()
    assert len(auto.paper.store.proposals()) == proposals
    assert not any("missed" in e["message"] for e in auto.paper.store.events())


def test_a_window_that_closes_while_running_is_recorded_as_missed(auto: Any) -> None:
    bot = Bot(auto.paper, clock=auto.clock)
    auto.at(ny(JUL31, 19))
    bot.step()
    auto.at(ny(AUG1, 16, 5))  # slept through the whole session (laptop asleep, say)
    bot.step()
    assert outcome(auto, "submit:2024-08-01") == "missed"
    assert outcome(auto, "sync_open:2024-08-01") == "missed"
    assert any("submit:2024-08-01 missed" in e["message"] for e in auto.paper.store.events())


def test_a_bot_started_mid_session_trades_the_same_day(auto: Any) -> None:
    """Started at 10am on a quiet mid-month Friday with nothing traded yet: it proposes from
    the previous close (sleeves that never traded take their positions now instead of waiting
    for month end) and sends market orders, since the opening auction has passed."""
    store = auto.paper.store
    bot = Bot(auto.paper, clock=auto.clock)
    start = auto.at(ny(AUG2, 10))
    nxt = bot.step()
    assert outcome(auto, "propose:2024-08-01") == "done"
    submit = store.get("bot:task:submit:2024-08-02")
    assert submit["outcome"] == "done"
    assert submit["message"].startswith("after the opening auction cutoff, sent as market orders")
    ma = store.proposals(strategy="ma-timing", session="2024-08-01")
    assert ma  # 1 August is not a month end: this is the catch-up
    assert {(p.status, p.time_in_force, p.decided_by) for p in ma} == {("submitted", "day", "auto")}
    assert not any("missed" in e["message"] for e in store.events())  # old windows are past

    # A follow-up sync shortly after, to record the market orders' fills.
    assert store.get("bot:sync_after") == (start + timedelta(minutes=2)).isoformat()
    assert bot.seconds_until_next(nxt) == pytest.approx(120)
    auto.at(start + timedelta(minutes=3))
    bot.step()
    assert store.get("bot:sync_after") is None
    assert any(e["message"].startswith("follow-up sync") for e in store.events())

    auto.at(ny(AUG2, 16, 35))  # the simulator fills day orders at the close
    bot.step()
    fills = store.fills(strategy="ma-timing")
    assert len(fills) == len(ma)
    assert {f.session for f in fills} == {"2024-08-02"}
    sleeve = next(
        s
        for s in jobs.status(auto.paper, auto.clock.now)["sleeves"]
        if s["strategy"] == "ma-timing"
    )
    assert sleeve["slippage_bps"] is None  # intraday fills aren't compared with the open

    auto.at(ny(AUG2, 19))  # having traded, the sleeve is back on its monthly schedule
    bot.step()
    assert outcome(auto, "propose:2024-08-02") == "done"
    assert store.proposals(strategy="ma-timing", session="2024-08-02") == []


def test_submission_waits_for_a_proposal_run_that_is_still_retrying(auto: Any) -> None:
    feed_up: list[bool] = []

    def refresh(now: datetime) -> str:
        if not feed_up:
            raise RuntimeError("feed down")
        lake = Lake(auto.root)
        symbols = sorted({s for sl in auto.paper.book.sleeves for s in sl.strategy.symbols()})
        return f"bars through {run_daily(lake, FakeSource(), symbols, now=now).end}"

    tuesday = date(2024, 8, 6)  # the lake stops on Friday 2 August: Monday's bars are missing
    bot = Bot(auto.paper, refresh_bars=refresh, clock=auto.clock)
    auto.at(ny(tuesday, 8, 55))
    bot.step()
    auto.at(ny(tuesday, 9, 12))
    bot.step()
    assert outcome(auto, "propose:2024-08-05") is None  # retrying
    assert outcome(auto, "submit:2024-08-06") is None  # not overtaken by an empty submission

    feed_up.append(True)
    auto.at(ny(tuesday, 9, 40))
    bot.step()
    assert outcome(auto, "propose:2024-08-05") == "done"
    submit = auto.paper.store.get("bot:task:submit:2024-08-06")
    assert submit["outcome"] == "done"
    assert "market orders" in submit["message"]
    sent = auto.paper.store.proposals(session="2024-08-05", status="submitted")
    assert sent
    assert {p.time_in_force for p in sent} == {"day"}


def test_stale_bars_are_refreshed_before_proposing(auto: Any) -> None:
    calls: list[datetime] = []

    def refresh(now: datetime) -> str:
        calls.append(now)
        lake = Lake(auto.root)
        symbols = sorted({s for sl in auto.paper.book.sleeves for s in sl.strategy.symbols()})
        result = run_daily(lake, FakeSource(), symbols, now=now)
        return f"bars through {result.end}"

    monday = date(2024, 8, 5)  # the lake stops on Friday 2 August
    bot = Bot(auto.paper, refresh_bars=refresh, clock=auto.clock)
    auto.at(ny(monday, 19))
    bot.step()
    assert len(calls) == 1
    assert outcome(auto, "propose:2024-08-05") == "done"
    assert any("bars through 2024-08-05" in e["message"] for e in auto.paper.store.events())


def test_failures_are_retried_later_then_given_up(auto: Any) -> None:
    attempts: list[datetime] = []

    def broken(now: datetime) -> str:
        attempts.append(now)
        raise RuntimeError("feed down")

    monday = date(2024, 8, 5)
    bot = Bot(auto.paper, refresh_bars=broken, clock=auto.clock)
    start = auto.at(ny(monday, 19))
    nxt = bot.step()
    assert nxt is not None
    assert nxt.key == "propose:2024-08-05"
    assert outcome(auto, "propose:2024-08-05") is None
    assert bot.seconds_until_next(nxt) == pytest.approx(300)  # capped wake-up, retry in 15
    auto.at(start + timedelta(minutes=5))
    bot.step()
    assert len(attempts) == 1  # not yet
    for minutes in range(16, 16 * 9, 16):
        auto.at(start + timedelta(minutes=minutes))
        bot.step()
    assert len(attempts) == 8
    assert outcome(auto, "propose:2024-08-05") == "failed"


def test_kill_switch_skips_submission(auto: Any) -> None:
    bot = Bot(auto.paper, clock=auto.clock)
    auto.at(ny(JUL31, 19))
    bot.step()
    auto.paper.risk.state.set_kill_switch("drill")
    auto.at(ny(AUG1, 9, 12))
    bot.step()
    record = auto.paper.store.get("bot:task:submit:2024-08-01")
    assert record["message"] == "skipped: kill switch engaged"
    assert auto.paper.store.proposals(status="submitted") == []


def test_repo_book_is_automatic() -> None:
    from pathlib import Path

    book = PaperBook.load(Path(__file__).resolve().parents[3] / "config" / "paper.toml")
    assert {s.approval for s in book.sleeves} == {"auto"}


def test_a_new_daily_sleeve_gets_its_bars_and_runs_every_evening(auto: Any) -> None:
    from tp_paper.config import Sleeve
    from tp_strategies.library import build

    refreshed: list[datetime] = []
    # The simulator fills from the lake, whose bars run to 2 August: fetch through then too
    # (just after the template's own ingest, so this run is the latest).
    lake_end = datetime(2024, 8, 2, 22, 5, tzinfo=UTC)

    def refresh(now: datetime) -> str:
        refreshed.append(now)
        lake = Lake(auto.root)
        symbols = sorted({s for sl in auto.paper.book.sleeves for s in sl.strategy.symbols()})
        return f"bars through {run_daily(lake, FakeSource(), symbols, now=lake_end).end}"

    risky = Sleeve(build("leveraged-momentum"), 10_000, "auto")
    auto.paper.book = PaperBook((*auto.paper.book.sleeves, risky))
    store = auto.paper.store
    bot = Bot(auto.paper, refresh_bars=refresh, clock=auto.clock)
    auto.at(ny(JUL31, 19))
    bot.step()
    assert len(refreshed) == 1  # its funds had no bars: their history was fetched first
    assert outcome(auto, "propose:2024-07-31") == "done"
    assert store.proposals(strategy="leveraged-momentum", session="2024-07-31")

    auto.at(ny(AUG1, 9, 12))
    bot.step()
    auto.at(ny(AUG1, 10))
    bot.step()
    assert store.fills(strategy="leveraged-momentum")
    auto.at(ny(AUG1, 19))
    bot.step()
    assert len(refreshed) == 1  # nothing behind any more
    # It runs every evening (whether it trades depends on how far the funds moved: the
    # strategy tests show it trading most days at 3x-fund volatility).
    assert outcome(auto, "propose:2024-08-01") == "done"
    assert not any("leveraged-momentum skipped" in e["message"] for e in store.events())
    assert [d.session for d in store.sleeve_days("leveraged-momentum")][-1] == "2024-08-01"


def test_network_errors_are_recognised() -> None:
    import socket

    import requests

    from tp_paper.bot import is_network_error

    offline = requests.exceptions.ConnectionError(
        "Failed to resolve 'paper-api.alpaca.markets' ([Errno 8] nodename nor servname provided)"
    )
    assert is_network_error(offline)
    assert is_network_error(socket.gaierror(8, "nodename nor servname provided"))
    assert is_network_error(TimeoutError())
    wrapped = RuntimeError("bars refresh failed")
    wrapped.__cause__ = ConnectionRefusedError()  # as `raise ... from` sets it
    assert is_network_error(wrapped)
    assert not is_network_error(RuntimeError("feed down"))
    assert not is_network_error(ValueError("bad symbol"))


def test_no_network_retries_every_two_minutes_until_it_is_back(auto: Any) -> None:
    import requests

    calls: list[datetime] = []
    online: list[bool] = []

    def refresh(now: datetime) -> str:
        calls.append(now)
        if not online:
            raise requests.exceptions.ConnectionError("Failed to resolve 'data.alpaca.markets'")
        lake = Lake(auto.root)
        symbols = sorted({s for sl in auto.paper.book.sleeves for s in sl.strategy.symbols()})
        return f"bars through {run_daily(lake, FakeSource(), symbols, now=now).end}"

    monday = date(2024, 8, 5)  # the lake stops on Friday 2 August: the bot must fetch bars
    bot = Bot(auto.paper, refresh_bars=refresh, clock=auto.clock)
    start = auto.at(ny(monday, 19))
    nxt = bot.step()
    retry = auto.paper.store.get("bot:retry:propose:2024-08-05")
    assert datetime.fromisoformat(retry["after"]) == start + timedelta(minutes=2)
    assert bot.seconds_until_next(nxt) == pytest.approx(120)
    assert any("no network, retrying at" in e["message"] for e in auto.paper.store.events())

    for minutes in range(5, 5 * 12, 5):  # an hour offline, a pass every 5 minutes (cron)
        auto.at(start + timedelta(minutes=minutes))
        bot.step()
    assert len(calls) == 12  # more than MAX_ATTEMPTS: being offline is no reason to give up
    assert outcome(auto, "propose:2024-08-05") is None

    online.append(True)
    auto.at(start + timedelta(minutes=61))
    bot.step()
    assert outcome(auto, "propose:2024-08-05") == "done"
