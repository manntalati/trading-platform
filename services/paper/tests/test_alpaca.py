"""The Alpaca paper adapter against recorded-shape HTTP responses (no network)."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from tp_paper.broker import AlpacaPaperBroker, OrderRejectedError, OrderRequest

PAPER = "https://paper-api.alpaca.markets/v2"
ORDER = {
    "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
    "client_order_id": "ma-timing-20240731-SPY-buy-1",
    "created_at": "2024-08-01T12:00:01.1Z",
    "submitted_at": "2024-08-01T12:00:01.2Z",
    "filled_at": None,
    "symbol": "SPY",
    "asset_class": "us_equity",
    "qty": "9",
    "filled_qty": "0",
    "filled_avg_price": None,
    "order_type": "market",
    "type": "market",
    "side": "buy",
    "time_in_force": "opg",
    "limit_price": None,
    "status": "accepted",
}


@pytest.fixture
def broker() -> AlpacaPaperBroker:
    return AlpacaPaperBroker("key", "secret")


@responses.activate
def test_submits_market_on_open_orders_to_the_paper_endpoint(broker: AlpacaPaperBroker) -> None:
    responses.post(f"{PAPER}/orders", json=ORDER)
    order = broker.submit(OrderRequest("ma-timing-20240731-SPY-buy-1", "SPY", "buy", 9))
    body = json.loads(responses.calls[0].request.body or "{}")
    assert body == {
        "symbol": "SPY",
        "qty": 9,
        "side": "buy",
        "type": "market",
        "time_in_force": "opg",
        "client_order_id": "ma-timing-20240731-SPY-buy-1",
    }
    assert responses.calls[0].request.headers["APCA-API-KEY-ID"] == "key"
    assert (order.status, order.broker_status, order.quantity) == ("submitted", "accepted", 9.0)


@responses.activate
def test_limit_orders_carry_their_price(broker: AlpacaPaperBroker) -> None:
    responses.post(f"{PAPER}/orders", json={**ORDER, "type": "limit", "limit_price": "430"})
    broker.submit(OrderRequest("x", "SPY", "sell", 2, time_in_force="day", limit_price=430.0))
    body = json.loads(responses.calls[0].request.body or "{}")
    assert (body["type"], body["limit_price"], body["time_in_force"]) == ("limit", 430.0, "day")


@responses.activate
def test_a_refused_order_raises(broker: AlpacaPaperBroker) -> None:
    responses.post(
        f"{PAPER}/orders",
        status=403,
        json={"code": 40310000, "message": "insufficient buying power"},
    )
    with pytest.raises(OrderRejectedError, match="insufficient buying power"):
        broker.submit(OrderRequest("x", "SPY", "buy", 9))


@responses.activate
def test_reads_orders_fills_positions_and_account(broker: AlpacaPaperBroker) -> None:
    filled = {**ORDER, "status": "filled", "filled_qty": "9", "filled_avg_price": "545.12",
              "filled_at": "2024-08-01T13:30:01Z"}  # fmt: skip
    responses.get(f"{PAPER}/orders", json=[filled])
    responses.get(f"{PAPER}/orders:by_client_order_id", json=filled)
    responses.get(
        f"{PAPER}/positions",
        json=[
            {"symbol": "SPY", "qty": "9", "side": "long"},
            {"symbol": "XYZ", "qty": "2", "side": "short"},
        ],
    )
    responses.get(
        f"{PAPER}/account",
        json={"account_number": "PA3ABCDEFG12", "status": "ACTIVE", "equity": "100012.5",
              "last_equity": "100000", "cash": "95106.42", "buying_power": "190212.84",
              "trading_blocked": False},
    )  # fmt: skip
    [order] = broker.orders_since(datetime(2024, 8, 1, tzinfo=UTC))
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["status"] == ["all"]
    assert query["after"][0].startswith("2024-08-01")
    assert (order.status, order.filled_quantity, order.filled_avg_price) == ("filled", 9.0, 545.12)
    by_id = broker.order_by_client_id(ORDER["client_order_id"])
    assert by_id is not None
    assert by_id.id == ORDER["id"]
    assert broker.positions() == {"SPY": 9.0, "XYZ": -2.0}
    account = broker.account()
    assert (account.equity, account.last_equity, account.cash) == (100012.5, 100000.0, 95106.42)


@responses.activate
def test_cancel_all_counts_the_canceled_orders(broker: AlpacaPaperBroker) -> None:
    responses.delete(
        f"{PAPER}/orders", json=[{"id": "a", "status": 200}, {"id": "b", "status": 200}]
    )
    assert broker.cancel_all() == 2


@pytest.mark.parametrize(
    ("alpaca", "ours"),
    [
        ("partially_filled", "partially_filled"),
        ("expired", "canceled"),
        ("done_for_day", "canceled"),
        ("rejected", "failed"),
        ("pending_new", "submitted"),
    ],
)
@responses.activate
def test_status_mapping(broker: AlpacaPaperBroker, alpaca: str, ours: str) -> None:
    responses.get(f"{PAPER}/orders:by_client_order_id", json={**ORDER, "status": alpaca})
    order = broker.order_by_client_id("x")
    assert order is not None
    assert order.status == ours


def test_the_client_is_always_a_paper_client() -> None:
    broker = AlpacaPaperBroker("key", "secret")
    assert broker._client._base_url == "https://paper-api.alpaca.markets"
