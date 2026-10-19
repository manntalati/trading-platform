"""The job scheduler: keeps the data, and the recommendations made from it, current by itself.

It runs inside the dashboard's server (``tp-api``; ``--no-jobs`` to turn it off) or on its own
(``tp-jobs run``, or ``tp-jobs once`` every five minutes from cron or launchd). Each job has
windows on the exchange calendar, in New York time, on trading days:

    macro            07:30 to 20:00             VIX and Treasury yields
    broker           every hour, 07:30 to 22:30  Fidelity through SnapTrade (once linked)
    options-midday   12:30 to 30 min before the close   option chains during the day
    options-close    15 minutes before the close  the plan's daily snapshot
    bars             18:30 to the next open      daily bars
    fundamentals     20:00 to 20:00 the next trading day   SEC filings (with TP_SEC_USER_AGENT)
    validation       Saturday 10:00 to Sunday 23:00   the backtest gate, weekly
    recommendations  whenever anything above brought new data

A job runs once in each window, as soon as the window opens or the machine is next awake inside
it: a laptop that slept through 18:30 gets its bars when it wakes at 21:00, where cron would skip
the day. A job whose data is already fresh (cron ran it, or you did) is not run again, so the
scheduler can sit next to the timers in ``infra/``; for a job scheduled there it waits a while
into the window to give that schedule its turn. A failed run is retried 15 minutes later (two
minutes when the network was down) until its window closes.

Each job runs as its own process, the same command you would type, with its output appended to
``data/logs/<job>.log``. Only one scheduler is active at a time (a lock under ``data/jobs``);
another one waits. ``TP_JOBS_OFF`` (comma-separated job names) turns jobs off.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import IO, Any

from tp_core.calendar import (
    NEW_YORK,
    is_session,
    next_session,
    previous_session,
    session_close,
    session_open,
)
from tp_core.config import Settings
from tp_core.storage import Lake, write_json
from tp_strategies.recommendations import input_stamp, run_paths

log = logging.getLogger(__name__)

TICK = 60.0  # seconds between looks at the clock
RETRY = timedelta(minutes=15)
NETWORK_RETRY = timedelta(minutes=2)
MAX_ATTEMPTS = 6
RECS_MIN_GAP = timedelta(minutes=10)
EXTERNAL_GRACE = timedelta(minutes=20)  # for a job cron or systemd also runs
HEARTBEAT_STALE = timedelta(minutes=3)
KEEP_WINDOWS = timedelta(days=4)
NETWORK_WORDS = (
    "ConnectionError",
    "ConnectTimeout",
    "NameResolutionError",
    "NewConnectionError",
    "MaxRetryError",
    "ReadTimeout",
    "Temporary failure in name resolution",
    "nodename nor servname",
)


@dataclass(frozen=True)
class Window:
    job: str
    start: datetime  # UTC
    end: datetime

    @property
    def key(self) -> str:
        return f"{self.start:%Y%m%dT%H%MZ}"


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK).astimezone(UTC)


def recent_sessions(now: datetime) -> list[date]:
    """The trading days whose windows can be open around ``now``: the current (or next) one and
    the two before it."""
    local = now.astimezone(NEW_YORK).date()
    current = local if is_session(local) else next_session(local)
    before = previous_session(current)
    return [previous_session(before), before, current]


# -- windows ---------------------------------------------------------------------------------------

WindowsFn = Callable[[date], list[tuple[datetime, datetime]]]


def _macro(day: date) -> list[tuple[datetime, datetime]]:
    return [(ny(day, 7, 30), ny(day, 20))]


def _broker(day: date) -> list[tuple[datetime, datetime]]:
    first = ny(day, 7, 30)
    return [(first + timedelta(hours=k), first + timedelta(hours=k + 1)) for k in range(15)]


def _options_midday(day: date) -> list[tuple[datetime, datetime]]:
    start, end = ny(day, 12, 30), session_close(day) - timedelta(minutes=30)
    return [(start, end)] if start < end else []  # none on a half day


def _options_close(day: date) -> list[tuple[datetime, datetime]]:
    close = session_close(day)
    return [(close - timedelta(minutes=15), close)]


def _bars(day: date) -> list[tuple[datetime, datetime]]:
    start = max(ny(day, 18, 30), session_close(day) + timedelta(hours=2))
    return [(start, session_open(next_session(day)) - timedelta(minutes=30))]


def _fundamentals(day: date) -> list[tuple[datetime, datetime]]:
    return [(ny(day, 20), ny(next_session(day), 20))]


def _weekly(now: datetime) -> list[tuple[datetime, datetime]]:
    local = now.astimezone(NEW_YORK).date()
    saturday = local - timedelta(days=(local.weekday() - 5) % 7)
    return [(ny(saturday, 10), ny(saturday + timedelta(days=1), 23))]


# -- is it already done? ---------------------------------------------------------------------------


def newest_mtime(paths: Iterable[Path]) -> float | None:
    times = []
    for p in paths:
        try:
            times.append(p.stat().st_mtime)
        except OSError:
            continue
    return max(times, default=None)


def written_since(directory: Path, pattern: str) -> Callable[[Lake, Window], bool]:
    def check(lake: Lake, window: Window) -> bool:
        newest = newest_mtime((lake.root / directory).glob(pattern))
        return newest is not None and newest >= window.start.timestamp()

    return check


def _broker_fresh(lake: Lake, window: Window) -> bool:
    days = sorted(lake.raw_dir("broker/accounts").glob("*/snapshot_date=*"), key=lambda p: p.name)
    newest = newest_mtime(days[-1].glob("*.parquet")) if days else None
    return newest is not None and newest >= window.start.timestamp()


# -- the jobs --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    name: str
    title: str
    when: str  # the schedule in words
    command: tuple[str, ...]
    fresh: Callable[[Lake, Window], bool]
    windows: WindowsFn | None = None  # per trading day
    weekly: Callable[[datetime], list[tuple[datetime, datetime]]] | None = None
    needs: Callable[[Settings], str | None] = lambda s: None  # why it can't run yet
    ok_codes: frozenset[int] = frozenset({0})
    timeout: float = 3600.0

    def windows_around(self, now: datetime) -> list[Window]:
        spans = (
            self.weekly(now)
            if self.weekly is not None
            else [w for d in recent_sessions(now) for w in (self.windows or (lambda d: []))(d)]
        )
        return [Window(self.name, a, b) for a, b in spans]


def _needs_alpaca(s: Settings) -> str | None:
    return None if s.alpaca_api_key and s.alpaca_secret_key else "needs Alpaca keys in .env"


def _needs_snaptrade(s: Settings) -> str | None:
    return None if s.snaptrade_client_id and s.snaptrade_consumer_key else "Fidelity isn't linked"


def _needs_sec(s: Settings) -> str | None:
    return None if s.sec_user_agent else "needs TP_SEC_USER_AGENT in .env"


REPORTS = Path("reports")
JOBS: tuple[Job, ...] = (
    Job(
        "macro",
        "VIX and Treasury yields",
        "trading days from 07:30",
        ("tp-data", "macro", "daily"),
        written_since(REPORTS / "macro", "*.json"),
        _macro,
    ),
    Job(
        "broker",
        "Fidelity sync",
        "hourly on trading days, 07:30 to 22:30",
        ("tp-broker", "sync", "--source", "snaptrade"),
        _broker_fresh,
        _broker,
        needs=_needs_snaptrade,
        timeout=900.0,
    ),
    Job(
        "options-midday",
        "Option chains (midday)",
        "trading days from 12:30",
        ("tp-data", "options", "snapshot"),
        written_since(REPORTS / "options_snapshot", "*.json"),
        _options_midday,
        needs=_needs_alpaca,
    ),
    Job(
        "options-close",
        "Option chains (close)",
        "15 minutes before the close",
        ("tp-data", "options", "snapshot"),
        written_since(REPORTS / "options_snapshot", "*.json"),
        _options_close,
        needs=_needs_alpaca,
    ),
    Job(
        "bars",
        "Daily bars",
        "trading days from 18:30 to the next open",
        ("tp-data", "bars", "daily"),
        written_since(REPORTS / "validation", "stock_bars_1d-*.json"),
        _bars,
        needs=_needs_alpaca,
    ),
    Job(
        "fundamentals",
        "Fundamentals (SEC)",
        "trading days from 20:00",
        ("tp-data", "fundamentals"),
        written_since(REPORTS / "fundamentals", "*.json"),
        _fundamentals,
        needs=_needs_sec,
    ),
    Job(
        "validation",
        "Backtest gate",
        "Saturdays from 10:00",
        ("tp-backtest", "validate", "--all"),
        written_since(REPORTS / "validation", "*/*.json"),
        weekly=_weekly,
        ok_codes=frozenset({0, 1}),
        timeout=4 * 3600.0,
    ),
)
RECS_COMMAND = ("tp-recs", "run")


# -- running a command -----------------------------------------------------------------------------

Runner = Callable[[tuple[str, ...], Path, float, dict[str, str]], tuple[int, str]]


def executable(name: str) -> str:
    """The command from this environment (the venv the server runs in), else from PATH."""
    here = Path(sys.executable).parent / name
    if here.exists():
        return str(here)
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f"{name} not found next to {sys.executable} or on PATH")
    return found


def run_command(
    command: tuple[str, ...], log_path: Path, timeout: float, env: dict[str, str]
) -> tuple[int, str]:
    """Run ``command`` with ``env`` and its output appended to ``log_path``; the exit code and the
    last lines it printed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        start = fh.tell()
        fh.write(f"\n=== {datetime.now(UTC).isoformat()} {' '.join(command)}\n")
        fh.flush()
        try:
            done = subprocess.run(
                [executable(command[0]), *command[1:]],
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                env=env,
            )
            code = done.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"timed out after {timeout:.0f}s\n")
            code = -1
        except OSError as exc:
            fh.write(f"{type(exc).__name__}: {exc}\n")
            code = -2
    with log_path.open(encoding="utf-8", errors="replace") as fh:
        fh.seek(start)
        lines = [line.rstrip() for line in fh.read().splitlines() if line.strip()]
    return code, "\n".join(lines[-15:])


