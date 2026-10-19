"""Models in use: kept, retrained on schedule, and watched.

    data/models/<model>/<version>/model.pkl, meta.json   the registry: every version trained for use
    clean/ml_predictions/part.parquet                    every prediction made with them
    reports/ml_monitor/<date>-<model>.json               each day's check

**Drift** is the population stability index (PSI) of each input feature: the last month's values
against the training window's last year, binned at the training deciles. Under 0.1 the feature is
stable, 0.1 to 0.25 moving, above 0.25 shifted (the thresholds credit scoring uses): a model fed
inputs unlike its training data is extrapolating.

**Live accuracy** compares the predictions made since a model went live with what happened: the
rank correlation of a ranker's scores with the following returns (on dates a holding period
apart, so the periods don't overlap), and a classifier's hit rate against the base rate.

Pickles in the registry are written by this code into your data folder; don't load ones from
anywhere else.
"""

from __future__ import annotations

import hashlib
import json
import math
import pickle
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core.storage import Lake, read_parquet_dir, write_json, write_parquet

MODELS = "models"
PREDICTIONS = "ml_predictions"
REPORTS = "ml_monitor"
STABLE, SHIFTED = 0.1, 0.25
PREDICTIONS_SCHEMA = pa.schema(
    [
        ("model", pa.string()),
        ("version", pa.string()),
        ("date", pa.date32()),
        ("symbol", pa.string()),
        ("score", pa.float64()),
    ]
)


# -- drift -----------------------------------------------------------------------------------------


def profile(frame: pd.DataFrame, columns: Sequence[str]) -> dict[str, dict[str, list[float]]]:
    """Each column's training deciles and the share of the training values in each bin: what
    later values are compared with."""
    out = {}
    for col in columns:
        if col not in frame.columns:
            continue
        values = frame[col].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < 50:
            continue
        edges = np.unique(np.quantile(values, np.linspace(0.1, 0.9, 9)))
        out[col] = {"edges": edges.tolist(), "expected": _shares(values, edges).tolist()}
    return out


