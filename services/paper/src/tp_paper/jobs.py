"""The paper-trading cycle: propose -> decide -> submit -> sync.

Every function takes ``now`` so the whole cycle can be replayed in tests.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from tp_core import metrics
from tp_core.bars import load_bars
from tp_core.calendar import NEW_YORK, is_session, last_completed_session
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_paper.broker import (
    BrokerAccount,
    BrokerOrder,
    FakePaperBroker,
    OrderRejectedError,
    OrderRequest,
    PaperBroker,
    TimeInForce,
)
from tp_paper.config import PaperBook
from tp_paper.ledger import PaperContext, PaperData, book_positions, sleeve_portfolio
from tp_paper.store import (
    OPEN_AT_BROKER,
    FillRecord,
    PaperStore,
    Proposal,
    SleeveDay,
    iso,
    now_iso,
)
from tp_risk.manager import RiskManager
from tp_trading.costs import CostModel
from tp_trading.events import OrderIntent, RiskDecision
from tp_trading.risk import Book

log = logging.getLogger(__name__)
GATE_DAYS = 60  # paper gate: trading days ...
GATE_TRADES = 30  # ... and trades, before a strategy can be considered for live micro
BOT_HEARTBEAT_STALE = timedelta(minutes=15)  # the bot writes one at least every 5 minutes


class PaperError(RuntimeError):
    """A job could not run (stale data, kill switch, bad decision...)."""


@dataclass
class Paper:
    """Everything a paper job needs."""

    store: PaperStore
    broker: PaperBroker
    book: PaperBook
    lake: Lake
    risk: RiskManager
    classifier: Classifier
    time_in_force: TimeInForce = "opg"


# -- sync -------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    updated: int = 0
    fills: int = 0
    breaks: dict[str, dict[str, float]] = field(default_factory=dict)
    recorded_session: str | None = None


def sync(paper: Paper, now: datetime) -> SyncReport:
    """Pull order updates and fills, reconcile with the broker, and record each sleeve's
    closing equity (once per session, after its bars are in the lake)."""
    report = SyncReport()
    store, broker = paper.store, paper.broker
    open_ = store.proposals(status=OPEN_AT_BROKER)
    if open_:
        since = min(datetime.fromisoformat(p.submitted_at or now_iso()) for p in open_)
        by_client = {o.client_order_id: o for o in broker.orders_since(since - timedelta(days=1))}
        for p in open_:
            order = by_client.get(p.id) or broker.order_by_client_id(p.id)
            if order is None:
                store.log("sync", f"{p.id} is not at the broker; left as {p.status}")
                continue
            report.fills += _record_fill(store, p, order, now)
            if _apply_order(store, p, order):
                report.updated += 1

    fills = store.fills()
    expected = book_positions(fills)
    actual = broker.positions()
    for symbol in sorted(set(expected) | set(actual)):
        mine, theirs = expected.get(symbol, 0.0), actual.get(symbol, 0.0)
        if abs(mine - theirs) > 1e-6:
            report.breaks[symbol] = {"ledger": mine, "broker": theirs}
    store.put(
        "reconciliation", {"at": now.isoformat(), "ok": not report.breaks, "breaks": report.breaks}
    )
    if report.breaks:
        store.log("reconciliation", f"positions differ from the broker: {report.breaks}")

    report.recorded_session = _record_close(paper, now, fills)
    return report


def _record_fill(store: PaperStore, p: Proposal, order: BrokerOrder, now: datetime) -> int:
    if order.filled_quantity <= p.filled_quantity + 1e-9 or order.filled_avg_price is None:
        return 0
    quantity = order.filled_quantity - p.filled_quantity
    before = p.filled_quantity * (p.avg_fill_price or 0.0)
    price = (order.filled_quantity * order.filled_avg_price - before) / quantity
    filled_at = order.filled_at or now.isoformat()
    session = datetime.fromisoformat(filled_at).astimezone(NEW_YORK).date()
    added = store.add_fill(
        FillRecord(
            id=f"{p.id}:{order.filled_quantity:g}",
            proposal_id=p.id,
            strategy=p.strategy,
            symbol=p.symbol,
            side=p.side,
            quantity=quantity,
            price=price,
            fees=0.0,  # Alpaca paper charges none; the backtest models them
            session=iso(session),
            filled_at=filled_at,
            reference_price=p.reference_price,
        )
    )
    return int(added)


def _apply_order(store: PaperStore, p: Proposal, order: BrokerOrder) -> bool:
    note = p.note
    if order.status in ("canceled", "failed"):
        note = f"broker: {order.broker_status}"
        if 0 < order.filled_quantity < order.quantity:
            note += f" after filling {order.filled_quantity:g} of {order.quantity:g}"
    if (
        order.status == p.status
        and order.filled_quantity == p.filled_quantity
        and order.filled_avg_price == p.avg_fill_price
    ):
        return False
    return store.update(
        p.id,
        status=order.status,
        filled_quantity=order.filled_quantity,
        avg_fill_price=order.filled_avg_price,
        broker_order_id=order.id,
        note=note,
    )


def _record_close(paper: Paper, now: datetime, fills: list[FillRecord]) -> str | None:
    session = last_completed_session(now)
    strategies = {s.name: s.capital for s in paper.book.sleeves}
    for f in fills:
        strategies.setdefault(f.strategy, _capital_of(paper, f.strategy))
    if not strategies:
        return None
    try:
        data = _load(paper, _held_symbols(fills) | _book_symbols(paper), session)
    except PaperError:
        return None
    if data.last_session != session:
        return None  # today's bars aren't in the lake yet; the evening run records it
    recorded = {d.strategy for d in paper.store.sleeve_days() if d.session == iso(session)}
    prices = data.closes(data.position_of(session))
    newly_recorded = False
    for name, capital in strategies.items():
        if name in recorded:
            continue
        mine = [f for f in fills if f.strategy == name and date.fromisoformat(f.session) <= session]
        portfolio = sleeve_portfolio(capital, mine)
        equity = portfolio.equity(prices)
        newly_recorded = True
        paper.store.record_sleeve_day(
            SleeveDay(
                name,
                iso(session),
                equity,
                portfolio.cash,
                {s: p.quantity for s, p in portfolio.positions.items()},
            )
        )
        paper.risk.end_of_day(session, name, equity)
    if newly_recorded and isinstance(paper.broker, FakePaperBroker):
        paper.broker.mark_close(paper.broker.account().equity)  # Alpaca tracks this itself
    return iso(session)


def _capital_of(paper: Paper, strategy: str) -> float:
    """Capital of a strategy that has left the book: its first recorded day's equity."""
    days = paper.store.sleeve_days(strategy)
    return days[0].equity if days else 0.0


