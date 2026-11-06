"""``tp-backtest research``: the vectorized research backtests that don't run on the event engine.

tp-backtest research pairs            # long/short pairs (strategy 7's real version)
tp-backtest research vrp              # buy-write and put-write on SPY, priced at the VIX
tp-backtest research orb              # opening-range breakout on stored 5-minute bars
tp-backtest research open-ema         # the first 5-minute candle against the 12 EMA, and which
                                      # tickers to trade it on
tp-backtest research stress-dip-calls # strategy 16: 1-3 session calls on index-fund dips when
                                      # the VIX says the market is stressed

Each prints the plan's tear sheet next to its benchmark (open-ema and stress-dip-calls print
their studies and keep them under reports/research/). These are for looking, before a strategy
earns an engine version and the validation gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

import pandas as pd
import typer

from tp_core import metrics, option_history
from tp_core.bars import close_matrix, load_bars, missing_symbols
from tp_core.config import Settings, load_universes
from tp_core.intraday import intraday_symbols, load_intraday
from tp_core.macro import load_macro
from tp_core.portfolio import Classifier
from tp_core.reference import LakeReference
from tp_core.storage import Lake, write_json
from tp_core.volatility import iv_history
from tp_strategies.library import open_ema, orb, pairs, stress_dip, vrp
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


INDEX_FUNDS = ("QQQ", "TQQQ", "SPY", "IWM", "DIA")
HEAD = (
    f"{'':46}{'trades':>7}{'won':>6}{'mean bp':>9}{'median':>8}{'avg R':>7}{'Sharpe':>8}"
    f"{'a year':>8}{'max DD':>8}"
)


def _row(name: str, st: dict[str, Any]) -> str:
    if not st.get("trades"):
        return f"{name[:45]:46}{'no trades':>16}"
    r = f"{st['avg_r']:+.2f}" if st.get("avg_r") is not None else "—"
    sharpe = f"{st['sharpe']:.2f}" if st.get("sharpe") is not None else "—"
    return (
        f"{name[:45]:46}{st['trades']:>7}{st['win_rate']:>6.0%}{st['mean_bps']:>+9.1f}"
        f"{st['median_bps']:>+8.1f}{r:>7}{sharpe:>8}{st['annual']:>+8.0%}{st['max_drawdown']:>8.1%}"
    )


def open_ema_report(out: dict[str, Any]) -> str:
    """The study in words and tables (``open_ema.study``)."""
    t = out["tickers"]
    lines = [
        f"First candle against the EMA: {out['config']}",
        f"{out['symbols']} symbols, {out['sessions']} sessions, {out['start']} to {out['end']}; "
        f"test: the sessions from {out['test_from']} (no choice below looked at them)",
        "",
        HEAD,
        *(_row(f"the rule, {k}", v) for k, v in out["rule"].items()),
        "",
        "Against the controls (test): does the EMA add anything?",
        _row("the rule", out["rule"]["test"]),
        *(_row(k, v["test"]) for k, v in out["controls"].items()),
        "",
        f"Exits chosen on the training sessions: {out['exits']['chosen']}",
        _row("chosen exits, train", out["exits"]["train"]),
        _row("chosen exits, test", out["exits"]["test"]),
        "",
        "Other EMA and candle lengths (test; robustness, not a choice)",
        *(_row(k, v) for k, v in out["lengths"].items()),
        "",
        "Costs each way (test)",
        *(_row(k, v) for k, v in out["costs"].items()),
        "",
        "Long and short (test)",
        *(_row(k, v["test"]) for k, v in out["sides"].items()),
        "",
        "Picking the tickers (test)",
        _row("every name", t["every name"]),
    ]
    best = t.get("best quarter on train")
    if best:
        lines += [
            _row("the best quarter on train", best["test"]),
            _row("the other three quarters", best["the rest"]),
            f"    (the best quarter on train: {', '.join(best['symbols'])})",
        ]
    lines += [_row(k, v) for k, v in t.items() if k.startswith("top ")]
    lines += [
        _row(f"relative volume {k}", v)
        for k, v in (t.get("relative volume quintiles") or {}).items()
    ]
    lines += [_row(k, v) for k, v in t.items() if k.startswith("gap of")]
    per = t["per_symbol"]
    funds = [k for k in INDEX_FUNDS if k in per]
    if funds:
        lines += ["", "The index funds (QQQ is the Nasdaq-100): train, then test"]
        for k in funds:
            lines += [_row(f"{k}, train", per[k]["train"]), _row(f"{k}, test", per[k]["test"])]
    ranked = sorted(
        (k for k in per if per[k]["test"].get("trades")),
        key=lambda k: -(per[k]["test"].get("sharpe") or -99.0),
    )
    lines += ["", "Each ticker (test), best and worst by Sharpe; train Sharpe after the name"]
    shown = ranked[:8] + [k for k in ranked[-8:] if k not in ranked[:8]]
    for k in shown:
        train = per[k]["train"].get("sharpe")
        tag = f"{k} ({train:+.1f})" if train is not None else k
        lines.append(_row(tag, per[k]["test"]))
    lines += ["", "By year (all sessions)", *(_row(k, v) for k, v in out["by_year"].items())]
    lines += [
        "",
        "Mean and median per trade in basis points after costs; avg R in multiples of the risk to "
        "the stop; Sharpe, return a year and drawdown for an account split equally among each "
        "day's trades. Shorts assume free borrowing. A backtest, not a promise.",
    ]
    return "\n".join(lines)


@app.command("open-ema")
def research_open_ema(
    symbols: Annotated[
        str | None, typer.Option(help="Comma-separated; default: every symbol with 5-minute bars.")
    ] = None,
    side: Annotated[str, typer.Option(help="both, long or short.")] = "both",
    ema: Annotated[int, typer.Option(help="EMA length: 9, 12, 20 or 26.")] = 12,
    candle: Annotated[int, typer.Option(help="Opening candle in minutes: 5 or 15.")] = 5,
    cost: Annotated[float, typer.Option(help="Cost each way in basis points.")] = 1.0,
    as_json: Annotated[bool, typer.Option("--json", help="Print the study as JSON.")] = False,
) -> None:
    """The first 5-minute candle against the 12 EMA (long above, short below, flat by the
    close) on the stored 5-minute bars: the rule against its controls, exits chosen on the
    earlier sessions and judged on the later, other lengths, costs, and which tickers to trade
    it on. Get the bars first: tp-data bars intraday --universe options --days 1000 --resume."""
    settings = Settings()
    lake = Lake(settings.data_root)
    names = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else intraday_symbols(lake)
    )
    bars = load_intraday(lake, names) if names else pd.DataFrame()
    if bars.empty:
        typer.echo(
            "no intraday bars: tp-data bars intraday --universe options --days 1000 --resume "
            "(or --symbols QQQ,SPY)"
        )
        raise typer.Exit(code=2)
    try:
        cfg = open_ema.Config(ema=ema, candle=candle, side=side, cost_bps=cost)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    out = open_ema.study(open_ema.sessions_from_bars(bars), cfg)
    path = write_json(
        out, lake.reports_dir("research") / f"open_ema-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    )
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    typer.echo(open_ema_report(out))
    typer.echo(f"\n(kept in {path})")


# -- strategy 16 ----------------------------------------------------------------------------------

SD_HEAD = f"{'':52}{'n':>6}{'mean':>8}{'median':>8}{'won':>6}{'worst 10%':>11}"
UD_HEAD = (
    f"{'':52}{'n':>6}{'1 day':>8}{'2 days':>8}{'3 days':>8}{'up 3d':>7}{'z 3d':>7}{'vs all':>8}"
)


def _sd_row(name: str, st: dict[str, Any] | None) -> str:
    if not st or not st.get("n"):
        return f"{name[:51]:52}{'none':>6}"
    return (
        f"{name[:51]:52}{st['n']:>6}{st['mean']:>+8.1%}{st['median']:>+8.1%}{st['win']:>6.0%}"
        f"{st['q10']:>+11.0%}"
    )


def _ud_row(name: str, st: dict[str, Any]) -> str:
    if not st.get("n"):
        return f"{name[:51]:52}{'none':>6}"
    excess = f"{st['excess3']:+.2%}" if st.get("excess3") is not None else "—"
    return (
        f"{name[:51]:52}{st['n']:>6}{st['mean1']:>+8.2%}{st['mean2']:>+8.2%}{st['mean3']:>+8.2%}"
        f"{st['hit3']:>7.0%}{st['z3']:>+7.2f}{excess:>8}"
    )


def stress_dip_report(out: dict[str, Any]) -> str:
    """The study in words and tables (``stress_dip.study``)."""
    cfg = out.get("config") or {}
    lines = [
        "Stress-dip calls (strategy 16): " + out["rule"],
        f"Exits: {cfg.get('exits')}; VIX timing: {cfg.get('vix_timing')} (config/stress_dip.toml)",
        f"{out['symbols']} symbols, {out['start']} to {out['end']}; test: the sessions from "
        f"{out['test_from']} (no choice below looked at them)",
        "",
        "The underlying after the signal close (z: in units of the name's daily volatility, less "
        "its average day; vs all: the 3-day mean over every day's)",
    ]
    for table in out["underlying"].values():
        for label, parts in table.items():
            for part in ("train", "test"):
                lines += ["", f"{label}, {part}", UD_HEAD]
                lines += [_ud_row(sig, st) for sig, st in parts[part].items()]
    m = out["modeled"]
    spec = m["spec"]
    lines += [
        "",
        f"Calls, modeled (Black-Scholes at IV = the fund's ratio x the VIX; QQQ, SPY, IWM): "
        f"{spec['model']}",
        SD_HEAD,
    ]
    for cost, parts in spec["costs"].items():
        lines += [
            _sd_row(f"{cost} round trip, train", parts["train"]),
            _sd_row(f"{cost} round trip, test", parts["test"]),
        ]
    for label in ("without the stress filter, 2%", "as traded (1 a day, 2 open), 2%"):
        lines += [
            _sd_row(f"{label}, train", spec[label]["train"]),
            _sd_row(f"{label}, test", spec[label]["test"]),
        ]
    lines += ["", f"Expiry and delta, exits chosen on train, after {m['cost']:.0%} (test)", SD_HEAD]
    for label, g in m["grid"].items():
        lines.append(_sd_row(f"{label}: chosen", g["test"]))
        lines.append(_sd_row(f"{label}: strategy's exits", g["spec_test"]))
        lines.append(_sd_row(f"{label}: every day (control)", g["control_test"]))
        lines.append(f"    chosen on train: {g['chosen']}")
    lines += ["", "Other profit targets, the rest as configured (21 days, delta 0.70, 2%)", SD_HEAD]
    for label, parts in m["targets"].items():
        lines += [
            _sd_row(f"{label}, train", parts["train"]),
            _sd_row(f"{label}, test", parts["test"]),
        ]
    real = out.get("real")
    if real:
        lines += [
            "",
            f"Calls, real prices (option_bars_1d trades, {real['start']} to {real['end']}; strike "
            "93-100% of spot, 10-60 days; the strategy's exits; one row per fund and session)",
            SD_HEAD,
        ]
        for group, table in real["groups"].items():
            for sig, parts in table.items():
                for part in ("all", "test"):
                    lines.append(_sd_row(f"{group}, {sig}, {part}, gross", parts[part]["gross"]))
                    lines.append(_sd_row(f"{group}, {sig}, {part}, 2%", parts[part]["2%"]))
        for label in ("index_by_dte", "index_by_symbol"):
            if real.get(label):
                lines += ["", f"Index funds, stressed dips, by {label.split('_')[-1]} (gross)"]
                lines += [_sd_row(k, v) for k, v in real[label].items()]
    by_vix = out.get("real_by_vix") or {}
    if by_vix:
        lines += [
            "",
            "Real prices by VIX timing, the strategy's exits, after 2% (live: the session's VIX "
            "close standing in for 15:45; prev: the previous close)",
            SD_HEAD,
        ]
        for timing, table in by_vix.items():
            for group in ("index (QQQ, SPY, IWM, DIA)", "every fund"):
                for sig in ("dip, stressed", "every day (control)"):
                    parts = ((table.get("groups") or {}).get(group) or {}).get(sig)
                    if parts:
                        for part in ("all", "test"):
                            lines.append(
                                _sd_row(f"{timing}: {group}, {sig}, {part}", parts[part]["2%"])
                            )
    late = out.get("at_1545")
    if late:
        a = late["agreement"]
        lines += [
            "",
            f"At 15:45 (5-minute bars, {late['start']} to {late['end']}): signals at both "
            f"{a['both']}, only at 15:45 {a['only at 15:45']}, only at the close "
            f"{a['only at the close']}. Calls: {late['model']}, bought and sold at 15:45 prices",
            SD_HEAD,
        ]
        for label in ("signal at 15:45", "signal at the close", "every day (control)"):
            calls = late[label]["calls"]
            for part in ("train", "test"):
                lines.append(_sd_row(f"{label}, {part}, gross", calls[part]["gross"]))
                lines.append(_sd_row(f"{label}, {part}, 2%", calls[part]["2%"]))
        for cost, st in (late["signal at 15:45"].get("as_traded_test") or {}).items():
            lines.append(_sd_row(f"15:45, as traded, test, {cost}", st))
        lines += [
            "",
            "The underlying from 15:45 to the later 15:45s",
            UD_HEAD.replace("vs all", ""),
        ]
        for label in ("signal at 15:45", "every day (control)"):
            for part, st in late[label]["underlying"].items():
                if st["n"]:
                    lines.append(
                        f"{label + ', ' + part:52}{st['n']:>6}{st['mean1']:>+8.2%}"
                        f"{st['mean2']:>+8.2%}{st['mean3']:>+8.2%}{st['hit3']:>7.0%}"
                    )
    lines += ["", "By year (modeled, the strategy's exits, 2%)", SD_HEAD]
    lines += [_sd_row(k, v) for k, v in out["by_year"].items()]
    lines += [
        "",
        "Returns are on the premium paid, per trade. Modeled prices are generous (compare each "
        "row with the every-day control); real prices are trade prints, not quotes, so fills pay "
        "the spread on top (the cost rows). A backtest, not a promise.",
    ]
    return "\n".join(lines)


@app.command("stress-dip-calls")
def research_stress_dip(
    as_json: Annotated[bool, typer.Option("--json", help="Print the study as JSON.")] = False,
    intraday: Annotated[
        bool, typer.Option(help="Re-check the trigger at 15:45 on the 5-minute bars.")
    ] = True,
) -> None:
    """Strategy 16: calls 14-30 days out bought in the last 15 minutes when an index fund dips in
    an uptrend and the VIX says the market is stressed, sold by the exits and with the VIX timing
    in config/stress_dip.toml (default: +20%, -40%, by session 5; the live VIX). The underlying
    against every day, with and without the stress filter; modeled calls across expiries and
    deltas, exits chosen on the earlier sessions; real option prices; costs; and the trigger
    worked out again at 15:45. Needs daily bars and the VIX; the real-price check needs
    tp-data options history, the 15:45 one tp-data bars intraday --symbols QQQ,SPY,IWM,DIA."""
    settings = Settings()
    lake = Lake(settings.data_root)
    classifier = Classifier.load(settings.classifications_file)
    macro = load_macro(lake)
    if macro.empty or "VIXCLS" not in macro.columns:
        typer.echo("needs the VIX: tp-data macro backfill")
        raise typer.Exit(code=2)
    universes = load_universes(settings.universes_file)
    wanted = list(dict.fromkeys([*stress_dip.INDEX, *universes.options_underlyings]))
    have = [s for s in wanted if s not in missing_symbols(lake, [s]) and s not in stress_dip.SKIP]
    if not set(stress_dip.TIER1) <= set(have):
        typer.echo("needs daily bars for QQQ, SPY and IWM: tp-data bars backfill")
        raise typer.Exit(code=2)
    frames = {}
    for symbol, g in load_bars(lake, have).groupby("symbol"):
        bars = g.set_index(pd.DatetimeIndex(g["session"]))
        frames[str(symbol)] = stress_dip.daily(
            pd.DataFrame(
                {
                    "open": bars["adj_open"],
                    "high": bars["adj_high"],
                    "low": bars["adj_low"],
                    "close": bars["adj_close"],
                    "raw_close": bars["close"],
                }
            )
        )
    vix = macro["VIXCLS"]
    rate = (macro["DGS3MO"].dropna() / 100) if "DGS3MO" in macro.columns else 0.04
    try:
        table = LakeReference(lake).earnings_table()
        earnings = {str(s): list(pd.to_datetime(g["session"])) for s, g in table.groupby("symbol")}
    except LookupError:
        earnings = {}
    bars_5m = (
        load_intraday(lake, [s for s in stress_dip.INDEX if s in intraday_symbols(lake)])
        if intraday
        else None
    )
    out = stress_dip.study(
        frames,
        vix,
        rate=rate,
        ratios=stress_dip.iv_ratios(iv_history(lake), vix),
        funds=list(classifier.funds),
        earnings=earnings,
        option_bars=option_history.load_bars(lake),
        contracts=option_history.load_matches(lake),
        intraday=bars_5m,
        config=stress_dip.load_config(settings.stress_dip_file),
    )
    path = write_json(
        out, lake.reports_dir("research") / f"stress_dip-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    )
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    typer.echo(stress_dip_report(out))
    typer.echo(f"\n(kept in {path})")
