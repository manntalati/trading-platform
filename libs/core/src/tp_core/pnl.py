"""Profit and loss from transactions: what closed trades made (realized), by average cost.

Works on the brokerage activities ``tp-broker sync`` stores, and on paper-trading fills. Every
instrument in an account keeps a running position and cost:

- a buy adds its cost (the cash paid, fees included);
- a sell realizes the cash received minus the average cost of the shares sold;
- options count contracts and use the cash that changed hands, so the x100 multiplier is already
  in the numbers; an expired contract closes at zero, so it realizes the whole premium as a loss
  (or, for a contract sold to open, as a gain);
- a short position (an option sold to open) works the same way with the signs flipped.

History only goes back to the first synced transaction. A sale of something bought before then
has an unknown cost and is reported with no realized figure rather than a wrong one.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from tp_core.occ import OccSymbol, parse_occ

EPS = 1e-9
OPTION_MULTIPLIER = 100.0

BUY_TYPES = frozenset({"BUY", "REI"})  # REI: dividend reinvestment, a buy
SELL_TYPES = frozenset({"SELL"})
CLOSE_AT_ZERO = frozenset({"OPTIONEXPIRATION", "OPTIONASSIGNMENT", "OPTIONEXERCISE"})
TRADE_TYPES = BUY_TYPES | SELL_TYPES | CLOSE_AT_ZERO | {"SPLIT"}
INCOME_TYPES = frozenset({"DIVIDEND", "INTEREST", "SUBSTITUTE_DIVIDEND"})
FEE_TYPES = frozenset({"FEE", "TAX"})
FLOW_IN = frozenset({"CONTRIBUTION", "DEPOSIT", "TRANSFER_IN"})
FLOW_OUT = frozenset({"WITHDRAWAL", "TRANSFER_OUT"})

_ACTIONS = {
    "BUY_TO_OPEN": "Buy to open",
    "BUY_TO_CLOSE": "Buy to close",
    "SELL_TO_OPEN": "Sell to open",
    "SELL_TO_CLOSE": "Sell to close",
    "OPTIONEXPIRATION": "Expired",
    "OPTIONASSIGNMENT": "Assigned",
    "OPTIONEXERCISE": "Exercised",
    "REI": "Reinvested",
    "SPLIT": "Split",
}


@dataclass
class Position:
    """A running average-cost position in one instrument."""

    quantity: float = 0.0
    cost: float = 0.0  # cash paid for a long position; minus the credit received for a short

    def trade(self, units: float, cash: float) -> float | None:
        """Apply ``units`` (+ bought, - sold) for ``cash`` (- paid, + received). Returns the
        realized P&L of the part that closed, or None if nothing closed."""
        if abs(units) < EPS:
            return None
        if abs(self.quantity) < EPS or (units > 0) == (self.quantity > 0):
            self.quantity += units
            self.cost -= cash
            return None
        closing = min(abs(units), abs(self.quantity))
        share = closing / abs(units)
        removed = self.cost * closing / abs(self.quantity)
        realized = cash * share - removed
        self.cost -= removed
        self.quantity += math.copysign(closing, units)
        rest = abs(units) - closing
        if rest > EPS:  # went through zero: the rest opens a position the other way
            self.quantity += math.copysign(rest, units)
            self.cost -= cash * (1.0 - share)
        if abs(self.quantity) < EPS:
            self.quantity, self.cost = 0.0, 0.0
        return realized


def option_label(symbol: str) -> str | None:
    """``XYZ260116C00050000`` -> ``XYZ Jan 16 '26 $50 Call``; None if not an option."""
    occ = _occ(symbol)
    if occ is None:
        return None
    e = occ.expiration
    return (
        f"{occ.root} {e:%b} {e.day} '{e:%y} ${occ.strike:g} {'Call' if occ.right == 'C' else 'Put'}"
    )


def _occ(symbol: object) -> OccSymbol | None:
    if not isinstance(symbol, str) or not symbol:
        return None
    try:
        return parse_occ(symbol)
    except ValueError:
        return None


# -- brokerage activities -----------------------------------------------------------------------

