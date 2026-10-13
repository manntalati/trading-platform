"""``tp-backtest``: run library strategies through the event-driven engine.

tp-backtest list
tp-backtest run ma-timing --start 2018-01-01
tp-backtest run ma-timing -p assets=SPY --fill next_close --slippage-bps 10
tp-backtest validate ma-timing          # the plan's stage-1 gate (see docs/backtesting.md)
tp-backtest validate --all
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_risk.manager import RiskManager
from tp_strategies.backtest import risk_gate, run_backtest
from tp_strategies.library import REGISTRY, build, parameters
from tp_strategies.validation import ValidationConfig, paper_params
from tp_strategies.validation import validate as run_validation
from tp_trading.costs import CostModel
from tp_trading.engine import EngineConfig
from tp_trading.execution import ExecutionConfig, FillAt

app = typer.Typer(add_completion=False, no_args_is_help=True)
log = logging.getLogger("tp_strategies.backtest")

ROWS = [
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("ann_volatility", "Volatility", "pct"),
    ("sharpe", "Sharpe", "num"),
    ("sortino", "Sortino", "num"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("max_dd_duration", "Longest drawdown (sessions)", "int"),
    ("calmar", "Calmar", "num"),
    ("beta", "Beta to benchmark", "num"),
]


@app.command("list")
def list_strategies() -> None:
    """Strategies available to backtest and paper-trade, with their parameters."""
    for name, cls in sorted(REGISTRY.items()):
        typer.echo(f"{name}: {cls.title}  ({cls.spec})")
        for key, default in parameters(cls).items():
            shown = ",".join(default) if isinstance(default, tuple) else default
            typer.echo(f"    {key.replace('_', '-')} = {shown}")


@app.command()
def run(
    strategy: Annotated[str, typer.Argument(help="Strategy name (see `tp-backtest list`).")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Strategy parameter as key=value; repeatable."),
    ] = None,
    start: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First session to trade.")
    ] = None,
    end: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Last session.")
    ] = None,
    capital: Annotated[float, typer.Option(help="Starting cash.")] = 100_000.0,
    fill: Annotated[str, typer.Option(help="next_open (realistic) or next_close.")] = "next_open",
    slippage_bps: Annotated[float, typer.Option(help="Per-fill slippage, basis points.")] = 5.0,
    fractional: Annotated[bool, typer.Option(help="Allow fractional shares.")] = False,
    benchmark: Annotated[str, typer.Option(help="Buy-and-hold comparison symbol.")] = "SPY",
    risk: Annotated[
        bool, typer.Option(help="Apply the pre-trade limits in config/risk.toml.")
    ] = True,
    enforce_drawdown: Annotated[
        bool,
        typer.Option(help="Stop opening trades after a drawdown-limit breach, as paper would."),
    ] = False,
    save: Annotated[bool, typer.Option(help="Write the run under reports/backtests/.")] = True,
) -> None:
    """Backtest one strategy and print its tear sheet next to buy-and-hold."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if fill not in ("next_open", "next_close"):
        raise typer.BadParameter("--fill must be next_open or next_close")
    try:
        built = build(strategy, _params(param or []))
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    fill_at: FillAt = "next_open" if fill == "next_open" else "next_close"
    config = EngineConfig(
        initial_cash=capital,
        start=start.date() if start else None,
        end=end.date() if end else None,
        execution=ExecutionConfig(
            fill_at=fill_at,
            fractional=fractional,
            costs=CostModel(slippage_bps=slippage_bps),
        ),
    )
    settings = Settings()
    classifier = Classifier.load(settings.classifications_file)
    gate = _risk_gate(settings, classifier, enforce_drawdown) if risk else None
    try:
        outcome = run_backtest(
            Lake(settings.data_root), built, config, risk=gate, sectors=classifier.sectors,
            benchmark=benchmark, now=datetime.now(UTC), save=save,
        )  # fmt: skip
    except KeyError as exc:
        typer.echo(str(exc.args[0]), err=True)
        raise typer.Exit(1) from exc

    stats = outcome.summary["stats"]
    bench = outcome.summary.get("benchmark", {})
    typer.echo(f"\n{built.title} ({built.name})  {stats['start']} to {stats['end']}")
    typer.echo(f"params: {built.params()}")
    typer.echo(
        f"fills at {fill}, {slippage_bps:g} bps slippage, start {capital:,.0f}, "
        f"risk limits {'on' if risk else 'OFF'}\n"
    )
    typer.echo(f"{'':30}{'strategy':>12}{benchmark + ' hold':>12}")
    for key, label, kind in ROWS:
        typer.echo(f"{label:30}{_fmt(stats.get(key), kind):>12}{_fmt(bench.get(key), kind):>12}")
    typer.echo("")
    typer.echo(f"final equity        {stats['final_equity']:,.2f}")
    typer.echo(f"trades              {stats['trades']} ({stats['trades_per_year']:.1f} a year)")
    typer.echo(f"annual turnover     {_fmt(stats['annual_turnover'], 'pct')}")
    typer.echo(f"average exposure    {_fmt(stats['avg_exposure'], 'pct')}")
    typer.echo(f"costs               fees {stats['fees']:,.2f}, slippage {stats['slippage']:,.2f}")
    typer.echo(f"orders              {stats['orders']}")
    rejected = outcome.result.orders[outcome.result.orders["status"] == "rejected"]
    if len(rejected):
        typer.echo("top rejection reasons:")
        for note, count in rejected["note"].value_counts().head(5).items():
            typer.echo(f"  {count:5d}  {note}")
    breaches = outcome.summary["risk"].get("drawdown_breaches", [])
    for b in breaches:
        action = "strategy disabled" if b["enforced"] else "recorded only"
        typer.echo(
            f"drawdown limit breached {b['session']}: {b['drawdown']:.1%} vs "
            f"-{b['limit']:.0%} ({action})"
        )
    if outcome.path:
        typer.echo(f"\nsaved to {outcome.path}")


