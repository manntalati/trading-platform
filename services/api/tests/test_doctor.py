"""``tp-doctor`` on an empty setup and on the demo lake (synthetic data only)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr

from tp_api import doctor
from tp_broker.base import ACCOUNT_COLUMNS, ACTIVITY_COLUMNS, HOLDING_COLUMNS, BrokerSnapshot
from tp_broker.jobs import run_sync
from tp_core.config import Settings
from tp_core.storage import Lake

from .test_api import lake_root, settings_for  # noqa: F401  (lake_root is a fixture)

NOW = datetime(2024, 7, 15, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
                "SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY", "TP_SEC_USER_AGENT",
                "ANTHROPIC_API_KEY", "FRED_API_KEY", "TP_DASHBOARD_TOKEN"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {f"{c.area}/{c.name}": c for c in checks}


def test_configuration(tmp_path: Path) -> None:
    empty = by_name(doctor.configuration(Settings(data_root=tmp_path), tmp_path))
    assert empty["Configuration/.env file"].status == "todo"
    assert empty["Configuration/Alpaca paper keys"].status == "todo"
    assert empty["Configuration/FRED API key"].status == "optional"
    (tmp_path / ".env").write_text("")
    configured = Settings(
        data_root=tmp_path,
        alpaca_api_key=SecretStr("k"),
        alpaca_secret_key=SecretStr("s"),
        sec_user_agent="tp me@example.com",
    )
    done = by_name(doctor.configuration(configured, tmp_path))
    assert done["Configuration/.env file"].status == "ok"
    assert done["Configuration/Alpaca paper keys"].status == "ok"
    assert done["Configuration/SEC user agent"].status == "ok"


def test_data_on_the_demo_lake(lake_root: Path) -> None:  # noqa: F811
    checks = by_name(doctor.data(settings_for(lake_root), NOW))
    assert checks["Data/Daily bars"].status == "ok"
    assert checks["Data/Option chains"].status == "ok"
    assert checks["Data/Fidelity positions"].status in ("ok", "warn")
    assert checks["Data/VIX and Treasury yields"].status == "todo"
    assert checks["Data/VIX and Treasury yields"].fix == "tp-data macro backfill"
    assert checks["Data/Fundamentals (SEC)"].status == "todo"
    assert checks["Data/Intraday bars"].status == "optional"


def test_schedules_from_systemd_or_cron(tmp_path: Path) -> None:
    timers = "tp-bars-daily.timer enabled enabled\ntp-macro-daily.timer disabled enabled\n"
    cron = tmp_path / "crontab"
    cron.write_text("# 0 1 * * * tp-data fundamentals\n30 11 * * 1-5 .venv/bin/tp-broker sync\n")
    checks = by_name(doctor.schedules(run=lambda cmd: timers, crontab_path=cron))
    assert checks["Schedules/Daily bars"].detail == "scheduled (systemd)"
    assert checks["Schedules/VIX and Treasury yields"].status == "todo"  # disabled
    assert checks["Schedules/Fundamentals (SEC)"].status == "todo"  # commented out
    assert checks["Schedules/Fidelity sync"].detail == "scheduled (cron)"
    assert "systemctl --user enable --now tp-macro-daily.timer" in (
        checks["Schedules/VIX and Treasury yields"].fix
    )
    no_systemd = by_name(doctor.schedules(run=lambda cmd: "", crontab_path=cron))
    assert "tp-api" in no_systemd["Schedules/Daily bars"].fix
    assert "crontab" in no_systemd["Schedules/Paper bot (service or cron)"].fix
    builtin = by_name(doctor.schedules(run=lambda cmd: "", crontab_path=cron, builtin=True))
    assert builtin["Schedules/Daily bars"].detail == "scheduled (the built-in scheduler)"
    assert (
        builtin["Schedules/Fidelity sync"].detail == "scheduled (cron, and the built-in scheduler)"
    )
    assert builtin["Schedules/Paper bot (service or cron)"].status == "todo"


def test_render_and_the_paper_bot(tmp_path: Path) -> None:
    running = doctor.services(Settings(data_root=tmp_path), NOW)
    assert by_name(running)["Running/Paper bot"].detail == "has never run"
    text = doctor.render(
        [
            doctor.Check("Data", "Daily bars", "ok", "50 symbols"),
            doctor.Check("Data", "Option chains", "todo", "none", "tp-data options snapshot"),
            doctor.Check("Configuration", "FRED API key", "optional", "not needed"),
        ]
    )
    assert "✓ Daily bars" in text
    assert "→ tp-data options snapshot" in text
    assert "1 to set up, 0 to look at." in text
    assert "→" not in text.split("FRED API key")[1]  # optional items don't nag


class OneAccount:
    """A broker source with one synthetic account, its connection state and freshness given."""

    name = "snaptrade"

    def __init__(self, disabled: bool, positions: str, through: str) -> None:
        self.row = {
            "account_id": "acct-1", "account_name": "Individual", "institution": "Fidelity",
            "account_number_masked": "…0001", "cash": 10.0, "total_value": 10.0,
            "currency": "USD", "holdings_as_of": pd.Timestamp(positions),
            "transactions_as_of": date.fromisoformat(through), "connection_disabled": disabled,
        }  # fmt: skip

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            accounts=pd.DataFrame([self.row], columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(columns=list(HOLDING_COLUMNS)),
        )

    def activities(self, since: object) -> pd.DataFrame:
        return pd.DataFrame(columns=list(ACTIVITY_COLUMNS))


@pytest.mark.parametrize(
    ("source", "status", "fix"),
    [
        (OneAccount(True, "2024-07-15T13:00Z", "2024-07-14"), "todo", "tp-broker link --reconnect"),
        (OneAccount(False, "2024-07-01T13:00Z", "2024-06-28"), "warn", "tp-broker check"),
    ],
)
def test_a_broken_or_stale_fidelity_connection_is_flagged(
    tmp_path: Path, source: OneAccount, status: str, fix: str
) -> None:
    lake = Lake(tmp_path)
    run_sync(lake, source, now=NOW)
    checks = [c for c in doctor.data(Settings(data_root=tmp_path), NOW) if c.area == "Data"]
    positions = next(c for c in checks if c.name == "Fidelity positions")
    assert positions.status == "ok"  # our sync ran on time...
    assert "transactions through" in positions.detail
    problems = [c for c in checks if c.name == "Fidelity connection"]
    assert problems  # ...but SnapTrade's data behind it isn't moving
    assert {c.status for c in problems} == {status}
    assert {c.fix for c in problems} == {fix}


def test_a_healthy_connection_adds_no_lines(tmp_path: Path) -> None:
    run_sync(Lake(tmp_path), OneAccount(False, "2024-07-15T13:00Z", "2024-07-14"), now=NOW)
    names = [c.name for c in doctor.data(Settings(data_root=tmp_path), NOW)]
    assert "Fidelity connection" not in names
