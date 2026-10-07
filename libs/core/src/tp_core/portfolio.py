"""Brokerage portfolio: read the synced snapshots and measure them against benchmarks.

Everything here is read-only analysis of what ``tp-broker sync`` stored in the lake. Two kinds of
performance are reported, and they answer different questions:

- **Account performance** (time-weighted return from daily snapshots, net of deposits and
  withdrawals): how the account actually did. History starts on the first sync.
- **Current-holdings backtest** (today's weights held constant over past prices): how the
  portfolio you hold *now* would have behaved. Available immediately, but it ignores every trade
  you made, so it describes the mix, not your track record.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tp_core import metrics
from tp_core.occ import OccSymbol, parse_occ
from tp_core.pnl import FLOW_IN, FLOW_OUT, OPTION_MULTIPLIER
from tp_core.schemas import (
    RAW_BROKER_ACCOUNTS,
    RAW_BROKER_ACCOUNTS_SCHEMA,
    RAW_BROKER_ACTIVITIES,
    RAW_BROKER_ACTIVITIES_SCHEMA,
    RAW_BROKER_HOLDINGS,
    RAW_BROKER_HOLDINGS_SCHEMA,
)
from tp_core.storage import Lake, read_parquet_dir

# Holdings we can price from our own daily bars (Alpaca covers US-listed stocks and ETFs).
MARKET_KINDS = frozenset({"stock", "etf", "adr", "cef"})
CASH_KIND = "cash"

BENCHMARKS: dict[str, dict[str, float]] = {
    "S&P 500 (SPY)": {"SPY": 1.0},
    "Nasdaq 100 (QQQ)": {"QQQ": 1.0},
    "60/40 (SPY/IEF)": {"SPY": 0.6, "IEF": 0.4},
}
BENCHMARK_SYMBOLS = frozenset(s for weights in BENCHMARKS.values() for s in weights)


# -- classification -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    sector: str
    asset_class: str


@dataclass(frozen=True)
class Classifier:
    sectors: dict[str, str]
    funds: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> Classifier:
        """Load ``path`` plus, if present, ``<name>.local.toml`` next to it (git-ignored), whose
        entries win. Put your own holdings there so they never land in the public repo."""
        sectors: dict[str, str] = {}
        funds: dict[str, str] = {}
        for candidate in (path, path.with_name(f"{path.stem}.local.toml")):
            if not candidate.exists():
                continue
            with candidate.open("rb") as f:
                raw = tomllib.load(f)
            sectors.update(raw.get("sectors", {}))
            funds.update(raw.get("funds", {}))
        return cls(sectors=sectors, funds=funds)

    def classify(self, symbol: str, kind: str) -> Classification:
        if kind == CASH_KIND or self.funds.get(symbol) == "Cash":
            return Classification("Cash", "Cash")
        if symbol in self.funds:
            return Classification(f"Fund: {self.funds[symbol]}", self.funds[symbol])
        if symbol in self.sectors:
            return Classification(self.sectors[symbol], "US Equity")
        if kind == "option":
            return Classification("Options", "Options")
        if kind == "crypto":
            return Classification("Crypto", "Crypto")
        if kind in {"etf", "mutualfund", "cef"}:
            return Classification("Unclassified fund", "Unclassified")
        return Classification("Unclassified", "Unclassified")


# -- reading snapshots --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioSnapshot:
    """The latest sync of every account, combined."""

    accounts: pd.DataFrame  # one row per account
    holdings: pd.DataFrame  # one row per account x holding

    @property
    def empty(self) -> bool:
        return self.accounts.empty

    @property
    def total_value(self) -> float:
        return float(account_totals(self.accounts, self.holdings).sum())


def read_accounts(lake: Lake) -> pd.DataFrame:
    return read_parquet_dir(lake.raw_dir(RAW_BROKER_ACCOUNTS), RAW_BROKER_ACCOUNTS_SCHEMA)


def read_holdings(lake: Lake) -> pd.DataFrame:
    return read_parquet_dir(lake.raw_dir(RAW_BROKER_HOLDINGS), RAW_BROKER_HOLDINGS_SCHEMA)


def read_activities(lake: Lake) -> pd.DataFrame:
    raw = read_parquet_dir(lake.raw_dir(RAW_BROKER_ACTIVITIES), RAW_BROKER_ACTIVITIES_SCHEMA)
    if raw.empty:
        return raw
    # Activities are re-fetched with overlap; the latest copy of each one wins.
    raw = raw.sort_values("ingested_at")
    return raw.drop_duplicates(["source", "activity_id"], keep="last").reset_index(drop=True)


def latest_snapshot(lake: Lake) -> PortfolioSnapshot:
    accounts = read_accounts(lake)
    holdings = read_holdings(lake)
    if accounts.empty:
        return PortfolioSnapshot(accounts, holdings)
    key = ["source", "account_id"]
    newest = accounts.groupby(key)["taken_at"].transform("max")
    accounts = accounts[accounts["taken_at"] == newest].reset_index(drop=True)
    if not holdings.empty:
        holdings = holdings.merge(accounts[[*key, "taken_at"]], on=[*key, "taken_at"])
    return PortfolioSnapshot(accounts, holdings.reset_index(drop=True))


def account_totals(accounts: pd.DataFrame, holdings: pd.DataFrame) -> pd.Series:
    """Account value: the broker's total if it reports one, else holdings + cash."""
    key = ["source", "account_id", "taken_at"]
    if holdings.empty:
        held = pd.Series(0.0, index=pd.MultiIndex.from_frame(accounts[key]))
    else:
        held = holdings.groupby(key)["market_value"].sum(min_count=1).fillna(0.0)
    totals = []
    for _, row in accounts.iterrows():
        reported = row["total_value"]
        if pd.notna(reported):
            totals.append(float(reported))
            continue
        k = (row["source"], row["account_id"], row["taken_at"])
        cash = float(row["cash"]) if pd.notna(row["cash"]) else 0.0
        totals.append(float(held.get(k, 0.0)) + cash)
    return pd.Series(totals, index=accounts.index, dtype=float)