# -- propose ----------------------------------------------------------------------------------


@dataclass
class ProposeReport:
    session: str
    proposed: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    expired: int = 0


def propose(paper: Paper, now: datetime, *, force: bool = False) -> ProposeReport:
    """Run every strategy in the book on the latest completed session and record its order
    proposals, each with the risk checks it passed or failed."""
    sync(paper, now)
    store = paper.store
    session = last_completed_session(now)
    report = ProposeReport(session=iso(session))
    for p in store.proposals(status=("pending", "approved")):
        if p.session < iso(session) and store.update(
            p.id,
            expect=("pending", "approved"),
            status="expired",
            note="not submitted before the next session",
        ):
            report.expired += 1

    fills = store.fills()
    data = _load(paper, _held_symbols(fills) | _book_symbols(paper), session)
    if data.last_session < session:
        raise PaperError(
            f"newest bars are from {data.last_session}, not {session}: run `tp-data bars daily`"
        )
    t = data.position_of(session)
    prices = data.closes(t)
    account = paper.broker.account()
    positions = dict(paper.broker.positions())
    cash = account.cash
    # Folded in as the run goes: proposals approved or awaiting approval count against later ones.
    for p in store.proposals(status=("pending", "approved"), session=iso(session)):
        signed = p.order_quantity if p.side == "buy" else -p.order_quantity
        positions[p.symbol] = positions.get(p.symbol, 0.0) + signed
        cash -= signed * p.reference_price

    for sleeve in paper.book.sleeves:
        name = sleeve.name
        earlier = store.proposals(strategy=name, session=iso(session))
        if earlier and not force:
            report.skipped[name] = "already proposed for this session"
            continue
        for p in earlier:
            if store.update(
                p.id,
                expect=("pending", "approved"),
                status="expired",
                note="replaced by a forced re-run",
            ):
                _unfold(p, positions)
                cash += (
                    p.order_quantity if p.side == "buy" else -p.order_quantity
                ) * p.reference_price
        if store.proposals(strategy=name, status=OPEN_AT_BROKER):
            report.skipped[name] = "orders still open at the broker"
            continue
        portfolio = sleeve_portfolio(sleeve.capital, [f for f in fills if f.strategy == name])
        ctx = PaperContext(sleeve.strategy, data, t, portfolio, paper.classifier.sectors)
        try:
            sleeve.strategy.on_bar(ctx)
        except Exception as exc:  # one broken strategy must not stop the others
            log.exception("%s failed", name)
            store.log("propose", f"{name} raised {type(exc).__name__}: {exc}")
            report.skipped[name] = f"error: {exc}"
            continue
        intents = ctx.drain()
        book = Book(
            session=session,
            equity=account.equity,
            cash=cash,
            positions=dict(positions),
            prices=prices,
            previous_equity=account.last_equity or account.equity,
            average_volume=data.average_volume(t),
            last_bar=data.adjusted.last_bar(t),
        )
        decisions = paper.risk.review(intents, book)
        rows = [
            _proposal(d, sleeve.approval, seq)
            for seq, d in enumerate(decisions, start=len(earlier) + 1)
        ]
        store.add_proposals(rows)
        for d in decisions:
            if d.approved:
                signed = d.intent.signed_quantity
                positions[d.intent.symbol] = positions.get(d.intent.symbol, 0.0) + signed
                cash -= signed * d.intent.reference_price
        report.proposed[name] = sum(r.status != "blocked" for r in rows)
        report.blocked[name] = sum(r.status == "blocked" for r in rows)
    store.log("propose", _propose_summary(report))
    return report


