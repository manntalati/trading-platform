"""Positions with statistics, and asking Claude about them (with a stand-in client: no network)."""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tp_api import positions as pos_api
from tp_api.app import QuoteMode, create_app
from tp_api.claude import Answer, ClaudeAdvisor
from tp_api.positions import AlpacaMarketData
from tp_core.positions import OptionQuote

from .test_api import lake_root, settings_for  # noqa: F401  (lake_root is a fixture)

NOW = datetime(2024, 7, 15, 15, 0, tzinfo=UTC)  # the Monday after the demo's last sync
WRITE = {"X-TP-Client": "dashboard"}


class FakeMarket:
    name = "fake"
    error: str | None = None

    def __init__(self) -> None:
        self.asked: list[Sequence[str]] = []

    def option_quotes(self, contracts: Sequence[str]) -> dict[str, OptionQuote]:
        self.asked.append(contracts)
        calls = [c for c in contracts if "C" in c[-9:]]
        return {c: OptionQuote(bid=11.8, ask=12.2, last=12.0) for c in calls}

    def stock_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        return {"AAPL": 240.0} if "AAPL" in symbols else {}


class FakeAdvisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def ask(self, system: str, messages: list[dict[str, Any]]) -> Answer:
        self.calls.append((system, messages))
        return Answer("Hold until Friday, then reassess.", "claude-test", "end_turn",
                      sources=[{"url": "https://example.test/x", "title": "X"}], searches=1,
                      input_tokens=1200, output_tokens=150)  # fmt: skip


@pytest.fixture
def app_client(
    lake_root: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Any]]:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(pos_api, "now_utc", lambda: NOW)
    app = create_app(settings_for(lake_root), quotes=QuoteMode.off, serve_dashboard=False)
    app.state.market = FakeMarket()
    app.state.advisor = FakeAdvisor()
    with TestClient(app) as c:
        yield c, app


def test_positions_have_stats_and_notes(app_client: tuple[TestClient, Any]) -> None:
    client, app = app_client
    body = client.get("/api/positions").json()
    assert body["synced"] is True
    rows = {p["symbol"]: p for p in body["positions"]}

    aapl = rows["AAPL"]
    assert (aapl["price"], aapl["price_source"]) == (240.0, "live")
    assert aapl["pnl"] == pytest.approx((240.0 - 150.0) * 40)
    assert aapl["stats"]["returns"]["1M"] is not None
    assert aapl["stats"]["vol_20d"] > 0
    assert "Volatility" in [n["title"] for n in aapl["notes"]]
    assert rows["MSFT"]["price_source"] == "last close"
    assert rows["FXAIX"]["price_source"] == "broker"  # a mutual fund: no bars, no stream

    call = next(
        p for p in body["positions"] if p["kind"] == "option" and p["label"].endswith("Call")
    )
    s = call["stats"]
    assert (call["price"], call["price_source"]) == (pytest.approx(12.0), "mid")
    assert s["spot"] == 240.0
    assert s["iv"] > 0
    assert s["theta_day"] < 0
    assert s["breakeven"] == pytest.approx(258.5)  # strike 250 + 8.50 paid
    assert len(s["scenarios"]["rows"]) == 7
    assert {"Time decay", "What it needs", "Hold or sell"} <= {n["title"] for n in call["notes"]}
    assert call["underlying_stats"]["vol_20d"] > 0

    put = next(p for p in body["positions"] if p["kind"] == "option" and p["label"].endswith("Put"))
    assert put["price_source"] == "broker"  # no quote for it
    assert put["notes"][0]["title"] == "No live quote"

    exposure = {e["underlying"]: e for e in body["exposure"]}
    assert exposure["AAPL"]["net_shares"] == pytest.approx(40 + s["delta"] * 200)
    assert body["totals"]["theta_day"] < 0
    assert body["cash"]["cash"] == pytest.approx(3120.0)  # the demo reports no totals
    assert body["cash"]["unsettled"] == 0
    assert body["market_data"] == {"source": "fake", "error": None, "option_quotes": 1,
                                   "options": 2}  # fmt: skip
    assert body["ask"] == {"available": False, "model": "claude-opus-5-5"}
    assert app.state.market.asked


def test_ask_sends_the_position_and_the_conversation(app_client: tuple[TestClient, Any]) -> None:
    client, app = app_client
    call = next(
        p for p in client.get("/api/positions").json()["positions"] if p["kind"] == "option"
    )
    history = [{"role": "user", "content": "Is it worth holding?"},
               {"role": "assistant", "content": "Depends on the move."}]  # fmt: skip
    body = {
        "question": "And if it drops 5% tomorrow?",
        "symbol": call["symbol"],
        "history": history,
    }
    assert client.post("/api/positions/ask", json=body).status_code == 403  # no client header

    reply = client.post("/api/positions/ask", json=body, headers=WRITE).json()
    assert reply["answer"] == "Hold until Friday, then reassess."
    assert reply["sources"] == [{"url": "https://example.test/x", "title": "X"}]
    assert reply["usage"] == {"input_tokens": 1200, "output_tokens": 150}
    system, messages = app.state.advisor.calls[-1]
    assert "<position_in_question>" in system
    assert call["label"] in system
    assert "Monday 2024-07-15 11:00 EDT" in system
    assert messages == [*history, {"role": "user", "content": "And if it drops 5% tomorrow?"}]

    whole = client.post("/api/positions/ask", json={"question": "How's the account?"},
                        headers=WRITE)  # fmt: skip
    assert whole.status_code == 200
    assert "account as a whole" in app.state.advisor.calls[-1][0]


