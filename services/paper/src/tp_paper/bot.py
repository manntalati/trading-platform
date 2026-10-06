"""The paper-trading bot: runs the whole daily cycle by itself.

Every trading session has four tasks, timed off the exchange calendar (New York time on a
normal day):

    09:10  submit      approved proposals -> market-on-open orders (window closes 09:28;
                       started later than that, it sends market orders instead)
    09:45  sync_open   fills from the opening auction, reconciliation
    16:30  sync_close  order updates after the close (30 min after an early close)
    18:45  propose     refresh bars if needed, mark sleeves, run strategies, risk-check;
                       sleeves with approval = "auto" are approved on the spot

With every sleeve on "auto" nothing waits for a person: proposals made in the evening go out
at the next open. The bot is a loop around the same idempotent jobs the ``tp-paper`` commands
run, so a crash or restart never doubles an order: what was done is recorded per task and
session, a task that fails is retried, and one whose window has passed is logged as missed.

Paper only, like the rest of the package; the risk checks and the kill switch apply unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from tp_core.calendar import (
    NEW_YORK,
    is_session,
    last_completed_session,
    next_session,
    previous_session,
    session_close,
    session_open,
)
from tp_paper import jobs
from tp_paper.jobs import Paper, PaperError

log = logging.getLogger(__name__)

TaskName = Literal["submit", "sync_open", "sync_close", "propose"]
PROPOSE_AT = time(18, 45)  # New York: after the 18:30 bars job
RETRY = timedelta(minutes=15)
MAX_ATTEMPTS = 8  # about two hours of retries before a task is given up for the session

BarsRefresher = Callable[[datetime], str]


@dataclass(frozen=True)
class Task:
    name: TaskName
    session: date  # the trading session the task belongs to
    due: datetime  # UTC: run from here...
    deadline: datetime  # ... until here; later it is missed

    @property
    def key(self) -> str:
        return f"{self.name}:{self.session.isoformat()}"


def tasks_for(session: date) -> list[Task]:
    """The four tasks of one session, from its real open and close (half days included).

    ``submit`` normally runs at 09:10 and sends market-on-open orders. If the bot only gets to
    it after the auction cutoff (it was started late, or was down), it still runs until 15
    minutes before the close and sends plain market orders instead, so a late start trades the
    same day. Likewise ``propose`` stays open until the next session's close, so a bot started
    the morning after still turns the previous close into orders.
    """
    open_, close = session_open(session), session_close(session)
    following = next_session(session)
    next_open, next_close = session_open(following), session_close(following)
    evening = datetime.combine(session, PROPOSE_AT, tzinfo=NEW_YORK).astimezone(UTC)
    return [
        Task("submit", session, open_ - timedelta(minutes=20), close - timedelta(minutes=15)),
        Task("sync_open", session, open_ + timedelta(minutes=15), close),
        Task(
            "sync_close", session, close + timedelta(minutes=30), next_open - timedelta(minutes=30)
        ),
        Task(
            "propose",
            session,
            max(evening, close + timedelta(hours=1)),
            next_close - timedelta(minutes=30),
        ),
    ]


def schedule(now: datetime) -> list[Task]:
    """Tasks from the last completed session through the next one, in due order."""
    last = last_completed_session(now)
    local = now.astimezone(NEW_YORK).date()
    current = local if is_session(local) else next_session(local)
    days = sorted({previous_session(last), last, current, next_session(current)})
    return sorted((t for d in days for t in tasks_for(d)), key=lambda t: t.due)


@dataclass
class Bot:
    paper: Paper
    refresh_bars: BarsRefresher | None = None  # fetch the day's bars when the lake is behind
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    max_sleep: float = 300.0  # wake at least this often (heartbeat, kill switch, new tasks)
    started_at: datetime | None = None

    # -- one pass ---------------------------------------------------------------------------------

    def step(self) -> Task | None:
        """Run every task that is due now; return the next one still to come."""
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
            self._log(f"started (pid {os.getpid()}, broker {self.paper.broker.name})")
        upcoming: Task | None = None
        for task in schedule(now):
            record = self._record(task)
            if record is not None:
                continue
            if now >= task.deadline:
                if task.deadline > self.started_at:  # closed while we were running: say so
                    self._finish(task, "missed", "its window closed before it could run")
                continue  # windows that closed before the bot started are simply past
            if now < task.due or self._waiting_for_proposals(task, now):
                upcoming = upcoming or task
                continue
            retry = self.paper.store.get(f"bot:retry:{task.key}")
            if retry and datetime.fromisoformat(retry["after"]) > now:
                upcoming = upcoming or task
                continue
            self._run(task, now, retry)
            if self._record(task) is None:  # will retry later
                upcoming = upcoming or task
        self._follow_up_sync(now)
        self._heartbeat(now, upcoming)
        return upcoming

    def seconds_until_next(self, upcoming: Task | None) -> float:
        now = self.clock()
        wakes = []
        if upcoming is not None:
            wake = upcoming.due
            retry = self.paper.store.get(f"bot:retry:{upcoming.key}")
            if retry:
                wake = max(wake, datetime.fromisoformat(retry["after"]))
            wakes.append(wake)
        follow_up = self.paper.store.get("bot:sync_after")
        if follow_up:
            wakes.append(datetime.fromisoformat(follow_up))
        if not wakes:
            return self.max_sleep
        return max(1.0, min(self.max_sleep, (min(wakes) - now).total_seconds()))

    def _waiting_for_proposals(self, task: Task, now: datetime) -> bool:
        """``submit`` waits while the previous close's ``propose`` can still run, so a late or
        retried propose isn't overtaken by an empty submission."""
        if task.name != "submit":
            return False
        before = tasks_for(previous_session(task.session))[-1]  # its propose
        return self._record(before) is None and now < before.deadline

    def _follow_up_sync(self, now: datetime) -> None:
        """A sync a couple of minutes after market orders went out, to record their fills."""
        due = self.paper.store.get("bot:sync_after")
        if not due or now < datetime.fromisoformat(due):
            return
        self.paper.store.put("bot:sync_after", None)
        try:
            result = jobs.sync(self.paper, now)
        except Exception as exc:
            self.paper.store.put("bot:sync_after", (now + timedelta(minutes=5)).isoformat())
            self._log(f"follow-up sync failed, retrying in 5 minutes: {exc}")
            return
        self._log(f"follow-up sync: {result.fills} fill(s), {result.updated} update(s)")

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                upcoming = self.step()
            except Exception:  # never die on one bad pass; the next pass retries
                log.exception("bot pass failed")
                upcoming = None
            stop.wait(self.seconds_until_next(upcoming))
        self._log("stopped")
        status = self.paper.store.get("bot:status") or {}
        self.paper.store.put("bot:status", status | {"state": "stopped"})

    # -- tasks ------------------------------------------------------------------------------------

    def _run(self, task: Task, now: datetime, retry: dict[str, Any] | None) -> None:
        attempts = (retry or {}).get("attempts", 0) + 1
        try:
            message = self._do(task, now)
        except PaperError as exc:
            self._fail(task, now, attempts, str(exc), retryable=task.name == "propose")
        except Exception as exc:
            log.exception("%s failed", task.key)
            self._fail(task, now, attempts, f"{type(exc).__name__}: {exc}", retryable=True)
        else:
            self._finish(task, "done", message)

    def _do(self, task: Task, now: datetime) -> str:
        if task.name == "submit":
            if self.paper.risk.state.kill_switch() is not None:
                return "skipped: kill switch engaged"
            late = now >= session_open(task.session) - timedelta(minutes=2)
            r = jobs.submit(self.paper, now, time_in_force="day" if late else "opg")
            message = (
                f"{len(r.submitted)} order(s) sent, {len(r.failed)} refused, "
                f"{len(r.expired)} expired"
            )
            if late:
                if r.submitted:
                    self.paper.store.put("bot:sync_after", (now + timedelta(minutes=2)).isoformat())
                message = f"after the opening auction cutoff, sent as market orders: {message}"
            return message
        if task.name in ("sync_open", "sync_close"):
            s = jobs.sync(self.paper, now)
            breaks = f", {len(s.breaks)} reconciliation break(s)" if s.breaks else ""
            return f"{s.fills} fill(s), {s.updated} update(s){breaks}"
        behind = jobs.bars_behind(self.paper, now)
        if behind and self.refresh_bars is not None:
            # The evening bars job may not have run, or a strategy was just added (its symbols
            # are backfilled). A feed that is down raises, and the task is retried.
            which = ", ".join(behind) if len(behind) <= 6 else f"{len(behind)} symbols"
            self._log(f"{task.key}: bars are behind for {which}; refreshing")
            self._log(f"{task.key}: {self.refresh_bars(now)}")
        report = jobs.propose(self.paper, now)
        made = sum(report.proposed.values())
        blocked = sum(report.blocked.values())
        message = f"{made} proposal(s), {blocked} blocked by risk"
        if report.skipped:
            message += "; skipped " + ", ".join(f"{k} ({v})" for k, v in report.skipped.items())
        return message

    def _fail(
        self, task: Task, now: datetime, attempts: int, message: str, *, retryable: bool
    ) -> None:
        if not retryable or attempts >= MAX_ATTEMPTS:
            self._finish(task, "failed", message)
            return
        after = min(now + RETRY, task.deadline)
        self.paper.store.put(
            f"bot:retry:{task.key}",
            {"after": after.isoformat(), "attempts": attempts, "error": message},
        )
        self._log(f"{task.key} attempt {attempts} failed, retrying at {after:%H:%M} UTC: {message}")

    # -- bookkeeping ------------------------------------------------------------------------------

    def _record(self, task: Task) -> dict[str, Any] | None:
        value: dict[str, Any] | None = self.paper.store.get(f"bot:task:{task.key}")
        return value

    def _finish(self, task: Task, outcome: str, message: str) -> None:
        now = self.clock()
        self.paper.store.put(
            f"bot:task:{task.key}", {"outcome": outcome, "at": now.isoformat(), "message": message}
        )
        status = self.paper.store.get("bot:status") or {}
        status["last"] = {
            "task": task.name,
            "session": task.session.isoformat(),
            "outcome": outcome,
            "at": now.isoformat(),
            "message": message,
        }
        self.paper.store.put("bot:status", status)
        self._log(f"{task.key} {outcome}: {message}")

    def _heartbeat(self, now: datetime, upcoming: Task | None) -> None:
        status = self.paper.store.get("bot:status") or {}
        status.update(
            state="running",
            pid=os.getpid(),
            started_at=(self.started_at or now).isoformat(),
            heartbeat=now.isoformat(),
            next=(
                {
                    "task": upcoming.name,
                    "session": upcoming.session.isoformat(),
                    "due": upcoming.due.isoformat(),
                }
                if upcoming
                else None
            ),
        )
        self.paper.store.put("bot:status", status)

    def _log(self, message: str) -> None:
        log.info(message)
        self.paper.store.log("bot", message)
