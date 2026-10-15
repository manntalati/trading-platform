"""``tp-ml``: features, models and experiment tracking.

tp-ml features                 # compute and store the daily feature set (versioned, registered)
tp-ml features --list          # the stored versions
tp-ml meta                     # evaluate the RSI(2) meta-labeling filter; logged to MLflow
tp-ml meta --model logistic --label barrier
tp-ml rank                     # evaluate cross-sectional ranking (IC, buckets) vs 12-1 momentum
tp-ml rank --model gbm
tp-ml runs                     # recent runs and their scores
tp-ml ui                       # MLflow's web UI on http://127.0.0.1:5000

Every run records its parameters, the feature set version, the data version and the git commit,
so a number in a note can always be traced back to what produced it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import typer

from tp_core.bars import load_bars, missing_symbols
from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_core.versions import data_version
from tp_ml import meta, ranking, tracking
from tp_ml.features import EARNINGS, FEATURES
from tp_ml.models import MODELS, REGRESSORS
from tp_ml.store import build_feature_set, load_feature_set, registry, wide_bars

app = typer.Typer(help="Features, models and experiment tracking.", no_args_is_help=True)

INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")
EXPERIMENT = "meta-labeling"
RANKING = "cross-sectional-ranking"


def default_symbols(universes: Path) -> list[str]:
    """RSI(2)'s universe: the index ETFs and the shared large caps (not your local tickers)."""
    with universes.open("rb") as f:
        raw = tomllib.load(f)
    return list(dict.fromkeys([*INDEX_ETFS, *raw.get("bars", {}).get("stocks", [])]))


def _symbols(
    value: str | None, settings: Settings, lake: Lake, *, stocks_only: bool = False
) -> list[str]:
    defaults = default_symbols(settings.universes_file)
    if stocks_only:
        defaults = [s for s in defaults if s not in INDEX_ETFS]
    wanted = [s.strip().upper() for s in value.split(",") if s.strip()] if value else defaults
    missing = set(missing_symbols(lake, wanted))
    return [s for s in wanted if s not in missing]


@app.command("features")
def features_cmd(
    symbols: Annotated[str | None, typer.Option(help="Comma-separated; default: all.")] = None,
    name: Annotated[str, typer.Option(help="Feature set name.")] = "daily",
    list_: Annotated[bool, typer.Option("--list", help="List stored versions.")] = False,
) -> None:
    """Compute the point-in-time features for every symbol with bars and store them."""
    settings = Settings()
    lake = Lake(settings.data_root)
    if list_:
        entries = registry(lake)
        if not entries:
            typer.echo("no feature sets yet: tp-ml features")
        for e in sorted(entries, key=lambda e: e["created_at"]):
            typer.echo(
                f"{e['name']:<12} {e['version']}  {e['symbols']:>3} symbols  "
                f"{e['first_session']} to {e['last_session']}  {e['rows']:>8,} rows  "
                f"built {e['created_at'][:16]}"
            )
        return
    chosen = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    classifier = Classifier.load(settings.classifications_file)
    try:
        entry = build_feature_set(
            lake, name=name, symbols=chosen, classifier=classifier, now=datetime.now(UTC)
        )
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from None
    typer.echo(
        f"{entry['name']} {entry['version']}: {entry['rows']:,} rows, {entry['symbols']} symbols, "
        f"{entry['first_session']} to {entry['last_session']}, {len(entry['columns'])} features"
    )
    typer.echo(f"stored in {Path(entry['path']).parent}")


