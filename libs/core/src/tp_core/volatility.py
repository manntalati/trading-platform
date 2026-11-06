"""How expensive options are: implied volatility now, against its own history and against how
much the stock actually moves, and events the option market is pricing in.

Everything here reads the daily chain snapshots (``tp-data options snapshot``). The history of
30-day implied volatility per underlying is derived from them once and kept in
``clean/option_iv_daily`` (rebuilt incrementally: each new snapshot adds a row), with two
research columns that nothing acts on yet (Xing-Zhang-Zhao skew, Cremers-Weinbaum spread):

- **Skew**: ``put25_iv30`` and ``call25_iv30``, the implied volatility of the 25-delta put and the
  25-delta call 30 days out (per expiration, interpolated in delta between the listed strikes
  either side, using the vendor's deltas; then to 30 days as for ``iv30``), and
  ``skew25 = put25_iv30 - call25_iv30`` (the risk-reversal form, in volatility points as a
  decimal). Xing, Zhang and Zhao used an out-of-the-money put minus the at-the-money call:
  that's ``put25_iv30 - iv30`` from the same row.
- **Call-minus-put IV** (``cp_spread``, Cremers and Weinbaum's volatility spread): the call's
  implied volatility minus the put's at the same strike and expiration, averaged over the pairs
  with strikes within 10% of the spot and 10 to 60 days to expiry, weighted by the pair's average
  open interest (equal weights when none is known); ``cp_pairs`` is how many pairs. American
  puts' early-exercise value and dividends push it below zero on their own, so read it against
  the underlying's own history, not against zero.

Both research columns come from the day's **last snapshot taken during the session**
(``research_at``), not from its last snapshot overall like ``iv30``. In a snapshot taken after the
close the option quotes are 16:00's but the vendor works out implied volatility from the
underlying's after-hours price, and that alone moves calls against puts: QQQ on 2026-10-14 read
+9.5 points call-minus-put at 20:45 and -0.3 at 11:01 (put-call parity on its quotes implied
QQQ at 729.9 with the stock at 727.8). Null on a day without an in-session snapshot.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core import greeks as bs
from tp_core.calendar import is_session, session_close, session_open
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, parquet_files, read_parquet_dir, write_parquet

IV_HISTORY = "option_iv_daily"
IV_HISTORY_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("underlying", pa.string()),
        ("iv30", pa.float64()),  # at-the-money implied volatility 30 calendar days out
        ("spot", pa.float64()),
        ("runs", pa.int32()),  # snapshot files the day had when this row was worked out
        ("put25_iv30", pa.float64()),  # the 25-delta put's implied volatility 30 days out
        ("call25_iv30", pa.float64()),  # the 25-delta call's
        ("skew25", pa.float64()),  # put25_iv30 - call25_iv30
        ("cp_spread", pa.float64()),  # call minus put IV at matched strikes near the money
        ("cp_pairs", pa.int32()),
        ("research_at", pa.timestamp("ns", tz="UTC")),  # the in-session snapshot of the 4 above
    ]
)
SKEW_DELTA = 0.25
CP_MONEYNESS = 0.10  # strikes within this share of the spot
CP_DTE = (10, 60)  # days to expiry of the pairs in cp_spread
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


# -- skew and call-versus-put implied volatility ------------------------------------------------


def delta_term(chain: pd.DataFrame, right: str, delta: float = SKEW_DELTA) -> pd.DataFrame:
    """Per expiration (2 days out or more), the implied volatility at ``delta`` (as a size: 0.25
    is the 25-delta call or put), interpolated between the strikes either side of it in the
    vendor's delta. Expirations whose strikes don't reach that delta are left out."""
    columns = ["expiration", "dte", "iv"]
    rows = []
    side = chain[(chain["right"] == right) & (chain["dte"] >= 2)]
    for expiration, g in side.groupby("expiration"):
        d = pd.to_numeric(g["delta"], errors="coerce").abs()
        iv = pd.to_numeric(g["implied_volatility"], errors="coerce")
        ok = d.notna() & iv.notna() & (iv > 0) & (d > 0) & (d < 1)
        if ok.sum() < 2:
            continue
        pts = pd.DataFrame({"d": d[ok], "iv": iv[ok]}).groupby("d")["iv"].mean().sort_index()
        if not pts.index[0] <= delta <= pts.index[-1]:
            continue
        value = float(np.interp(delta, pts.index.to_numpy(), pts.to_numpy()))
        rows.append({"expiration": expiration, "dte": int(g["dte"].iloc[0]), "iv": value})
    return pd.DataFrame(rows, columns=columns).sort_values("dte").reset_index(drop=True)


def skew(chain: pd.DataFrame, days: float = 30.0) -> dict[str, float | None]:
    """``put25_iv30``, ``call25_iv30`` and ``skew25`` (put minus call) ``days`` out."""
    put = iv_at(delta_term(chain, "P"), days)
    call = iv_at(delta_term(chain, "C"), days)
    return {
        "put25_iv30": put,
        "call25_iv30": call,
        "skew25": put - call if put is not None and call is not None else None,
    }


def call_put_spread(chain: pd.DataFrame, spot: float | None = None) -> dict[str, Any]:
    """``cp_spread``: call minus put implied volatility at the same strike and expiration, near
    the money and around a month out, weighted by open interest; ``cp_pairs``: how many pairs."""
    spot = spot or chain_spot(chain)
    if not spot:
        return {"cp_spread": None, "cp_pairs": 0}
    near = chain[
        ((chain["strike"] / spot - 1).abs() <= CP_MONEYNESS + 1e-9)
        & chain["dte"].between(*CP_DTE)
        & (pd.to_numeric(chain["implied_volatility"], errors="coerce") > 0)
    ]
    keys = ["expiration", "strike"]
    calls = near[near["right"] == "C"].drop_duplicates(keys).set_index(keys)
    puts = near[near["right"] == "P"].drop_duplicates(keys).set_index(keys)
    both = calls.join(puts, how="inner", lsuffix="_c", rsuffix="_p")
    if both.empty:
        return {"cp_spread": None, "cp_pairs": 0}
    spread = (both["implied_volatility_c"] - both["implied_volatility_p"]).astype(float)
    weight = both[["open_interest_c", "open_interest_p"]].astype(float).mean(axis=1).fillna(0.0)
    value = (
        float((spread * weight).sum() / weight.sum()) if weight.sum() > 0 else float(spread.mean())
    )
    return {"cp_spread": value, "cp_pairs": len(both)}


def in_session(chain: pd.DataFrame, day: date) -> pd.DataFrame:
    """The rows of ``chain`` snapshotted while ``day``'s session was open."""
    if not is_session(day):
        return chain.iloc[0:0]
    taken = pd.to_datetime(chain["snapshot_at"], utc=True)
    return chain[(taken >= session_open(day)) & (taken <= session_close(day))]


