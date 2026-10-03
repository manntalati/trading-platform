"""Local Parquet data lake.

Layout under ``<data_root>``::

    raw/<dataset>/<key>=<value>/run-<UTC timestamp>-<id>.parquet   append-only, never rewritten
    clean/<dataset>/symbol=<SYM>/part.parquet                     derived, rebuilt from raw
    reports/<kind>/<name>.json                                     job outputs (validation, ...)

Raw files are immutable: every ingest run writes new files and nothing in ``raw/`` is ever
overwritten, so any clean table can be rebuilt (and any bug in cleaning fixed) after the fact.
All writes are atomic (temp file + rename), so readers never see a half-written file.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Lake:
    root: Path

    def raw_dir(self, dataset: str) -> Path:
        return self.root / "raw" / dataset

    def clean_dir(self, dataset: str) -> Path:
        return self.root / "clean" / dataset

    def reports_dir(self, kind: str) -> Path:
        return self.root / "reports" / kind


def new_run_id(now: datetime) -> str:
    """Sortable, unique id for one job run, e.g. ``20261003T223000Z-1a2b3c4d``."""
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _atomic_write_bytes(path: Path, write: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_parquet(df: pd.DataFrame, path: Path, schema: pa.Schema | None = None) -> Path:
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    _atomic_write_bytes(path, lambda tmp: pq.write_table(table, tmp))
    return path


def write_json(obj: Any, path: Path) -> Path:
    text = json.dumps(obj, indent=2, sort_keys=True, default=str)
    _atomic_write_bytes(path, lambda tmp: tmp.write_text(text + "\n", encoding="utf-8"))
    return path


def write_raw(
    lake: Lake,
    dataset: str,
    df: pd.DataFrame,
    *,
    run_id: str,
    partition: Mapping[str, str],
    schema: pa.Schema | None = None,
) -> Path:
    """Append one immutable file to the raw zone. Refuses to overwrite an existing file."""
    directory = lake.raw_dir(dataset)
    for key, value in partition.items():
        directory = directory / f"{key}={value}"
    path = directory / f"run-{run_id}.parquet"
    if path.exists():
        raise FileExistsError(f"raw files are immutable and {path} already exists")
    return write_parquet(df, path, schema=schema)


def parquet_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.rglob("*.parquet") if not p.name.startswith("."))


def read_parquet_dir(directory: Path, schema: pa.Schema | None = None) -> pd.DataFrame:
    """Read every Parquet file under ``directory`` into one DataFrame.

    Partition directories are ignored when reading: every file already carries its own
    columns, so the data never depends on the path it happens to live under.
    """
    files = parquet_files(directory)
    if not files:
        if schema is None:
            return pd.DataFrame()
        empty: pd.DataFrame = schema.empty_table().to_pandas()
        return empty
    dataset = pads.dataset([str(f) for f in files], format="parquet", schema=schema)
    df: pd.DataFrame = dataset.to_table().to_pandas()
    return df
