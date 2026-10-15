"""What every brokerage connection provides. Read-only by construction: no order methods."""

from __future__ import annotations

from collections.abc import Mapping
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
    "holdings_as_of",  # when the aggregator last pulled positions from the brokerage (UTC)
    "transactions_as_of",  # the last day of transactions it has
    "buying_power",  # cash available to trade now: excludes purchases that haven't settled
    # The aggregator's link to the brokerage is broken (password changed, session expired):
    # it keeps serving the last data it got, so nothing new arrives until it is reconnected.
    "connection_disabled",
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
    "option_action",  # options: BUY_TO_OPEN, SELL_TO_CLOSE, ... (opening vs closing)
)


@dataclass(frozen=True)
class BrokerSnapshot:
    accounts: pd.DataFrame  # ACCOUNT_COLUMNS
    holdings: pd.DataFrame  # HOLDING_COLUMNS


# Where to start fetching transactions: one date for every account, or a date per account id
# (an account missing from the mapping, such as a newly linked one, gets its whole history).
Since = Mapping[str, date] | date | None


def since_for(since: Since, account_id: str) -> date | None:
    if isinstance(since, Mapping):
        return since.get(account_id)
    return since


class BrokerSource(Protocol):
    name: str

    def snapshot(self) -> BrokerSnapshot:
        """Current balances and positions of every linked account."""
        ...

    def activities(self, since: Since) -> pd.DataFrame:
        """Transactions with trade date on or after ``since`` (all history if None)."""
        ...


@dataclass(frozen=True)
class RefreshResult:
    """Asking the aggregator to pull fresh data from the brokerage before a sync."""

    requested: int  # connections asked to refresh
    completed: bool  # every account's positions came back newer before the timeout
    waited: float  # seconds
    message: str = ""


class RefreshableSource(BrokerSource, Protocol):
    def refresh(self, *, timeout: float = 180.0) -> RefreshResult:
        """Have the brokerage data re-pulled now (it is otherwise cached for a day)."""
        ...


def mask_account_number(number: str | None) -> str | None:
    """Keep only the last four characters: enough to tell accounts apart in the UI."""
    if not number:
        return None
    return f"…{number[-4:]}"
