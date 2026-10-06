"""``tp-paper``: run the paper-trading cycle on Alpaca's paper account.

    tp-paper status                       sleeves, gate progress, account, switches
    tp-paper propose                      after the close (and after `tp-data bars daily`)
    tp-paper proposals                    what is waiting for you
    tp-paper approve ID... | --all        approve (optionally --quantity fewer shares)
    tp-paper reject ID... --note "..."
    tp-paper submit                       before 9:28am ET: market-on-open orders
    tp-paper sync                         after the open: fills and reconciliation
    tp-paper kill --reason "..."          block new orders and cancel open ones

``--broker fake`` simulates the account from the lake's bars (no keys needed).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Annotated, Any

import pandas as pd
import typer

from tp_core.bars import load_bars
from tp_core.config import MissingCredentialsError, Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_paper import jobs
from tp_paper.broker import AlpacaPaperBroker, FakePaperBroker, PaperBroker, PriceSource
from tp_paper.config import PaperBook
from tp_paper.jobs import Paper, PaperError
from tp_paper.store import PaperStore
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import FileRiskState

app = typer.Typer(add_completion=False, no_args_is_help=True)
STATE: dict[str, Any] = {"broker": "alpaca"}


@app.callback()
def main_options(
    broker: Annotated[
        str,
        typer.Option(envvar="TP_PAPER_BROKER", help="alpaca (paper account) or fake (simulated)."),
    ] = "alpaca",
) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if broker not in ("alpaca", "fake"):
        raise typer.BadParameter("--broker must be alpaca or fake")
    STATE["broker"] = broker


def _now() -> datetime:
    return datetime.now(UTC)


def open_paper(settings: Settings, broker_kind: str, *, time_in_force: str = "opg") -> Paper:
    lake = Lake(settings.data_root)
    store = PaperStore.under(settings.data_root)
    broker: PaperBroker
    if broker_kind == "fake":
        broker = FakePaperBroker(
            settings.data_root / "state" / "fake_broker.json", _lake_prices(lake)
        )
    else:
        key, secret = settings.require_alpaca_keys()
        broker = AlpacaPaperBroker(key, secret)
    classifier = Classifier.load(settings.classifications_file)
    risk = RiskManager(
        Limits.load(settings.risk_file),
        classifier,
        FileRiskState.under(settings.data_root),
        enforce_drawdown=True,
    )
    return Paper(
        store=store,
        broker=broker,
        book=PaperBook.load(settings.paper_file),
        lake=lake,
        risk=risk,
        classifier=classifier,
        time_in_force="day" if time_in_force == "day" else "opg",
    )


def _lake_prices(lake: Lake) -> PriceSource:
    """Raw open and close per (symbol, session) from the lake, for the simulated broker."""
    cache: dict[str, dict[date, tuple[float, float]]] = {}

    def prices(symbol: str, day: date) -> tuple[float, float] | None:
        if symbol not in cache:
            try:
                bars = load_bars(lake, [symbol])
            except KeyError:
                bars = pd.DataFrame(columns=["session", "open", "close"])
            cache[symbol] = {
                s: (float(o), float(c))
                for s, o, c in zip(bars["session"], bars["open"], bars["close"], strict=True)
            }
        return cache[symbol].get(day)

    return prices


def _paper(time_in_force: str = "opg") -> Paper:
    try:
        return open_paper(Settings(), STATE["broker"], time_in_force=time_in_force)
    except MissingCredentialsError as exc:
        typer.echo(f"{exc}\n(or try --broker fake)", err=True)
        raise typer.Exit(2) from exc
    except (OSError, ValueError, KeyError) as exc:
        typer.echo(f"configuration problem: {exc}", err=True)
        raise typer.Exit(2) from exc


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(1)


@app.command()
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Sleeves, progress toward the paper gate, the account and the switches."""
    report = jobs.status(_paper(), _now())
    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return
    a = report["account"]
    typer.echo(
        f"{report['broker']} {a['number']} {a['status']}: equity {a['equity']:,.2f}, "
        f"cash {a['cash']:,.2f}; book capital {report['book_capital']:,.0f}"
    )
    kill = report["kill_switch"]
    typer.echo(f"kill switch: {'ENGAGED ' + kill['reason'] if kill else 'off'}")
    rec = report["reconciliation"]
    if rec:
        typer.echo(
            f"reconciliation: {'ok' if rec['ok'] else 'BREAKS ' + json.dumps(rec['breaks'])}"
        )
    typer.echo(
        f"\n{'strategy':16}{'equity':>12}{'return':>9}{'max dd':>9}{'days':>7}{'trades':>8}"
        f"{'slip bps':>10}  status"
    )
    for s in report["sleeves"]:
        slip = "—" if s["slippage_bps"] is None else f"{s['slippage_bps']:.1f}"
        state = f"DISABLED: {s['disabled']}" if s["disabled"] else s["approval"]
        if s["pending"]:
            state += f", {s['pending']} pending"
        typer.echo(
            f"{s['strategy']:16}{s['equity']:>12,.2f}{s['return']:>9.2%}{s['max_drawdown']:>9.2%}"
            f"{s['trading_days']:>4}/{jobs.GATE_DAYS}{s['trades']:>5}/{jobs.GATE_TRADES}"
            f"{slip:>10}  {state}"
        )


