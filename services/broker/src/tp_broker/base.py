"""What every brokerage connection provides. Read-only by construction: no order methods."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pandas as pd

ACCOUNT_COLUMNS = (
    "account_id",
    "account_name",
    "account_number_masked",
    "institution",
    "cash",
    "total_value",
    "currency",
)
HOLDING_COLUMNS = (
    "account_id",
    "symbol",
    "underlying",  # the symbol itself, or an option's underlying
    "description",
    "kind",
    "quantity",
    "price",
    "market_value",
    "cost_basis_per_unit",
    "currency",
)
ACTIVITY_COLUMNS = (
    "activity_id",
    "account_id",
    "type",
    "symbol",
    "trade_date",
    "settlement_date",
    "units",
    "price",
    "amount",
    "fee",
    "currency",
    "description",
)


@dataclass(frozen=True)
class BrokerSnapshot:
    accounts: pd.DataFrame  # ACCOUNT_COLUMNS
    holdings: pd.DataFrame  # HOLDING_COLUMNS


class BrokerSource(Protocol):
    name: str

    def snapshot(self) -> BrokerSnapshot:
        """Current balances and positions of every linked account."""
        ...

    def activities(self, since: date | None) -> pd.DataFrame:
        """Transactions with trade date on or after ``since`` (all history if None)."""
        ...


def mask_account_number(number: str | None) -> str | None:
    """Keep only the last four characters: enough to tell accounts apart in the UI."""
    if not number:
        return None
    return f"…{number[-4:]}"
