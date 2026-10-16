from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from tp_broker import cli
from tp_broker.alpaca import AlpacaAccountSource
from tp_broker.fake import FakeBroker
from tp_broker.jobs import ACTIVITY_OVERLAP, run_sync
from tp_core import portfolio as pf
from tp_core.storage import Lake

NOW = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)
runner = CliRunner()


def test_sync_stores_snapshot_and_activities(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    result = run_sync(lake, FakeBroker(as_of=NOW.date()), now=NOW)
    # 12 contributions, a dividend, 6 buys, 2 round trips, 3 option buys, an expiry, a sweep
    assert (result.accounts, result.activities) == (2, 28)
    assert result.holdings > 5
    snap = pf.latest_snapshot(lake)
    assert snap.total_value > 0
    assert "AAPL" in pf.held_symbols(lake)
    assert pf.external_flows(lake).sum() == pytest.approx(12 * 500)


def test_sync_can_refresh_first(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    result = run_sync(lake, FakeBroker(as_of=NOW.date()), now=NOW, refresh=True)
    assert result.refresh == "demo positions are always fresh"
    accounts = pf.latest_snapshot(lake).accounts
    assert (accounts["transactions_as_of"] == date(2024, 7, 11)).all()
    assert accounts["holdings_as_of"].notna().all()


def test_a_failed_refresh_does_not_stop_the_sync(tmp_path: Path) -> None:
    class Unrefreshable(FakeBroker):
        def refresh(self, *, timeout: float = 180.0) -> Any:
            raise RuntimeError("(503)\nReason: Service Unavailable\nHTTP response headers: ...")

    lake = Lake(tmp_path)
    result = run_sync(lake, Unrefreshable(as_of=NOW.date()), now=NOW, refresh=True)
    assert result.refresh == ("refresh failed (RuntimeError: (503)); synced what fake already had")
    assert result.activities == 28  # the sync itself still ran
    assert pf.latest_snapshot(lake).total_value > 0


def test_second_sync_fetches_activities_with_overlap(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    run_sync(lake, FakeBroker(as_of=NOW.date()), now=NOW)

    class Recording(FakeBroker):
        seen: Any = None

        def activities(self, since: Any) -> Any:
            Recording.seen = since
            return super().activities(since)

    run_sync(lake, Recording(as_of=NOW.date()), now=NOW.replace(day=13))
    # per account, from a month before its newest stored transaction (the 7 July sweep); the
    # Roth account has none stored, so it isn't listed and gets its whole history
    assert Recording.seen == {"demo-indiv": date(2024, 7, 7) - ACTIVITY_OVERLAP}
    # Re-fetched activities do not double count.
    assert pf.external_flows(lake).sum() == pytest.approx(12 * 500)


def test_alpaca_account_source_maps_raw_payloads() -> None:
    class Client:
        def get_account(self) -> dict[str, Any]:
            return {"id": "u1", "account_number": "PA3XYZ9876", "cash": "1000", "equity": "2500"}

        def get_all_positions(self) -> list[dict[str, Any]]:
            return [
                {
                    "symbol": "SPY",
                    "qty": "3",
                    "side": "long",
                    "asset_class": "us_equity",
                    "current_price": "500",
                    "market_value": "1500",
                    "avg_entry_price": "480",
                }
            ]

    snap = AlpacaAccountSource(Client()).snapshot()
    assert snap.accounts.iloc[0][["cash", "total_value", "account_number_masked"]].tolist() == [
        1000.0,
        2500.0,
        "…9876",
    ]
    assert snap.holdings.iloc[0][["symbol", "kind", "market_value"]].tolist() == [
        "SPY",
        "stock",
        1500.0,
    ]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "lake"))
    repo = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("TP_CLASSIFICATIONS_FILE", str(repo / "config" / "classifications.toml"))
    for var in ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    return tmp_path


def test_cli_fake_sync_then_show(env: Path) -> None:
    result = runner.invoke(cli.app, ["sync", "--source", "fake"])
    assert result.exit_code == 0, result.output
    assert "2 accounts" in result.output
    shown = runner.invoke(cli.app, ["show"])
    assert shown.exit_code == 0, shown.output
    assert "AAPL" in shown.output
    assert "total value" in shown.output


def test_cli_show_before_sync(env: Path) -> None:
    result = runner.invoke(cli.app, ["show"])
    assert result.exit_code == 0
    assert "no portfolio synced yet" in result.output


@pytest.mark.parametrize("args", [["sync"], ["link"], ["check"]])
def test_cli_missing_snaptrade_keys(env: Path, args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "SNAPTRADE_CLIENT_ID" in result.output


def test_cli_check_with_the_demo_source(env: Path) -> None:
    assert runner.invoke(cli.app, ["sync", "--source", "fake"]).exit_code == 0
    result = runner.invoke(cli.app, ["check", "--source", "fake"])
    assert result.exit_code == 0, result.output
    assert "Last sync here: 2024-07-12 17:30 ET (0 hours ago)" in result.output
    assert "status unknown" in result.output  # the demo has no connection to report on
    assert "the lake has" in result.output


class PortalApi:
    """Stands in for the SnapTrade SDK client in ``tp-broker link``."""

    links: ClassVar[list[dict[str, Any]]] = []
    reconnect: str | None = None

    def __init__(self, client_id: str, key: str) -> None:
        pass

    def list_connections(self) -> list[dict[str, Any]]:
        return PortalApi.links

    def list_accounts(self) -> list[dict[str, Any]]:
        return []

    def connection_portal_url(self, broker: str | None, reconnect: str | None = None) -> str:
        PortalApi.reconnect = reconnect
        return "https://app.snaptrade.com/portal?x"


@pytest.fixture
def portal(env: Path, monkeypatch: pytest.MonkeyPatch) -> type[PortalApi]:
    from tp_broker import snaptrade

    monkeypatch.setenv("SNAPTRADE_CLIENT_ID", "client")
    monkeypatch.setenv("SNAPTRADE_CONSUMER_KEY", "key")
    monkeypatch.setattr(snaptrade, "SdkSnapTradeApi", PortalApi)
    PortalApi.reconnect = None
    return PortalApi


def test_link_reconnects_the_disabled_connection(portal: type[PortalApi]) -> None:
    portal.links = [
        {"id": "ok-1", "disabled": False, "brokerage": {"name": "Alpaca"}},
        {"id": "broken-2", "disabled": True, "brokerage": {"name": "Fidelity"}},
    ]
    result = runner.invoke(cli.app, ["link", "--reconnect"])
    assert result.exit_code == 0, result.output
    assert portal.reconnect == "broken-2"
    assert "reconnecting the disabled connection" in result.output
    assert "https://app.snaptrade.com/portal" in result.output
    # a plain link adds a connection instead
    assert runner.invoke(cli.app, ["link"]).exit_code == 0
    assert portal.reconnect is None


def test_link_reconnect_refuses_to_guess(portal: type[PortalApi]) -> None:
    portal.links = [
        {"id": "a", "disabled": False, "brokerage": {"name": "Fidelity"}},
        {"id": "b", "disabled": False, "brokerage": {"name": "Alpaca"}},
    ]
    result = runner.invoke(cli.app, ["link", "--reconnect"])
    assert result.exit_code == 2
    assert "can't tell which to repair" in result.output