@app.command("meta")
def meta_cmd(
    symbols: Annotated[str | None, typer.Option(help="Comma-separated; default: RSI(2)'s.")] = None,
    model: Annotated[str, typer.Option(help=f"One of {', '.join(MODELS)}.")] = "gbm",
    label: Annotated[str, typer.Option(help=f"One of {', '.join(meta.LABELS)}.")] = "trade",
    threshold: Annotated[float, typer.Option(help="Keep trades the model gives this.")] = 0.55,
    folds: Annotated[int, typer.Option(help="Cross-validation folds.")] = 5,
    features: Annotated[
        str, typer.Option(help="'price' (what the strategy uses) or 'all' (+ macro, earnings).")
    ] = "price",
    log: Annotated[bool, typer.Option(help="Record the run in MLflow.")] = True,
) -> None:
    """Evaluate the meta-labeling filter on RSI(2)'s signals, out of sample."""
    if model not in MODELS or label not in meta.LABELS or features not in ("price", "all"):
        typer.echo(f"model: {MODELS}; label: {meta.LABELS}; features: price or all")
        raise typer.Exit(code=2)
    settings = Settings()
    lake = Lake(settings.data_root)
    chosen = _symbols(symbols, settings, lake)
    if len(chosen) < 2:
        typer.echo("needs daily bars: tp-data bars backfill")
        raise typer.Exit(code=2)
    cfg = meta.MetaConfig(model=model, label=label, threshold=threshold, folds=folds)
    columns = meta.PRICE_FEATURES if features == "price" else tuple(FEATURES)
    classifier = Classifier.load(settings.classifications_file)
    entry = build_feature_set(
        lake, name="rsi2-meta", symbols=chosen, classifier=classifier, now=datetime.now(UTC)
    )
    panel = load_feature_set(lake, entry["name"], entry["version"])
    raw = load_bars(lake, chosen)
    bars = wide_bars(raw)
    events = meta.rsi2_events(bars["close"], cfg.entry, cfg.trend)
    data = meta.dataset(events, panel, bars, cfg, columns)
    if len(data) < 100 or data["label"].nunique() < 2:
        typer.echo(f"only {len(data)} labelled signals: too few to evaluate")
        raise typer.Exit(code=2)
    typer.echo(f"{len(chosen)} symbols, feature set {entry['name']} {entry['version']}\n")
    report = meta.evaluate(data, cfg, columns)
    text = meta.report_text(report)
    typer.echo(text)
    if not log:
        return
    with tracking.run(
        lake,
        EXPERIMENT,
        name=f"rsi2-{label}-{model}",
        params={**asdict(cfg), "features": features, "symbols": len(chosen)},
        tags={
            "feature_set": f"{entry['name']}:{entry['version']}",
            "data_rows": data_version(raw)["rows"],
            "data_last_session": data_version(raw)["last_session"],
        },
    ) as active:
        tracking.log_metrics(_flat_metrics(report))
        tracking.log_text(text, "report.txt")
        summary = {k: v for k, v in report.items() if not k.startswith("_")}
        tracking.log_text(json.dumps(summary, indent=2, default=str), "report.json")
        predictions = data[["symbol", "date", "start", "end", "barrier", "ret", "label"]].assign(
            p_cv=report["_probabilities"], p_walk_forward=report["_walk_forward"]
        )
        tracking.log_text(predictions.to_csv(index=False), "predictions.csv")
    typer.echo(f"\nlogged to MLflow ({EXPERIMENT}, run {active.info.run_id[:8]}): tp-ml ui")


def _flat_metrics(report: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "events": report["events"],
        "base_rate": report["base_rate"],
        "avg_trade": report["avg_trade"],
    }
    for name, prefix in (("cv", "cv"), ("walk_forward", "wf")):
        for key, value in (report.get(name) or {}).items():
            out[f"{prefix}_{key}"] = value
    return out


@app.command("rank")
def rank_cmd(
    symbols: Annotated[
        str | None, typer.Option(help="Comma-separated; default: the large caps.")
    ] = None,
    model: Annotated[str, typer.Option(help=f"One of {', '.join(REGRESSORS)}.")] = "ridge",
    horizon: Annotated[int, typer.Option(help="Sessions ahead the return is ranked.")] = 21,
    buckets: Annotated[int, typer.Option(help="Groups by score (5 for a small universe).")] = 5,
    features: Annotated[
        str, typer.Option(help="'price' (what the strategy uses) or 'all' (+ earnings).")
    ] = "price",
    log: Annotated[bool, typer.Option(help="Record the run in MLflow.")] = True,
) -> None:
    """Evaluate a cross-sectional ranking model out of sample, against 12-1 momentum."""
    if model not in REGRESSORS or features not in ("price", "all") or buckets < 2:
        typer.echo(f"model: {REGRESSORS}; features: price or all; buckets: 2 or more")
        raise typer.Exit(code=2)
    settings = Settings()
    lake = Lake(settings.data_root)
    chosen = _symbols(symbols, settings, lake, stocks_only=True)
    if len(chosen) < max(buckets, 5):
        typer.echo("needs daily bars for at least 5 stocks: tp-data bars backfill")
        raise typer.Exit(code=2)
    cfg = ranking.RankConfig(model=model, horizon=horizon, buckets=buckets)
    columns = ranking.RANK_FEATURES + (EARNINGS if features == "all" else ())
    classifier = Classifier.load(settings.classifications_file)
    entry = build_feature_set(
        lake, name="ranking", symbols=chosen, classifier=classifier, now=datetime.now(UTC)
    )
    panel = load_feature_set(lake, entry["name"], entry["version"])
    raw = load_bars(lake, chosen)
    data = ranking.ranking_dataset(wide_bars(raw), panel, cfg, columns)
    months = data.index.get_level_values("date").nunique() if len(data) else 0
    if months < 24:
        typer.echo(f"only {months} month ends with a known next month: too few to evaluate")
        raise typer.Exit(code=2)
    typer.echo(f"feature set {entry['name']} {entry['version']}\n")
    report = ranking.evaluate_ranking(data, cfg, columns)
    text = ranking.report_text(report)
    typer.echo(text)
    if not log:
        return
    version = data_version(raw)
    with tracking.run(
        lake,
        RANKING,
        name=f"rank-{model}-{horizon}d",
        params={**asdict(cfg), "features": features, "symbols": len(chosen)},
        tags={
            "feature_set": f"{entry['name']}:{entry['version']}",
            "data_rows": version["rows"],
            "data_last_session": version["last_session"],
        },
    ) as active:
        metrics: dict[str, float | None] = {"rows": report["rows"]}
        for name, prefix in (("walk_forward", "wf"), ("cv", "cv"), ("momentum", "mom")):
            for key, value in (report.get(name) or {}).items():
                if isinstance(value, int | float):
                    metrics[f"{prefix}_{key}"] = value
        tracking.log_metrics(metrics)
        tracking.log_text(text, "report.txt")
        summary = {k: v for k, v in report.items() if not k.startswith("_")}
        tracking.log_text(json.dumps(summary, indent=2, default=str), "report.json")
        scored = report["_scored"][["fwd", "momentum", "p_walk_forward", "p_cv"]]
        tracking.log_text(scored.reset_index().to_csv(index=False), "predictions.csv")
    typer.echo(f"\nlogged to MLflow ({RANKING}, run {active.info.run_id[:8]}): tp-ml ui")


