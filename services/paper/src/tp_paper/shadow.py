"""Shadow trades for buys the risk checks blocked: what the trade would have made.

A blocked proposal stays in the store (status ``blocked``) with the shares, the reference price,
the strategy's reason and every check's text, so nothing is lost when the risk limit says no. A
strategy whose exit follows from each day's bar alone (``HoldRule``; strategy 17, IBS) can then
be replayed from the lake's bars:

- in at the open of the session after the signal (the fill the order would have had);
- held while the rule would have held it, and out at the open after the first close where it
  wouldn't (for IBS: the next open, later if IBS closed below the 0.2 threshold again);
- each fill pays ``SHADOW_COST_BPS``, and the return uses adjusted opens (dividends included).

Two things keep the missed P&L from being counted twice. A later blocked buy of the same symbol
by the same strategy while the shadow is still holding is part of that trade, not a new one
(had the first gone through, the strategy wouldn't have asked again). And if the sleeve really
buys the symbol while the shadow is still holding, the shadow ends at that open: from there on
the real position earns it.

Strategies without a hold rule still have their blocked buys listed and counted, with no shadow.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

from tp_paper.ledger import PaperData
from tp_paper.store import FillRecord, Proposal

SHADOW_COST_BPS = 2.0  # per fill, as in the strategy 17 study

ShadowStatus = Literal["closed", "open", "waiting", "no rule"]


@runtime_checkable
class HoldRule(Protocol):
    """A strategy whose exit can be replayed from one bar at a time."""

    def shadow_hold(
        self, symbol: str, high: float, low: float, close: float, session: date
    ) -> tuple[float, bool] | None:
        """The signal value at the close of ``session`` and whether the rule holds ``symbol``
        through the next session; None if the rule doesn't cover ``symbol``."""
        ...


@dataclass(frozen=True)
class BlockedEntry:
    """One blocked buy (with any later ones folded into it) and its shadow trade."""

    proposal_id: str
    strategy: str
    session: str  # signal session
    symbol: str
    quantity: float
    reference_price: float
    notional: float
    reason: str
    blocked_by: dict[str, str]  # failed check -> its text
    signal: float | None  # the rule's reading at the signal close (IBS)
    status: ShadowStatus
    entry_session: str | None = None
    entry_price: float | None = None  # raw open
    exit_session: str | None = None
    exit_price: float | None = None  # raw open (closed) or last raw close (open)
    sessions_held: int | None = None
    shadow_return: float | None = None  # after costs; for "open", marked at the last close
    missed_pnl: float | None = None  # quantity x entry price x shadow return
    ended_by_fill: bool = False  # the sleeve really bought it while the shadow held
    folded: list[str] = field(default_factory=list)  # later blocked buys that are this trade

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BlockedSummary:
    strategy: str
    symbol: str
    blocked: int  # blocked buy proposals, folded ones included
    trades: int  # closed shadow trades
    open: int
    without_rule: int
    avg_return: float | None  # closed trades
    missed_pnl: float | None  # closed trades

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def blocked_buys(
    proposals: Sequence[Proposal], checks: Collection[str] | None = None
) -> list[Proposal]:
    """Blocked buy proposals, oldest first; with ``checks``, only those one of them failed."""
    out = []
    for p in proposals:
        if p.status != "blocked" or p.side != "buy":
            continue
        failed = {c["check"] for c in p.checks if not c["passed"]}
        if checks is not None and not failed & set(checks):
            continue
        out.append(p)
    return sorted(out, key=lambda p: (p.session, p.created_at, p.id))


def shadow_entries(
    blocked: Sequence[Proposal],
    data: PaperData | None,
    rules: Mapping[str, HoldRule],
    fills: Sequence[FillRecord],
    *,
    cost_bps: float = SHADOW_COST_BPS,
) -> list[BlockedEntry]:
    """Replay each blocked buy (see the module docstring). ``rules`` by strategy name;
    ``data`` must hold the symbols' bars from the earliest signal session on."""
    real_buys: dict[tuple[str, str], set[date]] = {}
    for f in fills:
        if f.side == "buy":
            real_buys.setdefault((f.strategy, f.symbol), set()).add(date.fromisoformat(f.session))
    out: list[BlockedEntry] = []
    holding_until: dict[tuple[str, str], tuple[int, int]] = {}  # key -> (exit t, index in out)
    for p in sorted(blocked, key=lambda p: (p.session, p.created_at, p.id)):
        key = (p.strategy, p.symbol)
        signal_day = date.fromisoformat(p.session)
        if key in holding_until and data is not None:
            exit_t, at = holding_until[key]
            t = _session_index(data, signal_day)
            if t is not None and t < exit_t:
                out[at].folded.append(p.id)
                continue
        rule = rules.get(p.strategy)
        entry = _replay(p, data, rule, real_buys.get(key, set()), cost_bps)
        if entry.status in ("closed", "open") and data is not None:
            end = (
                _session_index(data, date.fromisoformat(entry.exit_session))
                if entry.status == "closed" and entry.exit_session
                else None
            )
            holding_until[key] = (end if end is not None else len(data.adjusted), len(out))
        out.append(entry)
    return out