TRADE_COLUMNS = [
    "source",
    "account_id",
    "activity_id",
    "trade_date",
    "type",
    "action",
    "symbol",
    "label",
    "kind",
    "quantity",
    "price",
    "amount",
    "fee",
    "realized_pnl",
    "cost_known",
]


def broker_trades(
    activities: pd.DataFrame, *, is_cash: Callable[[str], bool] = lambda s: False
) -> pd.DataFrame:
    """Every trade in ``activities`` (oldest first) with the P&L it realized.

    ``is_cash`` marks the cash sweep (Fidelity's money-market core position): its daily
    purchases and redemptions are cash management, not trades, and are left out.
    """
    if activities.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    acts = activities[activities["type"].isin(TRADE_TYPES) & activities["symbol"].notna()]
    acts = acts[[not is_cash(str(s)) for s in acts["symbol"]]]
    acts = acts.assign(_order=[_order(r) for r in acts.itertuples()])
    acts = acts.sort_values(["trade_date", "_order", "activity_id"], kind="stable")
    positions: dict[tuple[Any, ...], Position] = {}
    rows = []
    for a in acts.itertuples():
        symbol = str(a.symbol)
        option = _occ(symbol) is not None
        pos = positions.setdefault((a.source, a.account_id, symbol), Position())
        units = _float(a.units)
        if a.type in SELL_TYPES and units > 0:
            units = -units  # some brokers report sold units as positive
        realized: float | None = None
        cost_known = True
        if a.type == "SPLIT":
            pos.quantity += units
        elif a.type in CLOSE_AT_ZERO:
            units = units if abs(units) > EPS else -pos.quantity
            if abs(pos.quantity) < EPS:
                cost_known = False  # opened before the synced history
            else:
                realized = pos.trade(units, 0.0)
        else:
            cash = _cash(a, units, option)
            # How much of an existing position this trade can close: a sale beyond what the
            # synced history bought predates it, so that part's cost is unknown.
            closable = max(pos.quantity if units < 0 else -pos.quantity, 0.0)
            if _closes(a, option, units) and closable + EPS < abs(units):
                cost_known = False
                if closable > EPS:
                    realized = pos.trade(
                        math.copysign(closable, units), cash * closable / abs(units)
                    )
            else:
                realized = pos.trade(units, cash)
        rows.append(
            {
                "source": a.source,
                "account_id": a.account_id,
                "activity_id": a.activity_id,
                "trade_date": a.trade_date,
                "type": a.type,
                "action": _action(a, option),
                "symbol": symbol,
                "label": option_label(symbol) or symbol,
                "kind": "option" if option else "equity",
                "quantity": abs(units),
                "price": _float(a.price) if pd.notna(a.price) else math.nan,
                "amount": _float(a.amount) if pd.notna(a.amount) else math.nan,
                "fee": _float(a.fee),
                "realized_pnl": math.nan if realized is None else realized,
                "cost_known": cost_known,
            }
        )
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def _order(a: Any) -> int:
    """Within a day, opening trades come before closing ones (a same-day round trip)."""
    action = str(getattr(a, "option_action", "") or "")
    if action.endswith("TO_OPEN") or a.type in BUY_TYPES:
        return 0
    if action.endswith("TO_CLOSE") or a.type in SELL_TYPES:
        return 1
    return 2


def _closes(a: Any, option: bool, units: float) -> bool:
    """Whether the trade closes a position (a sale of shares; an option closing order)."""
    if not option:
        return units < 0
    action = str(getattr(a, "option_action", "") or "")
    if action:
        return action.endswith("TO_CLOSE")
    return "CLOSING" in str(a.description or "").upper()


def _action(a: Any, option: bool) -> str:
    option_action = str(getattr(a, "option_action", "") or "")
    if option_action in _ACTIONS:
        return _ACTIONS[option_action]
    if a.type in _ACTIONS:
        return _ACTIONS[a.type]
    if option:
        text = str(a.description or "").upper()
        side = "Buy" if a.type in BUY_TYPES else "Sell"
        if "OPENING" in text:
            return f"{side} to open"
        if "CLOSING" in text:
            return f"{side} to close"
    return "Buy" if a.type in BUY_TYPES else "Sell"