def _propose_summary(report: ProposeReport) -> str:
    """One readable line: only the strategies that did something."""
    parts = []
    for name in sorted(set(report.proposed) | set(report.skipped)):
        if name in report.skipped:
            parts.append(f"{name} skipped ({report.skipped[name]})")
            continue
        made, blocked = report.proposed[name], report.blocked[name]
        if made or blocked:
            parts.append(
                f"{name} {made} order(s)" + (f", {blocked} blocked by risk" if blocked else "")
            )
    return f"{report.session} close: " + ("; ".join(parts) if parts else "no trades proposed")


def _unfold(p: Proposal, positions: dict[str, float]) -> None:
    signed = p.order_quantity if p.side == "buy" else -p.order_quantity
    positions[p.symbol] = positions.get(p.symbol, 0.0) - signed


def _proposal(decision: RiskDecision, approval: str, seq: int) -> Proposal:
    i: OrderIntent = decision.intent
    created = now_iso()
    status = (
        "blocked" if not decision.approved else ("approved" if approval == "auto" else "pending")
    )
    return Proposal(
        id=i.order_id(seq),
        strategy=i.strategy,
        session=iso(i.session),
        symbol=i.symbol,
        side=str(i.side),
        quantity=i.quantity,
        order_type=str(i.order_type),
        reference_price=i.reference_price,
        reason=i.reason,
        checks=[
            {"check": c.check, "passed": c.passed, "detail": c.detail} for c in decision.checks
        ],
        status=status,
        created_at=created,
        updated_at=created,
        limit_price=i.limit_price,
        stop_price=i.stop_price,
        target_weight=i.target_weight,
        note="; ".join(decision.reasons),
        decided_at=created if status == "approved" else None,
        decided_by="auto" if status == "approved" else None,
    )


# -- decide -----------------------------------------------------------------------------------


def decide(
    store: PaperStore,
    proposal_id: str,
    *,
    approve: bool,
    by: str = "you",
    quantity: float | None = None,
    note: str = "",
) -> Proposal:
    """Approve (optionally for fewer shares) or reject a pending proposal.

    Rejecting also works on an approved proposal that hasn't been sent yet: the veto on
    automatic trading."""
    p = store.proposal(proposal_id)
    allowed = ("pending",) if approve else ("pending", "approved")
    if p.status not in allowed:
        raise PaperError(f"{proposal_id} is {p.status}, not {' or '.join(allowed)}")
    values: dict[str, Any] = {"decided_at": now_iso(), "decided_by": by, "note": note}
    if approve:
        if quantity is not None:
            if quantity != math.floor(quantity) or not 0 < quantity <= p.quantity:
                raise PaperError(
                    f"approve between 1 and {p.quantity:g} whole shares; more than proposed would "
                    "skip the risk checks"
                )
            values["approved_quantity"] = float(quantity)
        values["status"] = "approved"
    else:
        values["status"] = "rejected"
    if not store.update(proposal_id, expect=allowed, **values):
        raise PaperError(f"{proposal_id} changed while deciding; look again")
    return store.proposal(proposal_id)


