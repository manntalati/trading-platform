"""``tp-models``: the machine-learning strategies' models in use (``tp_strategies.model_monitor``).

tp-models monitor            retrain if due, predict today, check drift and live accuracy
tp-models monitor ml-rank    one model
tp-models list               every registered version
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_ml import monitor, tracking
from tp_strategies.model_monitor import MONITORED, monitor_all
from tp_strategies.validation import paper_params

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="The machine-learning strategies' models: registry, retraining, drift, live accuracy.",
)


def _pct(v: Any) -> str:
    return "—" if v is None else f"{v:.0%}"


def _num(v: Any, fmt: str = "+.3f") -> str:
    return "—" if v is None else format(v, fmt)


@app.command("monitor")
def monitor_cmd(
    names: Annotated[
        list[str] | None, typer.Argument(help=f"Default: {', '.join(MONITORED)}.")
    ] = None,
    log: Annotated[bool, typer.Option(help="Record each check in MLflow (monitoring).")] = True,
) -> None:
    """Retrain any model whose retrain date has come, predict today, and check drift and live
    accuracy. Uses each strategy's parameters from config/paper.toml when it trades there."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    lake = Lake(settings.data_root)
    wanted = names or list(MONITORED)
    unknown = sorted(set(wanted) - set(MONITORED))
    if unknown:
        raise typer.BadParameter(f"no model for {', '.join(unknown)}: {', '.join(MONITORED)}")
    reports = monitor_all(
        lake,
        wanted,
        paper_params(settings.paper_file),
        sectors=Classifier.load(settings.classifications_file).sectors,
        now=datetime.now(UTC),
    )
    for r in reports:
        version = (r.get("version") or {}).get("version", "none")
        typer.echo(
            f"{r['model']}: {r['status']} (version {version}"
            f"{', retrained today' if r.get('retrained') else ''})"
        )
        for reason in r.get("reasons", []):
            typer.echo(f"  ! {reason}")
        if r.get("drift"):
            top = r["drift"][0]
            typer.echo(
                f"  drift: most moved input {top['feature']} (PSI {top['psi']:.2f}, {top['level']})"
            )
        live = r.get("live") or {}
        if "dates" in live:
            validation = ((r.get("version") or {}).get("validation") or {}).get("rank_ic")
            typer.echo(
                f"  live: rank IC {_num(live.get('mean_ic'))} over {live['dates']} periods "
                f"(validation {_num(validation)})"
            )
        elif live.get("predictions"):
            typer.echo(
                f"  live: {live['predictions']} signals scored; taken ones won "
                f"{_pct(live.get('taken_hit_rate'))} vs {_pct(live.get('base_rate'))} for all"
            )
        if log and r.get("status") in ("ok", "warn"):
            with tracking.run(lake, "monitoring", name=r["model"], params={"version": version}):
                tracking.log_metrics(
                    {
                        "psi_max": r["drift"][0]["psi"] if r.get("drift") else None,
                        "live_rank_ic": live.get("mean_ic"),
                        "live_hit_rate": live.get("taken_hit_rate"),
                        "predictions_today": r.get("predictions_today"),
                    }
                )


@app.command("list")
def list_cmd() -> None:
    """Every registered version: when it was trained, on what, and how it scored out of sample."""
    lake = Lake(Settings().data_root)
    for name in MONITORED:
        found = monitor.versions(lake, name)
        typer.echo(f"{name}: {len(found)} version(s)")
        for v in found:
            val = v.get("validation") or {}
            score = (
                f"walk-forward rank IC {_num(val.get('rank_ic'))}"
                if "rank_ic" in val
                else f"walk-forward AUC {_num(val.get('auc'), '.2f')}"
            )
            typer.echo(f"  {v['version']}  trained to {str(v.get('trained_to'))[:10]}  {score}")


def main() -> None:
    app()