def _cash(a: Any, units: float, option: bool) -> float:
    """Cash that changed hands (- paid, + received), fees included."""
    if pd.notna(a.amount) and abs(float(a.amount)) > EPS:
        return float(a.amount)
    price = _float(a.price)
    gross = units * price * (OPTION_MULTIPLIER if option else 1.0)
    return -gross - _float(a.fee)


def _float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


# -- paper fills --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """One execution: ``quantity`` is positive, ``side`` is "buy" or "sell"."""

    book: str  # who traded it (a paper strategy)
    symbol: str
    side: str
    quantity: float
    price: float
    fees: float
    day: date
    ref: str = ""


def fill_trades(fills: Iterable[Fill]) -> list[dict[str, Any]]:
    """Each fill with the P&L it realized, by average cost per book and symbol."""
    positions: dict[tuple[str, str], Position] = {}
    out = []
    for f in sorted(fills, key=lambda f: (f.day, f.side == "sell", f.ref)):
        units = f.quantity if f.side == "buy" else -f.quantity
        cash = -units * f.price - f.fees
        realized = positions.setdefault((f.book, f.symbol), Position()).trade(units, cash)
        out.append({"fill": f, "realized_pnl": realized})
    return out


# -- summaries ----------------------------------------------------------------------------------


@dataclass
class PnlSummary:
    realized: float = 0.0
    realized_ytd: float = 0.0
    closed_trades: int = 0
    winners: int = 0
    unknown_cost: int = 0  # sales of positions bought before the synced history
    income: float = 0.0  # dividends and interest
    income_ytd: float = 0.0
    fees: float = 0.0  # account fees and taxes (trading commissions are inside trades)
    net_deposits: float = 0.0
    since: date | None = None
    by_symbol: dict[str, float] = field(default_factory=dict)  # options by their readable name

    @property
    def win_rate(self) -> float | None:
        return self.winners / self.closed_trades if self.closed_trades else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized": self.realized,
            "realized_ytd": self.realized_ytd,
            "closed_trades": self.closed_trades,
            "winners": self.winners,
            "win_rate": self.win_rate,
            "unknown_cost": self.unknown_cost,
            "income": self.income,
            "income_ytd": self.income_ytd,
            "fees": self.fees,
            "net_deposits": self.net_deposits,
            "since": self.since.isoformat() if self.since else None,
            "by_symbol": dict(self.by_symbol),
        }


def summarize(trades: pd.DataFrame, activities: pd.DataFrame, today: date) -> PnlSummary:
    """Realized P&L, income, fees and deposits over the synced history."""
    out = PnlSummary()
    if not activities.empty and activities["trade_date"].notna().any():
        out.since = activities["trade_date"].dropna().min()
    if not trades.empty:
        closed = trades[trades["realized_pnl"].notna()]
        out.realized = float(closed["realized_pnl"].sum())
        this_year = closed[[d.year == today.year for d in closed["trade_date"]]]
        out.realized_ytd = float(this_year["realized_pnl"].sum())
        out.closed_trades = len(closed)
        out.winners = int((closed["realized_pnl"] > 0).sum())
        out.unknown_cost = int((~trades["cost_known"].astype(bool)).sum())
        by = closed.groupby("label")["realized_pnl"].sum().sort_values()
        out.by_symbol = {str(k): float(v) for k, v in by.items()}
    if not activities.empty:
        kind = activities["type"].str.upper()
        amount = activities["amount"].fillna(0.0)
        year = [isinstance(d, date) and d.year == today.year for d in activities["trade_date"]]
        income = kind.isin(INCOME_TYPES)
        out.income = float(amount[income].sum())
        out.income_ytd = float(amount[income & pd.Series(year, index=activities.index)].sum())
        out.fees = -float(amount[kind.isin(FEE_TYPES)].sum()) or 0.0  # never -0.0
        out.net_deposits = float(
            amount[kind.isin(FLOW_IN)].abs().sum() - amount[kind.isin(FLOW_OUT)].abs().sum()
        )
    return out
