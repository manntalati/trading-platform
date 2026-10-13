"""Strategy validation from the dashboard, and the market regime."""

import shutil
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tp_api import queries
from tp_api.app import QuoteMode, create_app
from tp_core.storage import Lake
from tp_ingest.jobs.macro import run_backfill
from tp_ingest.sources.fred import FakeMacroSource

from .test_api import lake_root, settings_for  # noqa: F401  (lake_root is a fixture)

WRITE = {"X-TP-Client": "dashboard"}
NOW = datetime(2024, 7, 12, 22, tzinfo=UTC)


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
    assert set(body["history"][-1]) == {"date", "trend", "volatility", "breadth", "rates", "curve"}
    assert now["rates"] is None  # no FRED data in this lake
    assert now["volatility_source"] == "realized"
    assert client.get("/api/market/regime").json() == body  # cached


def test_vix_and_rates_once_fred_is_downloaded(
    lake_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "lake"
    shutil.copytree(lake_root, root)
    run_backfill(Lake(root), FakeMacroSource(end=date(2024, 7, 12)), now=NOW)
    monkeypatch.chdir(tmp_path)
    settings = settings_for(root)
    app = create_app(settings, quotes=QuoteMode.off, serve_dashboard=False)
    with TestClient(app) as c:
        now = c.get("/api/market/regime").json()["current"]
        assert now["volatility_source"] == "vix"
        assert now["vix"] > 0
        assert now["rate_10y"] > 0
        assert now["rates"] in ("rising", "steady", "falling")
        assert now["curve"] in ("normal", "flat", "inverted")
        macro = c.get("/api/status").json()["macro"]
        assert macro["series"]["DGS3MO"]["date"] == "2024-07-12"
        assert macro["latest_report"]["ok"] is True
        datasets = {d["name"]: d for d in c.get("/api/system/data").json()["datasets"]}
        assert datasets["VIX and Treasury yields"]["files"] >= 2  # raw + clean
    ctx = queries.Context(settings)
    assert ctx.risk_free_rate(at=date(2024, 7, 15)) == pytest.approx(now["rate_3m"] / 100)
    assert ctx.risk_free_rate() == settings.risk_free_rate  # 2024 data is stale today
