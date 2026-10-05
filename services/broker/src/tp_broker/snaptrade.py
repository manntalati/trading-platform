"""Fidelity (and other brokers) through SnapTrade, read-only, with a personal API key.

SnapTrade links the brokerage through its own connection portal (you log in to Fidelity there,
never here) and serves balances, positions and transactions. With a personal key the client
identifies the user, so no user id/secret has to be stored.

Data freshness: on SnapTrade's daily plans positions are cached and refreshed once a day, which
is fine here: quantities rarely change intraday, and live values come from the quote stream.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

import pandas as pd

from tp_broker.base import (
    ACCOUNT_COLUMNS,
    ACTIVITY_COLUMNS,
    HOLDING_COLUMNS,
    BrokerSnapshot,
    mask_account_number,
)
from tp_core.config import Settings

log = logging.getLogger(__name__)

# SnapTrade instrument kinds -> ours.
_KINDS = {
    "stock": "stock",
    "adr": "adr",
    "etf": "etf",
    "mutualfund": "mutualfund",
    "cef": "cef",
    "option": "option",
    "crypto": "crypto",
}
_ACTIVITY_PAGE = 1000


class SnapTradeApi(Protocol):
    """The handful of SnapTrade calls we use, returning plain JSON-like data."""

    def list_accounts(self) -> list[dict[str, Any]]: ...

    def account_balances(self, account_id: str) -> list[dict[str, Any]]: ...

    def account_positions(self, account_id: str) -> dict[str, Any]: ...

    def account_activities(
        self, account_id: str, start: date | None, offset: int, limit: int
    ) -> dict[str, Any]: ...

    def connection_portal_url(self, broker: str | None) -> str: ...


class SdkSnapTradeApi:
    """``SnapTradeApi`` backed by the official SDK (personal API key mode)."""

    def __init__(self, client_id: str, consumer_key: str) -> None:
        from snaptrade_client.auth import SnapTradeAuth
        from snaptrade_client.client import SnapTrade

        self._client = SnapTrade(
            auth=SnapTradeAuth.personal_api_key(consumer_key=consumer_key, client_id=client_id)
        )

    @staticmethod
    def _plain(body: Any) -> Any:
        # SDK bodies are frozen schema objects; round-trip through JSON to get dicts/lists.
        return json.loads(json.dumps(body, default=str))

    def list_accounts(self) -> list[dict[str, Any]]:
        body = self._plain(self._client.account_information.list_user_accounts().body)
        return list(body)

    def account_balances(self, account_id: str) -> list[dict[str, Any]]:
        info = self._client.account_information
        return list(self._plain(info.get_user_account_balance(account_id=account_id).body))

    def account_positions(self, account_id: str) -> dict[str, Any]:
        info = self._client.account_information
        body = info.get_all_account_positions(account_id=account_id).body
        return dict(self._plain(body))

    def account_activities(
        self, account_id: str, start: date | None, offset: int, limit: int
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"account_id": account_id, "offset": offset, "limit": limit}
        if start is not None:
            kwargs["start_date"] = start.isoformat()
        body = self._client.account_information.get_account_activities(**kwargs).body
        return dict(self._plain(body))

    def connection_portal_url(self, broker: str | None) -> str:
        kwargs: dict[str, Any] = {"connection_type": "read"}
        if broker:
            kwargs["broker"] = broker
        body = self._plain(self._client.authentication.login_snap_trade_user(**kwargs).body)
        return str(body.get("redirectURI") or body.get("redirect_uri") or body)


class SnapTradeSource:
    name = "snaptrade"

    def __init__(self, api: SnapTradeApi) -> None:
        self.api = api

    @classmethod
    def from_settings(cls, settings: Settings) -> SnapTradeSource:
        client_id, consumer_key = settings.require_snaptrade_keys()
        return cls(SdkSnapTradeApi(client_id, consumer_key))

    def snapshot(self) -> BrokerSnapshot:
        accounts: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        for account in self.api.list_accounts():
            account_id = str(account["id"])
            balances = self.api.account_balances(account_id)
            positions = self.api.account_positions(account_id).get("results") or []
            accounts.append(_account_row(account, balances))
            holdings.extend(_holding_row(account_id, p) for p in positions)
        return BrokerSnapshot(
            accounts=pd.DataFrame(accounts, columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(holdings, columns=list(HOLDING_COLUMNS)),
        )

    def activities(self, since: date | None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for account in self.api.list_accounts():
            account_id = str(account["id"])
            offset = 0
            while True:
                page = self.api.account_activities(account_id, since, offset, _ACTIVITY_PAGE)
                data = page.get("data") or []
                rows.extend(_activity_row(account_id, a) for a in data)
                total = (page.get("pagination") or {}).get("total")
                offset += len(data)
                if not data or (total is not None and offset >= int(total)):
                    break
        df = pd.DataFrame(rows, columns=list(ACTIVITY_COLUMNS))
        for col in ("trade_date", "settlement_date"):
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce").dt.date
        return df


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_row(account: Mapping[str, Any], balances: list[dict[str, Any]]) -> dict[str, Any]:
    usd = [b for b in balances if (b.get("currency") or {}).get("code", "USD") == "USD"]
    cash = sum(_num(b.get("cash")) or 0.0 for b in usd) if usd else None
    total = ((account.get("balance") or {}).get("total") or {}).get("amount")
    return {
        "account_id": str(account["id"]),
        "account_name": account.get("name"),
        "account_number_masked": mask_account_number(account.get("number")),
        "institution": account.get("institution_name"),
        "cash": cash,
        "total_value": _num(total),
        "currency": "USD",
    }


def _holding_row(account_id: str, position: Mapping[str, Any]) -> dict[str, Any]:
    instrument = position.get("instrument") or {}
    raw_kind = str(instrument.get("kind") or "")
    kind = "cash" if position.get("cash_equivalent") else _KINDS.get(raw_kind, "other")
    units = _num(position.get("units")) or 0.0
    price = _num(position.get("price"))
    multiplier = _num(instrument.get("multiplier")) if kind == "option" else 1.0
    market_value = units * price * (multiplier or 1.0) if price is not None else None
    return {
        "account_id": account_id,
        "symbol": (instrument.get("symbol") or "").upper() or None,
        "description": instrument.get("description"),
        "kind": kind,
        "quantity": units,
        "price": price,
        "market_value": market_value,
        "cost_basis_per_unit": _num(position.get("cost_basis")),
        "currency": position.get("currency") or instrument.get("currency") or "USD",
    }


def _activity_row(account_id: str, activity: Mapping[str, Any]) -> dict[str, Any]:
    symbol = activity.get("symbol") or {}
    currency = activity.get("currency") or {}
    return {
        "activity_id": str(activity.get("id")),
        "account_id": account_id,
        "type": (activity.get("type") or "").upper(),
        "symbol": (symbol.get("symbol") or "").upper() or None,
        "trade_date": activity.get("trade_date"),
        "settlement_date": activity.get("settlement_date"),
        "units": _num(activity.get("units")),
        "price": _num(activity.get("price")),
        "amount": _num(activity.get("amount")),
        "fee": _num(activity.get("fee")),
        "currency": currency.get("code") if isinstance(currency, Mapping) else None,
        "description": activity.get("description"),
    }
