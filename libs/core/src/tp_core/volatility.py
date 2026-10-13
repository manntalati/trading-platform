"""How expensive options are: implied volatility now, against its own history and against how
much the stock actually moves, and events the option market is pricing in.

Everything here reads the daily chain snapshots (``tp-data options snapshot``). The history of
30-day implied volatility per underlying is derived from them once and kept in
``clean/option_iv_daily`` (rebuilt incrementally: each new snapshot adds a row).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core import greeks as bs
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, read_parquet_dir, write_parquet

IV_HISTORY = "option_iv_daily"
IV_HISTORY_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("underlying", pa.string()),
        ("iv30", pa.float64()),  # at-the-money implied volatility 30 calendar days out
        ("spot", pa.float64()),
    ]
)
MIN_RANK_DAYS = 20  # fewer days of history than this and a rank means little
EVENT_JUMP = 1.15  # an expiry's ATM IV this far above both neighbours: an event inside it


# -- reading snapshots ----------------------------------------------------------------------------


def snapshot_partitions(lake: Lake) -> dict[tuple[date, str], Any]:
    """``(snapshot date, underlying) -> directory`` for every stored chain snapshot."""
    base = lake.raw_dir(RAW_OPTION_CHAIN_SNAPSHOTS)
    out = {}
    for d in base.glob("snapshot_date=*/underlying=*"):
        day = date.fromisoformat(d.parent.name.split("=", 1)[1])
        out[(day, d.name.split("=", 1)[1])] = d
    return out


def latest_chains(
    lake: Lake, underlyings: list[str] | None = None, *, since: date | None = None
) -> dict[str, pd.DataFrame]:
    """The most recent snapshot of each underlying (only its last run, if a day has several),
    skipping ones older than ``since``."""
    newest: dict[str, tuple[date, Any]] = {}
    for (day, underlying), path in snapshot_partitions(lake).items():
        if underlyings is not None and underlying not in underlyings:
            continue
        if since is not None and day < since:
            continue
        if underlying not in newest or day > newest[underlying][0]:
            newest[underlying] = (day, path)
    out = {}
    for underlying, (_, path) in newest.items():
        chain = read_parquet_dir(path, RAW_OPTION_CHAIN_SCHEMA)
        if chain.empty:
            continue
        out[underlying] = chain[chain["snapshot_at"] == chain["snapshot_at"].max()].reset_index(
            drop=True
        )
    return out


def chain_spot(chain: pd.DataFrame) -> float | None:
    prices = chain["underlying_price"].dropna()
    return float(prices.iloc[0]) if len(prices) else None


# -- term structure -------------------------------------------------------------------------------


def atm_term(chain: pd.DataFrame, spot: float | None = None) -> pd.DataFrame:
    """At-the-money implied volatility per expiration: the strike nearest the underlying, the
    average of its call and put. Vendor IVs are used; a missing one is solved from the mid."""
    spot = spot or chain_spot(chain)
    columns = ["expiration", "dte", "strike", "iv"]
    if spot is None or chain.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for expiration, g in chain.groupby("expiration"):
        dte = int(g["dte"].iloc[0])
        if dte < 1:
            continue
        strike = float(g.iloc[(g["strike"] - spot).abs().argsort()]["strike"].iloc[0])
        at = g[g["strike"] == strike]
        ivs = []
        for _, r in at.iterrows():
            iv = r["implied_volatility"]
            if not (isinstance(iv, float) and math.isfinite(iv) and iv > 0):
                iv = _solve(r, spot, dte)
            if iv:
                ivs.append(float(iv))
        if ivs:
            rows.append({"expiration": expiration, "dte": dte, "strike": strike,
                         "iv": float(np.mean(ivs))})  # fmt: skip
    return pd.DataFrame(rows, columns=columns).sort_values("dte").reset_index(drop=True)


def _solve(row: pd.Series, spot: float, dte: int) -> float | None:
    bid, ask = row["bid"], row["ask"]
    if not (pd.notna(bid) and pd.notna(ask) and ask > 0 and ask >= bid):
        return None
    mid = (float(bid) + float(ask)) / 2
    return bs.implied_vol(mid, spot, float(row["strike"]), dte / bs.YEAR_DAYS,
                          call=row["right"] == "C")  # fmt: skip


def iv_at(term: pd.DataFrame, days: float = 30.0) -> float | None:
    """Implied volatility ``days`` out, interpolated in total variance between the expirations
    either side (the nearest one's IV beyond the ends)."""
    t = term.dropna(subset=["iv"])
    if t.empty:
        return None
    dte, iv = t["dte"].to_numpy(dtype=float), t["iv"].to_numpy(dtype=float)
    if days <= dte[0]:
        return float(iv[0])
    if days >= dte[-1]:
        return float(iv[-1])
    i = int(np.searchsorted(dte, days))
    d0, d1 = dte[i - 1], dte[i]
    w0, w1 = iv[i - 1] ** 2 * d0, iv[i] ** 2 * d1
    w = w0 + (w1 - w0) * (days - d0) / (d1 - d0)
    return float(math.sqrt(max(w, 0.0) / days))


def event_expiry(term: pd.DataFrame, max_dte: int = 120) -> dict[str, Any] | None:
    """The first expiration whose at-the-money IV stands out above the expirations either side:
    the option market expects a jump (usually earnings) before it expires and after the one
    before it. None when the term structure is smooth."""
    t = term[(term["dte"] >= 2) & (term["dte"] <= max_dte)]
    ivs = [float(v) for v in t["iv"]]
    expirations = list(t["expiration"])
    for i, iv in enumerate(ivs):
        after = ivs[i + 1] if i + 1 < len(ivs) else None
        before = ivs[i - 1] if i > 0 else None
        if after is None or iv < EVENT_JUMP * after:
            continue
        if before is not None and iv < EVENT_JUMP * before:
            continue
        return {
            "by": _iso(expirations[i]),
            "after": _iso(expirations[i - 1]) if i > 0 else None,
            "iv": iv,
            "iv_after": after,
        }
    return None


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, date | datetime) else str(value)


