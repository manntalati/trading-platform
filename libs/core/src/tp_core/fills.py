"""Trades you made that the brokerage sync doesn't show yet, counted until it does.

The ideas size themselves to the latest Fidelity sync, which runs hourly on trading days (or on
**Sync now**). Between a trade and the sync that shows it, and on the days SnapTrade serves a
cached copy, the ideas would still count the stock you sold as held and its proceeds as not
there. A fill you record (``tp-journal trade``, ``take`` and ``close``, or the dashboard's form)
counts at once, on top of the latest sync:

- **The holding**: each leg's quantity is added to (bought) or taken from (sold) the account's,
  until a sync shows the account's quantity moved by at least that much since the fill was
  recorded. Fills on the same symbol are matched oldest first.
- **The cash**: the money paid or received moves the account's cash and buying power, until the
  sync shows the holding, and either the broker's buying power has moved by most of it since
  (``SEEN``) or the trade has settled (the next business day). So a broker that leaves unsettled
  sale proceeds out of its buying power still has them counted: they can buy with them.
- **Stale**: a fill the sync still doesn't show after ``STALE_SESSIONS`` sessions stops counting
  and is flagged. It was probably entered wrong (or in another account): drop it.

The fills are a JSON file next to the journal under the data root (``journal/fills.json``), out of
git. Nothing here places an order: you trade at your broker and write it down.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core.calendar import NEW_YORK, next_session, sessions
from tp_core.occ import format_occ, parse_occ
from tp_core.portfolio import PortfolioSnapshot
from tp_core.storage import Lake, write_json

STALE_SESSIONS = 5
SEEN = 0.9  # the share of a fill's cash the broker's buying power must have moved by
MULTIPLIER = 100  # shares per option contract


def fills_path(lake: Lake) -> Path:
    path: Path = lake.root / "journal" / "fills.json"
    return path


def load_fills(lake: Lake) -> list[dict[str, Any]]:
    path = fills_path(lake)
    if not path.exists():
        return []
    found: list[dict[str, Any]] = json.loads(path.read_text()).get("fills", [])
    return found


def save_fills(lake: Lake, fills: Sequence[Mapping[str, Any]]) -> None:
    write_json({"fills": list(fills)}, fills_path(lake))


def is_option(symbol: str) -> bool:
    try:
        parse_occ(symbol)
    except ValueError:
        return False
    return True


def multiplier(symbol: str) -> int:
    return MULTIPLIER if is_option(symbol) else 1


def instrument_symbol(
    symbol: str, kind: str = "stock", expiration: date | None = None, strike: float | None = None
) -> str:
    """The symbol a fill is kept under: the ticker for shares, the OCC contract for a ``call``
    or ``put`` on ``symbol`` (its expiration and strike given, or ``symbol`` already OCC)."""
    name = symbol.strip().upper().replace(" ", "")
    if kind == "stock" or is_option(name):
        return name
    if kind not in ("call", "put"):
        raise ValueError("the kind is stock, call or put")
    if expiration is None or strike is None or strike <= 0:
        raise ValueError(f"a {kind} needs its expiration and strike")
    return format_occ(name, expiration, "C" if kind == "call" else "P", strike)


def describe(symbol: str) -> dict[str, str]:
    """What a leg is, in words: ``kind`` (stock, call or put), ``label`` ("XYZ shares", "XYZ Dec
    18 '26 $25 Call") and ``unit`` (shares or contracts)."""
    try:
        occ = parse_occ(symbol)
    except ValueError:
        return {
            "kind": "stock",
            "label": f"{symbol} shares",
            "unit": "shares",
            "underlying": symbol,
        }
    e = occ.expiration
    right = "Call" if occ.right == "C" else "Put"
    return {
        "kind": right.lower(),
        "label": f"{occ.root} {e:%b} {e.day} '{e:%y} ${occ.strike:g} {right}",
        "unit": "contracts",
        "underlying": occ.root,
    }


def settles(at: datetime) -> date:
    """US stocks and options settle the business day after the trade (T+1)."""
    return next_session(at.astimezone(NEW_YORK).date())


def _holdings_of(snap: PortfolioSnapshot, source: str, account_id: str) -> pd.DataFrame:
    h = snap.holdings
    if h.empty:
        return h
    return h[(h["source"] == source) & (h["account_id"] == account_id)]


def _quantity(snap: PortfolioSnapshot, source: str, account_id: str, symbol: str) -> float:
    h = _holdings_of(snap, source, account_id)
    if h.empty:
        return 0.0
    mine = h[h["symbol"].astype(str).str.upper() == symbol.upper()]
    return float(mine["quantity"].fillna(0.0).sum())


def _account(snap: PortfolioSnapshot, source: str, account_id: str) -> pd.Series | None:
    a = snap.accounts
    if a.empty:
        return None
    mine = a[(a["source"] == source) & (a["account_id"] == account_id)]
    return mine.iloc[0] if len(mine) else None


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _power(row: pd.Series | None) -> float | None:
    if row is None:
        return None
    value = row.get("buying_power")
    return float(value) if value is not None and pd.notna(value) else None


def new_fill(
    fills: Sequence[Mapping[str, Any]],
    snap: PortfolioSnapshot,
    *,
    source: str,
    account_id: str,
    legs: Sequence[Mapping[str, Any]],
    at: datetime,
    cash: float | None = None,
    note: str | None = None,
    journal_id: int | None = None,
) -> dict[str, Any]:
    """A fill to add to ``fills``: ``legs`` are ``{symbol, quantity, price}`` (quantity + for
    bought, - for sold; price per share), ``cash`` what it paid (-) or received (+) in all, by
    default the legs' quantity x price x multiplier. It remembers the account's quantities and
    buying power in ``snap`` (the latest sync) to tell later when the sync has caught up."""
    clean: list[dict[str, Any]] = [
        {
            "symbol": str(leg["symbol"]).upper(),
            "quantity": float(leg["quantity"]),
            "price": float(leg["price"]),
        }
        for leg in legs
        if float(leg["quantity"]) != 0
    ]
    if not clean:
        raise ValueError("a fill needs a leg with a quantity")
    if cash is None:
        cash = -sum(x["quantity"] * x["price"] * multiplier(x["symbol"]) for x in clean)
    return {
        "id": max((int(f["id"]) for f in fills), default=0) + 1,
        "at": at.astimezone(UTC).isoformat(timespec="seconds"),
        "source": source,
        "account_id": account_id,
        "legs": clean,
        "cash": float(cash),
        "baseline": {x["symbol"]: _quantity(snap, source, account_id, x["symbol"]) for x in clean},
        "baseline_power": _power(_account(snap, source, account_id)),
        "settles": settles(at).isoformat(),
        "note": note,
        "journal_id": journal_id,
    }


@dataclass(frozen=True)
class FillState:
    fill: Mapping[str, Any]
    holding_seen: bool  # the sync's quantities show it
    cash_seen: bool  # and its buying power (or settlement) does too
    stale: bool

    @property
    def counts_holding(self) -> bool:
        return not self.holding_seen and not self.stale

    @property
    def counts_cash(self) -> bool:
        return not self.cash_seen and not self.stale

    @property
    def status(self) -> str:
        if self.stale:
            return "stale"
        if self.cash_seen:
            return "synced"
        if self.holding_seen:
            return "settling"
        return "pending"

    def to_dict(self) -> dict[str, Any]:
        return {**self.fill, "status": self.status, "counts": self.counts_cash}


def _sessions_since(at: datetime, today: date) -> int:
    day = at.astimezone(NEW_YORK).date()
    return max(len(sessions(day, today)) - 1, 0) if today > day else 0


def assess(
    fills: Sequence[Mapping[str, Any]], snap: PortfolioSnapshot, now: datetime
) -> list[FillState]:
    """Where each fill stands against the latest sync ``snap`` (oldest fill first)."""
    today = now.astimezone(NEW_YORK).date()
    ordered = sorted(fills, key=lambda f: str(f["at"]))
    by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for f in ordered:
        for leg in f["legs"]:
            by_key.setdefault((f["source"], f["account_id"], leg["symbol"]), []).append(f)
    covered: dict[tuple[int, str], bool] = {}
    for (source, account_id, symbol), group in by_key.items():
        now_qty = _quantity(snap, source, account_id, symbol)
        start = float(group[0]["baseline"].get(symbol, 0.0))
        moved = now_qty - start
        total = 0.0
        for f in group:
            leg = next(x for x in f["legs"] if x["symbol"] == symbol)
            total += float(leg["quantity"])
            ok = (
                math.copysign(1, moved) == math.copysign(1, total)
                and abs(moved) >= abs(total) - 1e-6
            )
            covered[(int(f["id"]), symbol)] = ok and moved != 0
    taken = snap.accounts["taken_at"].max() if not snap.accounts.empty else None
    synced_day = pd.Timestamp(taken).tz_convert(NEW_YORK).date() if taken is not None else None
    out = []
    for f in ordered:
        holding = all(covered.get((int(f["id"]), x["symbol"]), False) for x in f["legs"])
        cash = float(f["cash"])
        before, power = (
            f.get("baseline_power"),
            _power(_account(snap, f["source"], f["account_id"])),
        )
        moved_power = (
            before is not None
            and power is not None
            and cash != 0
            and math.copysign(1, power - before) == math.copysign(1, cash)
            and abs(power - before) >= SEEN * abs(cash)
        )
        settled = synced_day is not None and synced_day >= date.fromisoformat(str(f["settles"]))
        stale = not holding and _sessions_since(datetime.fromisoformat(str(f["at"])), today) > (
            STALE_SESSIONS
        )
        out.append(FillState(f, holding, holding and (moved_power or settled or cash == 0), stale))
    return out


def overlay(snap: PortfolioSnapshot, states: Sequence[FillState]) -> PortfolioSnapshot:
    """``snap`` with the fills it doesn't show yet: holdings moved by their legs, cash and buying
    power by their money. The account's value stays put: a trade swaps cash for a holding."""
    if snap.empty or not states:
        return snap
    accounts, holdings = snap.accounts.copy(), snap.holdings.copy()
    for s in states:
        f = s.fill
        mine = (accounts["source"] == f["source"]) & (accounts["account_id"] == f["account_id"])
        if not mine.any():
            continue
        row = accounts[mine].iloc[0]
        if s.counts_cash:
            accounts.loc[mine, "cash"] = accounts.loc[mine, "cash"].fillna(0.0) + float(f["cash"])
            if pd.notna(row.get("buying_power")):
                accounts.loc[mine, "buying_power"] = float(row["buying_power"]) + float(f["cash"])
        if s.counts_holding:
            for leg in f["legs"]:
                holdings = _move(holdings, row, leg)
    return PortfolioSnapshot(accounts.reset_index(drop=True), holdings.reset_index(drop=True))


def _move(holdings: pd.DataFrame, account: pd.Series, leg: Mapping[str, Any]) -> pd.DataFrame:
    symbol, qty, price = str(leg["symbol"]), float(leg["quantity"]), float(leg["price"])
    mult = multiplier(symbol)
    mask = pd.Series(False, index=holdings.index)
    if not holdings.empty:
        mask = (
            (holdings["source"] == account["source"])
            & (holdings["account_id"] == account["account_id"])
            & (holdings["symbol"].astype(str).str.upper() == symbol)
        )
    if mask.any():
        i = holdings.index[mask][0]
        new = (_num(holdings.loc[i, "quantity"]) or 0.0) + qty
        if abs(new) < 1e-9:
            dropped: pd.DataFrame = holdings.drop(index=i)
            return dropped
        unit = _num(holdings.loc[i, "price"])
        holdings.loc[i, "quantity"] = new
        holdings.loc[i, "market_value"] = new * (unit if unit is not None else price) * mult
        return holdings
    option = is_option(symbol)
    row = {
        "source": account["source"],
        "account_id": account["account_id"],
        "taken_at": account["taken_at"],
        "symbol": symbol,
        "underlying": parse_occ(symbol).root if option else symbol,
        "description": "not synced yet",
        "kind": "option" if option else "stock",
        "quantity": qty,
        "price": price,
        "market_value": qty * price * mult,
        "cost_basis_per_unit": price,
        "currency": "USD",
    }
    added = pd.DataFrame([row])
    if holdings.empty:
        return added
    return pd.concat([holdings, added.reindex(columns=holdings.columns)], ignore_index=True)


def with_fills(
    lake: Lake, snap: PortfolioSnapshot, now: datetime | None = None
) -> tuple[PortfolioSnapshot, list[FillState]]:
    """The latest sync with the recorded fills it doesn't show yet, and where each stands."""
    fills = load_fills(lake)
    if not fills or snap.empty:
        return snap, []
    states = assess(fills, snap, now or datetime.now(UTC))
    return overlay(snap, states), states


def pending_cash(states: Sequence[FillState], source: str, account_id: str) -> tuple[float, int]:
    """The money the fills still add to (or take from) an account, and how many fills."""
    mine = [
        s
        for s in states
        if s.counts_cash and s.fill["source"] == source and s.fill["account_id"] == account_id
    ]
    return sum(float(s.fill["cash"]) for s in mine), len(mine)
