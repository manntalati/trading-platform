"""Reference data for strategies, point in time (the ``tp_trading.strategy.Reference`` contract).

``LakeReference(lake).get(name, as_of)`` returns only what was known on ``as_of``:

- ``"fundamentals"``: each company's latest filing known by then, as first reported
  (``tp_core.fundamentals.history``): trailing-twelve-month EPS, net income, revenue and gross
  profit, equity, assets, shares outstanding. Indexed by symbol.
- ``"valuation"``: the same with that day's market capitalization (shares outstanding times the
  raw close, the shares carried across any split since they were reported) and the ratios the
  factor strategies rank on: earnings yield, book to market, gross profitability, return on
  equity. Indexed by symbol.
- ``"earnings"``: every quarterly result published up to then with its standardized surprise
  (SUE, from split-adjusted EPS as first reported) and ``session``, the first session it was in
  the data (the day of its 10-Q or 10-K): one row per company and quarter.
- ``"macro"``: VIX and Treasury yields up to then (``tp_core.macro``).

Tables are built once per lake and process (they take a few seconds for a few dozen companies)
and rebuilt when the lake's files change.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core import fundamentals as fund
from tp_core.bars import load_bars
from tp_core.calendar import sessions
from tp_core.macro import load_macro
from tp_core.storage import Lake, parquet_files

NAMES = ("fundamentals", "valuation", "earnings", "macro")
_CACHE: dict[tuple[str, str, float], Any] = {}


def _stamp(directory: Path) -> float:
    files = parquet_files(directory)
    return max((f.stat().st_mtime for f in files), default=0.0)


class LakeReference:
    def __init__(self, lake: Lake) -> None:
        self.lake = lake

    def _cached(self, key: str, directory: Path, build: Any) -> Any:
        k = (str(self.lake.root.resolve()), key, _stamp(directory))
        if k not in _CACHE:
            _CACHE[k] = build()
        return _CACHE[k]

    # -- tables ----------------------------------------------------------------------------------

    def _facts(self) -> pd.DataFrame:
        facts: pd.DataFrame = self._cached(
            "facts", self.lake.clean_dir(fund.CLEAN_FACTS), lambda: fund.load_facts(self.lake)
        )
        if facts.empty:
            raise LookupError("no fundamentals in the lake: run `tp-data fundamentals`")
        return facts

    def history(self) -> pd.DataFrame:
        facts = self._facts()
        out: pd.DataFrame = self._cached(
            "history", self.lake.clean_dir(fund.CLEAN_FACTS), lambda: fund.history(facts)
        )
        return out

    def _bars(self, field: str) -> pd.DataFrame:
        symbols = sorted(set(self._facts()["symbol"]))

        def build() -> pd.DataFrame:
            bars = load_bars(self.lake, symbols)
            if bars.empty:
                return pd.DataFrame()
            wide = bars.pivot(index="session", columns="symbol", values=field)
            wide.index = pd.DatetimeIndex(wide.index)
            return wide.sort_index()

        out: pd.DataFrame = self._cached(
            f"bars-{field}", self.lake.clean_dir("stock_bars_1d"), build
        )
        return out

    def earnings_table(self) -> pd.DataFrame:
        facts = self._facts()

        def build() -> pd.DataFrame:
            splits = self._bars("split_factor")
            frames = []
            for symbol, company in facts.groupby("symbol"):
                q = fund.quarterly(company, None, "eps_diluted", first_reported=True)
                if q.empty:
                    continue
                if str(symbol) in splits.columns:
                    q = fund.split_adjusted_eps(q, splits[str(symbol)].dropna())
                s = fund.sue(q)
                if s.empty:
                    continue
                s.insert(0, "symbol", symbol)
                s["session"] = [_session_on_or_after(k) for k in s["known"]]
                frames.append(s)
            cols = ["symbol", "end", "known", "eps", "change", "sue", "session"]
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

        out: pd.DataFrame = self._cached("earnings", self.lake.clean_dir(fund.CLEAN_FACTS), build)
        return out

    # -- the contract ----------------------------------------------------------------------------

    def get(self, name: str, as_of: date) -> pd.DataFrame:
        if name == "fundamentals":
            return fund.as_known(self.history(), as_of)
        if name == "valuation":
            return self.valuation(as_of)
        if name == "earnings":
            e = self.earnings_table()
            out: pd.DataFrame = e[e["session"].notna() & (e["session"] <= as_of)]
            return out
        if name == "macro":
            macro: pd.DataFrame = self._cached(
                "macro", self.lake.clean_dir("macro_1d"), lambda: load_macro(self.lake)
            )
            if macro.empty:
                raise LookupError("no macro data in the lake: run `tp-data macro backfill`")
            return macro[macro.index <= pd.Timestamp(as_of)]
        raise KeyError(f"unknown reference data {name!r}; known: {NAMES}")

    def valuation(self, as_of: date) -> pd.DataFrame:
        known = fund.as_known(self.history(), as_of)
        close, split = self._bars("close"), self._bars("split_factor")
        upto = close[close.index <= pd.Timestamp(as_of)]
        rows = []
        for symbol, r in known.iterrows():
            s = str(symbol)
            price = _last(upto, s)
            cap = None
            if price and r["shares_outstanding"] and r["shares_known"] is not None:
                then = _factor_on(split, s, r["shares_known"])
                now = _factor_on(split, s, as_of)
                cap = float(r["shares_outstanding"]) * (now / then) * price
            ni, gp, assets = r.get("ttm_net_income"), r.get("ttm_gross_profit"), r.get("assets")
            eq = _num(r.get("equity"))
            positive_equity = eq is not None and eq > 0
            rows.append(
                {
                    "symbol": s,
                    "known": r["known"],
                    "price": price,
                    "market_cap": cap,
                    "earnings_yield": _ratio(ni, cap),
                    "book_to_market": _ratio(eq, cap) if positive_equity else None,
                    "gross_profitability": _ratio(gp, assets),
                    "roe": _ratio(ni, eq) if positive_equity else None,
                }
            )
        return pd.DataFrame(rows).set_index("symbol") if rows else pd.DataFrame()


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ratio(a: Any, b: Any) -> float | None:
    x, y = _num(a), _num(b)
    return x / y if x is not None and y else None


def _last(frame: pd.DataFrame, symbol: str) -> float | None:
    if symbol not in frame.columns:
        return None
    col = frame[symbol].dropna()
    return float(col.iloc[-1]) if len(col) else None


def _factor_on(split: pd.DataFrame, symbol: str, day: date) -> float:
    """The backward split factor on ``day`` (1 when unknown): shares then times factor now over
    factor then are today's shares."""
    if symbol not in split.columns:
        return 1.0
    col = split[symbol].dropna()
    i = int(col.index.searchsorted(pd.Timestamp(day), side="right")) - 1
    return float(col.iloc[i]) if i >= 0 else float(col.iloc[0]) if len(col) else 1.0


def _session_on_or_after(day: date) -> date | None:
    ahead = sessions(day, day + timedelta(days=10))
    return ahead[0] if ahead else None
