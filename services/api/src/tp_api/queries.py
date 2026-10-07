"""Everything the dashboard shows, as plain JSON-able dicts. No web framework in here, so each
view can be tested (and reused by other clients) on its own."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from tp_core import metrics
from tp_core.bars import close_matrix, load_bars, missing_symbols
from tp_core.calendar import NEW_YORK, last_completed_session, xnys
from tp_core.chains import load_chain_snapshots
from tp_core.config import Settings, Universes, load_universes
from tp_core.pnl import Fill, broker_trades, fill_trades, option_label, summarize
from tp_core.portfolio import (
    BENCHMARK_SYMBOLS,
    CASH_KIND,
    Classifier,
    benchmark_returns,
    concentration,
    exposures,
    external_flows,
    holdings_backtest,
    holdings_table,
    latest_snapshot,
    priced_weights,
    read_activities,
    time_weighted_returns,
    value_history,
)
from tp_core.storage import Lake, parquet_files
from tp_paper.store import PaperStore
from tp_strategies.ideas import ideas_from_lake
from tp_strategies.ma_timing import equal_weight_monthly, ma_timing

GTAA = ["SPY", "EFA", "IEF", "VNQ", "DBC"]
WEIGHT_EPS = 1e-9


# -- JSON helpers -------------------------------------------------------------------------------


def clean(value: Any) -> Any:
    """Make pandas/numpy values JSON-safe (NaN -> None, timestamps -> ISO strings)."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    if isinstance(value, pd.Timestamp | datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, str):  # numpy scalar
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [clean(r) for r in json.loads(df.to_json(orient="records", date_format="iso"))]


def _day(idx: object) -> str:
    return pd.Timestamp(str(idx)).date().isoformat()


def series_points(s: pd.Series, *, value: str = "value") -> list[dict[str, Any]]:
    return [{"date": _day(idx), value: clean(v)} for idx, v in s.items()]


