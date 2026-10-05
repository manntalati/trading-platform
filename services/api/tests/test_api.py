from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from tp_api.app import QuoteMode, create_app
from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.bars import load_bars
from tp_core.config import Settings
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.jobs.options import run_snapshot
from tp_ingest.sources.fake import FakeSource

REPO = Path(__file__).resolve().parents[3]
DAY1 = datetime(2024, 7, 11, 21, 30, tzinfo=UTC)
DAY2 = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)
SYMBOLS = [
    "SPY", "QQQ", "IWM", "TLT", "GLD", "EFA", "IEF", "VNQ", "DBC",
    "AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XOM", "UNH",
]  # fmt: skip


@pytest.fixture(scope="module")
def lake_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lake")
    lake = Lake(root)
    run_backfill(lake, FakeSource(), SYMBOLS, now=DAY2, years=3)
    closes = load_bars(lake).groupby("symbol")["close"].last().to_dict()
    run_sync(lake, FakeBroker(price_of=closes.get, as_of=DAY1.date()), now=DAY1)
    run_sync(lake, FakeBroker(price_of=closes.get, as_of=DAY2.date()), now=DAY2)
    run_snapshot(lake, FakeSource(as_of=date(2024, 7, 12)), ["SPY"], now=DAY2, max_dte=60)
    return root


def settings_for(root: Path, token: str | None = None) -> Settings:
    return Settings(
        data_root=root,
        universes_file=REPO / "config" / "universes.toml",
        classifications_file=REPO / "config" / "classifications.toml",
        dashboard_token=SecretStr(token) if token else None,
    )


@pytest.fixture
def client(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)  # no stray .env
    app = create_app(
        settings_for(lake_root), quotes=QuoteMode.fake, live_interval=0.05, serve_dashboard=False
    )
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True


def test_status(client: TestClient) -> None:
    body = client.get("/api/status").json()
    assert set(body["market"]) >= {"is_open", "next_open", "next_close"}
    assert body["bars"]["symbols"] == len(SYMBOLS)
    assert body["validation"]["ok"] is True
    assert body["options"]["files"] == 1
    assert {b["institution"] for b in body["broker"]} == {"Fidelity (demo)"}
    assert body["live"]["source"] == "fake"
    assert "SPY" in body["live"]["symbols"]


def test_bars(client: TestClient) -> None:
    body = client.get("/api/bars/spy?days=30").json()
    assert body["symbol"] == "SPY"
    assert len(body["bars"]) == 30
    assert set(body["bars"][0]) >= {"date", "close", "adj_close"}
    assert client.get("/api/bars/NOPE").status_code == 404


def test_options_summary(client: TestClient) -> None:
    body = client.get("/api/options/SPY").json()
    assert body["spot"] > 0
    assert body["term"]
    first = body["term"][0]
    assert first["atm_iv"] > 0
    assert first["expected_move"] > 0
    assert {p["right"] for p in body["smile"]} == {"C", "P"}
    assert client.get("/api/options/QQQ").json()["term"] == []


def test_portfolio(client: TestClient) -> None:
    body = client.get("/api/portfolio").json()
    assert body["synced"] is True
    assert body["total_value"] > 0
    holdings = {h["symbol"]: h for h in body["holdings"]}
    assert holdings["AAPL"]["priced"] is True
    assert holdings["FXAIX"]["priced"] is False
    assert abs(sum(h["weight"] for h in body["holdings"]) - 1) < 1e-9
    assert body["by_sector"]["Information Technology"] > 0
    assert 0 < body["priced_share"] < 1
    assert all("…" in a["account_number_masked"] for a in body["accounts"])


def test_performance(client: TestClient) -> None:
    body = client.get("/api/portfolio/performance?days=365").json()
    backtest = body["holdings_backtest"]
    assert set(backtest["stats"]) == {
        "current holdings",
        "S&P 500 (SPY)",
        "Nasdaq 100 (QQQ)",
        "60/40 (SPY/IEF)",
    }
    assert backtest["growth"][0]["S&P 500 (SPY)"] > 0
    assert len(body["account"]["values"]) == 2


def test_ideas(client: TestClient) -> None:
    body = client.get("/api/ideas").json()
    assert "Not investment advice" in body["disclaimer"]
    kinds = {i["kind"] for i in body["ideas"]}
    assert {"portfolio", "candidate"} <= kinds


@pytest.mark.parametrize("universe", ["spy", "gtaa"])
def test_ma_timing(client: TestClient, universe: str) -> None:
    body = client.get(f"/api/strategies/ma-timing?universe={universe}").json()
    assert body["available"] is True
    assert set(body["stats"]) >= {"10-month MA timing", "Buy and hold"}
    assert body["growth"]


def test_ma_timing_rejects_unknown_universe(client: TestClient) -> None:
    assert client.get("/api/strategies/ma-timing?universe=x").status_code == 422


def test_live_websocket_streams_quotes_and_portfolio(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        symbols = {q["symbol"] for q in first["quotes"]}
        assert {"SPY", "AAPL", "NVDA"} <= symbols  # watchlist + market-priced holdings
        assert "FXAIX" not in symbols
        update = ws.receive_json()
        assert update["type"] == "update"
        assert any(q["live"] for q in update["quotes"])
        assert update["portfolio"]["value"] > 0
        assert update["feed"]["source"] == "fake"


def test_token_protects_api_and_websocket(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app(
        settings_for(lake_root, token="s3cret"), quotes=QuoteMode.off, serve_dashboard=False
    )
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/portfolio").status_code == 401
        ok = c.get("/api/portfolio", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        with c.websocket_connect("/ws/live?token=s3cret") as ws:
            assert ws.receive_json()["feed"]["source"] == "off"
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/live") as ws:
            ws.receive_json()


def test_empty_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app(settings_for(tmp_path / "empty"), quotes=QuoteMode.fake, serve_dashboard=False)
    with TestClient(app) as c:
        assert c.get("/api/status").json()["bars"]["symbols"] == 0
        assert c.get("/api/portfolio").json()["synced"] is False
        assert c.get("/api/strategies/ma-timing").json()["available"] is False
        assert c.get("/api/portfolio/performance").json() == {
            "holdings_backtest": None,
            "account": None,
        }
