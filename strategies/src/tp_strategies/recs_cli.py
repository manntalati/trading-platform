"""``tp-recs``: make, show and score the platform's recommendations.

    tp-recs run         scan for ideas and check the portfolio now; store the run
    tp-recs show        the latest run: each list, best first, new ones marked
    tp-recs scorecard   how past recommendations have done since

The job scheduler (``tp-jobs``, or the dashboard's server) runs ``tp-recs run`` by itself
whenever new data lands, so this is for a run by hand or from cron.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import typer

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings, Universes, load_universes
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_strategies import recommendations as recs

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
    )
    typer.echo(result.summary())


def _pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.0%}" if signed else f"{v:.0%}"


@app.command()
def show(
    only: Annotated[
        str | None, typer.Option(help="One list: swing, leaps, bearish, income.")
    ] = None,
) -> None:
    """The latest run, each list best first; ★ marks what's new since the previous session."""
    _, lake = _setup()
    latest = recs.latest_run(lake)
    if latest is None:
        typer.echo("no recommendations yet: run `tp-recs run`")
        raise typer.Exit(1)
    at = datetime.fromisoformat(latest["as_of"]).astimezone(NEW_YORK)
    typer.echo(f"as of {at:%a %Y-%m-%d %H:%M} ET (bars through {latest.get('bars_session')})\n")
    for k in recs.LISTS:
        if only and k != only:
            continue
        ideas = latest["research"].get(k) or []
        typer.echo(f"{recs.LIST_NAMES[k]} ({len(ideas)})")
        for i in ideas:
            pick = i.get("pick") or {}
            label = (
                pick.get("label") or (pick.get("long") or {}).get("label") or pick.get("kind") or ""
            )
            typer.echo(
                f"  {'★' if i.get('new') else ' '} {i['symbol']:<6} {i['score']:.2f}  {label}"
            )
        dropped = (latest.get("dropped") or {}).get(k) or []
        if dropped:
            typer.echo(f"    no longer listed: {', '.join(dropped)}")
        typer.echo("")
    typer.echo(latest.get("disclaimer", ""))


@app.command()
def scorecard(
    limit: Annotated[int, typer.Option(help="How many recent recommendations to list.")] = 20,
) -> None:
    """How past recommendations have done: the stock's move since in the recommended direction,
    and the position marked at the latest quotes (or at expiry)."""
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
        )
    typer.echo("")
    for r in record["recommendations"][:limit]:
        typer.echo(
            f"{r['recommended_at'][:10]}  {r['list']:<8} {r['symbol']:<6} {r['structure']:<18}"
            f" stock {_pct(r['move'])}  position {_pct(r['result'])}"
            f"{'  (expired)' if r.get('status') == 'expired' else ''}"
        )


def main() -> None:
    app()
