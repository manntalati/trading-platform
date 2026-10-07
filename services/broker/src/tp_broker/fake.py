"""A deterministic, Fidelity-shaped demo portfolio for tests and for trying the dashboard
without linking a real account."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from tp_broker.base import (
    ACCOUNT_COLUMNS,
    ACTIVITY_COLUMNS,
    HOLDING_COLUMNS,
    BrokerSnapshot,
    RefreshResult,
)
from tp_core.occ import Right, format_occ

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
# Demo option positions, expiring a while after ``as_of`` (a Friday about 10 and 24 weeks out):
# (underlying, weeks out, right, strike, contracts, cost per contract, fallback price per contract)
DEMO_OPTIONS: tuple[tuple[str, int, Right, float, float, float, float], ...] = (
    ("AAPL", 10, "C", 250.0, 2, 850.0, 1240.0),
    ("QQQ", 24, "P", 450.0, 1, 600.0, 310.0),
)


@dataclass
class FakeBroker:
    """``price_of`` supplies current prices (e.g. latest lake close); fallbacks are used when it
    returns None, so the demo works on an empty lake too."""

    price_of: Callable[[str], float | None] = lambda symbol: None
    # Includes the money-market sweep (SPAXX): brokers count it as cash, not as a holding.
    cash: Mapping[str, float] = field(default_factory=lambda: {"indiv": 3000.0, "roth": 120.0})
    as_of: date = date(2024, 7, 12)
    name: str = "fake"

    def option_symbols(self) -> list[str]:
        return [
            format_occ(u, _friday(self.as_of, weeks), right, strike)
            for u, weeks, right, strike, *_ in DEMO_OPTIONS
        ]

    def snapshot(self) -> BrokerSnapshot:
        holdings = []
        for symbol, (underlying, _, right, strike, qty, cost, premium) in zip(
            self.option_symbols(), DEMO_OPTIONS, strict=True
        ):
            holdings.append(
                {
                    "account_id": ACCOUNTS["indiv"][0],
                    "symbol": symbol,
                    "underlying": underlying,
                    "description": f"{underlying} {'CALL' if right == 'C' else 'PUT'} {strike:g}",
                    "kind": "option",
                    "quantity": qty,
                    "price": premium,
                    "market_value": qty * premium,
                    "cost_basis_per_unit": cost,
                    "currency": "USD",
                }
            )
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
                # like an aggregator's daily cache: positions from the last close, transactions
                # through the day before
                "holdings_as_of": pd.Timestamp(self.as_of, tz="UTC") + pd.Timedelta(hours=20),
                "transactions_as_of": self.as_of - timedelta(days=1),
            }
            for key, (account_id, name, masked) in ACCOUNTS.items()
        ]
        return BrokerSnapshot(
            accounts=pd.DataFrame(accounts, columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(holdings, columns=list(HOLDING_COLUMNS)),
        )

    def refresh(self, *, timeout: float = 180.0) -> RefreshResult:
        return RefreshResult(1, True, 0.0, "demo positions are always fresh")

    def activities(self, since: date | None) -> pd.DataFrame:
        """A $500 contribution on the first of each of the last 12 months, a dividend, the buys
        behind the individual account's holdings, and a few closed trades."""
        rows = []
        for months_back in range(12):
            day = (self.as_of.replace(day=1) - timedelta(days=31 * months_back)).replace(day=1)
            rows.append(_activity(f"c-{day}", "demo-indiv", "CONTRIBUTION", None, day, 500.0))
        rows.append(
            _activity("d-1", "demo-indiv", "DIVIDEND", "AAPL", self.as_of - timedelta(days=40), 10)
        )
        start = self.as_of - timedelta(days=300)
        for acct, symbol, _, kind, qty, cost, _ in DEMO_HOLDINGS:
            if acct == "indiv" and kind in {"stock", "etf"}:
                rows.append(_trade(f"b-{symbol}", "BUY", symbol, start, qty, cost))
        calls = self.option_symbols()
        expired = format_occ("MSFT", _friday(start, 8), "C", 400.0)
        rows += [
            # a stock bought and sold at a profit, another at a loss
            _trade("rt-1", "BUY", "AMD", start + timedelta(days=20), 10, 150.0),
            _trade("rt-2", "SELL", "AMD", start + timedelta(days=90), -10, 172.0),
            _trade("rt-3", "BUY", "INTC", start + timedelta(days=30), 20, 35.0),
            _trade("rt-4", "SELL", "INTC", start + timedelta(days=120), -20, 31.0),
            # the option positions held now, and a call that expired worthless
            _trade("o-1", "BUY", calls[0], self.as_of - timedelta(days=30), 2, 8.5, "BUY_TO_OPEN"),
            _trade("o-2", "BUY", calls[1], self.as_of - timedelta(days=20), 1, 6.0, "BUY_TO_OPEN"),
            _trade("o-3", "BUY", expired, start, 1, 4.2, "BUY_TO_OPEN"),
            _trade("o-4", "OPTIONEXPIRATION", expired, _friday(start, 8), -1, 0.0),
            # the money-market sweep buys (cash, not trades)
            _trade("s-1", "BUY", "SPAXX", self.as_of - timedelta(days=5), 250, 1.0),
        ]
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


def _trade(
    activity_id: str,
    kind: str,
    symbol: str,
    day: date,
    units: float,
    price: float,
    option_action: str | None = None,
) -> dict[str, object]:
    multiplier = 100.0 if option_action or kind == "OPTIONEXPIRATION" else 1.0
    return {
        **_activity(activity_id, "demo-indiv", kind, symbol, day, -units * price * multiplier),
        "units": float(units),
        "price": price,
        "option_action": option_action,
        "description": f"{kind.title()} {symbol}",
    }


def _friday(start: date, weeks: int) -> date:
    day = start + timedelta(weeks=weeks)
    return day + timedelta(days=(4 - day.weekday()) % 7)
