"""``tp-doctor`` on an empty setup and on the demo lake (synthetic data only)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from tp_api import doctor
from tp_core.config import Settings

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
    assert "crontab" in no_systemd["Schedules/Daily bars"].fix


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
