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
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Protocol

import pandas as pd

from tp_broker.base import (
    ACCOUNT_COLUMNS,
    ACTIVITY_COLUMNS,
    HOLDING_COLUMNS,
    BrokerSnapshot,
    RefreshResult,
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

    def refresh_connection(self, authorization_id: str) -> None: ...


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

    def refresh_connection(self, authorization_id: str) -> None:
        self._client.connections.refresh_brokerage_authorization(authorization_id=authorization_id)

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
            body = self.api.account_positions(account_id)
            # The live API returns "positions"; older SDK type stubs call it "results".
            positions = body.get("positions") or body.get("results") or []
            accounts.append(_account_row(account, balances))
            # Cash-equivalent positions (the money-market sweep) are already inside the cash
            # balance; keeping them as holdings would count that money twice.
            holdings.extend(
                _holding_row(account_id, p) for p in positions if not p.get("cash_equivalent")
            )
        return BrokerSnapshot(
            accounts=pd.DataFrame(accounts, columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(holdings, columns=list(HOLDING_COLUMNS)),
        )

    def refresh(
        self,
        *,
        timeout: float = 180.0,
        poll: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> RefreshResult:
        """Ask SnapTrade to pull positions from the brokerage now (it caches them for a day; this
        also pulls the previous day's transactions if it hasn't yet), then wait until every
        account reports positions newer than before. SnapTrade charges a small fee per refresh.

        Transactions are never updated intraday: a trade made today shows in the positions after
        a refresh, but in the transaction history only the next day."""
        accounts = self.api.list_accounts()
        before = {str(a["id"]): _holdings_as_of(a) for a in accounts}
        connections = sorted(
            {
                str(a["brokerage_authorization"])
                for a in accounts
                if a.get("brokerage_authorization")
            }
        )
        for connection in connections:
            self.api.refresh_connection(connection)
        start = clock()
        while True:
            now = {str(a["id"]): _holdings_as_of(a) for a in self.api.list_accounts()}
            fresh = all(
                now.get(k) is not None and (v is None or now[k] > v)  # type: ignore[operator]
                for k, v in before.items()
            )
            waited = clock() - start
            if fresh:
                return RefreshResult(len(connections), True, waited, "positions refreshed")
            if waited >= timeout:
                return RefreshResult(
                    len(connections),
                    False,
                    waited,
                    f"refresh requested; positions not updated after {waited:.0f}s (they will "
                    "be on the next sync)",
                )
            sleep(poll)

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
        # Fidelity connections carry the brokerage account number in meta.account_id.
        "account_number_masked": mask_account_number(
            account.get("number") or (account.get("meta") or {}).get("account_id")
        ),
        "institution": account.get("institution_name"),
        "cash": cash,
        "total_value": _num(total),
        "currency": "USD",
        "holdings_as_of": _holdings_as_of(account),
        "transactions_as_of": _transactions_as_of(account),
    }


def _holdings_as_of(account: Mapping[str, Any]) -> pd.Timestamp | None:
    value = ((account.get("sync_status") or {}).get("holdings") or {}).get("last_successful_sync")
    stamp = pd.to_datetime(value, utc=True, errors="coerce") if value else None
    return None if stamp is None or pd.isna(stamp) else stamp


def _transactions_as_of(account: Mapping[str, Any]) -> date | None:
    value = ((account.get("sync_status") or {}).get("transactions") or {}).get(
        "last_successful_sync"
    )
    stamp = pd.to_datetime(value, errors="coerce") if value else None
    return None if stamp is None or pd.isna(stamp) else stamp.date()


def _holding_row(account_id: str, position: Mapping[str, Any]) -> dict[str, Any]:
    """One position. Options are stored per contract (price and cost basis x multiplier) so that
    ``quantity x price = market value`` holds for every kind."""
    instrument = position.get("instrument") or {}
    kind = _KINDS.get(str(instrument.get("kind") or ""), "other")
    units = _num(position.get("units")) or 0.0
    price = _num(position.get("price"))
    cost = _num(position.get("cost_basis"))  # per share (per underlying share for options)
    symbol = _compact(instrument.get("symbol"))
    underlying = symbol
    if kind == "option":
        multiplier = _num(instrument.get("multiplier")) or 100.0
        price = price * multiplier if price is not None else None
        cost = cost * multiplier if cost is not None else None
        underlying = _compact((instrument.get("underlying") or {}).get("symbol"))
    return {
        "account_id": account_id,
        "symbol": symbol,
        "underlying": underlying,
        "description": instrument.get("description"),
        "kind": kind,
        "quantity": units,
        "price": price,
        "market_value": units * price if price is not None else None,
        "cost_basis_per_unit": cost,
        "currency": position.get("currency") or instrument.get("currency") or "USD",
    }


def _compact(symbol: object) -> str | None:
    """Upper-case and drop OCC padding spaces ("XYZ   281215C00050000" -> "XYZ281215C00050000")."""
    text = str(symbol or "").replace(" ", "").upper()
    return text or None


def _activity_row(account_id: str, activity: Mapping[str, Any]) -> dict[str, Any]:
    symbol = activity.get("symbol") or {}
    option = activity.get("option_symbol") or {}
    currency = activity.get("currency") or {}
    return {
        "activity_id": str(activity.get("id")),
        "account_id": account_id,
        "type": (activity.get("type") or "").upper(),
        # Option trades have no "symbol"; their contract is in option_symbol.ticker.
        "symbol": _compact(symbol.get("symbol") or option.get("ticker")),
        "trade_date": activity.get("trade_date"),
        "settlement_date": activity.get("settlement_date"),
        "units": _num(activity.get("units")),
        "price": _num(activity.get("price")),
        "amount": _num(activity.get("amount")),
        "fee": _num(activity.get("fee")),
        "currency": currency.get("code") if isinstance(currency, Mapping) else None,
        "description": activity.get("description"),
        "option_action": (activity.get("option_type") or None) if option else None,
    }