def test_ask_rejects_bad_requests(app_client: tuple[TestClient, Any]) -> None:
    client, app = app_client
    odd = {"question": "?", "history": [{"role": "assistant", "content": "hi"}]}
    assert client.post("/api/positions/ask", json=odd, headers=WRITE).status_code == 422
    unknown = {"question": "?", "symbol": "ZZZZ"}
    assert client.post("/api/positions/ask", json=unknown, headers=WRITE).status_code == 404

    app.state.advisor = ClaudeAdvisor(app.state.settings)  # no ANTHROPIC_API_KEY
    missing = client.post("/api/positions/ask", json={"question": "?"}, headers=WRITE)
    assert missing.status_code == 503
    assert "ANTHROPIC_API_KEY" in missing.json()["detail"]


# -- the Claude client ------------------------------------------------------------------------


def message(*content: Any, stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(content), stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                           stop_details=None)  # fmt: skip


def text(t: str, *urls: str) -> SimpleNamespace:
    cites = [SimpleNamespace(url=u, title=f"title {u[-1]}") for u in urls]
    return SimpleNamespace(type="text", text=t, citations=cites)


class FakeStream:
    def __init__(self, reply: Any) -> None:
        self.reply = reply

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.reply


class FakeMessages:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeStream(reply)


def fake_client(*replies: Any) -> SimpleNamespace:
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(list(replies))))


URL2 = "https://a.test/2"


def test_claude_request_shape_and_paused_turns(tmp_path: Path) -> None:
    search = SimpleNamespace(type="server_tool_use", name="web_search")
    client = fake_client(
        message(search, text("Earnings are Thursday. ", "https://a.test/1"), stop="pause_turn"),
        message(text("Selling before then avoids the IV crush.", "https://a.test/1", URL2)),
    )
    advisor = ClaudeAdvisor(settings_for(tmp_path), client=client)
    answer = advisor.ask("system", [{"role": "user", "content": "hold?"}])
    assert answer.text == "Earnings are Thursday. Selling before then avoids the IV crush."
    assert answer.searches == 1
    assert [s["url"] for s in answer.sources] == ["https://a.test/1", "https://a.test/2"]
    assert (answer.input_tokens, answer.output_tokens) == (200, 40)

    first, second = client.beta.messages.calls
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert "thinking" not in first  # adaptive by default
    # The paused turn is sent back so Claude can carry on from where it stopped.
    assert second["messages"][-1]["role"] == "assistant"
    assert len(second["messages"]) == 2


def test_claude_refusal_and_no_web_search(tmp_path: Path) -> None:
    refused = message(stop="refusal")
    refused.stop_details = SimpleNamespace(explanation="policy")
    advisor = ClaudeAdvisor(settings_for(tmp_path), client=fake_client(refused))
    answer = advisor.ask("s", [{"role": "user", "content": "?"}])
    assert (answer.stop_reason, answer.text) == (
        "refusal",
        "Claude declined to answer this one. (policy)",
    )

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    rejected = anthropic.BadRequestError(
        "web_search is not enabled", response=httpx2.Response(400, request=request), body=None
    )
    client = fake_client(rejected, message(text("Fine without it.")))
    answer = ClaudeAdvisor(settings_for(tmp_path), client=client).ask("s", [])
    assert answer.text == "Fine without it."
    assert answer.note == "Web search isn't enabled for this API key."
    assert "tools" not in client.beta.messages.calls[-1]


# -- market data cache ------------------------------------------------------------------------


class CountingSource:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    def option_snapshots(self, contracts: Sequence[str]) -> pd.DataFrame:
        self.calls.append(list(contracts))
        if self.fail:
            raise ConnectionError("offline")
        return pd.DataFrame(
            [{"contract": "XYZ260116C00100000", "bid": 1.0, "ask": 1.2, "last_price": None,
              "implied_volatility": 0.5, "quote_at": pd.Timestamp("2026-01-12T15:00Z")}]
        )  # fmt: skip

    def underlying_snapshots(self, symbols: Sequence[str]) -> pd.DataFrame:
        return pd.DataFrame([{"symbol": s, "price": 10.0} for s in symbols])


def test_market_data_is_cached_briefly_and_failures_reported() -> None:
    source = CountingSource()
    clock = [0.0]
    market = AlpacaMarketData(source, ttl=30, clock=lambda: clock[0])  # type: ignore[arg-type]
    wanted = ["XYZ260116C00100000", "XYZ260116P00090000"]
    quotes = market.option_quotes(wanted)
    assert quotes["XYZ260116C00100000"].bid == 1.0
    assert "XYZ260116P00090000" not in quotes
    market.option_quotes(wanted)
    assert len(source.calls) == 1  # both cached, the miss too
    clock[0] = 31
    source.fail = True
    assert market.option_quotes(wanted)["XYZ260116C00100000"].bid == 1.0  # stale beats nothing
    assert market.error == "ConnectionError: offline"
    assert market.stock_prices(["XYZ"]) == {"XYZ": 10.0}


def test_portfolio_risk(app_client: tuple[TestClient, Any]) -> None:
    client, _ = app_client
    risk = client.get("/api/positions/risk").json()
    assert risk["synced"] is True
    assert risk["beta"] is not None
    assert 0 < risk["var"]["historical_95"] <= risk["var"]["historical_99"]
    assert risk["greeks"]["theta_day"] < 0  # long options decay
    assert len(risk["scenarios"]["rows"]) == 7
    assert risk["scenarios"]["vol_shifts"] == [-0.05, 0.0, 0.05]
    assert 0 < risk["coverage"] < 1  # FXAIX has no bars
    assert risk["correlation"]["symbols"]
