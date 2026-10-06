from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tp_api.app import QuoteMode, create_app
from tp_core.config import Settings
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_paper import jobs
from tp_paper.runtime import open_paper

REPO = Path(__file__).resolve().parents[3]
MONTH_END = datetime(2024, 7, 31, 23, 0, tzinfo=UTC)
WRITE = {"X-TP-Client": "dashboard"}


def make_settings(root: Path, **overrides: object) -> Settings:
    book = root / "paper.toml"
    if not book.exists():
        book.write_text('[strategies.ma-timing]\ncapital = 20000\napproval = "manual"\n')
    base: dict[str, object] = {
        "data_root": root / "data",
        "universes_file": REPO / "config" / "universes.toml",
        "classifications_file": REPO / "config" / "classifications.toml",
        "risk_file": REPO / "config" / "risk.toml",
        "paper_file": book,
        "paper_broker": "fake",
        "dashboard_hosts": "localhost,127.0.0.1,testserver",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def lake_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("paper-api")
    run_backfill(Lake(root / "data"), FakeSource(), ["SPY", "EFA", "IEF", "VNQ", "DBC"],
                 now=datetime(2024, 8, 2, 22, tzinfo=UTC), years=3)  # fmt: skip
    return root


@pytest.fixture
def root(lake_template: Path, tmp_path: Path) -> Path:
    import shutil

    shutil.copytree(lake_template, tmp_path, dirs_exist_ok=True)
    return tmp_path


@pytest.fixture
def client(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.chdir(root)
    settings = make_settings(root)
    jobs.propose(open_paper(settings, "fake"), MONTH_END)
    app = create_app(settings, quotes=QuoteMode.off, serve_dashboard=False)
    with TestClient(app) as c:
        yield c


def pending(client: TestClient) -> list[dict[str, object]]:
    body: list[dict[str, object]] = client.get("/api/paper/proposals").json()
    return body


def test_status_shows_sleeves_gate_and_account(client: TestClient) -> None:
    body = client.get("/api/paper").json()
    assert body["broker"] == "fake-paper"
    assert body["gate"] == {"days": 60, "trades": 30}
    [sleeve] = body["sleeves"]
    assert sleeve["strategy"] == "ma-timing"
    assert sleeve["pending"] == body["pending"] > 0
    assert body["account"]["number"] == "…FAKE"
    assert body["kill_switch"] is None


def test_pending_proposals_carry_reasons_and_risk_checks(client: TestClient) -> None:
    rows = pending(client)
    assert rows
    first = rows[0]
    assert first["status"] == "pending"
    assert "10-month average" in str(first["reason"])
    assert {"check", "passed", "detail"} <= set(first["checks"][0])  # type: ignore[index]
    assert first["notional"] == pytest.approx(first["order_quantity"] * first["reference_price"])  # type: ignore[operator]


def test_writes_need_the_dashboard_header_and_the_same_origin(client: TestClient) -> None:
    pid = pending(client)[0]["id"]
    url = f"/api/paper/proposals/{pid}/approve"
    assert client.post(url, json={}).status_code == 403
    cross = client.post(url, json={}, headers={**WRITE, "Origin": "http://evil.example"})
    assert cross.status_code == 403
    assert cross.json()["detail"] == "cross-origin request refused"
    ok = client.post(url, json={"quantity": 1}, headers={**WRITE, "Origin": "http://testserver"})
    assert ok.status_code == 200, ok.text
    assert (ok.json()["status"], ok.json()["order_quantity"]) == ("approved", 1)
    assert ok.json()["decided_by"] == "dashboard"


def test_reject_then_approve_conflicts(client: TestClient) -> None:
    pid = pending(client)[0]["id"]
    rejected = client.post(f"/api/paper/proposals/{pid}/reject", json={"note": "no"}, headers=WRITE)
    assert rejected.json()["status"] == "rejected"
    again = client.post(f"/api/paper/proposals/{pid}/approve", json={}, headers=WRITE)
    assert again.status_code == 409
    assert "not pending" in again.json()["detail"]
    missing = client.post("/api/paper/proposals/nope/approve", json={}, headers=WRITE)
    assert missing.status_code == 404
    too_many = client.post(
        f"/api/paper/proposals/{pid}/approve", json={"quantity": 0}, headers=WRITE
    )
    assert too_many.status_code == 422


def test_approve_all_and_history_and_events(client: TestClient) -> None:
    count = len(pending(client))
    result = client.post("/api/paper/approve-all", json={"strategy": "ma-timing"}, headers=WRITE)
    assert len(result.json()["approved"]) == count
    assert pending(client) == []
    recent = client.get("/api/paper/proposals?scope=recent").json()
    assert {r["status"] for r in recent} == {"approved"}
    queued = client.get("/api/paper/proposals?scope=queued").json()
    assert len(queued) == count
    veto = client.post(f"/api/paper/proposals/{queued[0]['id']}/reject", json={}, headers=WRITE)
    assert veto.json()["status"] == "rejected"
    history = client.get("/api/paper/history").json()
    first = history["ma-timing"][0]
    assert first["growth"] == pytest.approx(first["equity"] / 20_000)  # $1 of capital
    assert any(e["kind"] == "propose" for e in client.get("/api/paper/events").json())


def test_kill_switch_from_the_dashboard(client: TestClient) -> None:
    assert client.post("/api/paper/kill", json={"reason": "x"}, headers=WRITE).status_code == 422
    killed = client.post("/api/paper/kill", json={"reason": "drill"}, headers=WRITE)
    assert killed.json()["kill_switch"] is True
    assert client.get("/api/paper").json()["kill_switch"]["reason"] == "drill"
    assert pending(client) == []  # unsent proposals expired


def test_without_alpaca_keys_status_still_answers(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(root)
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(make_settings(root, paper_broker="alpaca"), quotes=QuoteMode.off,
                     serve_dashboard=False)  # fmt: skip
    with TestClient(app) as c:
        body = c.get("/api/paper").json()
    assert body["account"] is None
    assert "Alpaca keys not set" in body["broker_error"]


def test_paper_needs_its_config_and_the_token(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    missing = create_app(make_settings(root, paper_file=root / "nope.toml"), quotes=QuoteMode.off,
                         serve_dashboard=False)  # fmt: skip
    with TestClient(missing) as c:
        assert c.get("/api/paper").status_code == 503
    locked = create_app(make_settings(root, dashboard_token=SecretStr("s3cret")),
                        quotes=QuoteMode.off, serve_dashboard=False)  # fmt: skip
    with TestClient(locked) as c:
        assert c.get("/api/paper/proposals").status_code == 401
        assert c.post("/api/paper/approve-all", json={}, headers=WRITE).status_code == 401
        authed = {**WRITE, "Authorization": "Bearer s3cret"}
        assert c.post("/api/paper/approve-all", json={}, headers=authed).status_code == 200