# -- submit -----------------------------------------------------------------------------------


@dataclass
class SubmitReport:
    submitted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    expired: list[str] = field(default_factory=list)


def submit(paper: Paper, now: datetime) -> SubmitReport:
    """Send approved proposals from the latest session to the broker (sells first); proposals
    still waiting for a decision expire."""
    risk_state = paper.risk.state
    kill = risk_state.kill_switch()
    if kill is not None:
        paper.store.log("submit", f"refused: kill switch engaged ({kill.reason})")
        raise PaperError(f"kill switch engaged ({kill.reason}); nothing submitted")
    local = now.astimezone(NEW_YORK)
    if (
        paper.time_in_force == "opg"
        and is_session(local.date())
        and time(9, 28) <= local.time() < time(19, 0)
    ):
        raise PaperError(
            "market-on-open orders are only accepted from 7:00pm to 9:28am ET; submit before "
            "9:28, or use --tif day to trade during market hours"
        )
    store = paper.store
    session = iso(last_completed_session(now))
    report = SubmitReport()
    for p in store.proposals(status=("pending", "approved")):
        stale = p.session < session
        if p.status == "pending" or stale:
            why = (
                "not submitted before the next session"
                if stale
                else "not approved before submission"
            )
            if store.update(p.id, expect=("pending", "approved"), status="expired", note=why):
                report.expired.append(p.id)
    approved = store.proposals(status="approved", session=session)
    for p in sorted(approved, key=lambda p: p.side == "buy"):
        disabled = risk_state.disabled(p.strategy)
        if disabled is not None and p.side == "buy":
            store.update(
                p.id,
                expect=("approved",),
                status="expired",
                note=f"strategy disabled before submission: {disabled.reason}",
            )
            report.expired.append(p.id)
            continue
        request = OrderRequest(
            client_order_id=p.id,
            symbol=p.symbol,
            side="buy" if p.side == "buy" else "sell",
            quantity=p.order_quantity,
            time_in_force=paper.time_in_force,
            limit_price=p.limit_price,
        )
        try:
            order = paper.broker.submit(request)
        except OrderRejectedError as exc:
            existing = paper.broker.order_by_client_id(p.id)  # resubmitting after a crash
            if existing is None:
                store.update(p.id, expect=("approved",), status="failed", note=f"broker: {exc}")
                report.failed[p.id] = str(exc)
                continue
            order = existing
        store.update(
            p.id,
            expect=("approved",),
            status=order.status,
            broker_order_id=order.id,
            submitted_at=order.submitted_at or now.isoformat(),
        )
        report.submitted.append(p.id)
    summary = f"{len(report.submitted)} order(s) sent"
    if report.failed:
        summary += f", {len(report.failed)} refused by the broker"
    if report.expired:
        summary += f", {len(report.expired)} expired"
    store.log("submit", summary)
    return report


# -- kill -------------------------------------------------------------------------------------


def kill(paper: Paper, reason: str, now: datetime) -> int:
    """Engage the kill switch, cancel every open broker order, and expire unsent proposals."""
    paper.risk.state.set_kill_switch(reason)  # first: blocks new orders even if the rest fails
    for p in paper.store.proposals(status=("pending", "approved")):
        paper.store.update(
            p.id, expect=("pending", "approved"), status="expired", note=f"kill switch: {reason}"
        )
    try:
        canceled = paper.broker.cancel_all()
    except Exception as exc:
        paper.store.log("kill", f"kill switch engaged ({reason}); canceling orders FAILED: {exc}")
        raise PaperError(
            f"kill switch engaged, but canceling open orders failed: {exc}; cancel them in "
            "Alpaca's dashboard"
        ) from exc
    paper.store.log("kill", f"kill switch engaged ({reason}); {canceled} open order(s) canceled")
    sync(paper, now)
    return canceled


# -- status -----------------------------------------------------------------------------------


