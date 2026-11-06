"""Dealer gamma exposure (GEX) per underlying per day, from the stored chain snapshots.

**Unvalidated research data.** Nothing reads it to make or filter an idea. It's collected because
the history only exists from the day we start keeping it (our snapshots start on 2026-10-07), and
a dealer-gamma regime filter can't be tested without it (``data/reports/research/
market-maker-strategies/FINDINGS-market-maker-strategies.md``, section 2.1).

For each contract with gamma and open interest::

    GEX = sign x gamma x open interest x 100 x spot^2 x 1%

in dollars of delta hedging per 1% move in the underlying, reported in $bn. **The sign is an
assumption, not something we observe**: calls count as positive and puts as negative, the common
"customers sell calls and buy puts" convention (dealers are long the calls and short the puts).
Who really holds what isn't in our data. Index-fund put open interest in particular may be
hedgers' and not dealer-short, which would make the puts' share wrong (IWM reads negative almost
every day). Gamma is the vendor's (Alpaca, indicative feed); open interest is as of the previous
session.

One row per underlying per snapshot date (New York), worked out from the **last snapshot of that
date**, whatever kind it is (``snapshot_kind``: often an evening run after the close):

- ``gex_bn``, ``call_gex_bn``, ``put_gex_bn``: the total and its two sides.
- ``gex_model_bn``: the same total with gamma recomputed by ``tp_core.greeks`` from each
  contract's implied volatility, at the snapshot's spot. A check on the vendor's gammas; it should
  be close to ``gex_bn``.
- ``flip``: the **gamma-flip level**, the spot where the total crosses zero. The total is
  recomputed (gammas from implied volatility, as for ``gex_model_bn``) on a grid of spots from
  10% below to 10% above the snapshot's, 0.25% apart, holding each contract's implied volatility
  and open interest fixed; the crossing nearest the spot, interpolated between grid points. Null
  when the total doesn't change sign inside the grid.
- ``call_oi_strike`` / ``put_oi_strike``: the strikes with the most call and put open interest
  (all expirations together; for SPY's puts that's a round strike far below, 500 in Nov 2026).
  ``call_oi_strike_week`` / ``put_oi_strike_week``: the same among the expirations in the next 7
  calendar days, the ones that can pin the price this week. ``max_gex_strike`` /
  ``min_gex_strike``: the strikes with the most positive and most negative GEX.

Time to expiry for the recomputed gammas is to 16:00 New York on the expiration date; contracts
past that (an evening snapshot on their last day) are left out of the profile. No dividends; the
rate is the 3-month Treasury yield (``tp_core.macro``). Kept in ``clean/dealer_gamma_daily``,
added to incrementally like ``tp_core.volatility.iv_history``.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core import greeks as bs
from tp_core.calendar import NEW_YORK
from tp_core.chains import fill_kinds, late_runs
from tp_core.macro import risk_free_rate
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA
from tp_core.storage import Lake, parquet_files, read_parquet_dir, write_parquet
from tp_core.volatility import chain_spot, snapshot_partitions

GAMMA_HISTORY = "dealer_gamma_daily"
_TS = pa.timestamp("ns", tz="UTC")
GAMMA_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("underlying", pa.string()),
        ("snapshot_at", _TS),  # the snapshot the row was worked out from
        ("snapshot_kind", pa.string()),
        ("spot", pa.float64()),
        ("gex_bn", pa.float64()),  # $bn of delta hedging per 1% move; calls +, puts -
        ("call_gex_bn", pa.float64()),
        ("put_gex_bn", pa.float64()),
        ("gex_model_bn", pa.float64()),  # gamma recomputed from implied volatility
        ("flip", pa.float64()),  # gamma-flip level: spot where the recomputed total crosses zero
        ("call_oi_strike", pa.float64()),
        ("put_oi_strike", pa.float64()),
        ("call_oi_strike_week", pa.float64()),  # the same among expirations in the next 7 days
        ("put_oi_strike_week", pa.float64()),
        ("max_gex_strike", pa.float64()),
        ("min_gex_strike", pa.float64()),
        ("contracts", pa.int32()),  # contracts with gamma and open interest
        ("open_interest", pa.float64()),  # all contracts' open interest
        ("runs", pa.int32()),  # snapshot files the day had when this row was worked out
    ]
)
INDEX_FUNDS = ("SPY", "QQQ", "IWM")
GRID = 0.10  # the flip is looked for this far either side of the spot
GRID_POINTS = 81  # 0.25% apart
WEEK_DAYS = 7  # the "this week" open-interest strikes: expirations this many calendar days out
DEFAULT_RATE = 0.04
UNVALIDATED = (
    "Unvalidated: dealer gamma assumes customers sell calls and buy puts, and it isn't used by "
    "any idea or filter."
)


def contract_gex(chain: pd.DataFrame, spot: float) -> pd.Series:
    """Each contract's GEX in dollars per 1% move (calls +, puts -); NaN without gamma or open
    interest."""
    sign = chain["right"].map({"C": 1.0, "P": -1.0})
    gamma = pd.to_numeric(chain["gamma"], errors="coerce")
    oi = pd.to_numeric(chain["open_interest"], errors="coerce")
    out: pd.Series = sign * gamma * oi * 100.0 * spot * spot * 0.01
    return out


def years_left(chain: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Calendar years from each row's snapshot to 16:00 New York on its expiration date."""
    expiry = (pd.to_datetime(chain["expiration"]) + pd.Timedelta(hours=16)).dt.tz_localize(NEW_YORK)
    taken = pd.to_datetime(chain["snapshot_at"], utc=True)
    seconds = (expiry - taken).dt.total_seconds().to_numpy(dtype=float)
    out: np.ndarray[Any, np.dtype[np.float64]] = np.asarray(
        seconds / (bs.YEAR_DAYS * 86400.0), dtype=np.float64
    )
    return out


