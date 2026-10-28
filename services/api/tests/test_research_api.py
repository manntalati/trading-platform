"""Options research endpoints on the demo lake (fake bars, fake chains, demo portfolio)."""

import shutil
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tp_api import positions as pos_api
from tp_api import research as research_api
from tp_api.app import QuoteMode, create_app
from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.bars import load_bars
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.jobs.fundamentals import run_fundamentals
from tp_ingest.jobs.options import run_snapshot
from tp_ingest.sources.fake import FakeSource
from tp_ingest.sources.sec import FakeSecSource

from .test_api import SYMBOLS, settings_for
from .test_positions_api import FakeAdvisor

DAY = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)
NOW = datetime(2024, 7, 15, 15, 0, tzinfo=UTC)
WRITE = {"X-TP-Client": "dashboard"}


@pytest.fixture(scope="module")
def research_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("research")
    lake = Lake(root)
    run_backfill(lake, FakeSource(), SYMBOLS, now=DAY, years=2)
    closes = load_bars(lake).groupby("symbol")["close"].last().to_dict()
    run_sync(lake, FakeBroker(price_of=closes.get, as_of=DAY.date()), now=DAY)
    source = FakeSource(as_of=date(2024, 7, 12))
    run_snapshot(lake, source, ["SPY", "AAPL", "MSFT", "NVDA", "JPM"], now=DAY, max_dte=800)
    run_fundamentals(lake, FakeSecSource(["AAPL"], last_year=2023), ["AAPL", "SPY"], now=DAY)
    return root


@pytest.fixture
def client(
    research_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, Any]]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(research_api, "now_utc", lambda: NOW)
    monkeypatch.setattr(pos_api, "now_utc", lambda: NOW)
    app = create_app(settings_for(research_root), quotes=QuoteMode.off, serve_dashboard=False)
    app.state.advisor = FakeAdvisor()
    with TestClient(app) as c:
        yield c, app


def test_research_ranks_ideas_with_contracts(client: tuple[TestClient, Any]) -> None:
    c, _ = client
    body = c.get("/api/research").json()
    assert body["scanned"] >= 10
    assert body["with_chains"] >= 5
    assert "TLT" not in {
        i["symbol"] for k in ("swing", "leaps", "bearish", "income", "volatility") for i in body[k]
    }
    ideas = [*body["swing"], *body["leaps"], *body["bearish"]]
    assert ideas, "the fake market should produce some ideas"
    for idea in ideas:
        assert idea["why"]
        assert set(idea) >= {"symbol", "kind", "score", "setup", "vol", "pick", "risks"}
    priced = [i for i in ideas if i["pick"]]
    assert priced
    budget = body["budget"]  # sized for the demo portfolio's account with the most buying power
    assert budget["account"] == "Individual …1234"
    for idea in priced:
        assert idea["pick"]["size"]["capital"] <= budget["buying_power"]
    for idea in body["stocks"]:  # shares: a stop under the price, sized the same way
        pick = idea["pick"]
        assert pick["exit"]["stop"] < pick["entry"]["limit"]
        assert pick["size"]["shares"] >= 1
        assert pick["size"]["capital"] <= budget["buying_power"]
    assert "holding_alerts" in body
    assert body["iv_history_days"] == 1
    assert "Not investment advice" in body["disclaimer"]
    assert c.get("/api/research").json() == body  # cached


def test_ideas_are_stored_runs_with_a_track_record(client: tuple[TestClient, Any]) -> None:
    c, app = client
    body = c.get("/api/research").json()
    run = body["run"]
    assert run["run_id"]
    assert run["as_of"].startswith("2024-07-15")
    lake = Lake(app.state.settings.data_root)
    stored = sorted(lake.reports_dir("recommendations").glob("*.json"))
    assert run["run_id"] in {p.stem for p in stored}
    again = c.post("/api/research/run", headers=WRITE).json()
    assert again["run"]["run_id"] != run["run_id"]
    assert again["run"]["previous_run"] is None  # both runs were made the same day
    record = c.get("/api/research/track-record").json()
    listed = {(r["list"], r["symbol"]) for r in record["recommendations"]}
    lists = ("swing", "leaps", "bearish", "income", "volatility", "stocks")
    ideas = {(k, i["symbol"]) for k in lists for i in body[k]}
    assert listed == ideas  # the same ideas twice are one recommendation each
    assert all(r["runs"] == 2 for r in record["recommendations"])
    assert {s["list"] for s in record["summary"]} <= set(lists)
    shares = [r for r in record["recommendations"] if r["list"] == "stocks"]
    assert all(r["structure"] == "shares" for r in shares)


def test_calibration_before_and_after_a_run(client: tuple[TestClient, Any]) -> None:
    c, app = client
    assert c.get("/api/research/calibration").json() == {"setups": {}}
    from tp_strategies.playbook_backtest import calibrate

    lake = Lake(app.state.settings.data_root)
    calibrate(lake, ["AAPL", "MSFT", "NVDA", "JPM", "SPY"], now=NOW)
    body = c.get("/api/research/calibration").json()
    assert body["setups"]["pullback"]["horizon"] == 10
    assert body["names"] >= 1
    shutil.rmtree(lake.reports_dir("playbook"))  # the lake is shared with the other tests


