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
    event_expiry,
    iv_at,
    iv_history,
    iv_rank,
    latest_chains,
)

AT = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)
DAY = AT.date()


def expiries(*days: int) -> list[date]:
    return [DAY + timedelta(days=d) for d in days]


def store(lake: Lake, chain: pd.DataFrame, day: date, underlying: str) -> None:
    write_raw(
        lake,
        RAW_OPTION_CHAIN_SNAPSHOTS,
        chain,
        run_id=f"r-{day}-{underlying}",
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


def test_iv_rank_needs_some_history() -> None:
    short = iv_rank(pd.Series([0.2, 0.3]), 0.25)
    assert short == {"days": 2, "rank": None, "percentile": None, "low": None, "high": None}
    history = pd.Series([0.20 + 0.01 * i for i in range(21)])  # 0.20 .. 0.40
    r = iv_rank(history, 0.25)
    assert r["rank"] == pytest.approx(0.25)
    assert r["percentile"] == pytest.approx(5 / 21)
    assert (r["low"], r["high"]) == (pytest.approx(0.2), pytest.approx(0.4))
