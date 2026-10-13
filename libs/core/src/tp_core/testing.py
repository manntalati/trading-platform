"""Builders for small, hand-checkable bar and corporate-action frames (used by tests)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pandas as pd

from tp_core.calendar import session_midnight_utc, sessions
from tp_core.schemas import RAW_CORPORATE_ACTIONS_SCHEMA, conform

T0 = datetime(2024, 7, 1, 22, 0, tzinfo=UTC)


def raw_bars(
    symbol: str,
    closes: Sequence[float],
    *,
    start: date = date(2024, 6, 3),
    run_id: str = "run-a",
    ingested_at: datetime = T0,
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """One bar per XNYS session from ``start`` with flat-ish OHLC around each close."""
    days = sessions(start, date(start.year + 1, 12, 31))[: len(closes)]
    df = pd.DataFrame(
        {
            "symbol": symbol,
            "session": days,
            "timestamp": [session_midnight_utc(d) for d in days],
            "open": list(closes),
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": list(closes),
            "volume": volume,
            "trade_count": 100.0,
            "vwap": list(closes),
            "feed": "sip",
            "adjustment": "raw",
            "ingested_at": pd.Timestamp(ingested_at),
            "run_id": run_id,
        }
    )
    return df


def actions(*rows: dict[str, object]) -> pd.DataFrame:
    base = {"ingested_at": pd.Timestamp(T0), "run_id": "run-a"}
    return conform(pd.DataFrame([{**base, **r} for r in rows]), RAW_CORPORATE_ACTIONS_SCHEMA)


def synthetic_chain(
    underlying: str,
    spot: float,
    snapshot_at: datetime,
    *,
    expirations: Sequence[date],
    iv: float | Sequence[float] = 0.3,
    strikes: Sequence[float] | None = None,
    half_spread: float = 0.02,
    open_interest: float = 1000.0,
    skew: float = 0.0,
    curvature: float = 0.0,
    rate: float = 0.0,
) -> pd.DataFrame:
    """A chain snapshot priced by Black-Scholes at ``iv`` (one per expiration, or the same for
    all), with a bid/ask ``half_spread`` (a share of the price) around the model price. A smile:
    each strike's volatility is ``iv + skew * x + curvature * x^2`` with ``x = ln(strike /
    spot)`` (negative ``skew``: puts dearer, as for stocks)."""
    from tp_core import greeks as bs
    from tp_core.occ import format_occ
    from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA

    vols = [iv] * len(expirations) if isinstance(iv, float | int) else list(iv)
    strikes = strikes or [round(spot * k / 20) * 1.0 for k in range(10, 31)]  # 50%..150%
    day = snapshot_at.date()
    rows = []
    for expiration, vol in zip(expirations, vols, strict=True):
        dte = (expiration - day).days
        for strike in strikes:
            x = math.log(strike / spot)
            smile = max(float(vol) + skew * x + curvature * x * x, 0.02)
            for right in ("C", "P"):
                call = right == "C"
                price = bs.price(spot, strike, dte / 365, smile, call=call, rate=rate)
                g = bs.greeks(spot, strike, dte / 365, smile, call=call, rate=rate)
                rows.append(
                    {
                        "snapshot_at": pd.Timestamp(snapshot_at),
                        "underlying": underlying,
                        "contract": format_occ(underlying, expiration, right, strike),
                        "expiration": expiration,
                        "right": right,
                        "strike": strike,
                        "dte": dte,
                        "underlying_price": spot,
                        "bid": round(max(price * (1 - half_spread), 0.0), 2),
                        "ask": round(max(price * (1 + half_spread), 0.01), 2),
                        "implied_volatility": smile,
                        "delta": g.delta,
                        "open_interest": open_interest,
                        "feed": "synthetic",
                        "ingested_at": pd.Timestamp(snapshot_at),
                        "run_id": f"syn-{day}",
                    }
                )
    return conform(pd.DataFrame(rows), RAW_OPTION_CHAIN_SCHEMA)


def synthetic_company_facts(
    cik: int,
    eps: dict[tuple[int, int], float],
    *,
    shares: float = 1e9,
    equity_per_share: float = 20.0,
    assets_per_share: float = 50.0,
    restated: dict[tuple[int, int], float] | None = None,
) -> dict[str, object]:
    """An SEC company-facts document shaped like the real one, for a company whose fiscal year
    is the calendar year. ``eps[(year, quarter)]`` drives everything: net income is EPS times
    ``shares``, revenue five times net income, gross profit 40% of revenue. 10-Qs (filed 35 days
    after the quarter) report the quarter and the year to date, with last year's same quarter as
    comparatives; 10-Ks (filed 60 days after the year) report the year only, as real ones do.
    ``restated`` changes a quarter's EPS in the following year's comparatives."""
    from datetime import timedelta

    restated = restated or {}
    q_end = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}

    def end(y: int, q: int) -> date:
        return date(y, *q_end[q])

    def start(y: int, q: int) -> date:
        return date(y, 1, 1) if q == 1 else end(y, q - 1) + timedelta(days=1)

    flows: dict[str, list[dict[str, object]]] = {k: [] for k in ("eps", "ni", "rev", "gp")}
    balances: dict[str, list[dict[str, object]]] = {"eq": [], "as": [], "sh": []}

    def add(
        y: int, q: int, s: date, e: date, value: float, form: str, filed: date, fp: str
    ) -> None:
        accn = f"0000{cik:06d}-{filed:%y}-{filed:%m%d}"
        meta = {"accn": accn, "fy": y, "fp": fp, "form": form, "filed": filed.isoformat()}
        for key, val in (("eps", value), ("ni", value * shares), ("rev", value * shares * 5),
                         ("gp", value * shares * 2)):  # fmt: skip
            flows[key].append({"start": s.isoformat(), "end": e.isoformat(), "val": val, **meta})

    def balance(y: int, fp: str, form: str, filed: date, on: date) -> None:
        meta = {"accn": f"0000{cik:06d}-{filed:%y}-{filed:%m%d}", "fy": y, "fp": fp,
                "form": form, "filed": filed.isoformat(), "end": on.isoformat()}  # fmt: skip
        balances["eq"].append({"val": shares * equity_per_share, **meta})
        balances["as"].append({"val": shares * assets_per_share, **meta})
        balances["sh"].append({"val": shares, **meta})

    years = sorted({y for y, _ in eps})
    for y in years:
        for q in (1, 2, 3):
            if (y, q) not in eps:
                continue
            filed = end(y, q) + timedelta(days=35)
            fp = f"Q{q}"
            add(y, q, start(y, q), end(y, q), eps[(y, q)], "10-Q", filed, fp)
            ytd = sum(eps.get((y, k), 0.0) for k in range(1, q + 1))
            if q > 1:
                add(y, q, date(y, 1, 1), end(y, q), ytd, "10-Q", filed, fp)
            if (y - 1, q) in eps:  # comparative: last year's quarter, as (re)stated now
                prior = restated.get((y - 1, q), eps[(y - 1, q)])
                add(y, q, start(y - 1, q), end(y - 1, q), prior, "10-Q", filed, fp)
            balance(y, fp, "10-Q", filed, end(y, q))
        if all((y, q) in eps for q in (1, 2, 3, 4)):
            filed = date(y + 1, 3, 1)
            total = sum(eps[(y, q)] for q in (1, 2, 3, 4))
            add(y, 4, date(y, 1, 1), end(y, 4), total, "10-K", filed, "FY")
            balance(y, "FY", "10-K", filed, end(y, 4))

    def concept(units: str, entries: list[dict[str, object]]) -> dict[str, object]:
        return {"label": "", "description": "", "units": {units: entries}}

    return {
        "cik": cik,
        "entityName": f"Synthetic {cik}",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": concept("shares", balances["sh"])},
            "us-gaap": {
                "EarningsPerShareDiluted": concept("USD/shares", flows["eps"]),
                "NetIncomeLoss": concept("USD", flows["ni"]),
                "Revenues": concept("USD", flows["rev"]),
                "GrossProfit": concept("USD", flows["gp"]),
                "StockholdersEquity": concept("USD", balances["eq"]),
                "Assets": concept("USD", balances["as"]),
            },
        },
    }


def synthetic_submissions(cik: int, quarters: list[tuple[int, int]]) -> dict[str, object]:
    """An SEC submissions document with an earnings release (8-K, items 2.02 and 9.01) 25 days
    after each quarter (30 for the fourth), at 4:05 PM New York, and the 10-Q or 10-K that
    ``synthetic_company_facts`` files."""
    from datetime import timedelta

    q_end = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    rows = []
    for y, q in quarters:
        end = date(y, *q_end[q])
        release = end + timedelta(days=30 if q == 4 else 25)
        rows.append(("8-K", release, f"{release.isoformat()}T16:05:00.000Z", "2.02,9.01", end))
        filing = date(y + 1, 3, 1) if q == 4 else end + timedelta(days=35)
        form = "10-K" if q == 4 else "10-Q"
        rows.append((form, filing, f"{filing.isoformat()}T06:01:00.000Z", "", end))
    rows.sort(key=lambda r: r[1], reverse=True)  # newest first, as EDGAR lists them
    return {
        "cik": str(cik),
        "name": f"Synthetic {cik}",
        "filings": {
            "recent": {
                "accessionNumber": [f"0000{cik:06d}-{r[1]:%y}-{i:06d}" for i, r in enumerate(rows)],
                "form": [r[0] for r in rows],
                "filingDate": [r[1].isoformat() for r in rows],
                "acceptanceDateTime": [r[2] for r in rows],
                "items": [r[3] for r in rows],
                "reportDate": [r[4].isoformat() for r in rows],
            },
            "files": [],
        },
    }
