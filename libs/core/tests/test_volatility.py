"""Implied volatility from chain snapshots: term structure, 30-day IV, events, history, rank."""

import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, write_raw
from tp_core.testing import synthetic_chain
from tp_core.volatility import (
    atm_term,
    call_put_spread,
    event_expiry,
    iv_at,
    iv_history,
    iv_rank,
    latest_chains,
    skew,
)

AT = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)
DAY = AT.date()


def expiries(*days: int) -> list[date]:
    return [DAY + timedelta(days=d) for d in days]


def store(lake: Lake, chain: pd.DataFrame, day: date, underlying: str, run: str = "") -> None:
    write_raw(
        lake,
        RAW_OPTION_CHAIN_SNAPSHOTS,
        chain,
        run_id=f"r-{day}-{underlying}{run}",
        partition={"snapshot_date": day.isoformat(), "underlying": underlying},
        schema=RAW_OPTION_CHAIN_SCHEMA,
    )


def test_term_structure_and_30_day_iv() -> None:
    chain = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(10, 40, 90), iv=[0.5, 0.3, 0.4])
    term = atm_term(chain)
    assert list(term["dte"]) == [10, 40, 90]
    assert list(term["strike"]) == [100.0] * 3
    assert list(term["iv"]) == pytest.approx([0.5, 0.3, 0.4])
    # between 10 and 40 days, interpolated in total variance
    w = 0.25 * 10 + (0.09 * 40 - 0.25 * 10) * (30 - 10) / 30
    assert iv_at(term, 30) == pytest.approx(math.sqrt(w / 30))
    assert iv_at(term, 5) == pytest.approx(0.5)
    assert iv_at(term, 200) == pytest.approx(0.4)


def test_missing_vendor_iv_is_solved_from_the_mid() -> None:
    chain = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(30), iv=0.35, half_spread=0.0)
    chain["implied_volatility"] = None
    assert atm_term(chain)["iv"].iloc[0] == pytest.approx(0.35, abs=0.01)


def test_an_event_shows_as_a_bump_in_the_term_structure() -> None:
    chain = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(4, 11, 18, 46),
                            iv=[0.30, 0.55, 0.40, 0.33])  # fmt: skip
    event = event_expiry(atm_term(chain))
    assert event is not None
    assert event["by"] == expiries(11)[0].isoformat()
    assert event["after"] == expiries(4)[0].isoformat()
    smooth = synthetic_chain(
        "XYZ", 100.0, AT, expirations=expiries(11, 18, 46), iv=[0.3, 0.31, 0.32]
    )
    assert event_expiry(atm_term(smooth)) is None


def test_history_is_derived_once_and_extended(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    for i, vol in enumerate([0.2, 0.3, 0.4]):
        day = DAY + timedelta(days=i)
        at = AT + timedelta(days=i)
        store(
            lake,
            synthetic_chain("XYZ", 100.0, at, expirations=expiries(30, 60), iv=vol),
            day,
            "XYZ",
        )
    first = iv_history(lake)
    assert list(first["iv30"]) == pytest.approx([0.2, 0.3, 0.4], abs=1e-6)
    assert (tmp_path / "clean" / "option_iv_daily" / "part.parquet").exists()

    store(lake, synthetic_chain("ABC", 50.0, AT, expirations=expiries(30), iv=0.5), DAY, "ABC")
    second = iv_history(lake)
    assert len(second) == 4
    assert set(second["underlying"]) == {"XYZ", "ABC"}

    latest = latest_chains(lake)
    assert set(latest) == {"XYZ", "ABC"}
    assert latest["XYZ"]["implied_volatility"].iloc[0] == pytest.approx(0.4)

    # a second snapshot later the same day (midday, then the close) replaces that day's value
    later = AT + timedelta(days=2, hours=3)
    write_raw(lake, RAW_OPTION_CHAIN_SNAPSHOTS,
              synthetic_chain("XYZ", 100.0, later, expirations=expiries(30, 60), iv=0.45),
              run_id="r-close", partition={"snapshot_date": (DAY + timedelta(days=2)).isoformat(),
                                           "underlying": "XYZ"},
              schema=RAW_OPTION_CHAIN_SCHEMA)  # fmt: skip
    third = iv_history(lake)
    assert len(third) == 4
    xyz = third[third["underlying"] == "XYZ"]["iv30"].tolist()
    assert xyz == pytest.approx([0.2, 0.3, 0.45], abs=1e-6)
    assert latest_chains(lake, since=DAY + timedelta(days=1)).keys() == {"XYZ"}


def test_skew_from_the_25_delta_put_and_call() -> None:
    flat = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(30), iv=0.3)
    s = skew(flat)
    assert s["put25_iv30"] == pytest.approx(0.3, abs=1e-6)
    assert s["skew25"] == pytest.approx(0.0, abs=1e-6)
    smirk = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(20, 40), iv=0.3, skew=-0.5,
                            strikes=[float(k) for k in range(60, 141)])  # fmt: skip
    s = skew(smirk)
    assert s["put25_iv30"] > 0.3 > s["call25_iv30"]  # puts dearer
    assert s["skew25"] == pytest.approx(s["put25_iv30"] - s["call25_iv30"])
    assert skew(smirk.iloc[0:0])["skew25"] is None