def status(paper: Paper, now: datetime) -> dict[str, Any]:
    """Account, switches, reconciliation, and each sleeve's progress toward the paper gate."""
    store = paper.store
    account: BrokerAccount | None
    try:
        account, broker_error = paper.broker.account(), None
    except Exception as exc:  # status must still work when the broker doesn't
        account, broker_error = None, f"{type(exc).__name__}: {exc}"
    fills = store.fills()
    days = store.sleeve_days()
    try:
        data = _load(
            paper, _held_symbols(fills) | _book_symbols(paper), last_completed_session(now)
        )
    except PaperError:
        data = None
    prices = data.closes(len(data.adjusted) - 1) if data else {}
    sleeves = []
    names = [s.name for s in paper.book.sleeves] + sorted(
        {f.strategy for f in fills} - {s.name for s in paper.book.sleeves}
    )
    for name in names:
        approval: str
        try:
            sleeve = paper.book.sleeve(name)
            capital, approval = sleeve.capital, sleeve.approval
        except KeyError:
            capital, approval = _capital_of(paper, name), "removed"
        mine = [f for f in fills if f.strategy == name]
        history = [d for d in days if d.strategy == name]
        portfolio = sleeve_portfolio(capital, mine)
        equity = portfolio.equity(prices)
        curve = pd.Series(
            [d.equity for d in history],
            index=pd.DatetimeIndex([d.session for d in history]),
            dtype=float,
        )
        returns = curve.pct_change().dropna()
        disabled = paper.risk.state.disabled(name)
        sleeves.append(
            {
                "strategy": name,
                "approval": approval,
                "capital": capital,
                "equity": equity,
                "return": equity / capital - 1 if capital else None,
                "max_drawdown": metrics.max_drawdown(returns) if len(returns) else 0.0,
                "sharpe": metrics.sharpe_ratio(returns) if len(returns) >= 20 else None,
                "positions": {s: p.quantity for s, p in portfolio.positions.items()},
                "trading_days": len(history),
                "trades": len(mine),
                "slippage_bps": _slippage_bps(mine, data),
                "modeled_slippage_bps": CostModel().slippage_bps,
                "gate": {"days": [len(history), GATE_DAYS], "trades": [len(mine), GATE_TRADES]},
                "disabled": disabled.reason if disabled else None,
                "pending": len(store.proposals(strategy=name, status="pending")),
                "since": history[0].session if history else None,
            }
        )
    kill_switch = paper.risk.state.kill_switch()
    return {
        "as_of": now.isoformat(),
        "broker": paper.broker.name,
        "broker_error": broker_error,
        "account": {
            "number": _mask(account.account_number),
            "status": account.status,
            "equity": account.equity,
            "last_equity": account.last_equity,
            "cash": account.cash,
        }
        if account
        else None,
        "book_capital": paper.book.capital,
        "kill_switch": {"reason": kill_switch.reason, "at": kill_switch.at}
        if kill_switch
        else None,
        "reconciliation": store.get("reconciliation"),
        "sleeves": sleeves,
        "pending": len(store.proposals(status="pending")),
        "bot": bot_status(store, now),
    }


def bot_status(store: PaperStore, now: datetime) -> dict[str, Any] | None:
    """The bot's last heartbeat, next task and last result; ``alive`` if it beat recently."""
    status: dict[str, Any] | None = store.get("bot:status")
    if not status:
        return None
    beat = status.get("heartbeat")
    alive = (
        status.get("state") == "running"
        and beat is not None
        and now - datetime.fromisoformat(beat) < BOT_HEARTBEAT_STALE
    )
    return status | {"alive": alive}


def _slippage_bps(fills: Sequence[FillRecord], data: PaperData | None) -> float | None:
    """Average fill price vs the session's official open, in bps against us."""
    if data is None:
        return None
    costs = []
    for f in fills:
        open_ = data.open_on(f.symbol, date.fromisoformat(f.session))
        if open_:
            sign = 1 if f.side == "buy" else -1
            costs.append(sign * (f.price / open_ - 1) * 10_000)
    return sum(costs) / len(costs) if costs else None


def _mask(number: str) -> str:
    return f"…{number[-4:]}" if len(number) > 4 else number


# -- data ---------------------------------------------------------------------------------------


def _book_symbols(paper: Paper) -> set[str]:
    return {sym for s in paper.book.sleeves for sym in s.strategy.symbols()}


def _held_symbols(fills: Sequence[FillRecord]) -> set[str]:
    return set(book_positions(fills))


def _load(paper: Paper, symbols: set[str], session: date) -> PaperData:
    try:
        bars = load_bars(paper.lake, sorted(symbols), end=session)
    except KeyError as exc:
        raise PaperError(str(exc.args[0])) from exc
    if bars.empty:
        raise PaperError("no bars in the lake; run `tp-data bars backfill`")
    return PaperData(bars)