def summarize(entries: Sequence[BlockedEntry]) -> list[BlockedSummary]:
    groups: dict[tuple[str, str], list[BlockedEntry]] = {}
    for e in entries:
        groups.setdefault((e.strategy, e.symbol), []).append(e)
    out = []
    for (strategy, symbol), rows in sorted(groups.items()):
        closed = [e for e in rows if e.status == "closed"]
        returns = [e.shadow_return for e in closed if e.shadow_return is not None]
        missed = [e.missed_pnl for e in closed if e.missed_pnl is not None]
        out.append(
            BlockedSummary(
                strategy=strategy,
                symbol=symbol,
                blocked=sum(1 + len(e.folded) for e in rows),
                trades=len(closed),
                open=sum(e.status == "open" for e in rows),
                without_rule=sum(e.status == "no rule" for e in rows),
                avg_return=sum(returns) / len(returns) if returns else None,
                missed_pnl=sum(missed) if missed else None,
            )
        )
    return out


def _replay(
    p: Proposal,
    data: PaperData | None,
    rule: HoldRule | None,
    real_buys: set[date],
    cost_bps: float,
) -> BlockedEntry:
    base: dict[str, Any] = {
        "proposal_id": p.id,
        "strategy": p.strategy,
        "session": p.session,
        "symbol": p.symbol,
        "quantity": p.order_quantity,
        "reference_price": p.reference_price,
        "notional": p.notional,
        "reason": p.reason,
        "blocked_by": {c["check"]: c["detail"] for c in p.checks if not c["passed"]},
        "folded": [],
    }
    t0 = _session_index(data, date.fromisoformat(p.session)) if data is not None else None
    if rule is None or data is None or t0 is None:
        return BlockedEntry(**base, signal=None, status="no rule" if rule is None else "waiting")
    signal = _hold(rule, data, p.symbol, t0)
    if signal is None:  # the rule doesn't cover this symbol (IBS: the cash fund)
        return BlockedEntry(**base, signal=None, status="no rule")
    base["signal"] = _finite(signal[0])
    sessions = data.adjusted.sessions
    t_in = _next_open(data, p.symbol, t0 + 1)
    if t_in is None:
        return BlockedEntry(**base, status="waiting")
    cost = cost_bps / 10_000
    adj_open = data.adjusted.frame("open")[p.symbol]
    entry_adj = float(adj_open.iloc[t_in])
    entry_raw = float(data.raw_open[p.symbol].iloc[t_in])
    base |= {"entry_session": sessions[t_in].isoformat(), "entry_price": entry_raw}

    t, t_out, ended_by_fill = t_in, None, False
    while t < len(sessions):
        if t > t_in and sessions[t] in real_buys:
            t_out, ended_by_fill = t, True  # the real position takes over at this open
            break
        held = _hold(rule, data, p.symbol, t)
        if held is None or not held[1]:
            t_out = _next_open(data, p.symbol, t + 1)  # None: that open hasn't happened yet
            break
        t += 1
    if t_out is None:  # still holding: mark at the last close, paying the exit fill
        last = len(sessions) - 1
        adj_close = float(data.adjusted.frame("close")[p.symbol].iloc[last])
        ret = adj_close / entry_adj * (1 - cost) / (1 + cost) - 1.0
        return BlockedEntry(
            **base,
            status="open",
            exit_session=sessions[last].isoformat(),
            exit_price=float(data.raw_close[p.symbol].iloc[last]),
            sessions_held=last - t_in + 1,
            shadow_return=ret,
            missed_pnl=p.order_quantity * entry_raw * ret,
        )
    exit_adj = float(adj_open.iloc[t_out])
    ret = exit_adj / entry_adj * (1 - cost) / (1 + cost) - 1.0
    return BlockedEntry(
        **base,
        status="closed",
        exit_session=sessions[t_out].isoformat(),
        exit_price=float(data.raw_open[p.symbol].iloc[t_out]),
        sessions_held=t_out - t_in,
        shadow_return=ret,
        missed_pnl=p.order_quantity * entry_raw * ret,
        ended_by_fill=ended_by_fill,
    )


def _hold(rule: HoldRule, data: PaperData, symbol: str, t: int) -> tuple[float, bool] | None:
    high, low, close = (data.adjusted.value(f, t, symbol) for f in ("high", "low", "close"))
    return rule.shadow_hold(symbol, high, low, close, data.adjusted.sessions[t])


def _next_open(data: PaperData, symbol: str, t: int) -> int | None:
    """The first session from ``t`` on with an open for ``symbol`` (in both price series)."""
    if symbol not in data.raw_open.columns:
        return None
    adj = data.adjusted.frame("open")[symbol].to_numpy()
    raw = data.raw_open[symbol].to_numpy()
    for i in range(t, len(adj)):
        if math.isfinite(adj[i]) and math.isfinite(raw[i]):
            return i
    return None


def _session_index(data: PaperData | None, day: date) -> int | None:
    if data is None:
        return None
    pos = int(data.adjusted.index.searchsorted(pd.Timestamp(day), side="left"))
    if pos >= len(data.adjusted) or data.adjusted.sessions[pos] != day:
        return None
    return pos


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None