def test_jobs_status_and_a_job_on_request(client: tuple[TestClient, Any]) -> None:
    c, app = client
    ran: list[str] = []
    app.state.scheduler.runner = lambda command, log_path, timeout, env: (
        ran.append(" ".join(command)) or (0, "ok")
    )
    status = c.get("/api/jobs").json()
    names = [j["name"] for j in status["jobs"]]
    assert names[-1] == "recommendations"
    assert {"bars", "options-close", "options-midday", "macro", "broker"} <= set(names)
    assert c.post("/api/jobs/nonsense/run", headers=WRITE).status_code == 404
    assert c.post("/api/jobs/macro/run", headers=WRITE).status_code == 202
    for _ in range(50):
        if "tp-data macro daily" in ran:
            break
        time.sleep(0.05)
    assert "tp-data macro daily" in ran


def test_one_symbol_in_depth(client: tuple[TestClient, Any]) -> None:
    c, _ = client
    body = c.get("/api/research/symbol/aapl").json()
    assert body["symbol"] == "AAPL"
    assert body["chain_age_days"] == 3
    assert body["held"].startswith("40 shares")
    assert {"swing_call", "leaps_call"} <= set(body["picks"])
    assert body["picks"]["leaps_call"]["dte"] >= 270
    assert body["vol"]["iv30"] > 0
    view = body["playbook"]  # the setups live on it and how close the others are
    assert isinstance(view["ideas"], list)
    assert any("RSI(2)" in line for line in view["levels"])
    assert view["calibrated"] is False
    f = body["fundamentals"]  # from the (synthetic) SEC filings
    assert f["period_end"] == "2023-12-31"
    assert f["last_release"] == "2024-01-31"
    assert f["pe"] > 0
    assert f["next_release_estimate"] is not None
    assert c.get("/api/research/symbol/MSFT").json()["fundamentals"] is None
    assert c.get("/api/research/symbol/NOPE").status_code == 404


def test_positions_carry_actions(client: tuple[TestClient, Any]) -> None:
    c, _ = client
    rows = c.get("/api/positions").json()["positions"]
    assert all(isinstance(p["actions"], list) for p in rows)


def test_ask_about_an_idea(client: tuple[TestClient, Any]) -> None:
    c, app = client
    q = {"question": "Is the LEAPS worth it?", "symbol": "AAPL"}
    assert c.post("/api/research/ask", json=q).status_code == 403
    reply = c.post("/api/research/ask", json=q, headers=WRITE).json()
    assert reply["answer"] == "Hold until Friday, then reassess."
    system, messages = app.state.advisor.calls[-1]
    assert "<idea>" in system
    assert '"symbol": "AAPL"' in system
    assert "scenarios" not in system  # left out to keep the context small
    assert messages[-1]["content"] == "Is the LEAPS worth it?"
    no_symbol = c.post("/api/research/ask", json={"question": "?"}, headers=WRITE)
    assert no_symbol.status_code == 422


def test_fetch_a_new_ticker(
    client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, app = client
    monkeypatch.setattr(
        app.state.ticker_fetch, "source", lambda: FakeSource(as_of=date(2024, 7, 12))
    )
    assert c.post("/api/research/fetch", json={"symbol": "ORCL"}).status_code == 403
    assert (
        c.post("/api/research/fetch", json={"symbol": "no way!"}, headers=WRITE).status_code == 422
    )
    started = c.post("/api/research/fetch", json={"symbol": "orcl"}, headers=WRITE)
    assert started.status_code == 202
    for _ in range(200):
        state = c.get("/api/research/fetch").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["ok"] is True, state
    assert state["symbol"] == "ORCL"
    assert "daily bars" in state["message"]
    assert c.get("/api/research/symbol/ORCL").status_code == 200


def test_data_map(client: tuple[TestClient, Any]) -> None:
    c, _ = client
    body = c.get("/api/system/data").json()
    by_name = {d["name"]: d for d in body["datasets"]}
    assert by_name["Option chains"]["files"] >= 5  # 6 once ORCL has been fetched
    assert by_name["Daily stock bars"]["files"] > 0
    assert by_name["Brokerage accounts and positions"]["updated_at"]
    assert by_name["Paper trading"]["files"] == 0
    assert body["root"].endswith("research0")


def test_one_failing_symbol_does_not_break_the_scan(
    client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tp_strategies import playbook

    real = playbook.analyze

    def flaky(symbol: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if symbol == "MSFT":
            raise KeyError("spread_pct")
        return real(symbol, *args, **kwargs)

    monkeypatch.setattr(playbook, "analyze", flaky)
    c, _ = client
    body = c.get("/api/research?refresh=true").json()
    assert body["errors"] == {"MSFT": "KeyError: 'spread_pct'"}
    assert body["scanned"] >= 9


def test_models_before_any_check(client: tuple[TestClient, Any]) -> None:
    c, _ = client
    body = c.get("/api/models").json()
    assert [m["model"] for m in body["models"]] == ["ml-rank", "rsi2-meta"]
    assert {m["status"] for m in body["models"]} == {"never run"}
    assert {m["versions"] for m in body["models"]} == {0}
