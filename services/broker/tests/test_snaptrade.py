"""SnapTrade adapter against payloads shaped like the live API (no network, synthetic values)."""

from datetime import date
from typing import Any

import pandas as pd
import pytest

from tp_broker.snaptrade import SnapTradeSource

ACCOUNT = {
    "id": "acct-1",
    "brokerage_authorization": "auth-1",
    "name": "Individual",
    "institution_name": "Fidelity",
    "meta": {"account_id": "Z12345678", "institution_name": "Fidelity"},
    "balance": {"total": {"amount": 6010.0, "currency": "USD"}},
    "is_paper": False,
    "sync_status": {
        "holdings": {"last_successful_sync": "2026-10-06T20:19:15.909815+00:00"},
        "transactions": {"last_successful_sync": "2026-10-05"},
    },
}


def stock(symbol: str, units: str, price: str, cost: str, kind: str = "stock") -> dict[str, Any]:
    return {
        "instrument": {"kind": kind, "symbol": symbol, "description": f"{symbol} Inc"},
        "units": units,
        "price": price,
        "cost_basis": cost,
        "currency": "USD",
    }


class FakeApi:
    def __init__(self, activity_pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.pages = activity_pages or [[]]
        self.calls: list[tuple[str, Any]] = []
        self.positions_key = "positions"

    def list_accounts(self) -> list[dict[str, Any]]:
        return [ACCOUNT]

    def account_balances(self, account_id: str) -> list[dict[str, Any]]:
        # Cash includes the money-market sweep below.
        return [{"currency": {"code": "USD"}, "cash": 1000.0, "buying_power": 1000.0}]

    def account_positions(self, account_id: str) -> dict[str, Any]:
        return {
            self.positions_key: [
                stock("AAPL", "20", "230.00", "150.00"),
                stock("VOO", "0.5", "600.00", "500.00", kind="etf"),
                {
                    **stock("SPAXX", "400", "1", "1", kind="mutualfund"),
                    "cash_equivalent": True,
                },
                {
                    "instrument": {
                        "kind": "option",
                        "symbol": "XYZ   281215C00050000",
                        "option_type": "CALL",
                        "strike_price": "50",
                        "expiration_date": "2028-12-15",
                        "multiplier": "100",
                        "underlying": {"kind": "stock", "symbol": "XYZ"},
                    },
                    "units": "2",
                    "price": "0.55",  # per share
                    "cost_basis": "1.25",  # per share
                    "currency": "USD",
                },
            ],
            "data_freshness": {"as_of": "2026-10-05T18:39:17Z"},
        }

    def account_activities(
        self, account_id: str, start: date | None, offset: int, limit: int
    ) -> dict[str, Any]:
        self.calls.append(("activities", (start, offset)))
        index = offset // limit if limit else 0
        data = self.pages[index] if index < len(self.pages) else []
        total = sum(len(p) for p in self.pages)
        return {"data": data, "pagination": {"offset": offset, "limit": limit, "total": total}}

    def connection_portal_url(self, broker: str | None) -> str:
        return "https://app.snaptrade.com/portal?x"

    def refresh_connection(self, authorization_id: str) -> None:
        self.calls.append(("refresh", authorization_id))


def test_snapshot_maps_accounts_and_positions() -> None:
    snap = SnapTradeSource(FakeApi()).snapshot()
    account = snap.accounts.iloc[0]
    assert account["account_number_masked"] == "…5678"  # from meta.account_id
    assert (account["institution"], account["cash"], account["total_value"]) == (
        "Fidelity",
        1000.0,
        6010.0,
    )

    h = snap.holdings.set_index("symbol")
    assert "SPAXX" not in h.index  # cash equivalent: already inside the cash balance
    assert h.loc["AAPL", "market_value"] == pytest.approx(4600.0)
    assert h.loc["AAPL", "cost_basis_per_unit"] == pytest.approx(150.0)
    assert h.loc["VOO", "kind"] == "etf"
    option = h.loc["XYZ281215C00050000"]  # OCC padding removed
    assert option["kind"] == "option"
    assert option["underlying"] == "XYZ"
    assert option["price"] == pytest.approx(55.0)  # per contract
    assert option["cost_basis_per_unit"] == pytest.approx(125.0)
    assert option["market_value"] == pytest.approx(110.0)
    # Holdings + cash reconcile with the broker's total.
    assert h["market_value"].sum() + account["cash"] == pytest.approx(account["total_value"])
    # How fresh SnapTrade's cached copy is.
    assert account["holdings_as_of"] == pd.Timestamp("2026-10-06T20:19:15.909815Z")
    assert account["transactions_as_of"] == date(2026, 10, 5)


class RefreshingApi(FakeApi):
    """Positions get newer a few polls after the refresh request (or never)."""

    def __init__(self, polls_until_fresh: int | None) -> None:
        super().__init__()
        self.polls_until_fresh = polls_until_fresh
        self.polls = 0

    def list_accounts(self) -> list[dict[str, Any]]:
        refreshed = any(c[0] == "refresh" for c in self.calls)
        if refreshed:
            self.polls += 1
        if refreshed and self.polls_until_fresh is not None and self.polls > self.polls_until_fresh:
            newer = {"holdings": {"last_successful_sync": "2026-10-07T14:05:00+00:00"}}
            return [ACCOUNT | {"sync_status": ACCOUNT["sync_status"] | newer}]
        return [ACCOUNT]


def test_refresh_asks_each_connection_and_waits_for_newer_positions() -> None:
    api = RefreshingApi(polls_until_fresh=2)
    slept: list[float] = []
    clock = iter(range(0, 1000, 15))
    result = SnapTradeSource(api).refresh(sleep=slept.append, clock=lambda: float(next(clock)))
    assert ("refresh", "auth-1") in api.calls
    assert result.completed
    assert result.requested == 1
    assert len(slept) == 2  # polled until the positions were newer


def test_refresh_gives_up_waiting_without_failing() -> None:
    clock = iter(range(0, 1000, 15))
    result = SnapTradeSource(RefreshingApi(polls_until_fresh=None)).refresh(
        timeout=60, sleep=lambda s: None, clock=lambda: float(next(clock))
    )
    assert not result.completed
    assert "next sync" in result.message


def test_positions_key_fallback_for_older_payloads() -> None:
    api = FakeApi()
    api.positions_key = "results"
    assert len(SnapTradeSource(api).snapshot().holdings) == 3


def act(i: int, kind: str = "BUY", option: bool = False) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"a{i}",
        "type": kind,
        "symbol": None if option else {"symbol": "aapl"},
        "option_symbol": {"ticker": "ABC   261218C00045000"} if option else None,
        "trade_date": "2024-07-01T04:00:00Z",
        "settlement_date": "2024-07-02T04:00:00Z",
        "units": 1,
        "price": 200,
        "amount": -200,
        "fee": 0,
        "currency": {"code": "USD"},
        "description": "YOU BOUGHT",
        "option_type": "BUY_TO_OPEN" if option else "",
    }
    return base


def test_activities_paginate_and_map() -> None:
    pages = [[act(i) for i in range(999)] + [act(999, option=True)], [act(1000, "CONTRIBUTION")]]
    api = FakeApi(pages)
    df = SnapTradeSource(api).activities(date(2024, 6, 1))
    assert len(df) == 1001
    assert df.iloc[-1]["type"] == "CONTRIBUTION"
    assert df.iloc[0]["symbol"] == "AAPL"
    assert df.iloc[999]["symbol"] == "ABC261218C00045000"  # option trade: from option_symbol
    assert df.iloc[999]["option_action"] == "BUY_TO_OPEN"
    assert pd.isna(df.iloc[0]["option_action"])
    assert df.iloc[0]["trade_date"] == date(2024, 7, 1)  # 04:00Z is midnight New York
    assert api.calls == [
        ("activities", (date(2024, 6, 1), 0)),
        ("activities", (date(2024, 6, 1), 1000)),
    ]
