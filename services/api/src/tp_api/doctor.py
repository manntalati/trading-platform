"""``tp-doctor``: what's set up and what isn't, with the command that fixes each gap.

Run it from the repo directory (where ``.env`` lives). It only reads: settings, the data lake,
the paper bot's heartbeat, the dashboard build, and the systemd user timers or the crontab. Exit
code 1 while something required is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import typer

from tp_api.dashboard import bundle_state
from tp_api.scheduler import scheduler_alive
from tp_core.bars import load_bars
from tp_core.calendar import last_completed_session
from tp_core.config import Settings
from tp_core.fundamentals import load_events
from tp_core.intraday import load_intraday
from tp_core.macro import load_macro
from tp_core.portfolio import connection_problems, latest_snapshot
from tp_core.storage import Lake, parquet_files
from tp_paper.jobs import bot_status
from tp_paper.store import PaperStore

Status = Literal["ok", "todo", "warn", "optional"]
MARK = {"ok": "✓", "todo": "✗", "warn": "!", "optional": "·"}

# job, the command in a crontab line, the systemd unit
JOBS = (
    ("Daily bars", "tp-data bars daily", "tp-bars-daily.timer"),
    ("Option chain snapshot", "tp-data options snapshot", "tp-options-snapshot.timer"),
    ("VIX and Treasury yields", "tp-data macro daily", "tp-macro-daily.timer"),
    ("Fundamentals (SEC)", "tp-data fundamentals", "tp-fundamentals.timer"),
    ("Fidelity sync", "tp-broker sync", "tp-broker-sync.timer"),
    ("Paper bot (service or cron)", "tp-paper bot", "tp-paper-bot.service"),
)


@dataclass(frozen=True)
class Check:
    area: str
    name: str
    status: Status
    detail: str
    fix: str = ""


def _secret(value: object) -> bool:
    if value is None:
        return False
    getter = getattr(value, "get_secret_value", None)
    return bool(getter() if getter else value)


def configuration(settings: Settings, cwd: Path) -> list[Check]:
    area = "Configuration"
    env = cwd / ".env"
    out = [
        Check(area, ".env file", "ok" if env.exists() else "todo",
              str(env) if env.exists() else "not found in this directory",
              "cp .env.example .env && chmod 600 .env, then fill it in"),
    ]  # fmt: skip
    alpaca = _secret(settings.alpaca_api_key) and _secret(settings.alpaca_secret_key)
    out.append(Check(area, "Alpaca paper keys", "ok" if alpaca else "todo",
                     "set" if alpaca else "ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
                     "paper keys from https://app.alpaca.markets/paper/dashboard/overview; "
                     "they fetch prices and option chains and run the paper bot"))  # fmt: skip
    snap = settings.snaptrade_client_id and _secret(settings.snaptrade_consumer_key)
    out.append(Check(area, "SnapTrade keys (Fidelity)", "ok" if snap else "todo",
                     "set" if snap else "SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY not set",
                     "a personal key from https://dashboard.snaptrade.com, then "
                     "`tp-broker link`"))  # fmt: skip
    sec = bool(settings.sec_user_agent and "@" in settings.sec_user_agent)
    out.append(Check(area, "SEC user agent", "ok" if sec else "todo",
                     "set" if sec else "TP_SEC_USER_AGENT not set (needed for fundamentals)",
                     'TP_SEC_USER_AGENT="trading-platform you@example.com" in .env'))  # fmt: skip
    for name, ok, why, fix in (
        ("Anthropic key (Ask Claude)", _secret(settings.anthropic_api_key),
         "the chat on positions and ideas", "ANTHROPIC_API_KEY from https://platform.claude.com"),
        ("FRED API key", _secret(settings.fred_api_key),
         "Cboe and the Treasury serve the macro data without it", "FRED_API_KEY (free)"),
        ("Dashboard token", _secret(settings.dashboard_token),
         "only needed to open the dashboard from another machine", "TP_DASHBOARD_TOKEN"),
    ):  # fmt: skip
        out.append(Check(area, name, "ok" if ok else "optional", "set" if ok else why, fix))
    return out


def _age(day: date | None, today: date) -> int | None:
    return (today - day).days if day else None


def data(settings: Settings, now: datetime) -> list[Check]:
    area = "Data"
    lake = Lake(settings.data_root)
    today = now.date()
    out: list[Check] = []

    bars = load_bars(lake)
    if bars.empty:
        out.append(Check(area, "Daily bars", "todo", "none",
                         "tp-data bars backfill --years 5"))  # fmt: skip
    else:
        latest, expected = bars["session"].max(), last_completed_session(now)
        fresh = latest >= expected
        out.append(Check(area, "Daily bars", "ok" if fresh else "warn",
                         f"{bars['symbol'].nunique()} symbols, latest {latest}"
                         + ("" if fresh else f" (expected {expected})"),
                         "tp-data bars daily"))  # fmt: skip

    chains = parquet_files(lake.raw_dir("alpaca/option_chain_snapshots"))
    days = sorted({p.parent.parent.name.split("=")[-1] for p in chains})
    if not days:
        out.append(Check(area, "Option chains", "todo", "none",
                         "tp-data options snapshot (while the market is open)"))  # fmt: skip
    else:
        age = _age(date.fromisoformat(days[-1]), today) or 0
        out.append(Check(area, "Option chains", "ok" if age <= 4 else "warn",
                         f"{len(days)} days stored, latest {days[-1]}",
                         "tp-data options snapshot (while the market is open)"))  # fmt: skip

    snap = latest_snapshot(lake)
    if snap.accounts.empty:
        out.append(Check(area, "Fidelity positions", "todo", "never synced",
                         "tp-broker link (once), then tp-broker sync"))  # fmt: skip
    else:
        synced = max(snap.accounts["taken_at"]).to_pydatetime(warn=False)
        stale = now - synced > timedelta(days=3)
        through = snap.accounts["transactions_as_of"].dropna()
        detail = f"{len(snap.accounts)} account(s), synced {synced:%Y-%m-%d %H:%M} UTC" + (
            f", transactions through {min(through)}" if len(through) else ""
        )
        out.append(Check(area, "Fidelity positions", "warn" if stale else "ok", detail,
                         "tp-broker sync"))  # fmt: skip
        # SnapTrade can keep answering with old data (a disabled connection): our sync running
        # on time doesn't mean new trades are arriving.
        for problem in connection_problems(snap.accounts, now):
            disabled = "disabled" in problem
            out.append(Check(area, "Fidelity connection", "todo" if disabled else "warn",
                             problem.split(". ")[0],
                             "tp-broker link --reconnect" if disabled
                             else "tp-broker check"))  # fmt: skip

    macro = load_macro(lake)
    if macro.empty:
        out.append(Check(area, "VIX and Treasury yields", "todo", "none",
                         "tp-data macro backfill"))  # fmt: skip
    else:
        latest_m = macro.index.max().date()
        age = _age(latest_m, today) or 0
        out.append(Check(area, "VIX and Treasury yields", "ok" if age <= 5 else "warn",
                         f"{', '.join(map(str, macro.columns))} to {latest_m}",
                         "tp-data macro daily"))  # fmt: skip

    events = load_events(lake)
    reports = sorted(lake.reports_dir("fundamentals").glob("*.json"))
    if events.empty:
        out.append(Check(area, "Fundamentals (SEC)", "todo", "none", "tp-data fundamentals"))
    else:
        last = date.fromisoformat(reports[-1].stem) if reports else None
        run_age = _age(last, today)
        recent = run_age is not None and run_age <= 5
        out.append(Check(area, "Fundamentals (SEC)", "ok" if recent else "warn",
                         f"{events['symbol'].nunique()} companies, last run {last}",
                         "tp-data fundamentals"))  # fmt: skip

    intraday = load_intraday(lake)
    out.append(Check(area, "Intraday bars", "ok" if len(intraday) else "optional",
                     f"{intraday['session'].nunique()} sessions" if len(intraday)
                     else "only for the opening-range breakout research",
                     "tp-data bars intraday --symbols SPY,QQQ --days 250"))  # fmt: skip

    validated = list(lake.reports_dir("validation").glob("*/*.json"))
    out.append(Check(area, "Strategy validation", "ok" if validated else "warn",
                     f"{len({p.parent.name for p in validated})} strategies validated"
                     if validated else "no reports: the paper sleeves haven't been checked",
                     "tp-backtest validate --all"))  # fmt: skip
    return out


def _run(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def schedules(
    run: Callable[[list[str]], str] = _run,
    crontab_path: Path | None = None,
    builtin: bool = False,
) -> list[Check]:
    """Each job scheduled by systemd, cron, or the built-in scheduler (``builtin``: it is running,
    in ``tp-api`` or ``tp-jobs``; it doesn't run the paper bot)."""
    area = "Schedules"
    timers = run(["systemctl", "--user", "list-unit-files", "tp-*", "--no-legend"])
    crontab = crontab_path.read_text() if crontab_path else run(["crontab", "-l"])
    out = []
    for name, command, unit in JOBS:
        enabled = any(
            line.split()[:2] == [unit, "enabled"] for line in timers.splitlines() if line.strip()
        )
        in_cron = any(
            command in line and not line.lstrip().startswith("#") for line in crontab.splitlines()
        )
        how = "systemd" if enabled else "cron" if in_cron else None
        if builtin and not command.startswith("tp-paper"):
            how = f"{how}, and the built-in scheduler" if how else "the built-in scheduler"
        fix = (
            f"systemctl --user enable --now {unit} (infra/README.md)"
            if timers
            else "keep `uv run tp-api` running (it runs the jobs), or `uv run tp-jobs run`"
            if not command.startswith("tp-paper")
            else "add it to your crontab: infra/cron/crontab.example"
        )
        out.append(Check(area, name, "ok" if how else "todo",
                         f"scheduled ({how})" if how else "not scheduled: it won't run on its own",
                         fix))  # fmt: skip
    return out


def services(settings: Settings, now: datetime) -> list[Check]:
    area = "Running"
    out = []
    path = settings.data_root / "state" / "paper.sqlite"
    bot = bot_status(PaperStore(path), now) if path.exists() else None
    if bot is None:
        out.append(Check(area, "Paper bot", "todo", "has never run",
                         "tp-paper bot (or its service / the cron line)"))  # fmt: skip
    else:
        out.append(Check(area, "Paper bot", "ok" if bot["alive"] else "warn",
                         "running" if bot["alive"] else f"last seen {bot.get('heartbeat')}",
                         "tp-paper bot (or its service / the cron line)"))  # fmt: skip
    state = bundle_state()
    built = state.built_at is not None
    out.append(Check(area, "Dashboard build", "ok" if built and not state.stale else "todo",
                     "built" if built and not state.stale
                     else "missing" if not built else "older than its source",
                     "make web-install web-build (needs Node 22+)"))  # fmt: skip
    return out


def all_checks(settings: Settings, now: datetime, cwd: Path) -> list[Check]:
    return [
        *configuration(settings, cwd),
        *data(settings, now),
        *schedules(builtin=scheduler_alive(settings.data_root, now)),
        *services(settings, now),
    ]


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    area = None
    width = max(len(c.name) for c in checks) + 2
    for c in checks:
        if c.area != area:
            area = c.area
            lines.append(f"\n{area}")
        lines.append(f"  {MARK[c.status]} {c.name:<{width}}{c.detail}")
        if c.status in ("todo", "warn") and c.fix:
            lines.append(f"      → {c.fix}")
    todo = sum(c.status == "todo" for c in checks)
    warn = sum(c.status == "warn" for c in checks)
    lines.append(
        f"\n{todo} to set up, {warn} to look at." if todo or warn else "\nEverything is set up."
    )
    return "\n".join(lines)


def main() -> None:
    def doctor() -> None:
        """What's set up and what isn't, with the command that fixes each gap."""
        checks = all_checks(Settings(), datetime.now(UTC), Path.cwd())
        typer.echo(render(checks))
        if any(c.status == "todo" for c in checks):
            raise typer.Exit(code=1)

    typer.run(doctor)
