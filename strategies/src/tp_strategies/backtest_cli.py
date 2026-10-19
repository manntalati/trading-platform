"""``tp-backtest``: run library strategies through the event-driven engine.

tp-backtest list
tp-backtest run ma-timing --start 2018-01-01
tp-backtest run ma-timing -p assets=SPY --fill next_close --slippage-bps 10
tp-backtest validate ma-timing          # the plan's stage-1 gate (see docs/backtesting.md)
tp-backtest validate --all
tp-backtest sizing ts-momentum           # the plan's three sizing methods side by side
tp-backtest regime ts-momentum           # switched off in bad regimes (hmm, trend, vix) or not
tp-backtest run rsi2 --regime hmm        # one backtest with a regime gate
tp-backtest research pairs|vrp|orb       # vectorized research backtests
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Annotated, Any

import typer

from tp_core.bars import close_matrix, load_bars, missing_symbols
from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_risk.manager import RiskManager
from tp_strategies.backtest import risk_gate, run_backtest
from tp_strategies.library import REGISTRY, build, parameters
from tp_strategies.library.ts_momentum import DIVERSIFIED
from tp_strategies.regime_gate import METHODS as REGIME_METHODS
from tp_strategies.regime_gate import make_gate
from tp_strategies.research_cli import app as research_app
from tp_strategies.search import (
    DEFAULT_FAMILIES,
    FAMILIES,
    SearchConfig,
    rule_sets,
    run_search,
    universe,
)
from tp_strategies.search import report_text as search_report_text
from tp_strategies.validation import ValidationConfig, paper_params, paper_regimes
from tp_strategies.validation import validate as run_validation
from tp_trading.costs import CostModel
from tp_trading.engine import EngineConfig
from tp_trading.execution import ExecutionConfig, FillAt
from tp_trading.regime import SeriesGate
from tp_trading.sizing import METHODS as SIZING_METHODS
from tp_trading.sizing import make as make_sizing

app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(research_app, name="research")
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
    sizing: Annotated[
        str, typer.Option(help="strategy (its own weights), fixed-fractional, vol-target or kelly.")
    ] = "strategy",
    regime: Annotated[
        str, typer.Option(help="Sit in cash in bad regimes: off, hmm, trend or vix.")
    ] = "off",
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
    try:
        sizer = make_sizing(sizing)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--sizing") from exc
    settings = Settings()
    config = EngineConfig(
        initial_cash=capital,
        start=start.date() if start else None,
        end=end.date() if end else None,
        execution=ExecutionConfig(
            fill_at=fill_at,
            fractional=fractional,
            costs=CostModel(slippage_bps=slippage_bps),
        ),
        sizing=sizer,
        regime=_gate(Lake(settings.data_root), regime, end.date() if end else None),
    )
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
        f"risk limits {'on' if risk else 'OFF'}, sizing {sizing}, regime gate {regime}\n"
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


def _gate(lake: Lake, method: str, end: date | None = None) -> SeriesGate | None:
    try:
        return make_gate(lake, method, end)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--regime") from exc


def _risk_gate(settings: Settings, classifier: Classifier, enforce_drawdown: bool) -> RiskManager:
    if not settings.risk_file.exists():
        typer.echo(
            f"risk limits file {settings.risk_file} not found: run from the repo root, set "
            "TP_RISK_FILE, or pass --no-risk",
            err=True,
        )
        raise typer.Exit(2)
    return risk_gate(settings.risk_file, classifier, enforce_drawdown=enforce_drawdown)


@app.command("sizing")
def compare_sizing(
    strategy: Annotated[str, typer.Argument(help="Strategy name (see `tp-backtest list`).")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Strategy parameter as key=value; repeatable."),
    ] = None,
    start: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First session to trade.")
    ] = None,
    capital: Annotated[float, typer.Option(help="Starting cash.")] = 100_000.0,
) -> None:
    """Backtest one strategy under each sizing method (tp_trading.sizing) and compare."""
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    classifier = Classifier.load(settings.classifications_file)
    rows = []
    for method in SIZING_METHODS:
        built = build(strategy, _params(param or []))
        config = EngineConfig(
            initial_cash=capital, start=start.date() if start else None, sizing=make_sizing(method)
        )
        gate = _risk_gate(settings, classifier, False) if settings.risk_file.exists() else None
        out = run_backtest(
            Lake(settings.data_root), built, config, risk=gate, sectors=classifier.sectors,
            now=datetime.now(UTC), save=False,
        )  # fmt: skip
        rows.append((method, out.summary["stats"]))
    typer.echo(f"\n{strategy}: sizing methods compared (same signals, costs and risk limits)\n")
    head = f"{'':18}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'max DD':>9}{'exposure':>10}{'turnover':>10}"
    typer.echo(head)
    for method, st in rows:
        typer.echo(
            f"{method:18}{_fmt(st.get('cagr'), 'pct'):>8}{_fmt(st.get('ann_volatility'), 'pct'):>8}"
            f"{_fmt(st.get('sharpe'), 'num'):>8}{_fmt(st.get('max_drawdown'), 'pct'):>9}"
            f"{_fmt(st.get('avg_exposure'), 'pct'):>10}{_fmt(st.get('annual_turnover'), 'pct'):>10}"
        )


@app.command("regime")
def compare_regimes(
    strategy: Annotated[str, typer.Argument(help="Strategy name (see `tp-backtest list`).")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Strategy parameter as key=value; repeatable."),
    ] = None,
    start: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First session to trade.")
    ] = None,
    capital: Annotated[float, typer.Option(help="Starting cash.")] = 100_000.0,
) -> None:
    """Backtest one strategy always in the market, then switched off by each regime gate
    (hmm, trend, vix), and compare. The gates use only data up to each day's close."""
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    lake = Lake(settings.data_root)
    classifier = Classifier.load(settings.classifications_file)
    rows = []
    for method in REGIME_METHODS:
        try:
            gate = make_gate(lake, method)
        except ValueError as exc:
            typer.echo(f"{method}: skipped ({exc})", err=True)
            continue
        built = build(strategy, _params(param or []))
        config = EngineConfig(
            initial_cash=capital, start=start.date() if start else None, regime=gate
        )
        risk = _risk_gate(settings, classifier, False) if settings.risk_file.exists() else None
        try:
            out = run_backtest(
                lake, built, config, risk=risk, sectors=classifier.sectors,
                now=datetime.now(UTC), save=False,
            )  # fmt: skip
        except KeyError as exc:  # bars missing for one of its symbols
            typer.echo(str(exc.args[0]), err=True)
            raise typer.Exit(1) from exc
        st = out.summary["stats"]
        first, last = out.result.equity.index[0], out.result.equity.index[-1]
        share = gate.share_off(first.date(), last.date()) if gate else 0.0
        rows.append((method, st, share))
    typer.echo(f"\n{strategy}: always in the market vs switched off by a regime gate\n")
    head = f"{'':10}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'max DD':>9}{'exposure':>10}{'gate off':>10}"
    typer.echo(head)
    for method, st, share in rows:
        typer.echo(
            f"{method:10}{_fmt(st.get('cagr'), 'pct'):>8}{_fmt(st.get('ann_volatility'), 'pct'):>8}"
            f"{_fmt(st.get('sharpe'), 'num'):>8}{_fmt(st.get('max_drawdown'), 'pct'):>9}"
            f"{_fmt(st.get('avg_exposure'), 'pct'):>10}{_fmt(share, 'pct'):>10}"
        )
    typer.echo(
        "\nA gate earns its place by cutting the drawdown by more than it costs in return. "
        "Validate the one you'd use (`tp-backtest validate <strategy> --regime <gate>`) before "
        'adding regime = "<gate>" to the strategy in config/paper.toml; every gate tried is '
        "another trial."
    )


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
    regime: Annotated[
        str | None,
        typer.Option(help="Regime gate (off, hmm, trend, vix); default: the paper sleeve's."),
    ] = None,
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
    lake = Lake(settings.data_root)
    gated = paper_regimes(settings.paper_file)
    failed = False
    for name in names:
        method = regime if regime is not None else gated.get(name, "off")
        try:
            report = run_validation(
                lake, name, engine=replace(engine, regime=_gate(lake, method)),
                params=_params(param or []) if param else on_paper.get(name, {}),
                config=config, sectors=classifier.sectors, now=datetime.now(UTC), save=save,
                risk=(lambda: _risk_gate(settings, classifier, False)) if risk else (lambda: None),
                progress=lambda m: typer.echo(f"  {m}", err=True),
            )  # fmt: skip
        except (KeyError, ValueError, LookupError) as exc:  # e.g. no fundamentals yet
            typer.echo(f"{name}: {exc.args[0] if exc.args else exc}", err=True)
            failed = True
            continue
        _print_report(report)
        failed |= not report["gate"]["passed"]
    if failed:
        raise typer.Exit(1)


