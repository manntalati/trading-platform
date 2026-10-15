"""How you trade, from your own history: round trips and what they say.

``round_trips`` matches each closing trade to the oldest open lot of the same instrument in the
same account (first in, first out), so every closed position becomes one or more round trips:
when it was opened and closed, how long it was held, what it cost and returned. Options count
contracts and the cash that changed hands (the x100 is already in it); an expired, assigned or
exercised contract closes at zero (a long loses its premium, a short keeps it). A sale with no
open lot in the synced history (bought before it) has no known cost and is only counted.

``trade_stats`` summarises them, for all trades and by kind (calls, puts, shares, and options
sold to open): win rate, the typical return (median, and cost-weighted), average winner and
loser, profit factor, how long positions are held (overall, winners vs losers), a holding-time
distribution, monthly realized P&L, the best and worst trades, and a few plain-words
observations (holding losers longer than winners is the classic "disposition effect").
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from tp_core.occ import parse_occ
from tp_core.pnl import OPTION_MULTIPLIER, broker_trades

BUCKETS = (
    ("same day", 0, 0),
    ("1-7 days", 1, 7),
    ("1-4 weeks", 8, 28),
    ("1-3 months", 29, 91),
    ("3-12 months", 92, 365),
    ("over a year", 366, 10**6),
)
CLOSE_AT_ZERO = {"Expired": "expired", "Assigned": "assigned", "Exercised": "exercised"}
TRIP_COLUMNS = [
    "account_id", "symbol", "label", "underlying", "kind", "side", "opened", "closed", "days",
    "quantity", "cost", "proceeds", "pnl", "return_pct", "exit",
]  # fmt: skip


@dataclass
class _Lot:
    quantity: float
    unit_cash: float  # per unit: paid for a long, received for a short (positive either way)
    opened: date
    side: int  # +1 long, -1 short


def _kind(symbol: str, option: bool) -> tuple[str, str]:
    """(kind, underlying): kind is "call", "put" or "shares"."""
    if not option:
        return "shares", symbol
    try:
        occ = parse_occ(symbol)
    except ValueError:
        return "call", symbol
    return ("call" if occ.right == "C" else "put"), occ.root


def _is_option(symbol: str) -> bool:
    try:
        parse_occ(symbol)
    except ValueError:
        return False
    return True


def _cash(row: Any, units: float, option: bool) -> float:
    """Cash that changed hands (- paid, + received), fees included."""
    amount = row.amount
    if amount == amount and amount is not None and abs(float(amount)) > 1e-9:
        return float(amount)
    price = float(row.price) if row.price == row.price else 0.0
    fee = float(row.fee) if row.fee == row.fee and row.fee is not None else 0.0
    return -units * price * (OPTION_MULTIPLIER if option else 1.0) - fee


def round_trips(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """From ``tp_core.pnl.broker_trades`` (oldest first): closed round trips, the lots still
    open (``account_id``, ``symbol``, ``kind``, ``side``, ``quantity``, ``opened``, ``cost``),
    and how many closing units had no known cost."""
    lots: dict[tuple[str, str], deque[_Lot]] = defaultdict(deque)
    trips: list[dict[str, Any]] = []
    unknown = 0
    for raw in trades.itertuples():
        row: Any = raw  # columns as in broker_trades
        symbol = str(row.symbol)
        option = row.kind == "option"
        kind, underlying = _kind(symbol, option)
        key = (str(row.account_id), symbol)
        day = row.trade_date
        action = str(row.action or "")
        qty = abs(float(row.quantity or 0.0))
        if qty <= 0:
            continue
        book = lots[key]
        if row.type == "SPLIT":
            held = sum(lot.quantity for lot in book)
            if held > 0:
                factor = (held + qty) / held
                for lot in book:
                    lot.quantity *= factor
                    lot.unit_cash /= factor
            continue
        exit_kind = CLOSE_AT_ZERO.get(action)
        buying = action.startswith("Buy") or action in ("Bought", "Reinvested")
        if exit_kind is None:
            if "to open" in action:
                closing = False
            elif "to close" in action:
                closing = True
            else:  # shares: a buy covers a short first, a sale closes a long
                closing = bool(book) and book[0].side == (-1 if buying else 1)
                if not closing and not buying:
                    unknown += 1  # a sale of shares bought before the synced history
                    continue
        else:
            closing = True
        signed = qty if buying else -qty
        cash = 0.0 if exit_kind else _cash(row, signed, option)
        if not closing:
            side = 1 if buying else -1
            book.append(_Lot(qty, abs(cash) / qty, day, side))
            continue
        per_unit = cash / qty  # + received when selling, - paid when buying back
        remaining = qty
        while remaining > 1e-9 and book:
            lot = book[0]
            take = min(lot.quantity, remaining)
            basis = lot.unit_cash * take
            if lot.side > 0:
                proceeds = per_unit * take
                pnl = proceeds - basis
            else:
                proceeds = -per_unit * take  # what buying back cost
                pnl = basis - proceeds
            trips.append(
                {
                    "account_id": key[0],
                    "symbol": symbol,
                    "label": row.label,
                    "underlying": underlying,
                    "kind": kind,
                    "side": "long" if lot.side > 0 else "short",
                    "opened": lot.opened,
                    "closed": day,
                    "days": (pd.Timestamp(day) - pd.Timestamp(lot.opened)).days,
                    "quantity": take,
                    "cost": basis,
                    "proceeds": proceeds,
                    "pnl": pnl,
                    "return_pct": pnl / basis if basis > 0 else math.nan,
                    "exit": exit_kind or ("sold" if lot.side > 0 else "bought back"),
                }
            )
            lot.quantity -= take
            remaining -= take
            if lot.quantity <= 1e-9:
                book.popleft()
        if remaining > 1e-9:
            unknown += 1
    still = [
        {"account_id": a, "symbol": s, "kind": _kind(s, _is_option(s))[0],
         "side": "long" if lot.side > 0 else "short", "quantity": lot.quantity,
         "opened": lot.opened, "cost": lot.unit_cash * lot.quantity}
        for (a, s), book in lots.items() for lot in book if lot.quantity > 1e-9
    ]  # fmt: skip
    open_lots = pd.DataFrame(
        still, columns=["account_id", "symbol", "kind", "side", "quantity", "opened", "cost"]
    )
    return pd.DataFrame(trips, columns=TRIP_COLUMNS), open_lots, unknown


def _group(t: pd.DataFrame) -> dict[str, Any]:
    if t.empty:
        return {"trades": 0}
    r, pnl, days = t["return_pct"], t["pnl"], t["days"]
    wins, losses = t[pnl > 0], t[pnl <= 0]
    gains, lost = float(wins["pnl"].sum()), float(-losses["pnl"].sum())
    return {
        "trades": len(t),
        "win_rate": float((pnl > 0).mean()),
        "median_return": _f(r.median()),
        "avg_return": _f(r.mean()),
        "weighted_return": _f(pnl.sum() / t["cost"].sum()) if t["cost"].sum() > 0 else None,
        "avg_win": _f(wins["return_pct"].mean()) if len(wins) else None,
        "avg_loss": _f(losses["return_pct"].mean()) if len(losses) else None,
        "total_pnl": float(pnl.sum()),
        "profit_factor": _f(gains / lost) if lost > 0 else None,
        "median_days": _f(days.median()),
        "avg_days": _f(days.mean()),
        "median_days_winners": _f(wins["days"].median()) if len(wins) else None,
        "median_days_losers": _f(losses["days"].median()) if len(losses) else None,
        "avg_size": _f(t["cost"].mean()),
        "expired": int((t["exit"] == "expired").sum()),
    }


def _f(value: Any) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _buckets(t: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {"bucket": name, "trades": int(((t["days"] >= lo) & (t["days"] <= hi)).sum())}
        for name, lo, hi in BUCKETS
    ]


def _trip_view(row: pd.Series) -> dict[str, Any]:
    return {
        "label": row["label"],
        "kind": row["kind"],
        "side": row["side"],
        "opened": str(row["opened"]),
        "closed": str(row["closed"]),
        "days": int(row["days"]),
        "pnl": float(row["pnl"]),
        "return_pct": _f(row["return_pct"]),
        "exit": row["exit"],
    }


def trade_stats(
    trips: pd.DataFrame, open_lots: pd.DataFrame, unknown: int, today: date
) -> dict[str, Any]:
    """Everything the Trades page and the side panel show about how you trade."""
    longs = trips[trips["side"] == "long"] if len(trips) else trips
    groups = {
        "all": trips,
        "calls": longs[longs["kind"] == "call"] if len(longs) else longs,
        "puts": longs[longs["kind"] == "put"] if len(longs) else longs,
        "shares": longs[longs["kind"] == "shares"] if len(longs) else longs,
        "options_sold": trips[(trips["side"] == "short") & (trips["kind"] != "shares")]
        if len(trips)
        else trips,
    }
    by_kind = {name: _group(t) for name, t in groups.items()}
    monthly: list[dict[str, Any]] = []
    if len(trips):
        months = pd.to_datetime(trips["closed"]).dt.to_period("M")
        per = trips.groupby(months)["pnl"].agg(["sum", "count"]).tail(18)
        monthly = [{"month": str(m), "pnl": float(v["sum"]), "trades": int(v["count"])}
                   for m, v in per.iterrows()]  # fmt: skip
    ranked = trips.dropna(subset=["return_pct"]).sort_values("return_pct") if len(trips) else trips
    # winners and losers only, so a short history never lists a loss among the best
    winners = ranked[ranked["return_pct"] > 0] if len(ranked) else ranked
    losers = ranked[ranked["return_pct"] < 0] if len(ranked) else ranked
    held = open_lots.copy()
    if len(held):
        held["age"] = [(pd.Timestamp(today) - pd.Timestamp(d)).days for d in held["opened"]]
    return {
        "since": str(min(trips["opened"])) if len(trips) else None,
        "by_kind": by_kind,
        "holding": {name: _buckets(t) for name, t in groups.items() if len(t)},
        "monthly": monthly,
        "best": [_trip_view(r) for _, r in winners.tail(5).iloc[::-1].iterrows()],
        "worst": [_trip_view(r) for _, r in losers.head(5).iterrows()],
        "open": {
            "positions": len(held),
            "median_age_days": _f(held["age"].median()) if len(held) else None,
            "oldest_days": int(held["age"].max()) if len(held) else None,
        },
        "unknown_cost": unknown,
        "observations": observations(by_kind),
    }


def observations(by_kind: dict[str, dict[str, Any]]) -> list[str]:
    """A few plain-words readings of the numbers (only where there's enough to say it)."""
    out = []
    names = {"calls": "calls", "puts": "puts", "shares": "shares", "options_sold": "options sold"}
    for key, name in names.items():
        g = by_kind.get(key) or {}
        if g.get("trades", 0) < 5:
            continue
        w, lo = g.get("median_days_winners"), g.get("median_days_losers")
        if w is not None and lo is not None and w > 0 and lo >= 1.5 * w:
            out.append(
                f"You hold losing {name} {lo / w:.1f}x longer than winning ones "
                f"({lo:.0f} vs {w:.0f} days): cutting losers sooner is the usual fix."
            )
        elif w is not None and lo is not None and lo > 0 and w >= 2 * lo:
            out.append(f"Your winning {name} run {w / lo:.1f}x longer than the losers: you let "
                       "winners ride.")  # fmt: skip
        win, loss = g.get("avg_win"), g.get("avg_loss")
        if win is not None and loss is not None and g.get("win_rate") is not None:
            breakeven = -loss / (win - loss) if win - loss > 0 else None
            if breakeven is not None:
                out.append(
                    f"{name.capitalize()}: winners average {win:+.0%}, losers {loss:+.0%}, so you "
                    f"need to be right {breakeven:.0%} of the time; you are {g['win_rate']:.0%}."
                )
        if key in ("calls", "puts") and g.get("expired"):
            share = g["expired"] / g["trades"]
            if share >= 0.2:
                out.append(f"{share:.0%} of your {name} were held to expiry and expired "
                           "worthless.")  # fmt: skip
    return out


def stats_from_activities(
    activities: pd.DataFrame, today: date, *, is_cash: Any = lambda s: False
) -> dict[str, Any]:
    """``trade_stats`` straight from the stored brokerage activities."""
    trades = broker_trades(activities, is_cash=is_cash)
    trips, open_lots, unknown = round_trips(trades)
    return trade_stats(trips, open_lots, unknown, today)
