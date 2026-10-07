from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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


def test_second_sync_fetches_activities_with_overlap(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    run_sync(lake, FakeBroker(as_of=NOW.date()), now=NOW)

    class Recording(FakeBroker):
        seen: date | None = None

        def activities(self, since: date | None) -> Any:
            Recording.seen = since
            return super().activities(since)

    run_sync(lake, Recording(as_of=NOW.date()), now=NOW.replace(day=13))
    assert Recording.seen == date(2024, 7, 7) - ACTIVITY_OVERLAP  # newest: the 7 July sweep
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


@pytest.mark.parametrize("args", [["sync"], ["link"]])
def test_cli_missing_snaptrade_keys(env: Path, args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "SNAPTRADE_CLIENT_ID" in result.output
