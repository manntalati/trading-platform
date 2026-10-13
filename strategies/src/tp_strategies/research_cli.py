"""``tp-backtest research``: the vectorized research backtests that don't run on the event engine.

tp-backtest research pairs            # long/short pairs (strategy 7's real version)
tp-backtest research vrp              # buy-write and put-write on SPY, priced at the VIX
tp-backtest research orb              # opening-range breakout on stored 5-minute bars

Each prints the plan's tear sheet next to its benchmark. Nothing is saved: these are for
looking, before a strategy earns an engine version and the validation gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import pandas as pd
import typer

from tp_core import metrics
from tp_core.bars import close_matrix, load_bars, missing_symbols
from tp_core.config import Settings
from tp_core.intraday import load_intraday
from tp_core.macro import load_macro
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_strategies.library import orb, pairs, vrp
from tp_strategies.library.xs_momentum import LARGE_CAPS

app = typer.Typer(
    help="Vectorized research backtests (not on the event engine).", no_args_is_help=True
)

ROWS = [
    ("start", "Start", "str"),
    ("end", "End", "str"),
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("ann_volatility", "Volatility", "pct"),
    ("sharpe", "Sharpe", "num"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("skew", "Skew", "num"),
    ("worst_day", "Worst day", "pct"),
]


def table(returns: dict[str, pd.Series]) -> str:
    sheets = {name: metrics.tear_sheet(r) for name, r in returns.items()}
    width = max(14, *(len(n) + 2 for n in returns))
    lines = [f"{'':24}" + "".join(f"{n:>{width}}" for n in returns)]
    for key, label, kind in ROWS:
        cells = []
        for name in returns:
            s = sheets[name]
            value: Any = s.get(key) if hasattr(s, "get") else None
            if key == "worst_day":
                value = float(returns[name].min())
            cells.append(f"{_fmt(value, kind):>{width}}")
        lines.append(f"{label:24}" + "".join(cells))
    return "\n".join(lines)


def _fmt(value: Any, kind: str) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "—"
    if kind == "pct":
        return f"{float(value):.1%}"
    if kind == "num":
        return f"{float(value):.2f}"
    return str(value)


def _start(value: datetime | None) -> pd.Timestamp | None:
    return pd.Timestamp(value.date()) if value else None


StartOpt = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First session.")]


@app.command("pairs")
def research_pairs(
    start: StartOpt = None,
    entry: Annotated[float, typer.Option(help="Entry z-score.")] = 2.0,
    max_pairs: Annotated[int, typer.Option(help="Pairs held at most.")] = 10,
) -> None:
    """Long/short pairs on the large caps, same-sector cointegration, monthly re-formation."""
    settings = Settings()
    lake = Lake(settings.data_root)
    classifier = Classifier.load(settings.classifications_file)
    have = [s for s in [*LARGE_CAPS, "SPY"] if s not in missing_symbols(lake, [s])]
    if "SPY" not in have or len(have) < 3:
        typer.echo("needs daily bars for the large caps and SPY: tp-data bars backfill")
        raise typer.Exit(code=2)
    closes = close_matrix(load_bars(lake, have))
    spy = closes.pop("SPY").pct_change(fill_method=None).fillna(0.0)
    returns, trips = pairs.long_short_returns(
        closes, classifier.sectors.get, entry=entry, max_pairs=max_pairs
    )
    begin = _start(start) or returns.index[min(252, len(returns) - 1)]
    returns, spy = returns[returns.index >= begin], spy[spy.index >= begin]
    typer.echo(f"\nLong/short pairs: {trips} round trips\n")
    typer.echo(table({"pairs": returns, "SPY": spy}))
    typer.echo(f"\nCorrelation with SPY: {returns.corr(spy):.2f} (near 0 is the point)")


@app.command("vrp")
def research_vrp(
    start: StartOpt = None,
    moneyness: Annotated[float, typer.Option(help="0 at the money; 0.02 = 2% out.")] = 0.0,
    iv_scale: Annotated[float, typer.Option(help="ATM volatility as a share of the VIX.")] = 0.9,
) -> None:
    """Buy-write and put-write on SPY, options priced with Black-Scholes at the VIX."""
    settings = Settings()
    lake = Lake(settings.data_root)
    bars = load_bars(lake, ["SPY"])
    macro = load_macro(lake)
    if bars.empty or macro.empty or "VIXCLS" not in macro.columns:
        typer.echo("needs SPY bars and the VIX: tp-data bars backfill; tp-data macro backfill")
        raise typer.Exit(code=2)
    spy = bars.set_index(pd.DatetimeIndex(bars["session"]))
    if start:
        spy = spy[spy.index >= pd.Timestamp(start.date())]
    cfg = vrp.VrpConfig(moneyness=moneyness, iv_scale=iv_scale)
    rate = macro["DGS3MO"] if "DGS3MO" in macro.columns else None
    out = {
        kind: vrp.premium_returns(
            spy["adj_close"], spy["close"], macro["VIXCLS"], rate, kind=kind, cfg=cfg
        )
        for kind in ("buy-write", "put-write")
    }
    bench = spy["adj_close"].pct_change().fillna(0.0).rename("SPY")
    typer.echo("\nVolatility risk premium on SPY (model prices; compare with Cboe BXM and PUT)\n")
    typer.echo(table({k: v["returns"] for k, v in out.items()} | {"SPY": bench}))
    for kind, frame in out.items():
        sold = frame["premium"][frame["premium"] > 0]
        typer.echo(f"{kind}: {len(sold)} rolls, average premium {sold.mean():.2%} of the price")


@app.command("orb")
def research_orb(
    symbols: Annotated[str, typer.Option(help="Comma-separated symbols.")] = "SPY,QQQ",
    or_minutes: Annotated[int, typer.Option(help="Opening range length.")] = 30,
    stop: Annotated[str, typer.Option(help="range or mid.")] = "range",
    direction: Annotated[str, typer.Option(help="long or both.")] = "long",
) -> None:
    """Opening-range breakout on the stored 5-minute bars (tp-data bars intraday)."""
    settings = Settings()
    names = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    bars = load_intraday(Lake(settings.data_root), names)
    if bars.empty:
        typer.echo("no intraday bars: tp-data bars intraday --symbols " + ",".join(names))
        raise typer.Exit(code=2)
    cfg = orb.OrbConfig(or_minutes=or_minutes, stop=stop, direction=direction)
    trades = orb.orb_trades(bars, cfg)
    daily = orb.daily_returns(trades, bars)
    s = orb.summary(trades)
    typer.echo(f"\nOpening-range breakout, {or_minutes}-minute range, {len(daily)} sessions\n")
    typer.echo(table({"orb": daily}))
    if s.get("trades"):
        typer.echo(
            f"\n{s['trades']} trades, {s['hit_rate']:.0%} winners, average {s['avg_trade']:.2%} "
            f"({s['avg_win']:.2%} / {s['avg_loss']:.2%}), {s['avg_r']:+.2f}R, "
            f"{s['stopped']:.0%} stopped"
        )
