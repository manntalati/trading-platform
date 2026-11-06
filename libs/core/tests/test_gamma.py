"""Dealer gamma (GEX) from chain snapshots: the per-contract number, the flip level, the store."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.gamma import (
    contract_gex,
    current_gamma,
    day_gamma,
    flip_level,
    gamma_history,
    gex_profile,
)
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, write_raw
from tp_core.testing import synthetic_chain

AT = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)  # 15:45 New York
DAY = AT.date()


def chain(oi_calls: float = 1000.0, oi_puts: float = 1000.0, spot: float = 100.0) -> pd.DataFrame:
    c = synthetic_chain("XYZ", spot, AT, expirations=[DAY + timedelta(days=d) for d in (10, 40)],
                        iv=0.3)  # fmt: skip
    c["open_interest"] = np.where(c["right"] == "C", oi_calls, oi_puts)
    return c


def store(lake: Lake, c: pd.DataFrame, day: date, run_id: str) -> None:
    write_raw(lake, RAW_OPTION_CHAIN_SNAPSHOTS, c, run_id=run_id,
              partition={"snapshot_date": day.isoformat(), "underlying": "XYZ"},
              schema=RAW_OPTION_CHAIN_SCHEMA)  # fmt: skip


def test_calls_count_positive_and_puts_negative() -> None:
    c = chain()
    gex = contract_gex(c, 100.0)
    row = c.iloc[0]
    expected = row["gamma"] * row["open_interest"] * 100 * 100.0**2 * 0.01
    assert gex.iloc[0] == pytest.approx(expected if row["right"] == "C" else -expected)
    # equal open interest on both sides: call and put gammas match, so the total is ~0
    out = day_gamma(c)
    assert out["call_gex_bn"] > 0 > out["put_gex_bn"]
    assert out["gex_bn"] == pytest.approx(0.0, abs=1e-12)
    assert out["contracts"] == len(c)


def test_the_recomputed_total_matches_the_vendor_gammas() -> None:
    out = day_gamma(chain(oi_calls=3000.0))
    assert out["gex_bn"] > 0
    assert out["gex_model_bn"] == pytest.approx(out["gex_bn"], rel=0.01)


def test_flip_level_is_where_the_profile_crosses_zero() -> None:
    spots = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
    assert flip_level(spots, np.array([-2.0, -1.0, 1.0, 2.0, 3.0]), 100.0) == pytest.approx(97.5)
    assert flip_level(spots, np.array([1.0, 1.0, 1.0, 1.0, 1.0]), 100.0) is None
    # two crossings: the one nearest the spot
    assert flip_level(spots, np.array([1.0, -1.0, -1.0, -1.0, 1.0]), 100.0) == pytest.approx(92.5)


def test_heavy_puts_below_and_calls_above_give_a_flip_between_them() -> None:
    c = chain()
    c["open_interest"] = np.where(
        (c["right"] == "P") & (c["strike"] < 100), 5000.0,
        np.where((c["right"] == "C") & (c["strike"] > 100), 5000.0, 0.0),
    )  # fmt: skip
    out = day_gamma(c)
    assert out["flip"] is not None
    assert 90 < out["flip"] < 110
    spots = np.array([out["flip"] - 1, out["flip"] + 1])
    below, above = gex_profile(c, spots)
    assert below < 0 < above


def test_expired_contracts_are_left_out_of_the_profile() -> None:
    c = chain()
    c["snapshot_at"] = pd.Timestamp(AT + timedelta(days=11))  # the 10-day expiry is gone
    alive = c[c["dte"] > 10]
    spots = np.array([100.0])
    assert gex_profile(c, spots) == pytest.approx(gex_profile(alive, spots))


def test_largest_open_interest_strikes() -> None:
    c = chain()
    c.loc[(c["right"] == "C") & (c["strike"] == 110), "open_interest"] = 9000.0
    c.loc[(c["right"] == "P") & (c["strike"] == 90) & (c["dte"] == 40), "open_interest"] = 9000.0
    out = day_gamma(c)
    assert out["call_oi_strike"] == 110.0
    assert out["put_oi_strike"] == 90.0
    assert out["call_oi_strike_week"] is None  # nothing expires within 7 days


def test_history_uses_each_days_last_snapshot_and_redoes_a_day_that_changed(
    tmp_path: Path,
) -> None:
    lake = Lake(tmp_path)
    store(lake, chain(oi_calls=3000.0), DAY, "r1")
    first = gamma_history(lake)
    assert len(first) == 1
    assert first["gex_bn"].iloc[0] > 0
    assert first["snapshot_kind"].iloc[0] == "close"  # older files: worked out from the clock
    assert (tmp_path / "clean" / "dealer_gamma_daily" / "part.parquet").exists()

    later = chain(oi_puts=3000.0)
    later["snapshot_at"] = pd.Timestamp(AT + timedelta(hours=5))  # 20:45 New York
    store(lake, later, DAY, "r2")
    second = gamma_history(lake)
    assert len(second) == 1
    assert second["gex_bn"].iloc[0] < 0
    assert second["snapshot_kind"].iloc[0] == "after_close"
    assert int(second["runs"].iloc[0]) == 2

    now = current_gamma(second, ("XYZ",))
    assert now[0]["sign"] == "negative"
    assert now[0]["underlying"] == "XYZ"
    assert gamma_history(lake, ["ABC"]).equals(second)  # nothing to do for others