def test_call_minus_put_iv_at_matched_strikes() -> None:
    chain = synthetic_chain("XYZ", 100.0, AT, expirations=expiries(5, 30, 90), iv=0.3)
    assert call_put_spread(chain)["cp_spread"] == pytest.approx(0.0, abs=1e-9)
    calls = chain["right"] == "C"
    chain.loc[calls, "implied_volatility"] += 0.02
    out = call_put_spread(chain)
    assert out["cp_spread"] == pytest.approx(0.02)
    # near the money (within 10%) and 10-60 days out only: 90 to 110 at 30 days
    assert out["cp_pairs"] == 5
    chain["open_interest"] = float("nan")
    assert call_put_spread(chain)["cp_spread"] == pytest.approx(0.02)  # equal weights


def test_history_carries_skew_columns(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    store(lake, synthetic_chain("XYZ", 100.0, AT, expirations=expiries(30, 60), iv=0.3), DAY,
          "XYZ")  # fmt: skip
    row = iv_history(lake).iloc[0]
    assert row["skew25"] == pytest.approx(0.0, abs=1e-6)
    assert row["cp_spread"] == pytest.approx(0.0, abs=1e-9)
    assert row["cp_pairs"] > 0
    assert row["research_at"] == pd.Timestamp(AT)


def test_skew_columns_skip_snapshots_after_the_close(tmp_path: Path) -> None:
    """An evening snapshot's implied volatilities use the after-hours price: iv30 reads it, the
    skew and the call-minus-put spread keep to the session's last snapshot."""
    lake = Lake(tmp_path)
    store(lake, synthetic_chain("XYZ", 100.0, AT, expirations=expiries(30, 60), iv=0.3), DAY,
          "XYZ")  # fmt: skip
    evening = AT + timedelta(hours=5)  # 20:45 New York
    late = synthetic_chain("XYZ", 100.0, evening, expirations=expiries(30, 60), iv=0.4)
    late.loc[late["right"] == "C", "implied_volatility"] += 0.05  # calls look rich
    store(lake, late, DAY, "XYZ", run="-evening")
    row = iv_history(lake).iloc[0]
    assert row["iv30"] > 0.35  # the day's last snapshot
    assert row["research_at"] == pd.Timestamp(AT)
    assert row["cp_spread"] == pytest.approx(0.0, abs=1e-9)


def test_iv_rank_needs_some_history() -> None:
    short = iv_rank(pd.Series([0.2, 0.3]), 0.25)
    assert short == {"days": 2, "rank": None, "percentile": None, "low": None, "high": None}
    history = pd.Series([0.20 + 0.01 * i for i in range(21)])  # 0.20 .. 0.40
    r = iv_rank(history, 0.25)
    assert r["rank"] == pytest.approx(0.25)
    assert r["percentile"] == pytest.approx(5 / 21)
    assert (r["low"], r["high"]) == (pytest.approx(0.2), pytest.approx(0.4))
