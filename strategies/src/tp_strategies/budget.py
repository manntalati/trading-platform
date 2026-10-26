"""What the options ideas can spend: the buying power of your brokerage account, from the latest
sync (``tp-broker sync``), and how many contracts of an idea fit.

- **The account**: an option position lives in one account, so ideas are sized for one: the
  one ``TP_OPTIONS_ACCOUNT`` names (its name, or the last 4 digits of its number), else the one
  with the most buying power. The Alpaca paper account counts only when nothing else is linked.
- **Buying power**: as the broker reports it (cash available to trade now, so purchases that
  haven't settled are already out of it); if it doesn't, cash plus money-market funds.
- **What an idea needs**, per contract: the debit for anything bought (calls, puts, debit
  spreads, straddles, LEAPS); the most it can lose for a credit spread or an iron condor (what
  the broker holds against it); the strike x 100 for a cash-secured put.
- **Size**: as many contracts as the buying power pays for, but no more than keep the most it
  can lose within ``TP_OPTIONS_RISK`` of the account's value (2% by default). When even one
  contract risks more than that, it's still one contract, flagged.
- **Approval level**: ``TP_OPTIONS_LEVEL`` leaves out what the broker won't let the account
  trade (``LEVELS``, after Fidelity's tiers).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from tp_core.config import Settings
from tp_core.portfolio import CASH_KIND, Classifier, account_cash, latest_snapshot
from tp_core.storage import Lake

PAPER_SOURCES = frozenset({"alpaca"})
# The options approval level each structure needs: 2 to buy calls and puts (and straddles) and
# to sell cash-secured puts, 3 for anything with two legs on different strikes or expiries.
LEVELS: dict[str, int] = {
    "covered call": 1,
    "call": 2,
    "put": 2,
    "LEAPS call": 2,
    "straddle": 2,
    "cash-secured put": 2,
    "call debit spread": 3,
    "put debit spread": 3,
    "LEAPS call spread": 3,
    "put credit spread": 3,
    "call credit spread": 3,
    "iron condor": 3,
    "calendar": 3,
}


@dataclass(frozen=True)
class Budget:
    buying_power: float
    account_value: float
    account: str  # the account's name and the last digits of its number
    risk_per_trade: float  # the most one idea should risk, as a share of the account's value
    options_level: int | None = None
    as_of: str | None = None  # when the broker last reported it
    paper: bool = False

    @property
    def risk_cap(self) -> float:
        return self.risk_per_trade * self.account_value

    def allows(self, structure: str) -> bool:
        return self.options_level is None or LEVELS.get(structure, 3) <= self.options_level

    def affords(self, capital: float) -> bool:
        """``capital``: what one contract needs, in dollars."""
        return capital <= self.buying_power

    def within_risk(self, max_loss: float) -> bool:
        return max_loss <= self.risk_cap

    def size(self, capital: float, max_loss: float) -> dict[str, Any]:
        """How many contracts to suggest, and what they'd take (dollars per contract in)."""
        by_power = math.floor(self.buying_power / capital) if capital > 0 else 0
        by_risk = math.floor(self.risk_cap / max_loss) if max_loss > 0 else by_power
        contracts = max(min(by_power, by_risk), 1) if by_power >= 1 else 0
        return {
            "contracts": contracts,
            "capital": contracts * capital,
            "max_loss": contracts * max_loss,
            "share_of_account": (
                contracts * max_loss / self.account_value if self.account_value > 0 else None
            ),
            "buying_power_left": self.buying_power - contracts * capital,
            "over_guideline": max_loss > self.risk_cap,
            "limited_by": "risk" if by_risk < by_power else "buying power",
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"risk_cap": self.risk_cap}


def budget_from_lake(
    lake: Lake,
    *,
    account: str | None = None,
    risk_per_trade: float = 0.02,
    options_level: int | None = None,
    classifier: Classifier | None = None,
) -> Budget | None:
    """The budget from the latest brokerage sync; None before the first (or if ``account``
    matches none)."""
    snap = latest_snapshot(lake)
    if snap.empty:
        return None
    accounts = snap.accounts
    real = accounts[~accounts["source"].isin(PAPER_SOURCES)]
    pool = real if len(real) else accounts
    cash = account_cash(snap.accounts, snap.holdings).loc[pool.index]
    rows: list[tuple[float, float, pd.Series]] = []
    for (_, a), (_, c) in zip(pool.iterrows(), cash.iterrows(), strict=True):
        power = float(c["buying_power"])
        if math.isnan(power):  # not reported: cash and money-market funds
            power = float(c["cash"]) + _cash_like(snap.holdings, a, classifier)
        rows.append((power, float(c["total_value"]), a))
    if account:
        wanted = account.strip().lower()
        rows = [r for r in rows if _matches(r[2], wanted)]
    if not rows:
        return None
    power, value, a = max(rows, key=lambda r: r[0])
    name = str(a["account_name"]) if pd.notna(a["account_name"]) else "Account"
    masked = a.get("account_number_masked")
    label = f"{name} {masked}" if isinstance(masked, str) and masked else name
    taken = pd.Timestamp(a["taken_at"])
    return Budget(
        buying_power=max(power, 0.0),
        account_value=value,
        account=label,
        risk_per_trade=risk_per_trade,
        options_level=options_level,
        as_of=taken.isoformat() if pd.notna(taken) else None,
        paper=str(a["source"]) in PAPER_SOURCES,
    )


def budget_from_settings(lake: Lake, settings: Settings) -> Budget | None:
    return budget_from_lake(
        lake,
        account=settings.options_account,
        risk_per_trade=settings.options_risk_per_trade,
        options_level=settings.options_level,
        classifier=Classifier.load(settings.classifications_file),
    )


def _matches(account: pd.Series, wanted: str) -> bool:
    name = str(account.get("account_name") or "").lower()
    number = str(account.get("account_number_masked") or "")
    digits = "".join(ch for ch in number if ch.isdigit())
    return wanted in name or (wanted.isdigit() and digits.endswith(wanted))


def _cash_like(holdings: pd.DataFrame, account: pd.Series, classifier: Classifier | None) -> float:
    if holdings.empty:
        return 0.0
    mine = holdings[
        (holdings["source"] == account["source"])
        & (holdings["account_id"] == account["account_id"])
    ]
    funds = classifier.funds if classifier is not None else {}
    cash = mine[(mine["kind"] == CASH_KIND) | mine["symbol"].map(lambda s: funds.get(s) == "Cash")]
    return float(cash["market_value"].fillna(0.0).sum())
