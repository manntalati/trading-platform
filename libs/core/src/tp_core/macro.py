"""Macro series from FRED: implied volatility and Treasury rates, as of each day's close.

- **VIX** (``VIXCLS``) and **VIX 3-month** (``VXVCLS``): the market's 30-day and 3-month implied
  volatility of the S&P 500. VIX above its 3-month version (an inverted volatility curve) is
  what stress looks like; it usually sits below.
- **Treasury yields**: 3 months (``DGS3MO``), 2 years (``DGS2``) and 10 years (``DGS10``), in
  percent. The 3-month yield is the risk-free rate for option pricing and Sharpe ratios; the
  10-year minus the 3-month is the yield curve (negative: inverted).

Stored like the bars: every download is an immutable raw file
(``raw/fred/macro_1d/ingest_date=.../run-....parquet``, columns ``series, date, value``), and
``clean/macro_1d/part.parquet`` is rebuilt from all of them, the latest download winning for each
series and day. Each value is the one published for that day's close; FRED's daily series are
not revised in practice, so the history is point-in-time for daily work.

Labels from them (``macro_regimes``), each from data up to that day:

- **Volatility** from VIX: calm under 15, stressed over 25.
- **Rates**: the 10-year yield more than a quarter point above its 200-day average is rising,
  more than a quarter point below is falling, else steady.
- **Curve**: 10-year minus 3-month under zero is inverted, under half a point flat, else normal.
"""

from __future__ import annotations

import io
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core.storage import Lake, read_parquet_dir, write_parquet

