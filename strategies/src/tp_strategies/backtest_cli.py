"""``tp-backtest``: run library strategies through the event-driven engine.

tp-backtest list
tp-backtest run ma-timing --start 2018-01-01
tp-backtest run ma-timing -p assets=SPY --fill next_close --slippage-bps 10
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
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import MemoryRiskState
from tp_strategies.backtest import run_backtest
from tp_strategies.library import REGISTRY, build, parameters
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
    gate = None
    if risk:
        if not settings.risk_file.exists():
            typer.echo(
                f"risk limits file {settings.risk_file} not found: run from the repo root, set "
                "TP_RISK_FILE, or pass --no-risk",
                err=True,
            )
            raise typer.Exit(2)
        gate = RiskManager(
            Limits.load(settings.risk_file),
            Classifier.load(settings.classifications_file),
            MemoryRiskState(),
            enforce_drawdown=enforce_drawdown,
        )
    try:
        outcome = run_backtest(
            Lake(settings.data_root), built, config, risk=gate, benchmark=benchmark,
            now=datetime.now(UTC), save=save,
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