@app.command("runs")
def runs_cmd(
    experiment: Annotated[
        str | None, typer.Option(help="MLflow experiment; default: both.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Most recent N.")] = 10,
) -> None:
    """Recent runs: what was tried and how it scored out of sample."""
    lake = Lake(Settings().data_root)
    shown = 0
    for name in [experiment] if experiment else [EXPERIMENT, RANKING]:
        try:
            frame = tracking.runs(lake, name)
        except Exception:  # mlflow raises several kinds for a missing store
            continue
        if frame.empty:
            continue
        table = ranking_table if name == RANKING else runs_table
        typer.echo(f"{'' if not shown else chr(10)}{name}\n{table(frame.head(limit))}")
        shown += 1
    if not shown:
        typer.echo("no runs yet: tp-ml meta, tp-ml rank")


def _cell(row: pd.Series, key: str, fmt: str) -> str:
    value = row.get(key)
    return "—" if value is None or pd.isna(value) else format(float(value), fmt)


def runs_table(frame: pd.DataFrame) -> str:
    cell = _cell
    lines = [
        f"{'started':<17} {'run':<22} {'events':>6} {'CV AUC':>7} {'WF AUC':>7} "
        f"{'kept':>5} {'trade kept':>10} {'all':>7}"
    ]
    for _, row in frame.iterrows():
        started = pd.Timestamp(row["start_time"]).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{started:<17} {str(row.get('tags.mlflow.runName', ''))[:22]:<22} "
            f"{cell(row, 'metrics.events', '.0f'):>6} {cell(row, 'metrics.cv_auc', '.3f'):>7} "
            f"{cell(row, 'metrics.wf_auc', '.3f'):>7} "
            f"{cell(row, 'metrics.wf_kept_share', '.0%'):>5} "
            f"{cell(row, 'metrics.wf_avg_trade_kept', '+.2%'):>10} "
            f"{cell(row, 'metrics.wf_avg_trade_all', '+.2%'):>7}"
        )
    return "\n".join(lines)


def ranking_table(frame: pd.DataFrame) -> str:
    cell = _cell
    lines = [
        f"{'started':<17} {'run':<22} {'months':>6} {'rank IC':>8} {'t':>5} {'spread':>8} "
        f"{'momentum IC':>12}"
    ]
    for _, row in frame.iterrows():
        started = pd.Timestamp(row["start_time"]).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{started:<17} {str(row.get('tags.mlflow.runName', ''))[:22]:<22} "
            f"{cell(row, 'metrics.wf_dates', '.0f'):>6} "
            f"{cell(row, 'metrics.wf_rank_ic', '+.3f'):>8} "
            f"{cell(row, 'metrics.wf_rank_ic_t', '+.1f'):>5} "
            f"{cell(row, 'metrics.wf_spread', '+.2%'):>8} "
            f"{cell(row, 'metrics.mom_rank_ic', '+.3f'):>12}"
        )
    return "\n".join(lines)


@app.command("ui")
def ui_cmd(port: Annotated[int, typer.Option(help="Port.")] = 5000) -> None:
    """MLflow's web UI on this machine (http://127.0.0.1:PORT). Ctrl-C to stop."""
    lake = Lake(Settings().data_root)
    tracking.tracking_dir(lake).mkdir(parents=True, exist_ok=True)
    uri = tracking.tracking_uri(lake)
    typer.echo(f"MLflow UI on http://127.0.0.1:{port} ({uri})")
    cmd = [sys.executable, "-m", "mlflow", "ui", "--backend-store-uri", uri]
    subprocess.run([*cmd, "--host", "127.0.0.1", "--port", str(port)], check=False)


def main() -> None:
    app()