# -- history and rank -----------------------------------------------------------------------------


def iv_history(lake: Lake) -> pd.DataFrame:
    """30-day ATM implied volatility per underlying per snapshot day, with the skew and the
    call-minus-put spread (see the module's docstring).

    Derived from the raw snapshots and cached in the clean zone; days not in the cache yet are
    worked out and added on the way (so the first call after many snapshots takes a while). A day
    snapshotted again later (a midday and a closing snapshot) is worked out again from its latest.
    """
    path = lake.clean_dir(IV_HISTORY) / "part.parquet"
    columns = [f.name for f in IV_HISTORY_SCHEMA]
    cached = pd.DataFrame(columns=columns)
    if path.exists():
        found = read_parquet_dir(path.parent)
        if set(columns) <= set(found.columns):  # an older cache missing a column is rebuilt
            cached = found[columns]
    have = {
        (d, u): int(n)
        for d, u, n in zip(
            cached["snapshot_date"], cached["underlying"], cached["runs"], strict=True
        )
    }
    rows = []
    for (day, underlying), directory in sorted(snapshot_partitions(lake).items()):
        runs = len(parquet_files(directory))
        if have.get((day, underlying)) == runs:
            continue
        chain = read_parquet_dir(directory, RAW_OPTION_CHAIN_SCHEMA)
        if chain.empty:
            continue
        session = in_session(chain, day)
        research = session[session["snapshot_at"] == session["snapshot_at"].max()]
        chain = chain[chain["snapshot_at"] == chain["snapshot_at"].max()]
        spot = chain_spot(chain)
        rows.append(
            {
                "snapshot_date": day,
                "underlying": underlying,
                "iv30": iv_at(atm_term(chain, spot)) if spot else None,
                "spot": spot,
                "runs": runs,
                **skew(research),
                **call_put_spread(research),
                "research_at": research["snapshot_at"].max() if len(research) else None,
            }
        )
    if not rows:
        return cached
    fresh = pd.DataFrame(rows, columns=columns)
    redone = set(zip(fresh["snapshot_date"], fresh["underlying"], strict=True))
    keep = [
        (d, u) not in redone
        for d, u in zip(cached["snapshot_date"], cached["underlying"], strict=True)
    ]
    cached = cached[keep]
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
