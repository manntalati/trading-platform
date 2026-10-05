"""Alpaca account (paper by default) as a portfolio source: useful before Fidelity is linked, and
later to track the paper-trading bots alongside the real account."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pandas as pd
from alpaca.trading.client import TradingClient

from tp_broker.base import (
    ACCOUNT_COLUMNS,
    ACTIVITY_COLUMNS,
    HOLDING_COLUMNS,
    BrokerSnapshot,
    mask_account_number,
)
from tp_core.config import Settings

_KINDS = {"us_equity": "stock", "us_option": "option", "crypto": "crypto"}


def _num(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


class AlpacaAccountSource:
    name = "alpaca"

    def __init__(self, client: Any, *, paper: bool = True) -> None:
        self._client = client
        self._institution = "Alpaca (paper)" if paper else "Alpaca"

    @classmethod
    def from_settings(cls, settings: Settings) -> AlpacaAccountSource:
        key, secret = settings.require_alpaca_keys()
        client = TradingClient(key, secret, paper=settings.alpaca_paper, raw_data=True)
        return cls(client, paper=settings.alpaca_paper)

    def snapshot(self) -> BrokerSnapshot:
        account = cast(dict[str, Any], self._client.get_account())
        positions = cast(list[dict[str, Any]], self._client.get_all_positions())
        account_id = str(account.get("id") or account.get("account_number"))
        accounts = [
            {
                "account_id": account_id,
                "account_name": "Alpaca",
                "account_number_masked": mask_account_number(account.get("account_number")),
                "institution": self._institution,
                "cash": _num(account.get("cash")),
                "total_value": _num(account.get("equity") or account.get("portfolio_value")),
                "currency": account.get("currency") or "USD",
            }
        ]
        holdings = []
        for p in positions:
            qty = _num(p.get("qty")) or 0.0
            sign = -1.0 if p.get("side") == "short" else 1.0
            holdings.append(
                {
                    "account_id": account_id,
                    "symbol": p.get("symbol"),
                    "description": None,
                    "kind": _KINDS.get(str(p.get("asset_class")), "other"),
                    "quantity": sign * abs(qty),
                    "price": _num(p.get("current_price")),
                    "market_value": _num(p.get("market_value")),
                    "cost_basis_per_unit": _num(p.get("avg_entry_price")),
                    "currency": "USD",
                }
            )
        return BrokerSnapshot(
            accounts=pd.DataFrame(accounts, columns=list(ACCOUNT_COLUMNS)),
            holdings=pd.DataFrame(holdings, columns=list(HOLDING_COLUMNS)),
        )

    def activities(self, since: date | None) -> pd.DataFrame:
        # Paper accounts have no deposits/withdrawals; fills will come from the execution
        # gateway's own records once it exists.
        return pd.DataFrame(columns=list(ACTIVITY_COLUMNS))