def held_symbols(lake: Lake) -> list[str]:
    """Market-priced symbols in the latest snapshot, and the underlyings of options held (to add
    to the daily bars ingest and the live quotes)."""
    snap = latest_snapshot(lake)
    if snap.holdings.empty:
        return []
    h = snap.holdings
    priced = h[h["kind"].isin(MARKET_KINDS)]["symbol"]
    underlyings = h[h["kind"] == "option"]["underlying"]
    return sorted({str(s).upper() for s in pd.concat([priced, underlyings]).dropna()})


# -- current holdings ---------------------------------------------------------------------------


def holdings_table(
    snap: PortfolioSnapshot, classifier: Classifier, today: date | None = None
) -> pd.DataFrame:
    """One row per symbol across accounts, plus a cash row, with weights and classification.

    Options are per contract (``price`` and cost x the multiplier, so quantity x price is the
    value), with the contract's terms parsed out and, given ``today``, the days to expiry.
    """
    columns = [
        "symbol",
        "description",
        "kind",
        "underlying",
        "quantity",
        "price",
        "market_value",
        "cost_basis_per_unit",
        "unrealized_pnl",
        "unrealized_pct",
        "weight",
        "sector",
        "asset_class",
        "multiplier",
        "expiration",
        "strike",
        "right",
        "days_to_expiry",
    ]
    if snap.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    if not snap.holdings.empty:
        h = snap.holdings.copy()
        h["cost_value"] = h["cost_basis_per_unit"] * h["quantity"]
        grouped = h.groupby("symbol", dropna=False).agg(
            description=("description", "first"),
            kind=("kind", "first"),
            underlying=("underlying", "first"),
            quantity=("quantity", "sum"),
            market_value=("market_value", "sum"),
            cost_value=("cost_value", lambda s: s.sum(min_count=len(s))),
        )
        for symbol, g in grouped.iterrows():
            qty = float(g["quantity"])
            mv = float(g["market_value"]) if pd.notna(g["market_value"]) else math.nan
            cost_value = float(g["cost_value"]) if pd.notna(g["cost_value"]) else math.nan
            row: dict[str, object] = {
                "symbol": symbol,
                "description": g["description"],
                "kind": g["kind"],
                "underlying": g["underlying"] if pd.notna(g["underlying"]) else symbol,
                "quantity": qty,
                "price": mv / qty if qty else math.nan,
                "market_value": mv,
                "cost_basis_per_unit": cost_value / qty if qty else math.nan,
                "unrealized_pnl": mv - cost_value,
                "unrealized_pct": (mv - cost_value) / abs(cost_value) if cost_value else math.nan,
                "multiplier": 1.0,
            }
            occ = _occ(str(symbol)) if g["kind"] == "option" else None
            if occ is not None:
                row |= {
                    "underlying": occ.root if pd.isna(g["underlying"]) else g["underlying"],
                    "multiplier": OPTION_MULTIPLIER,
                    "expiration": occ.expiration,
                    "strike": occ.strike,
                    "right": occ.right,
                    "days_to_expiry": (occ.expiration - today).days if today else math.nan,
                }
            rows.append(row)
    cash = float(snap.accounts["cash"].fillna(0.0).sum())
    if cash:
        rows.append(
            {"symbol": "CASH", "description": "Cash", "kind": CASH_KIND, "market_value": cash}
        )
    table = pd.DataFrame(rows, columns=columns)
    total = table["market_value"].sum()
    table["weight"] = table["market_value"] / total if total else math.nan
    classes = [
        _option_class(classifier, str(u)) if k == "option" else classifier.classify(str(s), str(k))
        for s, k, u in zip(table["symbol"], table["kind"], table["underlying"], strict=True)
    ]
    table["sector"] = [c.sector for c in classes]
    table["asset_class"] = [c.asset_class for c in classes]
    return table.sort_values("market_value", ascending=False).reset_index(drop=True)


