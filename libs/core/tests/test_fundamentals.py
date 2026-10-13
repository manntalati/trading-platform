"""SEC fundamentals: parsing, quarters, point in time, earnings dates and surprises (synthetic
companies only)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_core import fundamentals as f
from tp_core.storage import Lake, write_raw
from tp_core.testing import synthetic_company_facts, synthetic_submissions

EPS = {
    (y, q): round(1.0 + 0.1 * (y - 2020) + 0.02 * q, 2)
    for y in range(2020, 2025)
    for q in (1, 2, 3, 4)
}


def facts(**kwargs: object) -> pd.DataFrame:
    return f.parse_company_facts(synthetic_company_facts(7, EPS, **kwargs), "XYZ")  # type: ignore[arg-type]


def test_parse_keeps_the_concepts_from_periodic_filings() -> None:
    doc = synthetic_company_facts(7, EPS)
    us = doc["facts"]["us-gaap"]  # type: ignore[index]
    # an S-1 figure is ignored; a less preferred revenue tag is kept with its rank
    us["NetIncomeLoss"]["units"]["USD"].append(
        {
            "start": "2019-01-01",
            "end": "2019-12-31",
            "val": 1.0,
            "form": "S-1",
            "filed": "2020-02-01",
        }
    )
    us["SalesRevenueNet"] = {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 9.0, "form": "10-Q",
         "filed": "2024-05-05", "fy": 2024, "fp": "Q1", "accn": "x"}]}}  # fmt: skip
    df = f.parse_company_facts(doc, "XYZ")
    assert set(df["concept"]) == set(f.CONCEPTS)
    assert not (df["form"] == "S-1").any()
    assert df[df["tag"] == "SalesRevenueNet"]["rank"].iloc[0] == 2
    q = f.quarterly(df, "XYZ", "revenue")
    assert q[q["end"] == date(2024, 3, 31)]["value"].iloc[0] != 9.0  # "Revenues" is preferred


def test_quarters_fourth_quarter_and_point_in_time() -> None:
    df = facts(restated={(2022, 2): 1.5})
    q = f.quarterly(df, "XYZ", "eps_diluted")
    assert len(q) == 20
    q4 = q[q["end"] == date(2023, 12, 31)].iloc[0]
    assert q4["derived"]
    assert q4["value"] == pytest.approx(EPS[(2023, 4)])
    assert q4["known"] == date(2024, 3, 1)  # from the 10-K
    # before the 10-K, the fourth quarter isn't known
    early = f.quarterly(df, "XYZ", "eps_diluted", as_of=date(2024, 2, 15))
    assert early["end"].max() == date(2023, 9, 30)
    # a restatement counts only from the filing that made it; "known" stays the first filing
    restated = q[q["end"] == date(2022, 6, 30)].iloc[0]
    assert restated["value"] == 1.5
    assert restated["known"] == date(2022, 8, 4)
    before = f.quarterly(df, "XYZ", "eps_diluted", as_of=date(2023, 1, 1))
    assert before[before["end"] == date(2022, 6, 30)]["value"].iloc[0] == EPS[(2022, 2)]


def test_trailing_and_snapshot() -> None:
    df = facts()
    q = f.quarterly(df, "XYZ", "eps_diluted", as_of=date(2024, 12, 1))
    assert f.trailing(q) == pytest.approx(EPS[(2023, 4)] + sum(EPS[(2024, k)] for k in (1, 2, 3)))
    assert f.trailing(q.drop(q.index[-2])) is None  # a missing quarter
    snap = f.snapshot(df, ["XYZ", "NONE"], date(2024, 12, 1)).set_index("symbol")
    assert snap.loc["XYZ", "ttm_net_income"] == pytest.approx(
        snap.loc["XYZ", "ttm_eps_diluted"] * 1e9
    )
    assert snap.loc["XYZ", "equity"] == 20e9
    assert snap.loc["XYZ", "period_end"] == date(2024, 9, 30)
    assert pd.isna(snap.loc["NONE", "ttm_eps_diluted"])


def test_earnings_releases_and_the_session_that_reacts() -> None:
    doc = synthetic_submissions(7, sorted(EPS))
    recent = doc["filings"]["recent"]  # type: ignore[index]
    # an 8-K that isn't an earnings release is dropped
    for key, value in (("accessionNumber", "z"), ("form", "8-K"), ("filingDate", "2024-06-01"),
                       ("acceptanceDateTime", "2024-06-01T09:00:00.000Z"), ("items", "5.02"),
                       ("reportDate", "2024-06-01")):  # fmt: skip
        recent[key].append(value)
    older = synthetic_submissions(7, [(2019, 4)])["filings"]["recent"]  # type: ignore[index]
    ev = f.parse_submissions(doc, "XYZ", [older])
    assert set(ev["form"]) == {"8-K", "10-Q", "10-K"}
    assert "5.02" not in set(ev["items"])
    assert ev["filed"].min() == date(2020, 1, 30)  # from the older page
    release = ev[(ev["form"] == "8-K") & (ev["filed"] == date(2024, 10, 25))].iloc[0]
    assert release["accepted"] == pd.Timestamp("2024-10-25 20:05", tz="UTC")  # 4:05 PM EDT

    q = f.quarterly(facts(), "XYZ", "eps_diluted")
    a = f.announcements(ev, "XYZ", q).set_index("end")
    assert a.loc[date(2024, 9, 30), "session"] == date(2024, 10, 28)  # Friday after the close
    assert a.loc[date(2023, 12, 31), "session"] == date(2024, 1, 31)
    no_releases = f.announcements(ev[ev["form"] != "8-K"], "XYZ", q).set_index("end")
    assert no_releases.loc[date(2024, 9, 30), "session"] == date(2024, 11, 5)  # the 10-Q's day


@pytest.mark.parametrize(
    ("when", "session"),
    [
        ("2024-10-24 08:00", date(2024, 10, 24)),  # before the open
        ("2024-10-24 15:59", date(2024, 10, 24)),
        ("2024-10-24 16:01", date(2024, 10, 25)),
        ("2024-11-29 14:00", date(2024, 12, 2)),  # after a half day's 1 PM close
        ("2024-07-04 10:00", date(2024, 7, 5)),  # a holiday
    ],
)
def test_reaction_session(when: str, session: date) -> None:
    stamp = pd.Timestamp(when, tz="America/New_York")
    assert f.reaction_session(stamp) == session
    assert f.reaction_session(None) is None


def test_sue_flags_the_surprise() -> None:
    eps = dict(EPS)
    eps[(2024, 3)] = 3.0
    q = f.quarterly(f.parse_company_facts(synthetic_company_facts(7, eps), "XYZ"), "XYZ",
                    "eps_diluted")  # fmt: skip
    s = f.sue(q).set_index("end")
    assert s.loc[date(2024, 9, 30), "sue"] > 10
    assert abs(s.loc[date(2024, 6, 30), "sue"]) < 1
    assert pd.isna(s.loc[date(2021, 3, 31), "sue"])  # not enough history yet
    assert len(s) == 16  # the first year has nothing to compare with


def test_split_adjusted_eps() -> None:
    q = f.quarterly(facts(), "XYZ", "eps_diluted")
    # a 2-for-1 split on 2023-06-01: earlier sessions' factor is 0.5
    days = pd.bdate_range("2020-01-01", "2025-06-30")
    factor = pd.Series([0.5 if d < pd.Timestamp("2023-06-01") else 1.0 for d in days], index=days)
    adj = f.split_adjusted_eps(q, factor).set_index("end")
    assert adj.loc[date(2022, 12, 31), "value"] == pytest.approx(EPS[(2022, 4)] * 0.5)
    assert adj.loc[date(2023, 6, 30), "value"] == pytest.approx(EPS[(2023, 2)])  # filed after


def test_clean_tables(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    df = facts()
    for i, at in enumerate((datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 1, 3, tzinfo=UTC))):
        raw = df.assign(ingested_at=pd.Timestamp(at))
        write_raw(lake, f.FACTS_DATASET, raw, run_id=f"r{i}", partition={"ingest_date": str(i)},
                  schema=f.FACTS_SCHEMA)  # fmt: skip
    ev = f.parse_submissions(synthetic_submissions(7, sorted(EPS)), "XYZ")
    write_raw(lake, f.EVENTS_DATASET, ev.assign(ingested_at=pd.Timestamp("2025-01-03", tz="UTC")),
              run_id="e", partition={"ingest_date": "x"}, schema=f.EVENTS_SCHEMA)  # fmt: skip
    clean, _ = f.build_clean(lake)
    assert len(clean) == len(df)  # the second download replaced the first
    assert len(f.load_facts(lake, ["XYZ"])) == len(df)
    assert f.load_facts(lake, ["ABC"]).empty
    assert len(f.load_events(lake)) == len(ev)


def test_summary() -> None:
    df = facts()
    ev = f.parse_submissions(synthetic_submissions(7, sorted(EPS)), "XYZ")
    s = f.summary(df, ev, "XYZ", 100.0, date(2024, 12, 1))
    assert s is not None
    assert s["period_end"] == "2024-09-30"
    assert s["last_release"] == "2024-10-28"
    # 52 weeks after the same quarter's release (2024-01-31), same weekday
    assert s["next_release_estimate"] == "2025-01-29"
    ttm = EPS[(2023, 4)] + sum(EPS[(2024, k)] for k in (1, 2, 3))
    assert s["pe"] == pytest.approx(100.0 / ttm)
    assert s["book_to_market"] == pytest.approx(20e9 / (1e9 * 100.0))
    assert s["gross_profitability"] == pytest.approx(ttm * 2e9 / 50e9)
    assert s["sue"] is not None
    assert f.summary(df, ev, "NONE", 100.0, date(2024, 12, 1)) is None
