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


JUL31, AUG1 = date(2024, 7, 31), date(2024, 8, 1)


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
    assert names["submit"].deadline == ny(AUG1, 9, 28)
    assert names["sync_open"].due == ny(AUG1, 9, 45)
    assert names["sync_close"].due == ny(AUG1, 16, 30)
    assert names["propose"].due == ny(AUG1, 18, 45)
    assert names["propose"].deadline == ny(date(2024, 8, 2), 9, 5)  # before the next submit
    half_day = {t.name: t for t in tasks_for(date(2024, 7, 3))}
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
    auto.at(ny(AUG1, 10))  # slept through 09:10-09:28 (laptop asleep, say)
    bot.step()
    assert outcome(auto, "submit:2024-08-01") == "missed"
    assert any("submit:2024-08-01 missed" in e["message"] for e in auto.paper.store.events())


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
