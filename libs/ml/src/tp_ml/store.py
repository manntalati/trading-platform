"""Versioned feature sets in the lake, with a registry.

``build_feature_set`` computes ``tp_ml.features`` for the symbols with bars and writes
``<data root>/features/<name>/<version>/part.parquet``. The version is the hash of the feature
definitions and parameters plus the data it was built from (rows and last session), so the same
code on the same data gives the same version and a model can name exactly what it trained on.
``features/registry.json`` lists every version: columns, rows, sessions, the git commit and when.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core.bars import load_bars
from tp_core.macro import load_macro
from tp_core.portfolio import Classifier
from tp_core.reference import LakeReference
from tp_core.storage import Lake, read_parquet_dir, write_json, write_parquet
from tp_core.versions import data_version, git_version
from tp_ml.features import FEATURES, compute_features, feature_spec

FIELDS = ("open", "high", "low", "close", "volume")


def features_dir(lake: Lake) -> Path:
    return lake.root / "features"


def wide_bars(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Adjusted OHLCV as wide frames (sessions x symbols) from ``load_bars``' long format."""
    out = {}
    for name in FIELDS:
        w = bars.pivot(index="session", columns="symbol", values=f"adj_{name}")
        w.index = pd.DatetimeIndex(w.index)
        w.columns.name = None
        out[name] = w.sort_index()
    return out


def registry(lake: Lake) -> list[dict[str, Any]]:
    path = features_dir(lake) / "registry.json"
    entries: list[dict[str, Any]] = json.loads(path.read_text()) if path.exists() else []
    return entries


def build_feature_set(
    lake: Lake,
    *,
    name: str = "daily",
    symbols: Sequence[str] | None = None,
    classifier: Classifier | None = None,
    now: datetime,
) -> dict[str, Any]:
    bars = load_bars(lake, list(symbols)) if symbols else load_bars(lake)
    if bars.empty:
        raise LookupError("no daily bars in the lake: run `tp-data bars backfill`")
    macro = load_macro(lake)
    try:
        earnings = LakeReference(lake).earnings_table()
    except LookupError:
        earnings = None
    panel = compute_features(
        wide_bars(bars),
        sectors=classifier.sectors if classifier else None,
        macro=macro,
        earnings=earnings,
    )
    data = data_version(bars)
    spec = feature_spec(macro=not macro.empty, earnings=earnings is not None)
    version = hashlib.sha256(f"{spec}|{data['rows']}|{data['last_session']}".encode()).hexdigest()
    version = version[:12]
    path = features_dir(lake) / name / version / "part.parquet"
    write_parquet(panel.reset_index(), path)
    entry = {
        "name": name,
        "version": version,
        "spec": spec,
        "columns": list(FEATURES),
        "rows": len(panel),
        "symbols": int(panel.index.get_level_values("symbol").nunique()),
        "first_session": str(panel.index.get_level_values("date").min().date()),
        "last_session": str(panel.index.get_level_values("date").max().date()),
        "data": data,
        "code": git_version(),
        "created_at": now.isoformat(),
        "path": str(path),
    }
    entries = [e for e in registry(lake) if not (e["name"] == name and e["version"] == version)]
    write_json([*entries, entry], features_dir(lake) / "registry.json")
    return entry


def load_feature_set(lake: Lake, name: str = "daily", version: str | None = None) -> pd.DataFrame:
    """A stored feature set (the newest version unless one is named), indexed (date, symbol)."""
    entries = [e for e in registry(lake) if e["name"] == name]
    if version is not None:
        entries = [e for e in entries if e["version"] == version]
    if not entries:
        raise LookupError(f"no feature set {name!r}{f' version {version}' if version else ''}: "
                          "run `tp-ml features`")  # fmt: skip
    chosen = max(entries, key=lambda e: e["created_at"])
    df = read_parquet_dir(Path(chosen["path"]).parent)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index(["date", "symbol"]).sort_index()