DATASET = "fred/macro_1d"
CLEAN = "macro_1d"
SERIES: dict[str, str] = {
    "VIXCLS": "VIX (S&P 500 30-day implied volatility)",
    "VXVCLS": "VIX 3-month",
    "DGS3MO": "3-month Treasury yield",
    "DGS2": "2-year Treasury yield",
    "DGS10": "10-year Treasury yield",
}
FIRST_DATE = date(1990, 1, 2)  # VIX starts here; the Treasury series go back further
SCHEMA = pa.schema(
    [
        ("series", pa.string()),
        ("date", pa.date32()),
        ("value", pa.float64()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)

VIX_CALM, VIX_STRESSED = 15.0, 25.0
RATE_BAND = 0.25  # percentage points around the 200-day average
CURVE_FLAT = 0.5
MACRO_DIMENSIONS = ("rates", "curve")


def parse_fred_csv(text: str, series: str) -> pd.DataFrame:
    """FRED's ``fredgraph.csv``: a date column (``observation_date``, or ``DATE`` in older
    downloads) and one value column; missing days are ``.``. Returns ``series, date, value``
    with the missing days dropped."""
    raw = pd.read_csv(io.StringIO(text), dtype=str)
    if raw.shape[1] < 2:
        raise ValueError(f"{series}: expected a date and a value column, got {list(raw.columns)}")
    dates = pd.to_datetime(raw.iloc[:, 0], errors="coerce")
    values = pd.to_numeric(raw.iloc[:, 1].replace(".", None), errors="coerce")
    out = pd.DataFrame({"series": series, "date": dates.dt.date, "value": values})
    return out[dates.notna() & values.notna()].reset_index(drop=True)


def build_clean_macro(lake: Lake) -> pd.DataFrame:
    """Rebuild ``clean/macro_1d`` from every raw download: the latest wins per series and day."""
    raw = read_parquet_dir(lake.raw_dir(DATASET), SCHEMA)
    if raw.empty:
        return raw
    clean = (
        raw.sort_values("ingested_at")
        .drop_duplicates(["series", "date"], keep="last")
        .sort_values(["series", "date"])
        .reset_index(drop=True)
    )
    write_parquet(clean, lake.clean_dir(CLEAN) / "part.parquet", schema=SCHEMA)
    return clean


def load_macro(lake: Lake, end: date | None = None) -> pd.DataFrame:
    """Wide table: one row per date, one column per series (``VIXCLS``, ``DGS10``...); empty
    when nothing has been downloaded yet. ``end`` keeps only dates up to it."""
    long = read_parquet_dir(lake.clean_dir(CLEAN), SCHEMA)
    if long.empty:
        return pd.DataFrame()
    if end is not None:
        long = long[long["date"] <= end]
    wide = long.pivot_table(index="date", columns="series", values="value", aggfunc="last")
    wide.index = pd.DatetimeIndex(pd.to_datetime(wide.index))
    wide.columns.name = None
    return wide.sort_index()


def risk_free_rate(lake: Lake, default: float, at: date | None = None) -> float:
    """The latest 3-month Treasury yield as a decimal (0.043 for 4.3%), or ``default`` when it
    hasn't been downloaded (or is more than ten days old at ``at``)."""
    macro = load_macro(lake, end=at)
    if macro.empty or "DGS3MO" not in macro.columns:
        return default
    s = macro["DGS3MO"].dropna()
    if s.empty:
        return default
    when = at or datetime.now().astimezone().date()
    if (pd.Timestamp(when) - s.index[-1]).days > 10:
        return default
    return float(s.iloc[-1]) / 100.0


def macro_regimes(macro: pd.DataFrame, index: pd.Index | None = None) -> pd.DataFrame:
    """Per date (``index``, else the macro table's own): ``vix``, ``vix_3m``, ``vix_ratio``,
    ``vix_volatility`` (calm/normal/stressed), ``rate_10y``, ``rate_3m``, ``rate_10y_vs_ma200``,
    ``rates`` (rising/steady/falling), ``curve_10y_3m`` and ``curve`` (normal/flat/inverted).
    Values are carried forward over market holidays (at most five days)."""
    if macro.empty:
        return pd.DataFrame(index=index)
    m = macro.sort_index()
    target = pd.DatetimeIndex(index) if index is not None else m.index
    union = m.index.union(target)
    m = m.reindex(union).ffill(limit=5)

    def col(name: str) -> pd.Series:
        return m[name] if name in m.columns else pd.Series(np.nan, index=m.index)

    vix, vix3m, r10, r3m = col("VIXCLS"), col("VXVCLS"), col("DGS10"), col("DGS3MO")
    ma = r10.rolling(200, min_periods=150).mean()
    curve = r10 - r3m
    out = pd.DataFrame(
        {
            "vix": vix,
            "vix_3m": vix3m,
            "vix_ratio": vix / vix3m,
            "vix_volatility": [_vix_label(v) for v in vix],
            "rate_10y": r10,
            "rate_3m": r3m,
            "rate_10y_vs_ma200": r10 - ma,
            "rates": [_rates_label(v) for v in r10 - ma],
            "curve_10y_3m": curve,
            "curve": [_curve_label(v) for v in curve],
        },
        index=m.index,
    )
    return out.reindex(target)


def _vix_label(v: float) -> str | None:
    if v != v:
        return None
    return "calm" if v < VIX_CALM else "stressed" if v > VIX_STRESSED else "normal"


def _rates_label(v: float) -> str | None:
    if v != v:
        return None
    return "rising" if v > RATE_BAND else "falling" if v < -RATE_BAND else "steady"


def _curve_label(v: float) -> str | None:
    if v != v:
        return None
    return "inverted" if v < 0 else "flat" if v < CURVE_FLAT else "normal"


def latest(macro: pd.DataFrame) -> dict[str, Any]:
    """Each series' latest value and its date."""
    out: dict[str, Any] = {}
    for s in macro.columns:
        col = macro[s].dropna()
        if len(col):
            v = float(col.iloc[-1])
            out[str(s)] = {
                "value": v if math.isfinite(v) else None,
                "date": pd.Timestamp(col.index[-1]).date().isoformat(),
                "name": SERIES.get(str(s), str(s)),
            }
    return out