def _risk_gate(settings: Settings, classifier: Classifier, enforce_drawdown: bool) -> RiskManager:
    if not settings.risk_file.exists():
        typer.echo(
            f"risk limits file {settings.risk_file} not found: run from the repo root, set "
            "TP_RISK_FILE, or pass --no-risk",
            err=True,
        )
        raise typer.Exit(2)
    return risk_gate(settings.risk_file, classifier, enforce_drawdown=enforce_drawdown)


MARK = {True: "PASS", False: "FAIL", None: " n/a"}


@app.command()
def validate(
    strategy: Annotated[
        str | None, typer.Argument(help="Strategy name (see `tp-backtest list`).")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Validate every strategy.")] = False,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Strategy parameter as key=value; repeatable."),
    ] = None,
    capital: Annotated[float, typer.Option(help="Starting cash.")] = 100_000.0,
    slippage_bps: Annotated[float, typer.Option(help="Per-fill slippage, basis points.")] = 5.0,
    risk: Annotated[
        bool, typer.Option(help="Apply the pre-trade limits in config/risk.toml.")
    ] = True,
    save: Annotated[bool, typer.Option(help="Write the report under reports/validation/.")] = True,
) -> None:
    """Run the plan's backtest validation: locked test set, walk-forward, Deflated Sharpe,
    stress periods and parameter stability (config/validation.toml). Pass all five and the
    strategy is ready for paper trading."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if every == (strategy is not None):
        raise typer.BadParameter("name one strategy, or pass --all")
    names = sorted(REGISTRY) if every else [str(strategy)]
    settings = Settings()
    on_paper = paper_params(settings.paper_file)
    config = (
        ValidationConfig.load(settings.validation_file)
        if settings.validation_file.exists()
        else ValidationConfig()
    )
    classifier = Classifier.load(settings.classifications_file)
    engine = EngineConfig(
        initial_cash=capital,
        execution=ExecutionConfig(costs=CostModel(slippage_bps=slippage_bps)),
    )
    failed = False
    for name in names:
        try:
            report = run_validation(
                Lake(settings.data_root), name, engine=engine,
                params=_params(param or []) if param else on_paper.get(name, {}),
                config=config, sectors=classifier.sectors, now=datetime.now(UTC), save=save,
                risk=(lambda: _risk_gate(settings, classifier, False)) if risk else (lambda: None),
                progress=lambda m: typer.echo(f"  {m}", err=True),
            )  # fmt: skip
        except (KeyError, ValueError) as exc:
            typer.echo(f"{name}: {exc.args[0] if exc.args else exc}", err=True)
            failed = True
            continue
        _print_report(report)
        failed |= not report["gate"]["passed"]
    if failed:
        raise typer.Exit(1)


def _print_report(r: dict[str, Any]) -> None:
    g = r["gate"]
    typer.echo(f"\n{r['title']} ({r['strategy']})  {r['params']}")
    typer.echo(
        f"stage-1 gate: {'PASSED: ready for paper trading' if g['passed'] else 'not passed'}"
    )
    for c in g["checks"]:
        typer.echo(f"  [{MARK[c['passed']]}] {c['name']}: {c['detail']}")
    typer.echo(f"\n{'':12}{'sessions':>9}{'CAGR':>9}{'Sharpe':>8}{'max DD':>9}{'SPY CAGR':>10}")
    for label, p in [("full", r["full"]), *r["periods"].items()]:
        bench = p.get("benchmark") or {}
        typer.echo(
            f"{label:12}{p.get('sessions', 0):>9}{_fmt(p.get('cagr'), 'pct'):>9}"
            f"{_fmt(p.get('sharpe'), 'num'):>8}{_fmt(p.get('max_drawdown'), 'pct'):>9}"
            f"{_fmt(bench.get('cagr'), 'pct'):>10}"
        )
    mc = r.get("monte_carlo") or {}
    if mc:
        typer.echo(
            f"\nMonte Carlo ({mc['paths']} resampled paths): CAGR {_fmt(mc['cagr_p5'], 'pct')} to "
            f"{_fmt(mc['cagr_p95'], 'pct')} (5th-95th), bad-tail drawdown "
            f"{_fmt(mc['max_drawdown_p5'], 'pct')}, {mc['prob_loss']:.0%} of paths lose money"
        )
    for dim, rows in (r.get("regimes") or {}).items():
        parts = [f"{x['label']} {_fmt(x['ann_return'], 'pct')}/yr" for x in rows]
        typer.echo(f"by {dim:10} " + ", ".join(parts))
    if r.get("test_touches", 1) > 1:
        typer.echo(
            f"note: the locked test set has now been looked at {r['test_touches']} times for "
            "these parameters"
        )


def _params(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--param expects key=value, got {pair!r}")
        out[key.strip()] = value.strip()
    return out


def _fmt(value: Any, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if kind == "pct":
        return f"{value:.1%}"
    if kind == "int":
        return f"{int(value)}"
    return f"{value:.2f}"


def main() -> None:
    app()
