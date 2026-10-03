"""Ingest → lake → clean bars → strategy → tear sheet, all offline on synthetic data."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from tp_core import metrics as m
from tp_core.bars import close_matrix, load_bars
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeDividend, FakeSource, FakeSplit
from tp_strategies.ma_timing import equal_weight_monthly, ma_timing

GTAA = ["SPY", "EFA", "IEF", "VNQ", "DBC"]


def test_pipeline_end_to_end(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    source = FakeSource(
        splits={"VNQ": [FakeSplit(date(2022, 3, 1), 2.0)]},
        dividends={"SPY": [FakeDividend(date(2022, 6, 17), 1.5)]},
    )
    ingest = run_backfill(lake, source, GTAA, now=datetime(2024, 7, 12, 22, tzinfo=UTC), years=3)
    assert ingest.report.ok

    prices = close_matrix(load_bars(lake, GTAA))[GTAA]
    assert prices.notna().all().all()

    timing = ma_timing(prices)
    bench = equal_weight_monthly(prices, start_after=timing.signals.index[9])
    sheet = m.tear_sheet(pd.DataFrame({"timing": timing.returns, "bench": bench.returns}))

    assert sheet.loc["periods", "timing"] > 400
    assert 0.0 <= timing.exposure.min() <= timing.exposure.max() <= 1.0 + 1e-9
    assert not timing.trades.empty
    # The split is invisible in adjusted prices, so it can't trigger a spurious exit.
    assert abs(prices["VNQ"].pct_change().loc["2022-03-01"]) < 0.1