def external_schedules() -> dict[str, str]:
    """Commands already scheduled by cron or systemd user timers: command -> "cron"/"systemd"."""
    from tp_api.doctor import JOBS as DOCTOR_JOBS
    from tp_api.doctor import _run

    timers = _run(["systemctl", "--user", "list-unit-files", "tp-*", "--no-legend"])
    crontab = _run(["crontab", "-l"])
    enabled = {
        line.split()[0]
        for line in timers.splitlines()
        if len(line.split()) >= 2 and line.split()[1] == "enabled"
    }
    cron_lines = [line for line in crontab.splitlines() if not line.lstrip().startswith("#")]
    out: dict[str, str] = {}
    for _, command, unit in DOCTOR_JOBS:
        if unit in enabled:
            out[command] = "systemd"
        elif any(command in line for line in cron_lines):
            out[command] = "cron"
    for command in ("tp-recs run", "tp-backtest validate"):
        if any(command in line for line in cron_lines):
            out[command] = "cron"
    return out


def elsewhere(job_command: tuple[str, ...], external: dict[str, str]) -> str | None:
    """How ``job_command`` is already scheduled outside the scheduler, if it is."""
    text = " ".join(job_command)
    return next((how for command, how in external.items() if text.startswith(command)), None)


# -- the scheduler ---------------------------------------------------------------------------------