def _option_class(classifier: Classifier, underlying: str) -> Classification:
    """Options are their own asset class, in their underlying's sector (when it's known)."""
    of_underlying = classifier.classify(underlying, "stock")
    known = underlying in classifier.sectors or underlying in classifier.funds
    return Classification(of_underlying.sector if known else "Options", "Options")


def _occ(symbol: str) -> OccSymbol | None:
    try:
        return parse_occ(symbol)
    except ValueError:
        return None


def exposures(table: pd.DataFrame, by: str) -> pd.Series:
    """Portfolio weight by ``sector`` or ``asset_class``."""
    return table.groupby(by)["weight"].sum().sort_values(ascending=False)


def concentration(table: pd.DataFrame) -> dict[str, float | str]:
    """Largest position and the effective number of positions (1 / sum of squared weights)."""
    risky = table[(table["kind"] != CASH_KIND) & table["weight"].notna()]
    w = risky["weight"]
    if w.empty:
        return {"largest_weight": math.nan, "largest_symbol": "", "effective_positions": math.nan}
    return {
        "largest_weight": float(w.max()),
        "largest_symbol": str(risky["symbol"].to_numpy()[int(np.argmax(w.to_numpy()))]),
        "effective_positions": float(1.0 / (w**2).sum()) if (w**2).sum() else math.nan,
    }


# -- performance --------------------------------------------------------------------------------


