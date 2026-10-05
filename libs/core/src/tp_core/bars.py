"""Daily bars: raw zone → clean zone build, and the read API used by research and strategies."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from tp_core.adjust import adjust_symbol
from tp_core.calendar import NEW_YORK
from tp_core.schemas import (
    CLEAN_BARS_SCHEMA,
    CLEAN_STOCK_BARS_1D,
    QUARANTINE_STOCK_BARS_1D,
    RAW_BARS_SCHEMA,
    RAW_CORPORATE_ACTIONS,
    RAW_CORPORATE_ACTIONS_SCHEMA,
    RAW_STOCK_BARS_1D,
    conform,
)
from tp_core.storage import Lake, read_parquet_dir, write_json, write_parquet
from tp_core.validate import (
    Issue,
    Severity,
    ValidationReport,
    check_missing_sessions,
    check_price_jumps,
    check_structure,
)

log = logging.getLogger(__name__)


def latest_ingest(raw: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Keep only rows from the most recent run that delivered each key.

    Later runs supersede earlier ones (vendor corrections land this way). All rows of that
    latest run are kept, so genuine within-run duplicates still reach validation.
    """
    if raw.empty:
        return raw
    newest = raw.groupby(keys, dropna=False)["ingested_at"].transform("max")
    return raw[raw["ingested_at"] == newest]


def read_raw_bars(lake: Lake) -> pd.DataFrame:
    raw = read_parquet_dir(lake.raw_dir(RAW_STOCK_BARS_1D), RAW_BARS_SCHEMA)
    raw["session"] = raw["timestamp"].dt.tz_convert(NEW_YORK).dt.date
    return raw


def read_corporate_actions(lake: Lake) -> pd.DataFrame:
    raw = read_parquet_dir(lake.raw_dir(RAW_CORPORATE_ACTIONS), RAW_CORPORATE_ACTIONS_SCHEMA)
    key = ["symbol", "action_type", "ex_date", "rate", "new_rate", "old_rate"]
    newest = latest_ingest(raw, key)
    return newest.drop_duplicates(subset=key).reset_index(drop=True)


def build_clean_bars(
    lake: Lake, *, now: datetime, jump_threshold: float = 0.20
) -> ValidationReport:
    """Rebuild ``clean/stock_bars_1d`` from everything in the raw zone and write a report.

    Deterministic given the raw zone, so it is always safe to re-run.
    """
    raw = read_raw_bars(lake)
    report = ValidationReport(rows_in=len(raw))
    if raw.empty:
        log.warning("no raw bars under %s", lake.raw_dir(RAW_STOCK_BARS_1D))
        _write_report(lake, report, now)
        return report

    current = latest_ingest(raw, ["symbol", "session"])
    good, quarantined, issues = check_structure(current, now=now)
    report.issues.extend(issues)
    actions = read_corporate_actions(lake)

    clean_frames: list[pd.DataFrame] = []
    for symbol, group in good.groupby("symbol"):
        adjusted, problems = adjust_symbol(group, actions[actions["symbol"] == symbol])
        report.issues.extend(
            Issue(p.symbol, p.ex_date, "adjustment", Severity.WARNING, p.detail) for p in problems
        )
        clean = conform(adjusted, CLEAN_BARS_SCHEMA)
        write_parquet(clean, _symbol_path(lake, str(symbol)), schema=CLEAN_BARS_SCHEMA)
        clean_frames.append(clean)

    # A symbol whose rows are now all quarantined must not keep serving a stale clean file.
    keep = {_symbol_dir(lake, str(s)) for s in good["symbol"].unique()}
    for stale in lake.clean_dir(CLEAN_STOCK_BARS_1D).glob("symbol=*"):
        if stale not in keep:
            shutil.rmtree(stale)

    clean_all = pd.concat(clean_frames, ignore_index=True) if clean_frames else pd.DataFrame()
    if not clean_all.empty:
        report.issues.extend(check_missing_sessions(clean_all))
        report.issues.extend(check_price_jumps(clean_all, threshold=jump_threshold))

    report.rows_clean = len(clean_all)
    report.rows_quarantined = len(quarantined)
    report.symbols = int(current["symbol"].nunique())
    if not quarantined.empty:
        path = lake.clean_dir(QUARANTINE_STOCK_BARS_1D) / "part.parquet"
        write_parquet(quarantined.drop(columns=["session"]), path)
    _write_report(lake, report, now)
    log.info("clean bars rebuilt: %s", report.summary())
    return report


def _symbol_dir(lake: Lake, symbol: str) -> Path:
    return lake.clean_dir(CLEAN_STOCK_BARS_1D) / f"symbol={symbol.replace('/', '_')}"


def _symbol_path(lake: Lake, symbol: str) -> Path:
    return _symbol_dir(lake, symbol) / "part.parquet"


def _write_report(lake: Lake, report: ValidationReport, now: datetime) -> None:
    payload = {"generated_at": now.isoformat(), **report.to_dict()}
    write_json(payload, lake.reports_dir("validation") / f"stock_bars_1d-{now:%Y%m%dT%H%M%SZ}.json")


def load_bars(
    lake: Lake,
    symbols: Iterable[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Clean daily bars in long format (one row per symbol and session), sorted.

    Both raw (``open`` … ``volume``) and adjusted (``adj_open`` … ``adj_volume``) columns are
    included; use adjusted prices for returns and raw prices for anything order-related.
    """
    if symbols is None:
        df = read_parquet_dir(lake.clean_dir(CLEAN_STOCK_BARS_1D), CLEAN_BARS_SCHEMA)
    else:
        wanted = list(symbols)
        frames = [read_parquet_dir(_symbol_dir(lake, s), CLEAN_BARS_SCHEMA) for s in wanted]
        df = pd.concat(frames, ignore_index=True)
        missing = sorted(set(wanted) - set(df["symbol"]))
        if missing:
            raise KeyError(f"no clean bars for {missing}; run `tp-data bars backfill` first")
    if start is not None:
        df = df[df["session"] >= start]
    if end is not None:
        df = df[df["session"] <= end]
    return df.sort_values(["symbol", "session"]).reset_index(drop=True)


def close_matrix(bars: pd.DataFrame, *, adjusted: bool = True) -> pd.DataFrame:
    """Wide matrix of closes: index = session (DatetimeIndex), one column per symbol."""
    col = "adj_close" if adjusted else "close"
    wide = bars.pivot(index="session", columns="symbol", values=col)
    wide.index = pd.DatetimeIndex(wide.index, name="session")
    wide.columns.name = None
    return wide.sort_index()