@dataclass
class Scheduler:
    settings: Settings
    jobs: tuple[Job, ...] = JOBS
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    runner: Runner = run_command
    schedules: Callable[[], dict[str, str]] = external_schedules
    _lock_file: IO[str] | None = None
    _external: dict[str, str] | None = None
    _external_at: datetime | None = None
    _manual: list[str] = field(default_factory=list)
    _busy: threading.Lock = field(default_factory=threading.Lock)

    @property
    def lake(self) -> Lake:
        return Lake(self.settings.data_root)

    @property
    def state_path(self) -> Path:
        return self.settings.data_root / "jobs" / "state.json"

    def log_path(self, name: str) -> Path:
        return self.settings.data_root / "logs" / f"{name}.log"

    def off(self) -> set[str]:
        return {j.strip() for j in os.environ.get("TP_JOBS_OFF", "").split(",") if j.strip()}

    # -- state -------------------------------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        try:
            state: dict[str, Any] = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            state = {}
        state.setdefault("jobs", {})
        state.setdefault("recommendations", {})
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        write_json(state, self.state_path)

    # -- one scheduler at a time -------------------------------------------------------------------

    def acquire(self) -> bool:
        if self._lock_file is not None:
            return True
        path = self.settings.data_root / "jobs" / "scheduler.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._lock_file = fh
        return True

    def release(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def external(self, now: datetime) -> dict[str, str]:
        if (
            self._external is None
            or self._external_at is None
            or now - self._external_at > timedelta(hours=1)
        ):
            try:
                self._external = self.schedules()
            except Exception:  # never let a crontab read stop the jobs
                log.exception("reading cron/systemd schedules failed")
                self._external = {}
            self._external_at = now
        return self._external

    # -- what is due -------------------------------------------------------------------------------

    def due(self, now: datetime, state: dict[str, Any]) -> list[Window]:
        """The windows open now whose job hasn't run (or succeeded) in them yet."""
        out = []
        off = self.off()
        for job in self.jobs:
            if job.name in off or job.needs(self.settings) is not None:
                continue
            for w in job.windows_around(now):
                if not w.start <= now < w.end:
                    continue
                record = state["jobs"].get(job.name, {}).get("windows", {}).get(w.key, {})
                if record.get("status") in {"ok", "fresh", "gave up"}:
                    continue
                if (
                    record.get("retry_after")
                    and datetime.fromisoformat(record["retry_after"]) > now
                ):
                    continue
                if job.fresh(self.lake, w):
                    self._record(state, job.name, w, "fresh", "already up to date", now)
                    continue
                other = elsewhere(job.command, self.external(now))
                grace = min(EXTERNAL_GRACE, (w.end - w.start) / 2)
                if other and now < w.start + grace:
                    continue  # give cron/systemd its turn first
                out.append(w)
        return out

    def recs_due(self, now: datetime, state: dict[str, Any]) -> str | None:
        """A stamp of the inputs when new recommendations are due, else ``None``."""
        if "recommendations" in self.off():
            return None
        rec = state["recommendations"]
        stamp = hashlib.sha1(
            json.dumps(input_stamp(self.lake), sort_keys=True).encode()
        ).hexdigest()
        if rec.get("stamp") == stamp and rec.get("status") in {"ok", "gave up"}:
            return None
        last = rec.get("finished_at")
        if last and now - datetime.fromisoformat(last) < RECS_MIN_GAP:
            return None
        if rec.get("retry_after") and datetime.fromisoformat(rec["retry_after"]) > now:
            return None
        return stamp

    # -- running -----------------------------------------------------------------------------------

    def _record(
        self,
        state: dict[str, Any],
        name: str,
        w: Window,
        status: str,
        message: str,
        now: datetime,
        attempts: int = 0,
        retry_after: datetime | None = None,
    ) -> None:
        entry = state["jobs"].setdefault(name, {})
        windows = entry.setdefault("windows", {})
        windows[w.key] = {
            "status": status,
            "message": message,
            "attempts": attempts,
            "finished_at": now.isoformat(),
            "window": [w.start.isoformat(), w.end.isoformat()],
            **({"retry_after": retry_after.isoformat()} if retry_after else {}),
        }
        if status != "fresh" or "last" not in entry:
            entry["last"] = windows[w.key] | {"window_key": w.key}
        cutoff = (now - KEEP_WINDOWS).strftime("%Y%m%dT%H%MZ")
        entry["windows"] = {k: v for k, v in windows.items() if k >= cutoff}

    def _execute(
        self, name: str, command: tuple[str, ...], timeout: float, ok_codes: frozenset[int]
    ) -> tuple[bool, bool, str]:
        """Run one command: (ok, network trouble, the last lines it printed)."""
        log.info("job %s: %s", name, " ".join(command))
        env = {**os.environ, "TP_DATA_ROOT": str(self.settings.data_root.resolve())}
        code, tail = self.runner(command, self.log_path(name), timeout, env)
        ok = code in ok_codes
        network = not ok and any(word in tail for word in NETWORK_WORDS)
        log.log(logging.INFO if ok else logging.WARNING, "job %s finished (exit %s)", name, code)
        last = tail.splitlines()[-1] if tail else f"exit {code}"
        return ok, network, last if ok else f"exit {code}: {last}"

    def run_window(self, job: Job, w: Window, state: dict[str, Any]) -> None:
        record = state["jobs"].get(job.name, {}).get("windows", {}).get(w.key, {})
        attempts = int(record.get("attempts", 0)) + 1
        ok, network, message = self._execute(job.name, job.command, job.timeout, job.ok_codes)
        now = self.clock()
        if ok:
            self._record(state, job.name, w, "ok", message, now, attempts)
            return
        retry = now + (NETWORK_RETRY if network else RETRY)
        if (attempts >= MAX_ATTEMPTS and not network) or retry >= w.end:
            self._record(state, job.name, w, "gave up", message, now, attempts)
        else:
            self._record(state, job.name, w, "failed", message, now, attempts, retry)

    def run_recommendations(self, stamp: str, state: dict[str, Any]) -> None:
        rec = state["recommendations"]
        attempts = int(rec.get("attempts", 0)) + 1 if rec.get("stamp") == stamp else 1
        ok, network, message = self._execute(
            "recommendations", RECS_COMMAND, 1800.0, frozenset({0})
        )
        now = self.clock()
        status = "ok" if ok else "gave up" if attempts >= 3 else "failed"
        state["recommendations"] = {
            "stamp": stamp,
            "status": status,
            "message": message,
            "attempts": attempts,
            "finished_at": now.isoformat(),
            **(
                {"retry_after": (now + (NETWORK_RETRY if network else RETRY)).isoformat()}
                if status == "failed"
                else {}
            ),
        }

    def request(self, name: str) -> None:
        """Run ``name`` at the next tick, whatever its windows say."""
        if name not in {j.name for j in self.jobs} | {"recommendations"}:
            raise KeyError(name)
        if name not in self._manual:
            self._manual.append(name)

    def step(self) -> list[str]:
        """Run whatever is due now (one job at a time); the names of the jobs that ran."""
        if not self.acquire():
            return []
        with self._busy:
            ran: list[str] = []
            state = self.load_state()
            now = self.clock()
            state["scheduler"] = {"pid": os.getpid(), "heartbeat": now.isoformat()}
            self.save_state(state)
            by_name = {j.name: j for j in self.jobs}
            while self._manual:
                name = self._manual.pop(0)
                if name == "recommendations":
                    state["recommendations"].pop("stamp", None)
                    state["recommendations"].pop("finished_at", None)
                    state["recommendations"].pop("retry_after", None)
                    continue
                job = by_name[name]
                w = Window(name, now, now + timedelta(hours=1))
                self.run_window(job, w, state)
                ran.append(name)
                self.save_state(state)
            for w in self.due(now, state):
                self.run_window(by_name[w.job], w, state)
                ran.append(w.job)
                self.save_state(state)
            stamp = self.recs_due(self.clock(), state)
            if stamp is not None:
                self.run_recommendations(stamp, state)
                ran.append("recommendations")
            state["scheduler"]["heartbeat"] = self.clock().isoformat()
            self.save_state(state)
            return ran

    def loop(self, stop: threading.Event) -> None:
        log.info("job scheduler started (data in %s)", self.settings.data_root)
        while not stop.is_set():
            try:
                self.step()
            except Exception:  # keep scheduling whatever one tick did
                log.exception("job scheduler tick failed")
            stop.wait(TICK)
        self.release()

    # -- what the dashboard shows ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self.clock()
        state = self.load_state()
        beat = (state.get("scheduler") or {}).get("heartbeat")
        alive = beat is not None and now - datetime.fromisoformat(str(beat)) < HEARTBEAT_STALE
        off = self.off()
        jobs = []
        external = self.external(now)
        for job in self.jobs:
            entry = state["jobs"].get(job.name, {})
            upcoming = [w for w in job.windows_around(now) if w.end > now]
            nxt = min(upcoming, key=lambda w: w.start) if upcoming else None
            jobs.append(
                {
                    "name": job.name,
                    "title": job.title,
                    "when": job.when,
                    "command": " ".join(job.command),
                    "off": job.name in off,
                    "blocked": job.needs(self.settings),
                    "elsewhere": elsewhere(job.command, external),
                    "last": entry.get("last"),
                    "next": nxt.start.isoformat() if nxt and nxt.start > now else None,
                    "open_now": bool(nxt and nxt.start <= now),
                }
            )
        rec = dict(state["recommendations"])
        stored = run_paths(self.lake)
        if stored:  # a run made by hand (tp-recs run, Update now) counts too
            made = datetime.strptime(stored[-1].stem.split("-")[0] + "+0000", "%Y%m%dT%H%M%SZ%z")
            if not rec.get("finished_at") or datetime.fromisoformat(rec["finished_at"]) < made:
                rec = {"status": "ok", "message": f"run {stored[-1].stem}",
                       "finished_at": made.isoformat()}  # fmt: skip
        jobs.append(
            {
                "name": "recommendations",
                "title": "Recommendations",
                "when": "whenever new data lands",
                "command": " ".join(RECS_COMMAND),
                "off": "recommendations" in off,
                "blocked": None,
                "elsewhere": elsewhere(RECS_COMMAND, external),
                "last": rec or None,
                "next": None,
                "open_now": False,
            }
        )
        return {
            "running": alive,
            "pid": (state.get("scheduler") or {}).get("pid"),
            "heartbeat": beat,
            "here": self._lock_file is not None,
            "jobs": jobs,
        }


def scheduler_alive(data_root: Path, now: datetime) -> bool:
    """Whether a scheduler (in any process) has looked at the clock in the last few minutes."""
    try:
        state = json.loads((data_root / "jobs" / "state.json").read_text())
        beat = datetime.fromisoformat(state["scheduler"]["heartbeat"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return now - beat < HEARTBEAT_STALE


def start_in_background(scheduler: Scheduler) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(
        target=scheduler.loop, args=(stop,), name="job-scheduler", daemon=True
    )
    thread.start()
    return thread, stop


# -- tp-jobs ---------------------------------------------------------------------------------------


def main() -> None:
    import typer

    app = typer.Typer(
        add_completion=False,
        no_args_is_help=True,
        help="Run the data jobs and recommendations on their schedule.",
    )

    def make() -> Scheduler:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        return Scheduler(Settings())

    @app.command("run")
    def run_forever() -> None:
        """Keep running: look at the clock every minute and run what is due."""
        stop = threading.Event()
        try:
            make().loop(stop)
        except KeyboardInterrupt:
            stop.set()

    @app.command()
    def once() -> None:
        """Run what is due now and exit (for cron or launchd every five minutes)."""
        scheduler = make()
        if not scheduler.acquire():
            typer.echo("another scheduler is running (the dashboard's server, or tp-jobs run)")
            return
        ran = scheduler.step()
        scheduler.release()
        typer.echo(f"ran: {', '.join(ran)}" if ran else "nothing due")

    @app.command()
    def now(name: str) -> None:
        """Run one job now, whatever its schedule (or "recommendations")."""
        scheduler = make()
        if not scheduler.acquire():
            typer.echo("another scheduler is running: use the dashboard's System page instead")
            raise typer.Exit(1)
        try:
            scheduler.request(name)
        except KeyError:
            names = ", ".join([j.name for j in scheduler.jobs] + ["recommendations"])
            raise typer.BadParameter(f"one of: {names}") from None
        scheduler.step()
        scheduler.release()
        last = scheduler.status()
        for job in last["jobs"]:
            if job["name"] == name and job["last"]:
                typer.echo(f"{name}: {job['last'].get('status')} — {job['last'].get('message')}")

    @app.command()
    def status() -> None:
        """Each job: when it runs, and how its last run went."""
        scheduler = make()
        scheduler.external(scheduler.clock())
        s = scheduler.status()
        typer.echo("scheduler: " + (f"running (pid {s['pid']})" if s["running"] else "not running"))
        for job in s["jobs"]:
            last = job["last"] or {}
            state = "off" if job["off"] else job["blocked"] or last.get("status") or "not run yet"
            nxt = job["next"]
            when = (
                ""
                if job["off"] or job["blocked"]
                else datetime.fromisoformat(nxt).astimezone(NEW_YORK).strftime("%a %H:%M")
                if nxt
                else "open now"
                if job["open_now"]
                else ""
            )
            typer.echo(
                f"  {job['name']:<16} {state:<32} {('next ' + when) if when else '':<15}"
                f" {last.get('message', '')[:50]}"
            )

    app()
