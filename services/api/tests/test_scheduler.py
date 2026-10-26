"""The job scheduler on a fake clock, with a fake runner in place of the real commands."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from tp_api import scheduler as sched
from tp_api.scheduler import Scheduler, ny
from tp_core.config import Settings
from tp_core.storage import Lake

FRIDAY = date(2024, 7, 12)
MADE: list[Scheduler] = []


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Runner:
    """Records each command; ``outputs`` maps a command to (exit code, output), and ``writes``
    to the report a successful run leaves behind."""

    def __init__(self, lake: Lake) -> None:
        self.lake = lake
        self.calls: list[str] = []
        self.outputs: dict[str, tuple[int, str]] = {}
        self.writes: dict[str, str] = {
            "tp-data bars daily": "reports/validation/stock_bars_1d-x.json",
            "tp-data macro daily": "reports/macro/x.json",
            "tp-data options snapshot": "reports/options_snapshot/x.json",
        }

    def __call__(
        self, command: tuple[str, ...], log_path: Path, timeout: float, env: dict[str, str]
    ) -> tuple[int, str]:
        assert env["TP_DATA_ROOT"] == str(self.lake.root.resolve())
        text = " ".join(command)
        self.calls.append(text)
        code, out = self.outputs.get(text, (0, "done"))
        if code == 0 and text in self.writes:
            path = self.lake.root / self.writes[text]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
        return code, out


def make(
    tmp_path: Path, at: datetime, *, keys: bool = True,
    external: Callable[[], dict[str, str]] = dict,
) -> tuple[Scheduler, Runner, Clock]:  # fmt: skip
    settings = Settings(
        data_root=tmp_path,
        alpaca_api_key=SecretStr("k") if keys else None,
        alpaca_secret_key=SecretStr("s") if keys else None,
        snaptrade_client_id=None,
        snaptrade_consumer_key=None,
        sec_user_agent=None,
    )
    clock = Clock(at)
    runner = Runner(Lake(tmp_path))
    s = Scheduler(settings, clock=clock, runner=runner, schedules=external)
    MADE.append(s)
    return s, runner, clock


@pytest.fixture(autouse=True)
def no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TP_JOBS_OFF", raising=False)
    yield
    while MADE:
        MADE.pop().release()


def test_windows_follow_the_exchange_calendar() -> None:
    bars = next(j for j in sched.JOBS if j.name == "bars")
    friday_evening = ny(FRIDAY, 21)
    (w,) = [w for w in bars.windows_around(friday_evening) if w.start <= friday_evening < w.end]
    assert w.start == ny(FRIDAY, 18, 30)
    assert w.end == ny(date(2024, 7, 15), 9)  # Monday's open, minus 30 minutes
    midday = next(j for j in sched.JOBS if j.name == "options-midday")
    half_day = date(2024, 11, 29)  # the day after Thanksgiving closes at 13:00
    assert sched._options_midday(half_day) == []
    assert midday.windows_around(ny(FRIDAY, 13))
    weekly = next(j for j in sched.JOBS if j.name == "validation")
    (v,) = weekly.windows_around(ny(date(2024, 7, 14), 9))  # Sunday morning
    assert v.start == ny(date(2024, 7, 13), 10)
    # past option prices first, then the playbook calibration that uses them
    history = next(j for j in sched.JOBS if j.name == "option-history")
    calibrate = next(j for j in sched.JOBS if j.name == "calibrate")
    (h,) = history.windows_around(ny(date(2024, 7, 13), 12))
    (c,) = calibrate.windows_around(ny(date(2024, 7, 13), 12))
    assert (h.start, c.start) == (ny(date(2024, 7, 13), 8), ny(date(2024, 7, 13), 9))
    assert calibrate.command == ("tp-recs", "calibrate")


def test_a_missed_evening_is_caught_up_and_then_left_alone(tmp_path: Path) -> None:
    saturday = ny(date(2024, 7, 13), 10)  # the laptop slept through Friday 18:30
    s, runner, clock = make(tmp_path, saturday)
    s.step()
    assert "tp-data bars daily" in runner.calls
    assert "tp-recs run" in runner.calls  # new data, so new recommendations
    runner.calls.clear()
    clock.at += timedelta(minutes=5)
    s.step()
    assert runner.calls == []  # done for this window; recommendations made minutes ago


def test_fresh_data_from_cron_is_not_fetched_again(tmp_path: Path) -> None:
    at = ny(FRIDAY, 19)
    s, runner, _ = make(tmp_path, at)
    report = tmp_path / "reports" / "validation" / "stock_bars_1d-cron.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    os.utime(report, (at.timestamp(), at.timestamp()))
    s.step()
    assert "tp-data bars daily" not in runner.calls
    assert s.status()["jobs"][4]["last"]["status"] == "fresh"


def test_failures_retry_later_and_sooner_when_offline(tmp_path: Path) -> None:
    at = ny(FRIDAY, 19)
    s, runner, clock = make(tmp_path, at)
    runner.outputs["tp-data bars daily"] = (1, "ValueError: something odd")
    s.step()
    last = s.status()["jobs"][4]["last"]
    assert last["status"] == "failed"
    assert datetime.fromisoformat(last["retry_after"]) == at + sched.RETRY
    runner.calls.clear()
    clock.at += timedelta(minutes=5)
    s.step()
    assert "tp-data bars daily" not in runner.calls  # waiting for its retry
    runner.outputs["tp-data bars daily"] = (1, "requests.exceptions.ConnectionError: offline")
    clock.at = at + sched.RETRY
    s.step()
    last = s.status()["jobs"][4]["last"]
    assert datetime.fromisoformat(last["retry_after"]) == clock.at + sched.NETWORK_RETRY
    del runner.outputs["tp-data bars daily"]
    clock.at += sched.NETWORK_RETRY
    s.step()
    assert s.status()["jobs"][4]["last"]["status"] == "ok"


def test_a_job_cron_also_runs_gets_its_turn_first(tmp_path: Path) -> None:
    at = ny(FRIDAY, 18, 35)
    s, runner, clock = make(tmp_path, at, external=lambda: {"tp-data bars daily": "cron"})
    s.step()
    assert "tp-data bars daily" not in runner.calls
    clock.at = ny(FRIDAY, 18, 30) + sched.EXTERNAL_GRACE
    s.step()
    assert "tp-data bars daily" in runner.calls  # cron didn't deliver: the scheduler does
    assert s.status()["jobs"][4]["elsewhere"] == "cron"


def test_jobs_wait_for_their_keys_and_can_be_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = ny(FRIDAY, 19)
    s, runner, _ = make(tmp_path, at, keys=False)
    monkeypatch.setenv("TP_JOBS_OFF", "recommendations")
    s.step()
    assert "tp-data bars daily" not in runner.calls
    assert "tp-data macro daily" in runner.calls  # needs no key
    assert "tp-recs run" not in runner.calls
    status = {j["name"]: j for j in s.status()["jobs"]}
    assert status["bars"]["blocked"] == "needs Alpaca keys in .env"
    assert status["recommendations"]["off"]


def test_recommendations_follow_new_data(tmp_path: Path) -> None:
    at = ny(FRIDAY, 10)
    s, runner, clock = make(tmp_path, at)
    s.step()
    assert runner.calls.count("tp-recs run") == 1
    runner.calls.clear()
    clock.at = ny(FRIDAY, 13)  # the midday option snapshot lands
    s.step()
    assert runner.calls == ["tp-data options snapshot", "tp-recs run"]
    runner.calls.clear()
    clock.at += timedelta(minutes=30)
    s.step()
    assert runner.calls == []


def test_one_scheduler_at_a_time_and_a_job_on_request(tmp_path: Path) -> None:
    at = ny(date(2024, 7, 13), 12)  # Saturday: only catch-ups would be due
    first, runner, _ = make(tmp_path, at)
    second, other, _ = make(tmp_path, at)
    assert first.acquire()
    assert second.step() == []
    assert other.calls == []
    first.request("macro")
    first.step()
    assert runner.calls[0] == "tp-data macro daily"
    with pytest.raises(KeyError):
        first.request("nonsense")
    first.release()
    assert second.acquire()
    second.release()
    assert first.status()["running"]  # the heartbeat is fresh
    assert datetime.fromisoformat(first.status()["heartbeat"]).tzinfo == UTC