def gex_profile(
    chain: pd.DataFrame, spots: np.ndarray[Any, np.dtype[np.float64]], rate: float = 0.0
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """The total GEX (dollars per 1% move) if the underlying were at each of ``spots``, with every
    contract's gamma recomputed from its implied volatility. Contracts without implied volatility
    or open interest, or already expired, are left out."""
    iv = pd.to_numeric(chain["implied_volatility"], errors="coerce").to_numpy(dtype=float)
    oi = pd.to_numeric(chain["open_interest"], errors="coerce").to_numpy(dtype=float)
    years = years_left(chain)
    keep = np.isfinite(iv) & (iv > 0) & np.isfinite(oi) & (oi > 0) & (years > 0)
    if not keep.any():
        return np.zeros(len(spots))
    sign = np.where(chain["right"].to_numpy()[keep] == "C", 1.0, -1.0)
    strike = chain["strike"].to_numpy(dtype=float)[keep]
    s = np.asarray(spots, dtype=float)[:, None]
    gamma = bs.greeks_array(s, strike[None, :], years[keep][None, :], iv[keep][None, :], True,
                            rate)["gamma"]  # fmt: skip
    weight = (sign * oi[keep])[None, :]
    total = (gamma * weight).sum(axis=1) * 100.0 * s[:, 0] ** 2 * 0.01
    out: np.ndarray[Any, np.dtype[np.float64]] = np.asarray(total, dtype=np.float64)
    return out


def flip_level(
    spots: np.ndarray[Any, np.dtype[np.float64]],
    profile: np.ndarray[Any, np.dtype[np.float64]],
    spot: float,
) -> float | None:
    """The spot where ``profile`` crosses zero nearest ``spot`` (linear between grid points);
    None when it doesn't change sign."""
    crossings = []
    for i in range(len(spots) - 1):
        a, b = profile[i], profile[i + 1]
        if a == 0:
            crossings.append(float(spots[i]))
        elif a * b < 0:
            crossings.append(float(spots[i] + (spots[i + 1] - spots[i]) * a / (a - b)))
    if len(profile) and profile[-1] == 0:
        crossings.append(float(spots[-1]))
    return min(crossings, key=lambda x: abs(x - spot)) if crossings else None


def day_gamma(chain: pd.DataFrame, rate: float = 0.0) -> dict[str, Any]:
    """One snapshot's numbers (the ``GAMMA_SCHEMA`` columns but the date, underlying and runs)."""
    spot = chain_spot(chain)
    out: dict[str, Any] = {
        "snapshot_at": chain["snapshot_at"].max() if len(chain) else None,
        "snapshot_kind": chain["snapshot_kind"].dropna().iloc[-1]
        if chain["snapshot_kind"].notna().any()
        else None,
        "spot": spot,
        "open_interest": float(pd.to_numeric(chain["open_interest"], errors="coerce").sum()),
    }
    if not spot:
        return out
    gex = contract_gex(chain, spot)
    used = gex.notna()
    calls = chain["right"] == "C"
    by_strike = gex[used].groupby(chain.loc[used, "strike"]).sum()
    oi = pd.to_numeric(chain["open_interest"], errors="coerce")
    week = pd.to_numeric(chain["dte"], errors="coerce") <= WEEK_DAYS
    spots = spot * (1.0 + np.linspace(-GRID, GRID, GRID_POINTS))
    profile = gex_profile(chain, spots, rate)
    middle = GRID_POINTS // 2
    return out | {
        "gex_bn": float(gex[used].sum()) / 1e9,
        "call_gex_bn": float(gex[used & calls].sum()) / 1e9,
        "put_gex_bn": float(gex[used & ~calls].sum()) / 1e9,
        "gex_model_bn": float(profile[middle]) / 1e9,
        "flip": flip_level(spots, profile, spot),
        "call_oi_strike": _top(oi, chain, calls),
        "put_oi_strike": _top(oi, chain, ~calls),
        "call_oi_strike_week": _top(oi, chain, calls & week),
        "put_oi_strike_week": _top(oi, chain, ~calls & week),
        "max_gex_strike": _strike_at(by_strike, largest=True) if len(by_strike) else None,
        "min_gex_strike": _strike_at(by_strike, largest=False) if len(by_strike) else None,
        "contracts": int(used.sum()),
    }


def _strike_at(by_strike: pd.Series, *, largest: bool) -> float:
    """The strike (index) with the largest or the smallest value."""
    values = by_strike.to_numpy(dtype=float)
    i = int(np.argmax(values) if largest else np.argmin(values))
    return float(np.asarray(by_strike.index, dtype=float)[i])


def _top(oi: pd.Series, chain: pd.DataFrame, rows: pd.Series) -> float | None:
    """The strike with the most open interest among ``rows``."""
    by_strike = oi[rows].groupby(chain.loc[rows, "strike"]).sum().dropna()
    if by_strike.empty or not by_strike.max() > 0:
        return None
    return _strike_at(by_strike, largest=True)


def gamma_history(
    lake: Lake, underlyings: list[str] | tuple[str, ...] | None = None
) -> pd.DataFrame:
    """Dealer gamma per underlying per snapshot day (all underlyings, or only ``underlyings``
    brought up to date: the others' rows are returned as cached).

    Cached in the clean zone; a day whose snapshot files changed since its row was worked out (a
    later snapshot came in) is worked out again from its latest snapshot."""
    path = lake.clean_dir(GAMMA_HISTORY) / "part.parquet"
    columns = [f.name for f in GAMMA_SCHEMA]
    cached = pd.DataFrame(columns=columns)
    if path.exists():
        found = read_parquet_dir(path.parent)
        if set(columns) <= set(found.columns):  # an older cache without a column is rebuilt
            cached = found[columns]
    have = {
        (d, u): int(n)
        for d, u, n in zip(
            cached["snapshot_date"], cached["underlying"], cached["runs"], strict=True
        )
    }
    late = late_runs(lake)
    rates: dict[date, float] = {}
    rows = []
    for (day, underlying), directory in sorted(snapshot_partitions(lake).items()):
        if underlyings is not None and underlying not in underlyings:
            continue
        runs = len(parquet_files(directory))
        if have.get((day, underlying)) == runs:
            continue
        chain = read_parquet_dir(directory, RAW_OPTION_CHAIN_SCHEMA)
        if chain.empty:
            continue
        chain = chain[chain["snapshot_at"] == chain["snapshot_at"].max()].reset_index(drop=True)
        chain = fill_kinds(chain, late)
        if day not in rates:
            rates[day] = risk_free_rate(lake, DEFAULT_RATE, at=day)
        rows.append({"snapshot_date": day, "underlying": underlying, "runs": runs,
                     **day_gamma(chain, rates[day])})  # fmt: skip
    if not rows:
        return cached
    fresh = pd.DataFrame(rows).reindex(columns=columns)
    redone = set(zip(fresh["snapshot_date"], fresh["underlying"], strict=True))
    keep = [
        (d, u) not in redone
        for d, u in zip(cached["snapshot_date"], cached["underlying"], strict=True)
    ]
    cached = cached[keep]
    out = pd.concat([cached, fresh], ignore_index=True) if len(cached) else fresh
    out = out.sort_values(["underlying", "snapshot_date"]).reset_index(drop=True)
    write_parquet(out, path, GAMMA_SCHEMA)
    out = read_parquet_dir(path.parent, GAMMA_SCHEMA)  # the stored types, every time
    return out.sort_values(["underlying", "snapshot_date"]).reset_index(drop=True)


def current_gamma(
    history: pd.DataFrame, underlyings: tuple[str, ...] = INDEX_FUNDS
) -> list[dict[str, Any]]:
    """Each underlying's latest row, in words for the dashboard: ``sign`` (positive / negative),
    ``gex_bn``, ``flip`` and the strikes, with ``snapshot_at``."""
    out = []
    for underlying in underlyings:
        rows = history[(history["underlying"] == underlying) & history["gex_bn"].notna()]
        if rows.empty:
            continue
        r = rows.sort_values("snapshot_date").iloc[-1]
        gex = float(r["gex_bn"])
        out.append(
            {
                "underlying": underlying,
                "date": pd.Timestamp(r["snapshot_date"]).date().isoformat(),
                "snapshot_at": pd.Timestamp(r["snapshot_at"]).isoformat(),
                "snapshot_kind": r["snapshot_kind"]
                if isinstance(r["snapshot_kind"], str)
                else None,
                "sign": "positive" if gex >= 0 else "negative",
                "gex_bn": gex,
                "spot": _num(r["spot"]),
                "flip": _num(r["flip"]),
                "call_oi_strike": _num(r["call_oi_strike"]),
                "put_oi_strike": _num(r["put_oi_strike"]),
                "call_oi_strike_week": _num(r["call_oi_strike_week"]),
                "put_oi_strike_week": _num(r["put_oi_strike_week"]),
            }
        )
    return out


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
