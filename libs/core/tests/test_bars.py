import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.bars import build_clean_bars, close_matrix, load_bars, missing_symbols
from tp_core.schemas import (
    CLEAN_STOCK_BARS_1D,
    QUARANTINE_STOCK_BARS_1D,
    RAW_BARS_SCHEMA,
    RAW_CORPORATE_ACTIONS,
    RAW_CORPORATE_ACTIONS_SCHEMA,
    RAW_STOCK_BARS_1D,
    conform,
)
from tp_core.storage import Lake, read_parquet_dir, write_raw
from tp_core.testing import actions, raw_bars

NOW = datetime(2024, 7, 1, 22, 0, tzinfo=UTC)
LATER = datetime(2024, 7, 2, 22, 0, tzinfo=UTC)


def _store(lake: Lake, df: pd.DataFrame, run_id: str) -> None:
    write_raw(
        lake,
        RAW_STOCK_BARS_1D,
        conform(df, RAW_BARS_SCHEMA),
        run_id=run_id,
        partition={"ingest_date": "x"},
        schema=RAW_BARS_SCHEMA,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path)


def test_build_adjusts_and_load_reads_back(lake: Lake) -> None:
    _store(
        lake,
        pd.concat([raw_bars("AAA", [100, 102, 51, 52]), raw_bars("BBB", [10, 11, 12, 13])]),
        "r1",
    )
    split = actions(
        {
            "symbol": "AAA",
            "action_type": "forward_split",
            "ex_date": date(2024, 6, 5),
            "new_rate": 2.0,
            "old_rate": 1.0,
        }
    )
    write_raw(
        lake,
        RAW_CORPORATE_ACTIONS,
        split,
        run_id="r1",
        partition={},
        schema=RAW_CORPORATE_ACTIONS_SCHEMA,
    )

    report = build_clean_bars(lake, now=NOW)

    assert report.ok
    assert (report.rows_in, report.rows_clean, report.symbols) == (8, 8, 2)
    bars = load_bars(lake, ["AAA"])
    np.testing.assert_allclose(bars["adj_close"], [50, 51, 51, 52])
    np.testing.assert_allclose(bars["close"], [100, 102, 51, 52])
    assert bars["session"].iloc[0] == date(2024, 6, 3)
    reports = list(lake.reports_dir("validation").glob("*.json"))
    assert json.loads(reports[0].read_text())["ok"] is True


def test_latest_ingest_wins(lake: Lake) -> None:
    _store(lake, raw_bars("AAA", [10, 11, 12], run_id="r1", ingested_at=NOW), "r1")
    corrected = raw_bars("AAA", [10, 11, 12.5], run_id="r2", ingested_at=LATER).iloc[[2]]
    _store(lake, corrected, "r2")
    report = build_clean_bars(lake, now=LATER)
    assert report.ok
    assert list(load_bars(lake)["close"]) == [10, 11, 12.5]


def test_bad_rows_are_quarantined(lake: Lake) -> None:
    bad = raw_bars("AAA", [10, 11, 12])
    bad.loc[1, "close"] = -1.0
    _store(lake, bad, "r1")
    report = build_clean_bars(lake, now=NOW)
    assert not report.ok
    assert report.rows_quarantined == 1
    assert len(load_bars(lake)) == 2
    quarantined = read_parquet_dir(lake.clean_dir(QUARANTINE_STOCK_BARS_1D))
    assert list(quarantined["reason"]) == ["bad_price"]


def test_symbol_fully_quarantined_loses_stale_clean_file(lake: Lake) -> None:
    _store(lake, raw_bars("AAA", [10, 11], run_id="r1", ingested_at=NOW), "r1")
    build_clean_bars(lake, now=NOW)
    assert (lake.clean_dir(CLEAN_STOCK_BARS_1D) / "symbol=AAA").exists()
    _store(lake, raw_bars("AAA", [-1, -1], run_id="r2", ingested_at=LATER), "r2")
    build_clean_bars(lake, now=LATER)
    assert not (lake.clean_dir(CLEAN_STOCK_BARS_1D) / "symbol=AAA").exists()


def test_load_bars_filters_and_errors(lake: Lake) -> None:
    _store(lake, pd.concat([raw_bars("AAA", [1, 2, 3]), raw_bars("BBB", [4, 5, 6])]), "r1")
    build_clean_bars(lake, now=NOW)
    window = load_bars(lake, start=date(2024, 6, 4), end=date(2024, 6, 4))
    assert list(window["symbol"]) == ["AAA", "BBB"]
    with pytest.raises(KeyError, match="ZZZ"):
        load_bars(lake, ["AAA", "ZZZ"])
    assert missing_symbols(lake, ["AAA", "ZZZ", "BBB"]) == ["ZZZ"]


def test_close_matrix(lake: Lake) -> None:
    _store(lake, pd.concat([raw_bars("AAA", [1, 2, 3]), raw_bars("BBB", [4, 5, 6])]), "r1")
    build_clean_bars(lake, now=NOW)
    wide = close_matrix(load_bars(lake))
    assert list(wide.columns) == ["AAA", "BBB"]
    assert isinstance(wide.index, pd.DatetimeIndex)
    assert wide.loc["2024-06-05", "BBB"] == 6


def test_empty_raw_zone(lake: Lake) -> None:
    report = build_clean_bars(lake, now=NOW)
    assert report.rows_in == 0
    assert report.ok
