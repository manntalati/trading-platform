"""Point-in-time reference data from a synthetic lake: fundamentals, valuation across a split,
earnings surprises and macro."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core.bars import load_bars
from tp_core.fundamentals import load_facts, quarterly
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.jobs.fundamentals import run_fundamentals
from tp_ingest.jobs.macro import run_backfill as macro_backfill
from tp_ingest.sources.fake import FakeSource, FakeSplit
from tp_ingest.sources.macro import FakeMacroSource
from tp_ingest.sources.sec import FakeSecSource
from tp_strategies.reference import LakeReference

NOW = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
SPLIT = date(2023, 6, 1)


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    lake = Lake(tmp_path_factory.mktemp("ref"))
    source = FakeSource(splits={"AAA": [FakeSplit(SPLIT, 2.0)]})
    run_backfill(lake, source, ["AAA", "BBB"], now=NOW, years=5)
    run_fundamentals(lake, FakeSecSource(["AAA", "BBB"], years=5), ["AAA", "BBB"], now=NOW)
    macro_backfill(lake, FakeMacroSource(end=NOW.date()), now=NOW)
    return lake


def test_fundamentals_only_once_filed(lake: Lake) -> None:
    ref = LakeReference(lake)
    f = ref.get("fundamentals", date(2024, 6, 1))
    assert set(f.index) == {"AAA", "BBB"}
    assert (f["known"] <= date(2024, 6, 1)).all()
    assert ref.get("fundamentals", date(2020, 1, 1)).empty  # before the first filing


def test_market_cap_carries_across_a_split(lake: Lake) -> None:
    ref = LakeReference(lake)
    before = ref.get("valuation", date(2023, 5, 31)).loc["AAA"]
    after = ref.get("valuation", date(2023, 6, 2)).loc["AAA"]
    assert after["known"] < SPLIT  # shares still as reported before the split
    bars = load_bars(lake, ["AAA"]).set_index("session")
    move = bars.loc[date(2023, 6, 2), "adj_close"] / bars.loc[date(2023, 5, 31), "adj_close"]
    assert after["market_cap"] / before["market_cap"] == pytest.approx(move, rel=1e-6)
    assert after["price"] == pytest.approx(before["price"] * move / 2, rel=1e-6)  # raw halves
    assert after["earnings_yield"] > 0
    assert 0 < after["gross_profitability"] < 1


def test_earnings_surprises_are_split_adjusted_and_dated(lake: Lake) -> None:
    ref = LakeReference(lake)
    e = ref.get("earnings", date(2025, 12, 31))
    assert set(e["symbol"]) == {"AAA", "BBB"}
    assert (e["session"] >= e["known"]).all()
    assert e["sue"].notna().any()
    adjusted = e[e["symbol"] == "AAA"].set_index("end")["eps"]
    reported = quarterly(load_facts(lake, ["AAA"]), "AAA", "eps_diluted", first_reported=True)
    reported = reported.set_index("end")["value"]
    before, after = date(2022, 12, 31), date(2024, 3, 31)  # filed before and after the split
    assert adjusted[before] == pytest.approx(reported[before] * 0.5)
    assert adjusted[after] == pytest.approx(reported[after])
    assert ref.get("earnings", date(2021, 1, 1)).empty


def test_macro_unknown_and_empty(lake: Lake, tmp_path: Path) -> None:
    ref = LakeReference(lake)
    m = ref.get("macro", date(2024, 1, 31))
    assert m.index.max() <= pd.Timestamp("2024-01-31")
    with pytest.raises(KeyError, match="unknown reference"):
        ref.get("news", date(2024, 1, 31))
    with pytest.raises(LookupError, match="tp-data fundamentals"):
        LakeReference(Lake(tmp_path)).get("valuation", date(2024, 1, 31))
