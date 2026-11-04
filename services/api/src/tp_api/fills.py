"""Trades you made that the brokerage sync doesn't show yet (``tp_core.fills``), from the dashboard.

``GET /api/fills`` lists them with where each stands (pending, settling, synced, stale) and the
accounts a new one can go in; ``POST`` records one (a sale's proceeds count in the ideas' buying
power at once, the holding is gone from them); ``DELETE /api/fills/{id}`` drops one entered
wrong. Writes need the dashboard's client header, like every other write. Nothing is ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_api.queries import clean
from tp_core.fills import assess, load_fills, multiplier, new_fill, save_fills, with_fills
from tp_core.portfolio import account_label, latest_snapshot
from tp_strategies.budget import PAPER_SOURCES, fill_account

router = APIRouter(prefix="/api/fills", dependencies=[Depends(require_token)])
Write = [Depends(require_dashboard_client)]
SHOWN = 20  # the newest fills listed


def now_utc() -> datetime:
    return datetime.now(UTC)


class NewFill(BaseModel):
    side: Literal["buy", "sell"]
    symbol: str = Field(min_length=1, max_length=32)
    quantity: float = Field(gt=0, description="Shares, or contracts for an option")
    price: float = Field(ge=0, description="Your fill, per share")
    account: str | None = Field(default=None, max_length=64, description="Name or last 4 digits")
    note: str = Field(default="", max_length=500)


def _key(row: Any) -> str:
    """How the form names an account: the last four digits of its number, else its name."""
    digits = "".join(ch for ch in str(row.get("account_number_masked") or "") if ch.isdigit())
    return digits[-4:] if digits else str(row.get("account_name") or "")


def view(c: Ctx) -> dict[str, Any]:
    raw = latest_snapshot(c.lake)
    fills = load_fills(c.lake)
    states = assess(fills, raw, now_utc()) if fills and not raw.empty else []
    names = {}
    if not raw.empty:
        for _, a in raw.accounts.iterrows():
            names[(str(a["source"]), str(a["account_id"]))] = account_label(a.to_dict())
    shown = []
    for st in reversed(states[-SHOWN:]):
        f = st.to_dict()
        f["account"] = names.get((str(f.pop("source")), str(f.pop("account_id"))), "an account")
        f.pop("baseline", None)
        f.pop("baseline_power", None)
        shown.append(f)
    accounts = (
        [
            {"key": _key(a), "label": account_label(a.to_dict())}
            for _, a in raw.accounts.iterrows()
            if str(a["source"]) not in PAPER_SOURCES
        ]
        if not raw.empty
        else []
    )
    out: dict[str, Any] = clean({"fills": shown, "accounts": accounts, "synced": not raw.empty})
    return out


@router.get("")
def list_fills(c: Ctx) -> dict[str, Any]:
    return view(c)


def _changed(request: Request) -> None:
    """The ideas re-run on the next look (the fills are one of their inputs)."""
    cache = getattr(request.app.state, "research_cache", None)
    if cache is not None:
        cache.clear()


@router.post("", dependencies=Write)
def record(body: NewFill, c: Ctx, request: Request) -> dict[str, Any]:
    raw = latest_snapshot(c.lake)
    if raw.empty:
        raise HTTPException(409, "no brokerage sync yet: there's no account to count it in")
    snap, _ = with_fills(c.lake, raw, now_utc())
    symbol = body.symbol.strip().upper()
    row = fill_account(snap, account=body.account, symbol=symbol,
                       default=c.settings.options_account, classifier=c.classifier)  # fmt: skip
    if row is None:
        raise HTTPException(422, f"no account matches {body.account!r}")
    signed = body.quantity if body.side == "buy" else -body.quantity
    fills = load_fills(c.lake)
    fill = new_fill(
        fills,
        raw,
        source=str(row["source"]),
        account_id=str(row["account_id"]),
        legs=[{"symbol": symbol, "quantity": signed, "price": body.price}],
        cash=-signed * body.price * multiplier(symbol),
        at=now_utc(),
        note=body.note or None,
    )
    save_fills(c.lake, [*fills, fill])
    _changed(request)
    return view(c)


@router.delete("/{fill_id}", dependencies=Write)
def drop(fill_id: int, c: Ctx, request: Request) -> dict[str, Any]:
    fills = load_fills(c.lake)
    kept = [f for f in fills if int(f["id"]) != fill_id]
    if len(kept) == len(fills):
        raise HTTPException(404, f"no fill #{fill_id}")
    save_fills(c.lake, kept)
    _changed(request)
    return view(c)
