from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.storage import Lake, new_run_id, read_parquet_dir, write_raw


def test_raw_files_are_immutable(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    df = pd.DataFrame({"x": [1, 2]})
    path = write_raw(lake, "demo", df, run_id="r1", partition={"ingest_date": "2024-07-01"})
    assert path == tmp_path / "raw/demo/ingest_date=2024-07-01/run-r1.parquet"
    with pytest.raises(FileExistsError):
        write_raw(lake, "demo", df, run_id="r1", partition={"ingest_date": "2024-07-01"})


def test_read_concatenates_all_runs_and_ignores_temp_files(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    write_raw(lake, "demo", pd.DataFrame({"x": [1]}), run_id="a", partition={"d": "1"})
    write_raw(lake, "demo", pd.DataFrame({"x": [2]}), run_id="b", partition={"d": "2"})
    (lake.raw_dir("demo") / "d=2" / ".run-c.parquet.tmp-123").write_bytes(b"partial")
    out = read_parquet_dir(lake.raw_dir("demo"))
    assert sorted(out["x"]) == [1, 2]
    assert "d" not in out.columns  # partition dirs never leak into the data


def test_read_missing_dir_is_empty(tmp_path: Path) -> None:
    assert read_parquet_dir(tmp_path / "nope").empty


def test_run_id_sortable() -> None:
    run_id = new_run_id(datetime(2026, 10, 3, 22, 30, tzinfo=UTC))
    assert run_id.startswith("20261003T223000Z-")