def tear_sheet_dict(sheet: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {str(col): clean(sheet[col].to_dict()) for col in sheet.columns}


# -- context ------------------------------------------------------------------------------------


class Context:
    """Settings, lake and config loaded once per request."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lake = Lake(settings.data_root)
        self.classifier = Classifier.load(settings.classifications_file)
        try:
            self.universes: Universes = load_universes(settings.universes_file)
        except OSError:
            self.universes = Universes(bars=())

    def prices(self, symbols: list[str] | None = None) -> pd.DataFrame:
        bars = load_bars(self.lake, symbols) if symbols else load_bars(self.lake)
        return close_matrix(bars) if not bars.empty else pd.DataFrame()

    def last_closes(self, symbols: list[str]) -> dict[str, float]:
        """Latest raw close of each symbol that has bars."""
        present = [s for s in dict.fromkeys(symbols) if s not in missing_symbols(self.lake, [s])]
        if not present:
            return {}
        bars = load_bars(self.lake, present)
        last = bars.sort_values("session").groupby("symbol")["close"].last()
        return {str(k): float(v) for k, v in last.items()}

    def is_cash(self, symbol: str) -> bool:
        """The money-market sweep (e.g. SPAXX): cash, not a holding or a trade."""
        return self.classifier.funds.get(symbol) == "Cash"


# -- status -------------------------------------------------------------------------------------


def market_clock(now: datetime) -> dict[str, Any]:
    cal = xnys()
    ts = pd.Timestamp(now.astimezone(UTC))
    is_open = bool(cal.is_open_on_minute(ts.floor("min"), ignore_breaks=True))
    nxt_open = cal.next_open(ts)
    nxt_close = cal.next_close(ts)
    return {
        "now": now.astimezone(NEW_YORK).isoformat(),
        "is_open": is_open,
        "next_open": nxt_open.tz_convert(NEW_YORK).isoformat(),
        "next_close": nxt_close.tz_convert(NEW_YORK).isoformat(),
        "last_completed_session": last_completed_session(now).isoformat(),
    }


def _latest_json(directory: Any) -> dict[str, Any] | None:
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    if not files:
        return None
    payload: dict[str, Any] = json.loads(files[-1].read_text())
    payload.pop("issues", None)  # can be long; the summary counts are enough here
    payload["file"] = files[-1].name
    return payload


def status(ctx: Context, now: datetime) -> dict[str, Any]:
    bars = load_bars(ctx.lake)
    bars_info: dict[str, Any] = {"symbols": 0, "rows": 0, "latest_session": None}
    if not bars.empty:
        latest = bars["session"].max()
        expected = last_completed_session(now)
        bars_info = {
            "symbols": int(bars["symbol"].nunique()),
            "rows": len(bars),
            "first_session": bars["session"].min().isoformat(),
            "latest_session": latest.isoformat(),
            "stale": latest < expected,
            "expected_session": expected.isoformat(),
        }
    snap = latest_snapshot(ctx.lake)
    broker = [
        {
            "source": r["source"],
            "institution": r["institution"],
            "account": r["account_number_masked"] or r["account_name"],
            "synced_at": clean(r["taken_at"]),
        }
        for _, r in snap.accounts.iterrows()
    ]
    snapshots = parquet_files(ctx.lake.raw_dir("alpaca/option_chain_snapshots"))
    return {
        "market": market_clock(now),
        "bars": bars_info,
        "validation": _latest_json(ctx.lake.reports_dir("validation")),
        "options": {
            "files": len(snapshots),
            "latest_report": _latest_json(ctx.lake.reports_dir("options_snapshot")),
        },
        "broker": broker,
        "universe": {
            "bars": len(ctx.universes.bars),
            "options": len(ctx.universes.options_underlyings),
            "watchlist": list(ctx.universes.watchlist),
        },
    }


# -- market -------------------------------------------------------------------------------------


def bars_for(ctx: Context, symbol: str, days: int) -> dict[str, Any]:
    bars = load_bars(ctx.lake, [symbol.upper()])
    bars = bars.sort_values("session").tail(days)
    cols = ["session", "open", "high", "low", "close", "adj_close", "volume"]
    return {
        "symbol": symbol.upper(),
        "bars": records(bars[cols].rename(columns={"session": "date"})),
    }


def option_summary(ctx: Context, underlying: str) -> dict[str, Any]:
    """Latest snapshot: ATM IV and expected move per expiration, and the nearest monthly smile."""
    chain = load_chain_snapshots(ctx.lake, underlying.upper())
    if chain.empty:
        return {"underlying": underlying.upper(), "snapshot_at": None, "term": [], "smile": []}
    latest = chain[chain["snapshot_at"] == chain["snapshot_at"].max()].copy()
    spot = (
        float(latest["underlying_price"].dropna().iloc[0])
        if latest["underlying_price"].notna().any()
        else math.nan
    )
    latest["mid"] = (latest["bid"] + latest["ask"]) / 2
    term = []
    for expiration, g in latest.groupby("expiration"):
        if math.isnan(spot):
            break
        strike = g.iloc[(g["strike"] - spot).abs().argsort()]["strike"].iloc[0]
        atm = g[g["strike"] == strike]
        straddle = atm["mid"].sum() if len(atm) == 2 else math.nan
        term.append(
            {
                "expiration": expiration.isoformat(),
                "dte": int(g["dte"].iloc[0]),
                "atm_strike": float(strike),
                "atm_iv": clean(float(atm["implied_volatility"].mean())),
                "expected_move": clean(float(straddle)),
                "expected_move_pct": clean(float(straddle / spot)) if spot else None,
                "contracts": len(g),
            }
        )
    monthly = [t for t in term if t["dte"] >= 20] or term
    smile: list[dict[str, Any]] = []
    if monthly and not math.isnan(spot):
        target = date.fromisoformat(monthly[0]["expiration"])
        g = latest[latest["expiration"] == target]
        # out-of-the-money side only: the liquid quotes, and the usual way a smile is drawn
        otm = {"C": g["strike"] >= spot, "P": g["strike"] < spot}
        for right in ("C", "P"):
            side = g[(g["right"] == right) & otm[right] & g["implied_volatility"].notna()]
            smile += [
                {"strike": float(k), "iv": clean(float(v)), "right": right}
                for k, v in zip(side["strike"], side["implied_volatility"], strict=True)
            ]
    return {
        "underlying": underlying.upper(),
        "snapshot_at": clean(latest["snapshot_at"].iloc[0]),
        "spot": clean(spot),
        "term": term,
        "smile": smile,
    }  # fmt: skip


# -- portfolio ----------------------------------------------------------------------------------


def portfolio(ctx: Context, now: datetime | None = None) -> dict[str, Any]:
    snap = latest_snapshot(ctx.lake)
    if snap.empty:
        return {"synced": False, "accounts": [], "holdings": []}
    today = (now or datetime.now(UTC)).astimezone(NEW_YORK).date()
    table = _with_option_terms(ctx, holdings_table(snap, ctx.classifier, today))
    priced = set(ctx.prices().columns)
    columns = ["source", "account_name", "account_number_masked", "institution", "cash"]
    accounts = snap.accounts[[*columns, "total_value", "taken_at"]]
    return {
        "synced": True,
        "total_value": snap.total_value,
        "cash": float(snap.accounts["cash"].fillna(0).sum()),
        "accounts": records(accounts),
        "holdings": records(table.assign(priced=table["symbol"].isin(priced))),
        "by_sector": clean(exposures(table, "sector").to_dict()),
        "by_asset_class": clean(exposures(table, "asset_class").to_dict()),
        "concentration": clean(concentration(table)),
        "priced_share": float(priced_weights(table, priced).sum()),
        "pnl": clean(_pnl(ctx, table, snap.total_value, today)),
    }


def _with_option_terms(ctx: Context, table: pd.DataFrame) -> pd.DataFrame:
    """Per-share premium and cost (as brokers quote them), breakeven and moneyness for options."""
    out = table.assign(
        label=table["symbol"],
        premium=math.nan,
        cost_premium=math.nan,
        underlying_price=math.nan,
        breakeven=math.nan,
        moneyness=math.nan,
    )
    out["expiration"] = [d.isoformat() if isinstance(d, date) else None for d in out["expiration"]]
    options = out["kind"] == "option"
    if not options.any():
        return out
    closes = ctx.last_closes([str(u) for u in out.loc[options, "underlying"].dropna()])
    for i in out.index[options]:
        row = out.loc[i]
        multiplier = float(row["multiplier"]) or 1.0
        premium = float(row["price"]) / multiplier
        cost = float(row["cost_basis_per_unit"]) / multiplier
        strike, call = float(row["strike"]), row["right"] == "C"
        spot = closes.get(str(row["underlying"]), math.nan)
        out.loc[i, ["label", "premium", "cost_premium", "underlying_price"]] = [
            option_label(str(row["symbol"])) or row["symbol"],
            premium,
            cost,
            spot,
        ]
        out.loc[i, "breakeven"] = strike + cost if call else strike - cost
        out.loc[i, "moneyness"] = (
            (spot / strike - 1.0) * (1 if call else -1) if strike else math.nan
        )
    return out


def _pnl(ctx: Context, table: pd.DataFrame, value: float, today: date) -> dict[str, Any]:
    """Unrealized (open positions vs their cost), realized (closed trades in the synced history),
    income and fees: how much the account is up or down."""
    held = table[(table["kind"] != CASH_KIND) & table["unrealized_pnl"].notna()]
    unrealized = float(held["unrealized_pnl"].sum())
    cost = float((held["market_value"] - held["unrealized_pnl"]).abs().sum())
    acts = read_activities(ctx.lake)
    summary = summarize(broker_trades(acts, is_cash=ctx.is_cash), acts, today)
    total = unrealized + summary.realized + summary.income - summary.fees
    return {
        **summary.to_dict(),
        "unrealized": unrealized,
        "unrealized_pct": unrealized / cost if cost else None,
        "total": total,
        "value": value,
        # Only meaningful when the synced history starts with the account's first deposit.
        "value_minus_deposits": value - summary.net_deposits if summary.net_deposits else None,
    }


# -- trades -------------------------------------------------------------------------------------


def trades(ctx: Context, source: str = "all", limit: int = 200) -> dict[str, Any]:
    """Every trade on one list: yours (brokerage activity) and the paper bot's fills, newest
    first, each with the P&L it realized; plus totals for both."""
    rows: list[dict[str, Any]] = []
    today = datetime.now(UTC).astimezone(NEW_YORK).date()
    mine = paper = None
    if source in ("all", "mine"):
        acts = read_activities(ctx.lake)
        broker = broker_trades(acts, is_cash=ctx.is_cash)
        labels = _account_labels(ctx)
        for t in broker.to_dict("records"):
            rows.append(
                {
                    "date": t["trade_date"],
                    "source": "broker",
                    "account": labels.get(str(t["account_id"]), "Brokerage"),
                    "strategy": None,
                    **{k: t[k] for k in ("symbol", "label", "kind", "action", "quantity")},
                    **{k: t[k] for k in ("price", "amount", "fee", "realized_pnl", "cost_known")},
                }
            )
        mine = summarize(broker, acts, today).to_dict()
    if source in ("all", "paper"):
        paper_rows, paper = _paper_trades(ctx)
        rows += paper_rows
    rows.sort(key=lambda r: str(r["date"]), reverse=True)
    return {"trades": clean(rows[:limit]), "mine": clean(mine), "paper": clean(paper)}


def _account_labels(ctx: Context) -> dict[str, str]:
    snap = latest_snapshot(ctx.lake)
    out = {}
    for _, a in snap.accounts.iterrows():
        name = a["account_number_masked"] or a["account_name"] or ""
        out[str(a["account_id"])] = f"{a['institution'] or a['source']} {name}".strip()
    return out


def _paper_trades(ctx: Context) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    path = ctx.settings.data_root / "state" / "paper.sqlite"
    if not path.exists():
        return [], None
    store = PaperStore(path)
    fills = [
        Fill(f.strategy, f.symbol, f.side, f.quantity, f.price, f.fees,
             date.fromisoformat(f.session), f.id)
        for f in store.fills()
    ]  # fmt: skip
    rows = []
    realized: list[float] = []
    for r in fill_trades(fills):
        f: Fill = r["fill"]
        units = f.quantity if f.side == "buy" else -f.quantity
        if r["realized_pnl"] is not None:
            realized.append(r["realized_pnl"])
        rows.append(
            {
                "date": f.day,
                "source": "paper",
                "account": f"Paper · {f.book}",
                "strategy": f.book,
                "symbol": f.symbol,
                "label": f.symbol,
                "kind": "equity",
                "action": "Buy" if f.side == "buy" else "Sell",
                "quantity": f.quantity,
                "price": f.price,
                "amount": -units * f.price - f.fees,
                "fee": f.fees,
                "realized_pnl": r["realized_pnl"],
                "cost_known": True,
            }
        )
    latest = {d.strategy: d for d in store.sleeve_days()}  # sorted by session: the last wins
    sleeves = {name: d.equity - d.capital for name, d in latest.items() if d.capital is not None}
    summary = {
        "realized": sum(realized),
        "closed_trades": len(realized),
        "winners": sum(1 for v in realized if v > 0),
        "win_rate": sum(1 for v in realized if v > 0) / len(realized) if realized else None,
        "fills": len(rows),
        # Each sleeve's equity at its last recorded close less its capital: everything it has
        # made or lost, open positions included.
        "pnl": sum(sleeves.values()) if sleeves else None,
        "by_strategy": sleeves,
    }
    return rows, summary


def performance(ctx: Context, days: int) -> dict[str, Any]:
    """Current-holdings backtest and account TWR, each next to the benchmarks."""
    snap = latest_snapshot(ctx.lake)
    prices = ctx.prices()
    out: dict[str, Any] = {"holdings_backtest": None, "account": None}
    if prices.empty:
        return out
    prices = prices[prices.index >= prices.index[-1] - timedelta(days=days)]
    bench = benchmark_returns(prices[[c for c in prices.columns if c in BENCHMARK_SYMBOLS]])
    if not snap.empty:
        table = holdings_table(snap, ctx.classifier)
        weights = priced_weights(table, set(prices.columns))
        if weights.sum() > 0:
            port = holdings_backtest(weights / weights.sum(), prices)
            frame = pd.concat([port, bench], axis=1).dropna()
            out["holdings_backtest"] = {
                "coverage": float(weights.sum()),
                "growth": _growth(frame),
                "stats": tear_sheet_dict(_compare(frame)),
            }
    values = value_history(ctx.lake)
    if len(values) >= 2:
        twr = time_weighted_returns(values, external_flows(ctx.lake)).rename("account")
        frame = pd.concat([twr, bench.reindex(twr.index)], axis=1).dropna()
        out["account"] = {
            "values": series_points(values),
            "growth": _growth(frame) if len(frame) else [],
            "stats": tear_sheet_dict(_compare(frame)) if len(frame) > 2 else {},
        }
    return out


def _growth(returns: pd.DataFrame) -> list[dict[str, Any]]:
    wealth = (1 + returns).cumprod()
    return [{"date": _day(idx), **clean(row.to_dict())} for idx, row in wealth.iterrows()]


def _compare(returns: pd.DataFrame) -> pd.DataFrame:
    sheet = metrics.tear_sheet(returns)
    assert isinstance(sheet, pd.DataFrame)
    keep = ["cagr", "ann_volatility", "sharpe", "max_drawdown", "total_return"]
    sheet = sheet.loc[keep]
    spy = "S&P 500 (SPY)"
    if spy in returns.columns:
        sheet.loc["beta_to_spy"] = [metrics.beta(returns[c], returns[spy]) for c in returns]
    return sheet


# -- strategies and ideas -----------------------------------------------------------------------


def ma_timing_view(ctx: Context, universe: str) -> dict[str, Any]:
    symbols = GTAA if universe == "gtaa" else ["SPY"]
    prices = ctx.prices()
    if prices.empty or not set(symbols) <= set(prices.columns):
        return {"universe": universe, "available": False}
    px = prices[symbols].dropna()
    timing = ma_timing(px, cost_bps=5.0)
    if timing.returns.empty:
        return {"universe": universe, "available": False}
    bench = equal_weight_monthly(px, start_after=timing.signals.index[9], cost_bps=5.0)
    frame = pd.concat(
        [timing.returns.rename("10-month MA timing"), bench.returns.rename("Buy and hold")], axis=1
    ).dropna()
    weekly = (1 + frame).cumprod().resample("W-FRI").last()
    # entries and exits only; the monthly drift rebalances in between are noise on a dashboard
    t = timing.trades
    switches = t[(t["weight_before"] < WEIGHT_EPS) | (t["weight_after"] < WEIGHT_EPS)]
    trades = switches.tail(12).assign(date=lambda d: d["date"].dt.date.astype(str))
    return {
        "universe": universe,
        "available": True,
        "symbols": symbols,
        "growth": [{"date": _day(i), **clean(r.to_dict())} for i, r in weekly.iterrows()],
        "stats": tear_sheet_dict(_compare(frame)),
        "exposure_now": clean(float(timing.exposure.iloc[-1])),
        "trades": records(trades),
        "signals": clean(timing.signals.iloc[-1].to_dict()),
    }


def ideas(ctx: Context) -> dict[str, Any]:
    report = ideas_from_lake(ctx.lake, ctx.classifier)
    return report.to_dict()