def value_history(lake: Lake) -> pd.Series:
    """Total portfolio value per day (last sync of the day per account, accounts summed).

    An account missing on a day carries its last known value forward, so one failed sync does not
    show up as a crash in the chart.
    """
    accounts = read_accounts(lake)
    if accounts.empty:
        return pd.Series(dtype=float, name="value")
    holdings = read_holdings(lake)
    accounts = accounts.assign(value=account_totals(accounts, holdings).to_numpy())
    accounts = accounts.sort_values("taken_at")
    daily = accounts.groupby(["snapshot_date", "source", "account_id"])["value"].last()
    wide = daily.unstack(["source", "account_id"]).sort_index().ffill()
    series = wide.sum(axis=1, min_count=1)
    series.index = pd.DatetimeIndex(series.index, name="date")
    return series.rename("value")


def external_flows(lake: Lake) -> pd.Series:
    """Net money moved into (+) or out of (-) the accounts per day; trades are not flows."""
    acts = read_activities(lake)
    if acts.empty:
        return pd.Series(dtype=float, name="flow")
    kind = acts["type"].str.upper()
    amount = acts["amount"].abs()
    signed = np.where(kind.isin(FLOW_IN), amount, np.where(kind.isin(FLOW_OUT), -amount, 0.0))
    flows = pd.Series(signed, index=pd.DatetimeIndex(acts["trade_date"], name="date"))
    flows = flows[flows != 0].groupby(level=0).sum()
    return flows.rename("flow")


def time_weighted_returns(values: pd.Series, flows: pd.Series | None = None) -> pd.Series:
    """Daily time-weighted returns: deposits and withdrawals do not count as performance.

    ``r_t = (V_t - F_t) / V_{t-1} - 1`` where ``F_t`` is the net flow since the previous value
    (assumed to arrive just before ``V_t`` is measured).
    """
    values = values.dropna().sort_index()
    if len(values) < 2:
        return pd.Series(dtype=float, name="return")
    flows = flows if flows is not None else pd.Series(dtype=float)
    out = []
    for prev_day, day in zip(values.index[:-1], values.index[1:], strict=True):
        flow = float(flows[(flows.index > prev_day) & (flows.index <= day)].sum())
        prev = float(values[prev_day])
        out.append((float(values[day]) - flow) / prev - 1.0 if prev else math.nan)
    return pd.Series(out, index=values.index[1:], name="return")


def priced_weights(table: pd.DataFrame, priced: set[str]) -> pd.Series:
    """Weights of the holdings we have price history for.

    Unpriced holdings (mutual funds, options, symbols with no bars yet) and cash earn nothing in
    the holdings backtest; ``priced_weights(...).sum()`` is the share of the portfolio covered.
    """
    w = table.set_index("symbol")["weight"].dropna()
    return w[[s in priced for s in w.index]]


def holdings_backtest(weights: pd.Series, prices: pd.DataFrame) -> pd.Series:
    """Daily returns of ``weights`` held constant (rebalanced daily); the rest earns 0."""
    symbols = [s for s in weights.index if s in prices.columns]
    if not symbols:
        return pd.Series(dtype=float, name="current holdings")
    rets = prices[symbols].pct_change(fill_method=None).iloc[1:]
    rets = rets.dropna(how="all").fillna(0.0)
    return (rets * weights[symbols]).sum(axis=1).rename("current holdings")


def benchmark_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Daily returns of each benchmark (fixed weights, rebalanced daily)."""
    rets = prices.pct_change(fill_method=None).iloc[1:]
    out = {}
    for name, weights in BENCHMARKS.items():
        if all(s in rets.columns for s in weights):
            out[name] = sum(rets[s] * w for s, w in weights.items())
    return pd.DataFrame(out).dropna(how="all")


def compare(returns: pd.DataFrame, *, rf: float = 0.0) -> pd.DataFrame:
    """Tear sheet for each column over their common window, plus beta to SPY when present."""
    common = returns.dropna()
    sheet = metrics.tear_sheet(common, rf=rf)
    assert isinstance(sheet, pd.DataFrame)
    spy = "S&P 500 (SPY)"
    if spy in common.columns:
        sheet.loc["beta_to_spy"] = [metrics.beta(common[c], common[spy]) for c in common.columns]
    return sheet
