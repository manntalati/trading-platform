"""Strategy validation from the dashboard, and the market regime."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tp_api.app import QuoteMode, create_app

from .test_api import lake_root, settings_for  # noqa: F401  (lake_root is a fixture)

WRITE = {"X-TP-Client": "dashboard"}


@pytest.fixture
def client(
    lake_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)  # no stray .env; no config/validation.toml: the defaults
    app = create_app(settings_for(lake_root), quotes=QuoteMode.off, serve_dashboard=False)
    with TestClient(app) as c:
        yield c


def test_validate_from_the_dashboard(client: TestClient) -> None:
    before = client.get("/api/strategies/validation").json()
    names = {s["name"] for s in before["strategies"]}
    assert {"ma-timing", "rsi2", "leveraged-momentum"} <= names
    assert before["job"] == {"running": False}
    assert before["gate"]["min_test_sharpe"] == 0.0

    assert (
        client.post("/api/strategies/validation", json={"names": ["ma-timing"]}).status_code == 403
    )
    missing = client.post("/api/strategies/validation", json={"names": ["nope"]}, headers=WRITE)
    assert missing.status_code == 404
    started = client.post(
        "/api/strategies/validation", json={"names": ["ma-timing"]}, headers=WRITE
    )
    assert started.status_code == 202
    for _ in range(600):
        state = client.get("/api/strategies/validation").json()["job"]
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["ok"] is True, state
    assert state["done"] == ["ma-timing"]

    after = client.get("/api/strategies/validation").json()
    ma = next(s for s in after["strategies"] if s["name"] == "ma-timing")
    report = ma["report"]
    assert report["strategy"] == "ma-timing"
    assert report["gate"]["checks"][0]["name"] == "Locked test set"
    assert report["gate"]["passed"] is False  # 3 years of fake data: no locked test period
    assert ma["on_paper"] is False


def test_market_regime(client: TestClient) -> None:
    body = client.get("/api/market/regime").json()
    now = body["current"]
    assert now["trend"] in ("up", "down")
    assert now["volatility"] in ("calm", "normal", "stressed")
    assert now["summary"]
    assert len(body["history"]) <= 252
    assert set(body["history"][-1]) == {"date", "trend", "volatility", "breadth"}
    assert client.get("/api/market/regime").json() == body  # cached