def _shares(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    counts = np.bincount(np.searchsorted(edges, values, side="right"), minlength=len(edges) + 1)
    return np.maximum(counts / max(len(values), 1), 1e-4)


def psi(expected: Sequence[float], edges: Sequence[float], values: np.ndarray) -> float | None:
    """The population stability index of ``values`` against a training profile."""
    values = values[np.isfinite(values)]
    if len(values) < 20:
        return None
    e = np.asarray(expected, dtype=float)
    a = _shares(values, np.asarray(edges, dtype=float))
    return float(np.sum((a - e) * np.log(a / e)))


def drift(
    profiles: Mapping[str, Mapping[str, list[float]]], current: pd.DataFrame
) -> list[dict[str, Any]]:
    """PSI per feature, most shifted first."""
    rows = []
    for col, prof in profiles.items():
        if col not in current.columns:
            continue
        value = psi(prof["expected"], prof["edges"], current[col].to_numpy(dtype=float))
        if value is None:
            continue
        level = "stable" if value < STABLE else "moving" if value < SHIFTED else "shifted"
        rows.append({"feature": col, "psi": value, "level": level})
    return sorted(rows, key=lambda r: -r["psi"])


# -- the registry ----------------------------------------------------------------------------------


def models_dir(lake: Lake, name: str) -> Path:
    return lake.root / MODELS / name


def register(lake: Lake, name: str, model: Any, meta: Mapping[str, Any]) -> dict[str, Any]:
    """Store ``model`` as a new version of ``name`` with ``meta`` (it should say what it was
    trained on: ``cutoff``, parameters, training metrics). Returns the stored meta."""
    blob = pickle.dumps(model)
    cutoff = pd.Timestamp(meta.get("cutoff") or datetime.now(UTC)).strftime("%Y%m%d")
    version = f"{cutoff}-{hashlib.sha256(blob).hexdigest()[:8]}"
    folder = models_dir(lake, name) / version
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "model.pkl.tmp"
    tmp.write_bytes(blob)
    tmp.replace(folder / "model.pkl")
    stored = {"name": name, "version": version, **_plain(dict(meta))}
    write_json(stored, folder / "meta.json")
    return stored


def versions(lake: Lake, name: str) -> list[dict[str, Any]]:
    """Every registered version of ``name``, oldest first."""
    out = []
    for meta in sorted(models_dir(lake, name).glob("*/meta.json")):
        try:
            out.append(json.loads(meta.read_text()))
        except ValueError:
            continue
    return sorted(out, key=lambda m: (str(m.get("trained_at", "")), m["version"]))


def latest(lake: Lake, name: str) -> dict[str, Any] | None:
    found = versions(lake, name)
    return found[-1] if found else None


def load_model(lake: Lake, name: str, version: str) -> Any:
    with (models_dir(lake, name) / version / "model.pkl").open("rb") as f:
        return pickle.load(f)  # our own registry, under the data root


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, date | pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return _plain(value.item())
    return value


# -- predictions -----------------------------------------------------------------------------------


def load_predictions(lake: Lake, name: str | None = None) -> pd.DataFrame:
    path = lake.clean_dir(PREDICTIONS)
    if not (path / "part.parquet").exists():
        return pd.DataFrame(columns=[f.name for f in PREDICTIONS_SCHEMA])
    df = read_parquet_dir(path, PREDICTIONS_SCHEMA)
    return df[df["model"] == name] if name else df


def record_predictions(lake: Lake, name: str, version: str, day: date, scores: pd.Series) -> int:
    """Store the scores ``name`` gave on ``day`` (replacing any from an earlier run that day)."""
    old = load_predictions(lake)
    keep = old[~((old["model"] == name) & (old["date"] == day))]
    fresh = pd.DataFrame(
        {"model": name, "version": version, "date": day,
         "symbol": [str(s) for s in scores.index], "score": scores.to_numpy(dtype=float)}
    )  # fmt: skip
    frames = [f for f in (keep, fresh) if len(f)]
    out = pd.concat(frames, ignore_index=True) if frames else fresh
    write_parquet(out, lake.clean_dir(PREDICTIONS) / "part.parquet", PREDICTIONS_SCHEMA)
    return len(fresh)


# -- live accuracy ---------------------------------------------------------------------------------


def spaced(days: Sequence[date], sessions: pd.DatetimeIndex, gap: int) -> list[date]:
    """Prediction dates at least ``gap`` sessions apart (the earliest first)."""
    out: list[date] = []
    last = -(10**9)
    positions = {d.date(): i for i, d in enumerate(sessions)}
    for d in sorted(set(days)):
        i = positions.get(d)
        if i is not None and i - last >= gap:
            out.append(d)
            last = i
    return out


def live_rank_ic(preds: pd.DataFrame, closes: pd.DataFrame, horizon: int) -> dict[str, Any]:
    """Rank correlation of each prediction date's scores with the next ``horizon`` sessions'
    returns, on dates ``horizon`` apart so no two periods overlap."""
    sessions = pd.DatetimeIndex(closes.index)
    rows = []
    for d in spaced(list(preds["date"]), sessions, horizon):
        i = int(sessions.searchsorted(pd.Timestamp(d)))
        if i + horizon >= len(sessions):
            continue
        day = preds[preds["date"] == d].set_index("symbol")["score"]
        symbols = [s for s in day.index if s in closes.columns]
        fwd = closes[symbols].iloc[i + horizon] / closes[symbols].iloc[i] - 1
        both = pd.DataFrame({"score": day[symbols], "fwd": fwd}).dropna()
        if len(both) >= 5:
            ic = both["score"].rank().corr(both["fwd"].rank())
            if ic == ic:
                rows.append({"date": d.isoformat(), "ic": float(ic)})
    ics = pd.Series([r["ic"] for r in rows], dtype=float)
    n = len(ics)
    return {
        "dates": n,
        "mean_ic": float(ics.mean()) if n else None,
        "t_stat": float(ics.mean() / (ics.std() / math.sqrt(n)))
        if n > 2 and ics.std() > 0
        else None,
        "positive": float((ics > 0).mean()) if n else None,
        "series": rows,
    }


def live_classification(scored: pd.DataFrame, threshold: float) -> dict[str, Any]:
    """``scored``: predictions with ``score`` (a probability) and the realized ``label`` (1 when
    the trade made money). How the ones the model would take did against all of them."""
    if scored.empty:
        return {"predictions": 0}
    y = scored["label"].astype(int)
    p = scored["score"].astype(float)
    taken = y[p >= threshold]
    out: dict[str, Any] = {
        "predictions": len(scored),
        "base_rate": float(y.mean()),
        "taken": len(taken),
        "taken_hit_rate": float(taken.mean()) if len(taken) else None,
        "brier": float(((p - y) ** 2).mean()),
        "auc": None,
    }
    if y.nunique() == 2:
        from sklearn.metrics import roc_auc_score

        out["auc"] = float(roc_auc_score(y, p))
    return out


# -- reports ---------------------------------------------------------------------------------------


def save_report(lake: Lake, name: str, report: Mapping[str, Any], day: date) -> Path:
    return write_json(
        _plain(dict(report)), lake.reports_dir(REPORTS) / f"{day.isoformat()}-{name}.json"
    )


def latest_reports(lake: Lake) -> dict[str, dict[str, Any]]:
    """The newest check of each model."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(lake.reports_dir(REPORTS).glob("*.json")):
        try:
            report = json.loads(path.read_text())
        except ValueError:
            continue
        out[str(report.get("model"))] = report
    return out
