"""``tp-recs``: make, show and score the platform's recommendations.

    tp-recs run         scan for ideas and check the portfolio now; store the run
    tp-recs show        the latest run: each list, best first, new ones marked
    tp-recs scorecard   how past recommendations have done since
    tp-recs calibrate   backtest the playbook's setups: the drift and exits the ideas use

The job scheduler (``tp-jobs``, or the dashboard's server) runs ``tp-recs run`` by itself
whenever new data lands, so this is for a run by hand or from cron.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings, Universes, load_universes
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_strategies import recommendations as recs
from tp_strategies.budget import budget_from_settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Make, show and score the platform's recommendations.",
)


def _setup() -> tuple[Settings, Lake]:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    return settings, Lake(settings.data_root)


@app.command()
def run() -> None:
    """Scan for ideas and check the portfolio now, and store the run."""
    settings, lake = _setup()
    try:
        universes = load_universes(settings.universes_file)
    except OSError:
        universes = Universes(bars=())
    result = recs.run_recommendations(
        lake,
        universes=universes,
        classifier=Classifier.load(settings.classifications_file),
        now=datetime.now(UTC),
        default_rate=settings.risk_free_rate,
        budget=budget_from_settings(lake, settings),
    )
    typer.echo(result.summary())


def _pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.0%}"


@app.command()
def show(
    only: Annotated[
        str | None,
        typer.Option(help="One list: swing, leaps, bearish, income, volatility, stocks, watch."),
    ] = None,
) -> None:
    """The latest run, each list best first, with the setup, the contract, the modeled edge, the
    entry limit and the exit plan; ★ marks what's new since the previous session."""
    _, lake = _setup()
    latest = recs.latest_run(lake)
    if latest is None:
        typer.echo("no recommendations yet: run `tp-recs run`")
        raise typer.Exit(1)
    at = datetime.fromisoformat(latest["as_of"]).astimezone(NEW_YORK)
    typer.echo(f"as of {at:%a %Y-%m-%d %H:%M} ET (bars through {latest.get('bars_session')})")
    budget = latest["research"].get("budget")
    typer.echo(
        f"sized for {budget['account']}: buying power ${budget['buying_power']:,.0f}, up to "
        f"{budget['risk_per_trade']:.0%} of ${budget['account_value']:,.0f} a trade"
        + (f", options level {budget['options_level']}" if budget.get("options_level") else "")
        + "\n"
        if budget
        else "not sized to an account: sync the brokerage (tp-broker sync)\n"
    )
    for k in recs.LISTS:
        if only and k != only:
            continue
        ideas = latest["research"].get(k) or []
        typer.echo(f"{recs.LIST_NAMES[k]} ({len(ideas)})")
        for i in ideas:
            news = i.get("news")
            heard = f"  · news {news['sentiment']:+.2f} ({news['headlines']})" if news else ""
            typer.echo(f"  {'★' if i.get('new') else ' '} {i['symbol']:<6} {_line(i)}{heard}")
            plan = (i.get("pick") or {}).get("exit")
            if plan:
                typer.echo(f"           {_plan_line(i['pick'])}")
        dropped = (latest.get("dropped") or {}).get(k) or []
        if dropped:
            typer.echo(f"    no longer listed: {', '.join(dropped)}")
        typer.echo("")
    missed = latest["research"].get("out_of_reach") or []
    if missed and only is None:
        typer.echo(f"Out of reach ({len(missed)})")
        for m in missed:
            why = (
                "more than the buying power"
                if m["reason"] == "buying power"
                else f"needs options level {m['level']}"
            )
            unit = "a share" if m["structure"] == "shares" else "a contract"
            typer.echo(
                f"    {m['symbol']:<6} {m['setup']}: {m['structure']} at "
                f"${m['needs']:,.0f} {unit}, {why}"
            )
        typer.echo("")
    alerts = latest["research"].get("holding_alerts") or []
    if alerts and only in (None, "stocks"):
        typer.echo(f"On what you hold ({len(alerts)})")
        for a in alerts:
            typer.echo(f"    {a['symbol']:<6} {a['text']} (held: {a['held']})")
        typer.echo("")
    watch = latest["research"].get("watch") or []
    if watch and only in (None, "watch"):
        typer.echo(f"Watch for an entry ({len(watch)})")
        for w in watch:
            typer.echo(f"    {w['symbol']:<6} {w['text']}")
        typer.echo("")
    typer.echo(latest.get("disclaimer", ""))


def _line(idea: dict[str, Any]) -> str:
    pick = idea.get("pick") or {}
    play = idea.get("play") or {}
    if pick.get("kind") == "shares":
        model = pick.get("model") or {}
        test = pick.get("backtest") or {}
        record = (
            f", backtest won {_pct(test.get('win_rate'), False)} of {test['trades']}"
            if test.get("trades")
            else ""
        )
        return (
            f"{play.get('title', '')}: shares at {pick['entry']['limit']:,.2f}, drift"
            f" {_pct(model.get('drift'))} over {model.get('horizon')} sessions{record}"
        )
    legs = pick.get("legs") or []
    label = legs[0]["label"] if legs else pick.get("label") or pick.get("kind") or ""
    model = pick.get("model") or {}
    edge = (
        f"  expected {_pct(model.get('expected_return'))} over {model.get('horizon')} sessions,"
        f" {_pct(model.get('prob_profit'), False)} odds"
        if model
        else ""
    )
    return f"{play.get('title', '')}: {pick.get('structure', '')} {label}{edge}"