@app.command("search")
def search_cmd(
    families: Annotated[
        str,
        typer.Option(
            help=f"Comma-separated, from {', '.join(FAMILIES)} (grammar: a genetic search over "
            "a grammar of signals; opt in, it adds many trials)."
        ),
    ] = ",".join(DEFAULT_FAMILIES),
    top: Annotated[int, typer.Option(help="Rule sets taken from train to validation.")] = 5,
    cost_bps: Annotated[float, typer.Option(help="Cost per unit traded, basis points.")] = 5.0,
    record: Annotated[
        bool,
        typer.Option(help="Count this search in the trials ledger and the test looks (keep on)."),
    ] = True,
    log: Annotated[bool, typer.Option(help="Record the run in MLflow.")] = True,
    generations: Annotated[int, typer.Option(help="grammar: generations to breed.")] = 15,
    population: Annotated[int, typer.Option(help="grammar: rules per generation.")] = 60,
    seed: Annotated[int, typer.Option(help="grammar: random seed.")] = 0,
) -> None:
    """Search many simple rule sets at once, guarded by train/validation/test, the Deflated
    Sharpe over every rule set ever tried, the probability of backtest overfitting and a
    walk-forward of the search itself (vectorized: a screen; the engine's validate is the gate)."""
    chosen = tuple(f.strip() for f in families.split(",") if f.strip())
    if not chosen or any(f not in FAMILIES for f in chosen):
        raise typer.BadParameter(f"families: {', '.join(FAMILIES)}")
    settings = Settings()
    lake = Lake(settings.data_root)
    periods = (
        ValidationConfig.load(settings.validation_file)
        if settings.validation_file.exists()
        else ValidationConfig()
    )
    wanted = sorted({s for rule in rule_sets(chosen) for s in universe(rule)}
                    | (set(DIVERSIFIED) if "grammar" in chosen else set()))  # fmt: skip
    have = [s for s in wanted if s not in missing_symbols(lake, [s])]
    if not have:
        typer.echo("needs daily bars: tp-data bars backfill --years 10")
        raise typer.Exit(code=2)
    closes = close_matrix(load_bars(lake, have))
    now = datetime.now(UTC)
    cfg = SearchConfig(
        top=top, cost_bps=cost_bps, families=chosen,
        generations=generations, population=population, seed=seed,
    )  # fmt: skip
    try:
        report, looks = run_search(lake, closes, cfg, periods, now=now, record=record)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from None
    text = search_report_text(report, looks)
    typer.echo(text)
    if not log:
        return
    from tp_ml import tracking

    d, p, wf, test = report["deflated"], report["pbo"], report["walk_forward"], report["test"]
    with tracking.run(
        lake,
        "strategy-search",
        name=f"search-{'+'.join(chosen)}",
        params={
            "families": ",".join(chosen),
            "top": top,
            "cost_bps": cost_bps,
            "rule_sets": report["rule_sets"],
            "chosen": report["chosen"].label,
        },
        tags={"data_last_session": report["last"]},
    ) as active:
        tracking.log_metrics(
            {
                "trials_all_time": d["trials"],
                "dsr_probability": d["probability"],
                "dsr_sharpe": d["sharpe"],
                "dsr_threshold": d["threshold"],
                "pbo": p["pbo"],
                "wf_sharpe": wf.get("sharpe"),
                "wf_efficiency": wf.get("efficiency"),
                "test_sharpe": test["sharpe"] if test else None,
            }
        )
        tracking.log_text(text, "report.txt")
        tracking.log_text(report["table"].to_csv(), "rule_sets.csv")
    typer.echo(f"\nlogged to MLflow (strategy-search, run {active.info.run_id[:8]}): tp-ml ui")


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
