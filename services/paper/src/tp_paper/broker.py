"""The paper broker: Alpaca's paper account, or a simulated one for tests and demos.

``AlpacaPaperBroker`` always creates its client with ``paper=True``; there is deliberately no
way to point this package at a live account.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from tp_core.calendar import NEW_YORK, is_session, next_session
from tp_core.storage import write_json

TimeInForce = Literal["opg", "day"]

# Alpaca order statuses -> ours (see store.py for the lifecycle).
_STATUS = {
    "new": "submitted",
    "accepted": "submitted",
    "pending_new": "submitted",
    "accepted_for_bidding": "submitted",
    "calculated": "submitted",
    "held": "submitted",
    "pending_replace": "submitted",
    "replaced": "submitted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "done_for_day": "canceled",
    "canceled": "canceled",
    "pending_cancel": "canceled",
    "expired": "canceled",
    "stopped": "canceled",
    "rejected": "failed",
    "suspended": "failed",
}


@dataclass(frozen=True)
class BrokerAccount:
    account_number: str
    status: str
    equity: float
    last_equity: float  # equity at the previous close
    cash: float
    buying_power: float
    trading_blocked: bool = False


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    time_in_force: TimeInForce = "opg"
    limit_price: float | None = None


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: float
    filled_quantity: float
    filled_avg_price: float | None
    status: str  # ours, mapped from the broker's
    broker_status: str
    submitted_at: str | None = None
    filled_at: str | None = None


class PaperBroker(Protocol):
    name: str

    def account(self) -> BrokerAccount: ...

    def positions(self) -> dict[str, float]: ...

    def submit(self, request: OrderRequest) -> BrokerOrder: ...

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def orders_since(self, after: datetime) -> list[BrokerOrder]: ...

    def cancel_all(self) -> int: ...


class OrderRejectedError(RuntimeError):
    """The broker refused an order (insufficient buying power, halted symbol, bad window...)."""


# -- Alpaca -----------------------------------------------------------------------------------


class AlpacaPaperBroker:
    name = "alpaca-paper"

    def __init__(self, api_key: str, secret_key: str, *, client: Any = None) -> None:
        if client is None:
            from alpaca.trading.client import TradingClient

            client = TradingClient(api_key, secret_key, paper=True, raw_data=True)
        self._client = client

    def account(self) -> BrokerAccount:
        a = cast(dict[str, Any], self._client.get_account())
        return BrokerAccount(
            account_number=str(a.get("account_number", "")),
            status=str(a.get("status", "")),
            equity=_num(a.get("equity")),
            last_equity=_num(a.get("last_equity")),
            cash=_num(a.get("cash")),
            buying_power=_num(a.get("buying_power")),
            trading_blocked=bool(a.get("trading_blocked") or a.get("account_blocked")),
        )

    def positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in cast(list[dict[str, Any]], self._client.get_all_positions()):
            qty = abs(_num(p.get("qty")))
            out[str(p["symbol"])] = -qty if p.get("side") == "short" else qty
        return out

    def submit(self, request: OrderRequest) -> BrokerOrder:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        common: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": request.quantity,
            "side": OrderSide.BUY if request.side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.OPG if request.time_in_force == "opg" else TimeInForce.DAY,
            "client_order_id": request.client_order_id,
        }
        order = (
            LimitOrderRequest(limit_price=request.limit_price, **common)
            if request.limit_price is not None
            else MarketOrderRequest(**common)
        )
        try:
            raw = self._client.submit_order(order_data=order)
        except APIError as exc:
            raise OrderRejectedError(str(exc)) from exc
        return _alpaca_order(cast(dict[str, Any], raw))

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        from alpaca.common.exceptions import APIError

        try:
            raw = self._client.get_order_by_client_id(client_order_id)
        except APIError:
            return None
        return _alpaca_order(cast(dict[str, Any], raw))

    def orders_since(self, after: datetime) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        raw = self._client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500)
        )
        return [_alpaca_order(o) for o in cast(list[dict[str, Any]], raw)]

    def cancel_all(self) -> int:
        return len(cast(list[Any], self._client.cancel_orders() or []))


def _alpaca_order(o: dict[str, Any]) -> BrokerOrder:
    status = str(o.get("status", ""))
    avg = o.get("filled_avg_price")
    return BrokerOrder(
        id=str(o.get("id", "")),
        client_order_id=str(o.get("client_order_id", "")),
        symbol=str(o.get("symbol", "")),
        side=str(o.get("side", "")),
        quantity=_num(o.get("qty")),
        filled_quantity=_num(o.get("filled_qty")),
        filled_avg_price=None if avg in (None, "") else float(avg),
        status=_STATUS.get(status, "submitted"),
        broker_status=status,
        submitted_at=o.get("submitted_at"),
        filled_at=o.get("filled_at"),
    )


def _num(value: Any) -> float:
    return 0.0 if value in (None, "") else float(value)


# -- simulated ---------------------------------------------------------------------------------

PriceSource = Callable[[str, date], tuple[float, float] | None]  # (open, close) of a session


class FakePaperBroker:
    """A paper account simulated from the lake's bars, persisted in a JSON file so that
    ``propose``, ``submit`` and ``sync`` can run as separate processes.

    Opening-auction (``opg``) orders fill at the open of the session they were queued for;
    ``day`` orders at that session's close. No fees, no slippage beyond the bar price.
    """

    name = "fake-paper"

    def __init__(
        self,
        path: Path,
        prices: PriceSource,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        starting_cash: float = 100_000.0,
    ) -> None:
        self.path = path
        self.prices = prices
        self.now = now
        self.starting_cash = starting_cash

    # state ------------------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            data: dict[str, Any] = json.loads(self.path.read_text())
            return data
        return {
            "cash": self.starting_cash,
            "last_equity": self.starting_cash,
            "positions": {},
            "orders": [],
            "seq": 0,
        }

    def _save(self, state: dict[str, Any]) -> None:
        write_json(state, self.path)

    def _settled(self) -> dict[str, Any]:
        """State after filling every order whose auction has happened by now."""
        state = self._load()
        now = self.now()
        for order in state["orders"]:
            if order["status"] != "accepted":
                continue
            session = date.fromisoformat(order["session"])
            at = time(9, 30) if order["time_in_force"] == "opg" else time(16, 0)
            if now < datetime.combine(session, at, tzinfo=NEW_YORK):
                continue
            bar = self.prices(order["symbol"], session)
            if bar is None:
                order["status"] = "expired"  # no trading in it that day: the auction order dies
                continue
            price = bar[0] if order["time_in_force"] == "opg" else bar[1]
            limit = order.get("limit_price")
            if limit is not None and (
                (order["side"] == "buy" and price > limit)
                or (order["side"] == "sell" and price < limit)
            ):
                order["status"] = "expired"
                continue
            signed = order["qty"] if order["side"] == "buy" else -order["qty"]
            held = state["positions"].get(order["symbol"], 0.0)
            if order["side"] == "sell" and order["qty"] > held + 1e-9:
                order["status"] = "rejected"
                continue
            state["positions"][order["symbol"]] = held + signed
            if abs(state["positions"][order["symbol"]]) < 1e-9:
                del state["positions"][order["symbol"]]
            state["cash"] -= signed * price
            order.update(
                status="filled",
                filled_qty=order["qty"],
                filled_avg_price=price,
                filled_at=datetime.combine(session, at, tzinfo=NEW_YORK)
                .astimezone(UTC)
                .isoformat(),
            )
        self._save(state)
        return state

    # PaperBroker ------------------------------------------------------------------------------

    def account(self) -> BrokerAccount:
        state = self._settled()
        today = self.now().astimezone(NEW_YORK).date()
        equity = state["cash"]
        for symbol, qty in state["positions"].items():
            equity += qty * self._last_close(symbol, today)
        return BrokerAccount(
            "PA-FAKE", "ACTIVE", equity, state["last_equity"], state["cash"], state["cash"]
        )

    def mark_close(self, equity: float) -> None:
        """Remember today's closing equity as tomorrow's ``last_equity``."""
        state = self._load()
        state["last_equity"] = equity
        self._save(state)

    def positions(self) -> dict[str, float]:
        return dict(self._settled()["positions"])

    def submit(self, request: OrderRequest) -> BrokerOrder:
        state = self._settled()
        if any(o["client_order_id"] == request.client_order_id for o in state["orders"]):
            raise OrderRejectedError(f"client_order_id must be unique: {request.client_order_id}")
        now = self.now()
        local = now.astimezone(NEW_YORK)
        if request.time_in_force == "opg":
            if is_session(local.date()) and time(9, 28) <= local.time() < time(19, 0):
                raise OrderRejectedError("opg orders are not accepted between 9:28am and 7:00pm ET")
            session = (
                local.date()
                if is_session(local.date()) and local.time() < time(9, 28)
                else next_session(local.date())
            )
        else:
            session = (
                local.date()
                if is_session(local.date()) and local.time() < time(16, 0)
                else next_session(local.date())
            )
        state["seq"] += 1
        order = {
            "id": f"fake-{state['seq']}",
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "side": request.side,
            "qty": request.quantity,
            "time_in_force": request.time_in_force,
            "limit_price": request.limit_price,
            "status": "accepted",
            "session": session.isoformat(),
            "submitted_at": now.isoformat(),
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "filled_at": None,
        }
        state["orders"].append(order)
        self._save(state)
        return _fake_order(order)

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        for o in self._settled()["orders"]:
            if o["client_order_id"] == client_order_id:
                return _fake_order(o)
        return None

    def orders_since(self, after: datetime) -> list[BrokerOrder]:
        return [
            _fake_order(o)
            for o in self._settled()["orders"]
            if datetime.fromisoformat(o["submitted_at"]) >= after
        ]

    def cancel_all(self) -> int:
        state = self._settled()
        open_orders = [o for o in state["orders"] if o["status"] == "accepted"]
        for o in open_orders:
            o["status"] = "canceled"
        self._save(state)
        return len(open_orders)

    def _last_close(self, symbol: str, today: date) -> float:
        day = today
        for _ in range(10):
            bar = self.prices(symbol, day)
            if bar is not None and math.isfinite(bar[1]):
                return bar[1]
            day = date.fromordinal(day.toordinal() - 1)
        return 0.0


def _fake_order(o: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        id=o["id"],
        client_order_id=o["client_order_id"],
        symbol=o["symbol"],
        side=o["side"],
        quantity=float(o["qty"]),
        filled_quantity=float(o["filled_qty"]),
        filled_avg_price=o["filled_avg_price"],
        status=_STATUS.get(o["status"], "submitted"),
        broker_status=o["status"],
        submitted_at=o["submitted_at"],
        filled_at=o["filled_at"],
    )
