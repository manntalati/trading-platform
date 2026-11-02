"""``tp-journal``: write down what you did with the ideas, after checking the gates.

    tp-journal check SYMBOL         the gates on taking SYMBOL from the latest run, nothing written
    tp-journal take SYMBOL          check the gates and record that you took it
    tp-journal pass SYMBOL          record that you passed on it (and why, with --note)
    tp-journal close ID PRICE       record the exit: the P&L in dollars and in R
    tp-journal list                 the entries, newest first
    tp-journal review               taken against passed, and how the gates did

Nothing here places an order: you trade at your broker, then write it down. The journal is a
JSON file under the data root (TP_JOURNAL_FILE), kept out of git.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings
from tp_core.storage import Lake
from tp_risk.state import FileRiskState
from tp_strategies import journal as j
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
