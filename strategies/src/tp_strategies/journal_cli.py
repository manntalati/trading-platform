"""``tp-journal``: write down what you did with the ideas, after checking the gates.

    tp-journal check SYMBOL         the gates on taking SYMBOL from the latest run, nothing written
    tp-journal take SYMBOL          check the gates and record that you took it
    tp-journal pass SYMBOL          record that you passed on it (and why, with --note)
    tp-journal close ID PRICE       record the exit: the P&L in dollars and in R
    tp-journal list                 the entries, newest first
    tp-journal review               taken against passed, and how the gates did
    tp-journal trade sell XYZ 40 52.10   any other trade (selling something you held)
    tp-journal pending              recorded trades the brokerage sync doesn't show yet

Nothing here places an order: you trade at your broker, then write it down. The journal is a
JSON file under the data root (TP_JOURNAL_FILE), kept out of git.

``take``, ``close`` and ``trade`` also record the fill (``tp_core.fills``), which the ideas count
at once (proceeds in the buying power, the holding gone or added) until a sync shows it; add
``--no-fill`` when the sync already does (writing down a trade from an earlier day).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

import typer

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings
from tp_core.fills import (
    assess,
    describe,
    instrument_symbol,
    load_fills,
    multiplier,
    new_fill,
    save_fills,
    with_fills,
)
from tp_core.portfolio import Classifier, account_label, latest_snapshot
from tp_core.storage import Lake
from tp_risk.state import FileRiskState
from tp_strategies import journal as j
from tp_strategies.budget import fill_account
from tp_strategies.recommendations import latest_run

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Write down what you took or passed on from the ideas, after checking the gates.",
)

Symbol = Annotated[str, typer.Argument(help="The idea's ticker, from `tp-recs show`.")]
ListOpt = Annotated[
    str | None, typer.Option("--list", help="Which list, if the ticker is on more than one.")
]
Units = Annotated[
    float | None,
    typer.Option(help="Contracts (or shares) you took. Default: the size the idea suggests."),
]
Price = Annotated[
    float | None, typer.Option(help="Your fill, per share. Default: the idea's entry limit.")
]
Note = Annotated[str | None, typer.Option(help="Why, in a line; shown in the review.")]
Fill = Annotated[
    bool,
    typer.Option(
        help="Count the trade until the brokerage sync shows it (--no-fill: it already does)."
    ),
]
Account = Annotated[
    str | None,
    typer.Option(help="The account (its name or last 4 digits). Default: the ideas' account."),
]
MARK = {"ok": "✓", "warn": "!", "block": "✗"}


def _now() -> datetime:
    return datetime.now(UTC)


def _context(
    symbol: str, idea_list: str | None, units: float | None, price: float | None
) -> tuple[Settings, dict[str, Any], dict[str, Any], j.Sizing | None, list[dict[str, Any]]]:
    settings = Settings()
    run = latest_run(Lake(settings.data_root))
    if run is None:
        typer.echo("no recommendations yet: run `tp-recs run`")
        raise typer.Exit(1)
    try:
        idea = j.find_idea(run, symbol, idea_list)
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    pick = idea.get("pick")
    sizing = j.Sizing.of(pick, units, price) if pick else None
    return settings, run, idea, sizing, j.load(j.journal_path(settings))


def _record_fill(
    settings: Settings,
    legs: list[dict[str, Any]],
    cash: float,
    *,
    symbol: str,
    account: str | None = None,
    note: str | None = None,
    journal_id: int | None = None,
) -> None:
    """Write the fill down so the ideas count it until a sync shows it."""
    lake = Lake(settings.data_root)
    raw = latest_snapshot(lake)
    if raw.empty:
        typer.echo("no brokerage sync yet, so there's no account to count it in (tp-broker sync)")
        return
    snap, _ = with_fills(lake, raw)
    classifier = Classifier.load(settings.classifications_file)
    row = fill_account(snap, account=account, symbol=symbol, default=settings.options_account,
                       classifier=classifier)  # fmt: skip
    if row is None:
        typer.echo(f"no account matches {account!r}: the fill isn't counted")
        raise typer.Exit(1)
    fills = load_fills(lake)
    f = new_fill(fills, raw, source=str(row["source"]), account_id=str(row["account_id"]),
                 legs=legs, cash=cash, at=_now(), note=note, journal_id=journal_id)  # fmt: skip
    save_fills(lake, [*fills, f])
    money = f"${abs(cash):,.2f} {'received' if cash >= 0 else 'paid'}"
    typer.echo(
        f"fill #{f['id']}: {money} in {account_label(row.to_dict())}, counted by the ideas "
        "until a sync shows it (`tp-journal pending`)"
    )


def _checks(
    settings: Settings,
    run: dict[str, Any],
    idea: dict[str, Any],
    sizing: j.Sizing | None,
    entries: list[dict[str, Any]],
) -> list[j.Check]:
    checks = j.gates(
        idea,
        run,
        sizing=sizing,
        entries=entries,
        rules=j.Rules.from_settings(settings),
        today=_now().astimezone(NEW_YORK).date(),
        kill=FileRiskState.under(settings.data_root).kill_switch(),
    )
    what = f"{idea['symbol']} ({idea.get('list') or idea.get('kind')})"
    if sizing is not None:
        what += f": {sizing.amount}" + (
            f" at {sizing.entry:g} {sizing.side}" if sizing.entry is not None else ""
        )
    typer.echo(what)
    for c in checks:
        typer.echo(f"  {MARK[c.verdict]} {c.gate:<14} {c.reason}")
    return checks


@app.command()
def check(
    symbol: Symbol, idea_list: ListOpt = None, units: Units = None, price: Price = None
) -> None:
    """The gates on taking SYMBOL from the latest run; nothing is written. Exits 1 if any
    blocks."""
    settings, run, idea, sizing, entries = _context(symbol, idea_list, units, price)
    if j.blocked(_checks(settings, run, idea, sizing, entries)):
        raise typer.Exit(1)


@app.command()
def take(
    symbol: Symbol,
    idea_list: ListOpt = None,
    units: Units = None,
    price: Price = None,
    note: Note = None,
    override: Annotated[
        str | None,
        typer.Option(help="Take it although a gate blocks; the reason is kept with the trade."),
    ] = None,
    fill: Fill = True,
    account: Account = None,
) -> None:
    """Check the gates and record that you took SYMBOL (at your broker: nothing is ordered)."""
    settings, run, idea, sizing, entries = _context(symbol, idea_list, units, price)
    checks = _checks(settings, run, idea, sizing, entries)
    if sizing is None:
        typer.echo("nothing to take: `tp-journal pass` records a decision on a watch-list name")
        raise typer.Exit(1)
    stops = j.blocked(checks)
    if stops and not override:
        typer.echo(f"not recorded: {', '.join(c.gate for c in stops)} blocks it (--override WHY)")
        raise typer.Exit(1)
    e = j.entry("took", idea, run, at=_now(), entries=entries, sizing=sizing, checks=checks,
                note=note, override=override if stops else None)  # fmt: skip
    j.save(j.journal_path(settings), [*entries, e])
    typer.echo(f"#{e['id']} took {e['symbol']}: `tp-journal close {e['id']} PRICE` when you exit")
    made = j.fill_legs(e, closing=False) if fill else None
    if made is not None:
        _record_fill(settings, made[0], made[1], symbol=str(e["symbol"]), account=account,
                     journal_id=int(e["id"]))  # fmt: skip


@app.command("pass")
def pass_(symbol: Symbol, idea_list: ListOpt = None, note: Note = None) -> None:
    """Record that you passed on SYMBOL, with the gates as they stood."""
    settings, run, idea, sizing, entries = _context(symbol, idea_list, None, None)
    checks = _checks(settings, run, idea, sizing, entries)
    e = j.entry("passed", idea, run, at=_now(), entries=entries, sizing=sizing, checks=checks,
                note=note)  # fmt: skip
    j.save(j.journal_path(settings), [*entries, e])
    typer.echo(f"#{e['id']} passed on {e['symbol']}")


@app.command()
def close(
    entry_id: Annotated[int, typer.Argument(help="The entry's number, from `tp-journal list`.")],
    price: Annotated[float, typer.Argument(help="Your exit, per share like the entry.")],
    note: Note = None,
    fill: Fill = True,
    account: Account = None,
) -> None:
    """Record the exit of a trade you took: the P&L in dollars and in R (the multiple of the most
    it could lose)."""
    settings = Settings()
    path = j.journal_path(settings)
    entries = j.load(path)
    found = next((e for e in entries if int(e["id"]) == entry_id), None)
    if found is None:
        typer.echo(f"no entry #{entry_id}")
        raise typer.Exit(1)
    try:
        j.close(found, price, at=_now(), note=note)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    j.save(path, entries)
    done = found["closed"]
    pnl = f"${done['pnl']:+,.2f}" if done["pnl"] is not None else "P&L unknown (no entry price)"
    r = f", {done['r']:+.2f}R" if done["r"] is not None else ""
    typer.echo(f"#{entry_id} {found['symbol']} closed at {price:g}: {pnl}{r}")
    if not fill:
        return
    made = j.fill_legs(found, closing=True, price=price)
    if made is None:
        typer.echo(
            "taken before fills were kept: `tp-journal trade sell ...` counts the sale until "
            "the sync shows it"
        )
        return
    first = str(made[0][0]["symbol"]) if made[0] else str(found["symbol"])
    _record_fill(settings, made[0], made[1], symbol=first, account=account,
                 journal_id=entry_id)  # fmt: skip


@app.command("list")
def list_(limit: Annotated[int, typer.Option(help="How many, newest first.")] = 20) -> None:
    """The entries, newest first."""
    entries = j.load(j.journal_path(Settings()))
    if not entries:
        typer.echo("the journal is empty: `tp-journal take` or `tp-journal pass` an idea")
        return
    today = _now().astimezone(NEW_YORK).date().isoformat()
    for e in reversed(entries[-limit:]):
        at = datetime.fromisoformat(e["at"]).astimezone(NEW_YORK)
        line = f"#{e['id']:<3} {at:%Y-%m-%d} {e['decision']:<6} {e['symbol']:<6} {e['structure']}"
        if e["decision"] == "took":
            line += f" {e['units']:g} {e['unit']}"
            done = e.get("closed")
            if done and done.get("pnl") is not None:
                line += f"  closed ${done['pnl']:+,.0f}"
            elif not done:
                plan = e.get("plan") or {}
                out_by = plan.get("out_by") or plan.get("sell_by")
                line += "  open" + (f", out by {out_by}" if out_by else "")
                if out_by and str(out_by) < today:
                    line += " (past its exit date)"
        if e.get("override"):
            line += f"  (override: {e['override']})"
        if e.get("note"):
            line += f"  · {e['note']}"
        typer.echo(line)


def _stats_line(name: str, s: dict[str, Any]) -> str:
    if not s["closed"]:
        return f"  {name:<20} none closed"
    win = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "—"
    pnl = f"${s['pnl']:+,.0f}" if s["pnl"] is not None else "—"
    r = f"{s['mean_r']:+.2f}R" if s["mean_r"] is not None else "—"
    return f"  {name:<20} {s['closed']:>3} closed · won {win} · {pnl} · mean {r}"


@app.command()
def review() -> None:
    """Taken against passed, how the closed trades did, and whether overriding a gate paid."""
    r = j.review(j.load(j.journal_path(Settings())))
    typer.echo(f"took {r['took']} ({r['open']} open), passed on {r['passed']}")
    for name in ("all", "followed the gates", "overrode a gate"):
        typer.echo(_stats_line(name, r[name]))
    if r["by_setup"]:
        typer.echo("by setup:")
        for setup, s in r["by_setup"].items():
            typer.echo(_stats_line(setup, s))


def main() -> None:
    app()


def said(quantity: float, symbol: str) -> str:
    """ "sold 2 contracts of XYZ Dec 18 '26 $25 Call", "bought 40 shares of XYZ"."""
    what = describe(symbol)
    name = what["label"].removesuffix(" shares")
    return f"{'bought' if quantity > 0 else 'sold'} {abs(quantity):g} {what['unit']} of {name}"


class Side(StrEnum):
    buy = "buy"
    sell = "sell"


@app.command()
def trade(
    side: Annotated[Side, typer.Argument(help="buy or sell.")],
    symbol: Annotated[str, typer.Argument(help="The ticker (an option's too), or its OCC symbol.")],
    quantity: Annotated[float, typer.Argument(help="Shares, or contracts for an option.")],
    price: Annotated[float, typer.Argument(help="Your fill, per share (an option's: 1.45).")],
    call: Annotated[
        bool, typer.Option("--call", help="A call: give --expiration and --strike.")
    ] = False,
    put: Annotated[
        bool, typer.Option("--put", help="A put: give --expiration and --strike.")
    ] = False,
    expiration: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="The option's expiration.")
    ] = None,
    strike: Annotated[float | None, typer.Option(help="The option's strike.")] = None,
    account: Account = None,
    note: Note = None,
) -> None:
    """Record any other trade you made at your broker (selling something you held, say), so the
    ideas count it at once: a sale's proceeds in the buying power and the holding gone, a
    purchase the other way round, until a brokerage sync shows it. Shares by default; --call or
    --put with --expiration and --strike for an option. Nothing is ordered."""
    if quantity <= 0 or price < 0:
        raise typer.BadParameter("the quantity is more than 0 and the price isn't negative")
    if call and put:
        raise typer.BadParameter("a call or a put, not both")
    kind = "call" if call else "put" if put else "stock"
    try:
        name = instrument_symbol(symbol, kind, expiration.date() if expiration else None, strike)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = Settings()
    signed = quantity if side == Side.buy else -quantity
    typer.echo(said(signed, name))
    cash = -signed * price * multiplier(name)
    _record_fill(settings, [{"symbol": name, "quantity": signed, "price": price}], cash,
                 symbol=name, account=account, note=note)  # fmt: skip


@app.command()
def pending(
    drop: Annotated[
        int | None, typer.Option(help="Remove fill ID (entered wrong, or in the wrong account).")
    ] = None,
) -> None:
    """Recorded trades and whether the brokerage sync shows them yet: pending (counted in full),
    settling (the sync shows the holding; its money still counted until the buying power moves
    or it settles), synced, or stale (not seen after a week: no longer counted, check it)."""
    settings = Settings()
    lake = Lake(settings.data_root)
    fills = load_fills(lake)
    if drop is not None:
        kept = [f for f in fills if int(f["id"]) != drop]
        if len(kept) == len(fills):
            typer.echo(f"no fill #{drop}")
            raise typer.Exit(1)
        save_fills(lake, kept)
        typer.echo(f"fill #{drop} removed")
        return
    if not fills:
        typer.echo("no recorded trades: `tp-journal trade sell XYZ 40 52.10` after you trade")
        return
    snap = latest_snapshot(lake)
    states = assess(fills, snap, _now()) if not snap.empty else []
    for st in reversed(states):
        f = st.fill
        at = datetime.fromisoformat(str(f["at"])).astimezone(NEW_YORK)
        legs = ", ".join(said(float(x["quantity"]), str(x["symbol"])) for x in f["legs"])
        money = f"${float(f['cash']):+,.2f}"
        counted = " (counted)" if st.counts_cash else ""
        typer.echo(f"#{f['id']:<3} {at:%Y-%m-%d %H:%M} {st.status:<8} {legs}  {money}{counted}")
