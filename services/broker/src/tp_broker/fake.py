"""A deterministic, Fidelity-shaped demo portfolio for tests and for trying the dashboard
without linking a real account."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from tp_broker.base import ACCOUNT_COLUMNS, ACTIVITY_COLUMNS, HOLDING_COLUMNS, BrokerSnapshot

# (account, symbol, description, kind, quantity, cost per unit, fallback price)
DEMO_HOLDINGS: tuple[tuple[str, str, str, str, float, float, float], ...] = (
    ("indiv", "AAPL", "Apple Inc", "stock", 40, 150.0, 230.0),
    ("indiv", "MSFT", "Microsoft Corp", "stock", 25, 310.0, 430.0),
    ("indiv", "NVDA", "NVIDIA Corp", "stock", 60, 45.0, 130.0),
    ("indiv", "AMZN", "Amazon.com Inc", "stock", 30, 140.0, 200.0),
    ("indiv", "JPM", "JPMorgan Chase & Co", "stock", 15, 150.0, 240.0),
    ("indiv", "QQQ", "Invesco QQQ Trust", "etf", 10, 380.0, 500.0),
    ("roth", "FXAIX", "Fidelity 500 Index Fund", "mutualfund", 45, 160.0, 210.0),
    ("roth", "XOM", "Exxon Mobil Corp", "stock", 20, 100.0, 115.0),
)
ACCOUNTS = {
    "indiv": ("demo-indiv", "Individual", "…1234"),
    "roth": ("demo-roth", "ROTH IRA", "…5678"),
}


@dataclass
class FakeBroker:
    """``price_of`` supplies current prices (e.g. latest lake close); fallbacks are used when it
    returns None, so the demo works on an empty lake too."""

    price_of: Callable[[str], float | None] = lambda symbol: None
    # Includes the money-market sweep (SPAXX): brokers count it as cash, not as a holding.
    cash: Mapping[str, float] = field(default_factory=lambda: {"indiv": 3000.0, "roth": 120.0})
    as_of: date = date(2024, 7, 12)
    name: str = "fake"

    def snapshot(self) -> BrokerSnapshot:
        holdings = []
        for acct, symbol, desc, kind, qty, cost, fallback in DEMO_HOLDINGS:
            price = self.price_of(symbol) if kind in {"stock", "etf"} else None
            price = fallback if price is None else price
            holdings.append(
                {
                    "account_id": ACCOUNTS[acct][0],
                    "symbol": symbol,
                    "underlying": symbol,
                    "description": desc,
                    "kind": kind,
                    "quantity": float(qty),
                    "price": price,
                    "market_value": qty * price,
                    "cost_basis_per_unit": cost,
                    "currency": "USD",
                }
            )
        accounts = [
            {
                "account_id": account_id,
                "account_name": name,
                "account_number_masked": masked,
                "institution": "Fidelity (demo)",
                "cash": self.cash[key],
                "total_value": None,
                "currency": "USD",
            }
            for key, (account_id, name, masked) in ACCOUNTS.items()
        ]
        return BrokerSnapshot(
            accounts=pd.DataFrame(accounts, columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(holdings, columns=list(HOLDING_COLUMNS)),
        )

    def activities(self, since: date | None) -> pd.DataFrame:
        """A $500 contribution on the first of each of the last 12 months, plus a dividend."""
        rows = []
        for months_back in range(12):
            day = (self.as_of.replace(day=1) - timedelta(days=31 * months_back)).replace(day=1)
            rows.append(_activity(f"c-{day}", "demo-indiv", "CONTRIBUTION", None, day, 500.0))
        rows.append(
            _activity("d-1", "demo-indiv", "DIVIDEND", "AAPL", self.as_of - timedelta(days=40), 10)
        )
        df = pd.DataFrame(rows, columns=list(ACTIVITY_COLUMNS))
        if since is not None:
            df = df[df["trade_date"] >= since]
        return df.reset_index(drop=True)


def _activity(
    activity_id: str, account: str, kind: str, symbol: str | None, day: date, amount: float
) -> dict[str, object]:
    return {
        "activity_id": activity_id,
        "account_id": account,
        "type": kind,
        "symbol": symbol,
        "trade_date": day,
        "settlement_date": day,
        "units": None,
        "price": None,
        "amount": amount,
        "fee": 0.0,
        "currency": "USD",
        "description": kind.title(),
    }