def _plan_line(pick: dict[str, Any]) -> str:
    entry, plan = pick.get("entry") or {}, pick["exit"]
    if pick.get("kind") == "shares":
        parts = [f"limit {entry.get('limit')}, no more than {entry.get('worst')}"
                 f" (valid to {entry.get('valid_until')})", f"stop {plan.get('stop')}"]  # fmt: skip
        if plan.get("target") is not None:
            parts.append(f"target {plan['target']}")
        if (plan.get("stock") or {}).get("text"):
            parts.append(f"or {plan['stock']['text']}")
        parts.append(f"sell by {plan.get('sell_by')}")
        size = pick.get("size")
        if size:
            parts.append(
                f"{size['shares']} shares (${size['capital']:,.0f}, ${size['max_loss']:,.0f} to"
                " the stop" + (", over your risk guideline" if size["over_guideline"] else "") + ")"
            )
        return "; ".join(parts)
    side = "credit" if entry.get("side") == "credit" else "limit"
    parts = [f"{side} {entry.get('limit')} (valid to {entry.get('valid_until')})"]
    if plan.get("take_profit") is not None:
        parts.append(f"take profit at {plan['take_profit']}")
    if plan.get("stop") is not None:
        parts.append(f"stop at {plan['stop']}")
    stock = plan.get("stock") or {}
    if stock.get("text"):
        parts.append(f"or {stock['text']}")
    parts.append(f"out by {plan.get('sell_by')}")
    size = pick.get("size")
    if size:
        parts.append(
            f"{size['contracts']} contract{'s' * (size['contracts'] != 1)} "
            f"(${size['capital']:,.0f}, at most ${size['max_loss']:,.0f} at risk"
            + (", over your risk guideline" if size["over_guideline"] else "")
            + ")"
        )
    return "; ".join(parts)


@app.command()
def scorecard(
    limit: Annotated[int, typer.Option(help="How many recent ideas to list.")] = 20,
) -> None:
    """How past ideas have done (each once, however many runs listed it): the stock's move since
    in the recommended direction, and the position marked at the latest quotes (or at
    expiry)."""
    _, lake = _setup()
    record = recs.track_record(lake, datetime.now(UTC).astimezone(NEW_YORK).date())
    if not record["recommendations"]:
        typer.echo("no recommendations stored yet: run `tp-recs run` (the scheduler does daily)")
        raise typer.Exit(1)
    for s in record["summary"]:
        profitable, right_way = _pct(s["profitable"], False), _pct(s["right_way_21d"], False)
        typer.echo(
            f"{s['name']:<16} {s['recommendations']:>4} ideas · profitable {profitable}"
            f" of {s['marked']} marked · median {_pct(s['median_result'])} · right way after 21"
            f" sessions {right_way} · beat the S&P 500 {_pct(s['beat_spy'], False)}"
            f" · following the exit plans: {s['plan_closed']} closed, won"
            f" {_pct(s['plan_win_rate'], False)}, median {_pct(s['plan_median'])}"
        )
    if record.get("by_news"):
        typer.echo("\nBy the news when listed:")
        for g in record["by_news"]:
            typer.echo(
                f"  {g['name']:<26} {g['recommendations']:>4} ideas · right way after 21 sessions"
                f" {_pct(g['right_way_21d'], False)} · following the plans: {g['plan_closed']}"
                f" closed, won {_pct(g['plan_win_rate'], False)}"
            )
    if record.get("by_setup"):
        typer.echo("\nBy setup, following the exit plans:")
        for s in record["by_setup"]:
            typer.echo(
                f"  {s['setup']:<14} {s['recommendations']:>4} ideas · {s['plan_closed']} closed,"
                f" won {_pct(s['plan_win_rate'], False)}, median {_pct(s['plan_median'])}"
            )
    typer.echo("")
    for r in record["recommendations"][:limit]:
        followed = r.get("followed") or {}
        plan = (
            f"  plan: {followed['status']}"
            + (
                f" ({followed['reason']}, {_pct(followed['result'])})"
                if followed.get("reason")
                else f" {_pct(followed.get('result'))}"
            )
            if followed
            else ""
        )
        runs = f"  ({r['runs']} runs)" if r.get("runs", 1) > 1 else ""
        typer.echo(
            f"{r['recommended_at'][:10]}  {r['list']:<8} {r['symbol']:<6} {r['structure']:<18}"
            f" stock {_pct(r['move'])}  position {_pct(r['result'])}"
            f"{'  (expired)' if r.get('status') == 'expired' else ''}{plan}{runs}"
        )


@app.command()
def calibrate() -> None:
    """Backtest every playbook setup on the names the ideas scan, over all their daily bars, and
    store each setup's drift and best exits (chosen on the earlier 60% of signals, reported on the
    rest) for the live ideas. Also lists the real contracts worth fetching for the next
    calibration (`tp-data options history`). Takes a minute or two; the scheduler runs it
    weekly."""
    from tp_strategies.options_research import eligible
    from tp_strategies.playbook_backtest import calibrate as run_calibration
    from tp_strategies.playbook_backtest import report_lines

    settings, lake = _setup()
    try:
        universes = load_universes(settings.universes_file)
    except OSError:
        universes = Universes(bars=())
    classifier = Classifier.load(settings.classifications_file)
    symbols = [s for s in recs.research_symbols(lake, universes) if eligible(s, classifier)]
    try:
        report = run_calibration(lake, symbols, now=datetime.now(UTC))
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    for line in report_lines(report):
        typer.echo(line)


def main() -> None:
    app()
