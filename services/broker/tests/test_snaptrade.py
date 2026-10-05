"""SnapTrade adapter against responses shaped like SnapTrade's API reference (no network)."""

from datetime import date
from typing import Any

import pytest

from tp_broker.snaptrade import SnapTradeSource

ACCOUNT = {
    "id": "acct-1",
    "brokerage_authorization": "auth-1",
    "name": "Individual",
    "number": "Z12345678",
    "institution_name": "Fidelity",
    "balance": {"total": {"amount": 15234.5, "currency": "USD"}},
    "is_paper": False,
}


class FakeApi:
    def __init__(self, activity_pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.pages = activity_pages or [[]]
        self.calls: list[tuple[str, Any]] = []

    def list_accounts(self) -> list[dict[str, Any]]:
        return [ACCOUNT]

    def account_balances(self, account_id: str) -> list[dict[str, Any]]:
        return [{"currency": {"code": "USD"}, "cash": 812.25, "buying_power": 812.25}]

    def account_positions(self, account_id: str) -> dict[str, Any]:
        return {
            "results": [
                {
                    "instrument": {"kind": "stock", "symbol": "AAPL", "description": "Apple"},
                    "units": "12.5",
                    "price": "230.10",
                    "cost_basis": "151.20",
                    "currency": "USD",
                    "cash_equivalent": False,
                },
                {
                    "instrument": {"kind": "mutualfund", "symbol": "SPAXX"},
                    "units": "2500",
                    "price": "1",
                    "cash_equivalent": True,
                },
                {
                    "instrument": {
                        "kind": "option",
                        "symbol": "AAPL  250117C00250000",
                        "multiplier": "100",
                    },
                    "units": "1",
                    "price": "3.10",
                },
            ],
            "data_freshness": {},
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


def test_snapshot_maps_accounts_and_positions() -> None:
    snap = SnapTradeSource(FakeApi()).snapshot()
    account = snap.accounts.iloc[0]
    assert account["account_number_masked"] == "…5678"
    assert account["institution"] == "Fidelity"
    assert (account["cash"], account["total_value"]) == (812.25, 15234.5)

    h = snap.holdings.set_index("kind")
    assert h.loc["stock", "symbol"] == "AAPL"
    assert h.loc["stock", "market_value"] == pytest.approx(12.5 * 230.10)
    assert h.loc["stock", "cost_basis_per_unit"] == pytest.approx(151.20)
    assert h.loc["cash", "symbol"] == "SPAXX"  # cash-equivalent flag wins over "mutualfund"
    assert h.loc["option", "market_value"] == pytest.approx(310.0)  # x100 multiplier


def act(i: int, kind: str = "BUY") -> dict[str, Any]:
    return {
        "id": f"a{i}",
        "type": kind,
        "symbol": {"symbol": "aapl"},
        "trade_date": "2024-07-01T00:00:00Z",
        "settlement_date": "2024-07-02T00:00:00Z",
        "units": 1,
        "price": 200,
        "amount": -200,
        "fee": 0,
        "currency": {"code": "USD"},
        "description": "BOUGHT",
    }


def test_activities_paginate_and_map() -> None:
    pages = [[act(i) for i in range(1000)], [act(1000, "CONTRIBUTION")]]
    api = FakeApi(pages)
    df = SnapTradeSource(api).activities(date(2024, 6, 1))
    assert len(df) == 1001
    assert df.iloc[-1]["type"] == "CONTRIBUTION"
    assert df.iloc[0]["symbol"] == "AAPL"
    assert df.iloc[0]["trade_date"] == date(2024, 7, 1)
    assert api.calls == [
        ("activities", (date(2024, 6, 1), 0)),
        ("activities", (date(2024, 6, 1), 1000)),
    ]