@app.command()
def propose(
    force: Annotated[bool, typer.Option(help="Re-run a session already proposed.")] = False,
) -> None:
    """Run every strategy on the latest completed session and record its proposals."""
    paper = _paper()
    try:
        report = jobs.propose(paper, _now(), force=force)
    except PaperError as exc:
        raise _fail(exc) from exc
    typer.echo(f"session {report.session}")
    for name in sorted(set(report.proposed) | set(report.skipped)):
        if name in report.skipped:
            typer.echo(f"  {name:16} skipped: {report.skipped[name]}")
        else:
            proposed, blocked = report.proposed[name], report.blocked[name]
            typer.echo(f"  {name:16} {proposed} proposed, {blocked} blocked by risk")
    if report.expired:
        typer.echo(f"  {report.expired} older proposal(s) expired")
    _print_proposals(
        paper.store.proposals(status=("pending", "approved", "blocked"), session=report.session)
    )


@app.command()
def proposals(
    show_all: Annotated[
        bool, typer.Option("--all", help="Every status, not just pending.")
    ] = False,
    strategy: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 50,
) -> None:
    """List proposals (pending ones by default)."""
    store = _paper().store
    rows = store.proposals(status=None if show_all else "pending", strategy=strategy, limit=limit)
    _print_proposals(rows)


def _print_proposals(rows: list[Any]) -> None:
    if not rows:
        typer.echo("no proposals")
        return
    for p in rows:
        qty = f"{p.order_quantity:g}" + (
            f" (of {p.quantity:g})" if p.approved_quantity is not None else ""
        )
        typer.echo(
            f"{p.status:17} {p.id}\n{'':18}{p.side} {qty} {p.symbol} ~{p.notional:,.0f}: {p.reason}"
        )
        if p.status == "blocked" or p.note:
            typer.echo(f"{'':18}{p.note}")


@app.command()
def approve(
    ids: Annotated[list[str] | None, typer.Argument()] = None,
    approve_all: Annotated[bool, typer.Option("--all", help="Every pending proposal.")] = False,
    strategy: Annotated[str | None, typer.Option(help="With --all: only this strategy.")] = None,
    quantity: Annotated[float | None, typer.Option(help="Approve fewer shares (one id).")] = None,
    note: Annotated[str, typer.Option()] = "",
) -> None:
    """Approve pending proposals for the next submission."""
    store = _paper().store
    targets = list(ids or [])
    if approve_all:
        targets += [p.id for p in store.proposals(status="pending", strategy=strategy)]
    if not targets:
        typer.echo("nothing to approve (give ids or --all)")
        raise typer.Exit(1)
    if quantity is not None and len(targets) != 1:
        raise typer.BadParameter("--quantity applies to exactly one proposal")
    for pid in targets:
        try:
            p = jobs.decide(store, pid, approve=True, quantity=quantity, note=note)
        except (PaperError, KeyError) as exc:
            raise _fail(exc) from exc
        typer.echo(f"approved {p.id}: {p.side} {p.order_quantity:g} {p.symbol}")


@app.command()
def reject(
    ids: Annotated[list[str], typer.Argument()],
    note: Annotated[str, typer.Option(help="Why; kept with the decision.")] = "",
) -> None:
    """Reject pending proposals."""
    store = _paper().store
    for pid in ids:
        try:
            p = jobs.decide(store, pid, approve=False, note=note)
        except (PaperError, KeyError) as exc:
            raise _fail(exc) from exc
        typer.echo(f"rejected {p.id}")


@app.command()
def submit(
    tif: Annotated[
        str, typer.Option(help="opg (market on open, before 9:28am ET) or day.")
    ] = "opg",
) -> None:
    """Send approved proposals to the paper account; unapproved ones expire."""
    if tif not in ("opg", "day"):
        raise typer.BadParameter("--tif must be opg or day")
    try:
        report = jobs.submit(_paper(tif), _now())
    except PaperError as exc:
        raise _fail(exc) from exc
    typer.echo(
        f"submitted {len(report.submitted)}, failed {len(report.failed)}, "
        f"expired {len(report.expired)}"
    )
    for pid, why in report.failed.items():
        typer.echo(f"  failed {pid}: {why}")


@app.command()
def sync() -> None:
    """Record fills and order updates, reconcile positions, mark sleeves at the close."""
    report = jobs.sync(_paper(), _now())
    typer.echo(f"{report.updated} order update(s), {report.fills} new fill(s)")
    if report.breaks:
        typer.echo(f"RECONCILIATION BREAKS: {json.dumps(report.breaks)}")
    if report.recorded_session:
        typer.echo(f"sleeves marked at the {report.recorded_session} close")


@app.command()
def kill(reason: Annotated[str, typer.Option(help="Why; shown on every refused order.")]) -> None:
    """Engage the kill switch: no new orders, open broker orders canceled."""
    canceled = jobs.kill(_paper(), reason, _now())
    typer.echo(
        f"kill switch ENGAGED; {canceled} open order(s) canceled. `tp-risk resume` lifts it."
    )


def main() -> None:
    app()