# -- history and rank -----------------------------------------------------------------------------


def iv_history(lake: Lake) -> pd.DataFrame:
    """30-day ATM implied volatility per underlying per snapshot day.

    Derived from the raw snapshots and cached in the clean zone; days not in the cache yet are
    worked out and added on the way (so the first call after many snapshots takes a while).
    """
    path = lake.clean_dir(IV_HISTORY) / "part.parquet"
    cached = (
        read_parquet_dir(path.parent, IV_HISTORY_SCHEMA)
        if path.exists()
        else pd.DataFrame(columns=[f.name for f in IV_HISTORY_SCHEMA])
    )
    have = set(zip(cached["snapshot_date"], cached["underlying"], strict=True))
    rows = []
    for (day, underlying), directory in sorted(snapshot_partitions(lake).items()):
        if (day, underlying) in have:
            continue
        chain = read_parquet_dir(directory, RAW_OPTION_CHAIN_SCHEMA)
        if chain.empty:
            continue
        chain = chain[chain["snapshot_at"] == chain["snapshot_at"].max()]
        spot = chain_spot(chain)
        rows.append(
            {
                "snapshot_date": day,
                "underlying": underlying,
                "iv30": iv_at(atm_term(chain, spot)) if spot else None,
                "spot": spot,
            }
        )
    if not rows:
        return cached
    fresh = pd.DataFrame(rows, columns=[f.name for f in IV_HISTORY_SCHEMA])
    out = pd.concat([cached, fresh], ignore_index=True) if len(cached) else fresh
    out = out.sort_values(["underlying", "snapshot_date"]).reset_index(drop=True)
    write_parquet(out, path, IV_HISTORY_SCHEMA)
    return out


def iv_rank(history: pd.Series, current: float | None, days: int = 252) -> dict[str, Any]:
    """Where today's IV sits in its last ``days`` sessions of history: rank (0 = the low,
    1 = the high) and percentile (share of days below today). Both None until there are
    ``MIN_RANK_DAYS`` days of history."""
    h = history.dropna().astype(float).iloc[-days:]
    out: dict[str, Any] = {"days": len(h), "rank": None, "percentile": None,
                           "low": None, "high": None}  # fmt: skip
    if current is None or len(h) < MIN_RANK_DAYS:
        return out
    low, high = float(h.min()), float(h.max())
    out |= {
        "low": low,
        "high": high,
        "rank": (current - low) / (high - low) if high > low else 0.5,
        "percentile": float((h < current).mean()),
    }
    return out
