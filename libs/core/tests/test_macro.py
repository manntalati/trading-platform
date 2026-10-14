"""FRED macro series: parsing, storage, the risk-free rate and the labels built from them."""

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.macro import (
    DATASET,
    SCHEMA,
    build_clean_macro,
    latest,
    load_macro,
    macro_regimes,
    parse_fred_csv,
    risk_free_rate,
)
from tp_core.regime import DIMENSIONS, current, market_regimes, performance_by_regime
from tp_core.storage import Lake, write_raw


def test_parse_both_header_styles_and_missing_days() -> None:
    new = "observation_date,VIXCLS\n2026-10-01,16.1\n2026-10-02,.\n2026-10-05,17.5\n"
    old = "DATE,DGS10\n2026-10-01,4.12\n2026-10-02,4.10\n"
    a = parse_fred_csv(new, "VIXCLS")
    assert list(a.columns) == ["series", "date", "value"]
    assert a["date"].tolist() == [date(2026, 10, 1), date(2026, 10, 5)]  # "." dropped
    assert a["value"].tolist() == [16.1, 17.5]
    b = parse_fred_csv(old, "DGS10")
    assert (b["series"] == "DGS10").all()
    assert len(b) == 2
    with pytest.raises(ValueError, match="expected a date and a value column"):
        parse_fred_csv("just_one_column\n1\n", "X")


def _store(lake: Lake, rows: list[tuple[str, date, float]], at: datetime, run: str) -> None:
    df = pd.DataFrame(rows, columns=["series", "date", "value"])
    df["ingested_at"] = pd.Timestamp(at)
    write_raw(lake, DATASET, df, run_id=run, partition={"ingest_date": "x"}, schema=SCHEMA)


def test_clean_keeps_the_latest_download_and_loads_wide(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    assert load_macro(lake).empty
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    _store(
        lake, [("VIXCLS", d1, 16.0), ("DGS3MO", d1, 4.0)], datetime(2026, 10, 1, tzinfo=UTC), "a"
    )
    _store(
        lake, [("VIXCLS", d1, 16.5), ("VIXCLS", d2, 18.0)], datetime(2026, 10, 2, tzinfo=UTC), "b"
    )
    clean = build_clean_macro(lake)
    assert len(clean) == 3
    wide = load_macro(lake)
    assert wide.loc["2026-10-01", "VIXCLS"] == 16.5  # the later download wins
    assert np.isnan(wide.loc["2026-10-02", "DGS3MO"])
    assert load_macro(lake, end=d1).index.max() == pd.Timestamp(d1)
    assert latest(wide)["VIXCLS"]["value"] == 18.0

    assert risk_free_rate(lake, 0.04, at=date(2026, 10, 3)) == pytest.approx(0.04)  # DGS3MO 4.0
    assert risk_free_rate(lake, 0.05, at=date(2026, 12, 1)) == 0.05  # too old: the default
    assert risk_free_rate(Lake(tmp_path / "empty"), 0.03, at=d1) == 0.03


def macro_frame(days: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(days)
    vix = np.r_[np.full(n - 50, 13.0), np.full(50, 32.0)]  # calm, then a spike
    r10 = np.r_[np.full(n - 60, 3.0), np.linspace(3.0, 4.5, 60)]  # rates climb at the end
    r3m = np.r_[np.full(n - 60, 2.0), np.full(60, 5.0)]  # and the curve inverts
    return pd.DataFrame(
        {"VIXCLS": vix, "VXVCLS": vix * 0.95 + 1, "DGS10": r10, "DGS3MO": r3m}, index=days
    )


def test_macro_labels() -> None:
    days = pd.bdate_range("2025-01-01", periods=400)
    m = macro_regimes(macro_frame(days))
    assert m["vix_volatility"].iloc[100] == "calm"
    assert m["vix_volatility"].iloc[-1] == "stressed"
    assert m["vix_ratio"].iloc[-1] > 1  # VIX above its 3-month version
    assert m["curve"].iloc[100] == "normal"
    assert m["curve"].iloc[-1] == "inverted"
    assert m["rates"].iloc[300] == "steady"
    assert m["rates"].iloc[-1] == "rising"
    assert m["rates"].iloc[:149].isna().all()  # the 200-day average needs 150 days


def test_market_regimes_use_vix_and_add_rates() -> None:
    days = pd.bdate_range("2025-01-01", periods=400)
    rng = np.random.default_rng(1)
    closes = pd.DataFrame(
        {
            "SPY": 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.004, 400))),
            "A": 50 * np.exp(np.cumsum(rng.normal(0, 0.01, 400))),
        },
        index=days,
    )
    plain = market_regimes(closes)
    assert set(DIMENSIONS) <= set(plain.columns)
    assert plain["rates"].isna().all()
    assert current(plain)["volatility_source"] == "realized"

    # FRED skips holidays the exchange trades, and vice versa: values carry forward
    macro = macro_frame(days).drop(days[[350, 351]])
    r = market_regimes(closes, macro=macro)
    assert r["volatility"].iloc[-1] == "stressed"  # from VIX 32, though SPY itself was quiet
    assert plain["volatility"].iloc[-1] != "stressed"
    assert r["vix"].iloc[351] == 13.0  # the last published value (day 349), carried forward
    now = current(r)
    assert now["curve"] == "inverted"
    assert now["rates"] == "rising"
    assert now["volatility_source"] == "vix"
    assert now["vix"] == 32.0
    assert now["rate_10y"] == pytest.approx(4.5)
    assert "yield curve inverted" in now["summary"]

    perf = performance_by_regime(closes["A"].pct_change().fillna(0), r)
    assert {row["label"] for row in perf["curve"]} == {"normal", "inverted"}


def test_publisher_formats() -> None:
    from tp_core.macro import parse_cboe_csv, parse_fred_json, parse_treasury_csv

    cboe = parse_cboe_csv(
        "DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.24,17.24,17.24\n", "VIXCLS"
    )
    assert (cboe["date"].iloc[0], cboe["value"].iloc[0]) == (date(1990, 1, 2), 17.24)
    with pytest.raises(ValueError, match="DATE and CLOSE"):
        parse_cboe_csv("X\n1\n", "VIXCLS")
    treasury = parse_treasury_csv(
        'Date,"1 Mo","3 Mo","2 Yr","10 Yr"\n10/13/2026,3.9,3.88,3.45,4.05\n'
    )
    assert treasury["DGS10"]["value"].iloc[0] == 4.05
    assert treasury["DGS3MO"]["date"].iloc[0] == date(2026, 10, 13)
    assert all(df.empty for df in parse_treasury_csv('Date,"3 Mo"\n').values())
    with pytest.raises(ValueError, match="10 YR"):
        parse_treasury_csv('Date,"3 Mo","2 Yr"\n10/13/2026,3.9,3.4\n')
    fred = parse_fred_json({"observations": [{"date": "2026-10-13", "value": "."}]}, "DGS2")
    assert fred.empty
