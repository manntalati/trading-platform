"""Fresher brokerage data: on-demand sync, how fresh the data is, trades not posted yet."""

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tp_api.app import QuoteMode, create_app
from tp_broker.base import BrokerSnapshot
from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.storage import Lake

from .test_api import settings_for

DAY1 = datetime(2024, 7, 11, 21, 30, tzinfo=UTC)
DAY2 = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)
WRITE = {"X-TP-Client": "dashboard"}


class TradedToday(FakeBroker):
    """The demo account after buying 5 more NVDA at 140 and selling all the XOM."""

    def snapshot(self) -> BrokerSnapshot:
        snap = super().snapshot()
        h = snap.holdings.set_index("symbol")
        qty, cost = h.loc["NVDA", "quantity"], h.loc["NVDA", "cost_basis_per_unit"]
        h.loc["NVDA", "cost_basis_per_unit"] = (qty * cost + 5 * 140.0) / (qty + 5)
        h.loc["NVDA", "quantity"] = qty + 5
        h = h.drop(index="XOM")
        return BrokerSnapshot(snap.accounts, h.reset_index())

    def activities(self, since: date | None) -> pd.DataFrame:
        return super().activities(since)  # today's trades aren't posted until tomorrow


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    root = tmp_path / "data"
    lake = Lake(root)
    run_sync(lake, FakeBroker(as_of=DAY1.date()), now=DAY1)
    run_sync(lake, TradedToday(as_of=DAY2.date()), now=DAY2)
    monkeypatch.chdir(tmp_path)
    app = create_app(settings_for(root), quotes=QuoteMode.off, serve_dashboard=False)
    with TestClient(app) as c:
        yield c


def test_trades_not_posted_yet_show_from_the_positions(client: TestClient) -> None:
    body = client.get("/api/trades?source=mine").json()
    pending = {r["symbol"]: r for r in body["trades"] if r["pending"]}
    assert set(pending) == {"NVDA", "XOM"}
    assert pending["NVDA"]["action"] == "Bought"
    assert pending["NVDA"]["quantity"] == pytest.approx(5)
    assert pending["NVDA"]["price"] == pytest.approx(140.0)  # from the change in cost
    assert pending["NVDA"]["date"] == "2024-07-12"
    assert pending["XOM"]["action"] == "Sold"
    assert pending["XOM"]["price"] is None
    assert body["trades"][0]["pending"] is True  # newest first
    assert body["freshness"]["transactions_through"] == "2024-07-11"


def test_portfolio_says_how_fresh_the_data_is(client: TestClient) -> None:
    fresh = client.get("/api/portfolio").json()["freshness"]
    assert fresh["synced_at"].startswith("2024-07-12T21:30")
    assert fresh["positions_as_of"].startswith("2024-07-12T20:00")
    assert fresh["transactions_through"] == "2024-07-11"


def test_sync_from_the_dashboard(client: TestClient) -> None:
    assert client.post("/api/broker/sync", json={"refresh": True}).status_code == 403  # no header
    started = client.post("/api/broker/sync", json={"refresh": True}, headers=WRITE)
    assert started.status_code == 202
    assert started.json()["source"] == "fake"  # the source of the latest snapshot
    state: dict[str, Any] = {}
    for _ in range(100):
        state = client.get("/api/broker/sync").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["ok"] is True
    assert state["message"].startswith("demo positions are always fresh")
    assert state["finished_at"]
