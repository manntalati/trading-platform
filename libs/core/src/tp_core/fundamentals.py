"""Company fundamentals from SEC EDGAR, point in time.

Two documents per company, both free (``tp-data fundamentals``):

- **Company facts** (``data.sec.gov/api/xbrl/companyfacts/CIK##########.json``): every number the
  company has tagged in its 10-Q and 10-K filings, each with the period it covers and the date
  it was **filed**. A figure is known from its filing date, never before: that is what makes a
  backtest on them honest. Later filings repeat earlier periods (comparatives) and sometimes
  restate them; each copy keeps its own filing date.
- **Submissions** (``data.sec.gov/submissions/CIK##########.json``): the list of filings. An 8-K
  with item 2.02 ("Results of Operations") is the earnings release, usually weeks before the
  10-Q, and its acceptance time says whether it came before or after the close.

The concepts kept (``CONCEPTS``) are the ones the factor and earnings strategies need: diluted
EPS, net income, revenue, gross profit (flows over a quarter or a year) and stockholders' equity,
total assets and shares outstanding (balances on a date). Companies switch tags over the years
(revenue especially); each concept lists its tags in order of preference.

Quarterly series: 10-Qs report the quarter (about 90 days) and the year to date; 10-Ks report the
year. The fourth quarter is never reported on its own, so it is the year minus the first three
quarters, known from the 10-K's filing date.

EPS is per share, so a stock split breaks comparisons over time: ``split_adjusted_eps`` scales
each figure by the split factor of the session it was filed (``tp_core.adjust``), so every
quarter is in today's shares, as the adjusted prices are.

**SUE** (standardized unexpected earnings, Bernard and Thomas): a seasonal random walk with drift
expects this quarter's EPS to be the same quarter last year's plus the average seasonal change;
the surprise is the difference, divided by the standard deviation of the past changes (eight
quarters, at least six).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from tp_core.calendar import NEW_YORK, is_session, sessions, xnys
from tp_core.storage import Lake, read_parquet_dir, write_parquet

FACTS_DATASET = "sec/company_facts"
EVENTS_DATASET = "sec/earnings_events"
CLEAN_FACTS = "sec_facts"
CLEAN_EVENTS = "earnings_events"

# concept: (taxonomy, tags in order of preference, unit, flow over a period or balance on a date)
CONCEPTS: dict[str, tuple[str, tuple[str, ...], str, bool]] = {
    "eps_diluted": (
        "us-gaap",
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"),
        "USD/shares",
        True,
    ),
    "net_income": ("us-gaap", ("NetIncomeLoss", "ProfitLoss"), "USD", True),
    "revenue": (
        "us-gaap",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        "USD",
        True,
    ),
    "gross_profit": ("us-gaap", ("GrossProfit",), "USD", True),
    "equity": (
        "us-gaap",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        "USD",
        False,
    ),
    "assets": ("us-gaap", ("Assets",), "USD", False),
    "shares_outstanding": ("dei", ("EntityCommonStockSharesOutstanding",), "shares", False),
}
FLOWS = tuple(k for k, v in CONCEPTS.items() if v[3])
FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})
QUARTER_DAYS = (75, 105)
YEAR_DAYS = (340, 390)

FACTS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("cik", pa.int64()),
        ("concept", pa.string()),
        ("tag", pa.string()),
        ("rank", pa.int16()),  # the tag's place in the concept's preference list
        ("unit", pa.string()),
        ("value", pa.float64()),
        ("start", pa.date32()),
        ("end", pa.date32()),
        ("fy", pa.int32()),
        ("fp", pa.string()),
        ("form", pa.string()),
        ("filed", pa.date32()),
        ("accn", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)
EVENTS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("cik", pa.int64()),
        ("form", pa.string()),
        ("items", pa.string()),
        ("filed", pa.date32()),
        ("accepted", pa.timestamp("us", tz="UTC")),
        ("report_date", pa.date32()),
        ("accn", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _day(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# -- parsing ------------------------------------------------------------------------------------


def parse_company_facts(doc: Mapping[str, Any], symbol: str) -> pd.DataFrame:
    """The ``CONCEPTS`` from one company-facts document, from 10-Q and 10-K filings: one row per
    reported figure (``FACTS_SCHEMA`` columns but ``ingested_at``)."""
    cik = int(doc.get("cik") or 0)
    facts = doc.get("facts") or {}
    rows = []
    for concept, (taxonomy, tags, unit, _) in CONCEPTS.items():
        for rank, tag in enumerate(tags):
            entries = ((facts.get(taxonomy) or {}).get(tag) or {}).get("units", {}).get(unit, [])
            for e in entries:
                form = str(e.get("form") or "")
                end, filed = _day(e.get("end")), _day(e.get("filed"))
                if form not in FORMS or end is None or filed is None or e.get("val") is None:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "concept": concept,
                        "tag": tag,
                        "rank": rank,
                        "unit": unit,
                        "value": float(e["val"]),
                        "start": _day(e.get("start")),
                        "end": end,
                        "fy": int(e["fy"]) if e.get("fy") is not None else None,
                        "fp": str(e.get("fp") or ""),
                        "form": form,
                        "filed": filed,
                        "accn": str(e.get("accn") or ""),
                    }
                )
    columns = [f.name for f in FACTS_SCHEMA if f.name != "ingested_at"]
    return pd.DataFrame(rows, columns=columns)


def parse_submissions(
    doc: Mapping[str, Any], symbol: str, extra: Sequence[Mapping[str, Any]] = ()
) -> pd.DataFrame:
    """Earnings releases (8-K with item 2.02) and periodic reports (10-Q, 10-K) from a
    submissions document and its older pages (``extra``). SEC's ``acceptanceDateTime`` carries
    a ``Z`` but is New York time; it is stored in UTC."""
    cik = int(doc.get("cik") or 0)
    pages = [(doc.get("filings") or {}).get("recent") or {}, *extra]
    rows = []
    for page in pages:
        forms = page.get("form") or []
        for i, form in enumerate(forms):
            items = str((page.get("items") or [""] * len(forms))[i] or "")
            earnings = form in ("8-K", "8-K/A") and "2.02" in items.split(",")
            if not (earnings or form in FORMS):
                continue
            accepted = _accepted((page.get("acceptanceDateTime") or [None] * len(forms))[i])
            rows.append(
                {
                    "symbol": symbol,
                    "cik": cik,
                    "form": str(form),
                    "items": items,
                    "filed": _day(page["filingDate"][i]),
                    "accepted": accepted,
                    "report_date": _day((page.get("reportDate") or [None] * len(forms))[i]),
                    "accn": str(page["accessionNumber"][i]),
                }
            )
    columns = [f.name for f in EVENTS_SCHEMA if f.name != "ingested_at"]
    return pd.DataFrame(rows, columns=columns)


def _accepted(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    local = pd.Timestamp(str(value).replace("Z", "").replace(".000", ""))
    if local.tzinfo is not None:
        local = local.tz_localize(None)
    return local.tz_localize(NEW_YORK).tz_convert("UTC")


# -- storage ------------------------------------------------------------------------------------


def build_clean(lake: Lake) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rebuild ``clean/sec_facts`` and ``clean/earnings_events`` from every raw download; when
    the same figure from the same filing was downloaded twice, the latest download wins."""
    facts = read_parquet_dir(lake.raw_dir(FACTS_DATASET), FACTS_SCHEMA)
    if not facts.empty:
        facts = (
            facts.sort_values("ingested_at")
            .drop_duplicates(["symbol", "concept", "tag", "start", "end", "accn"], keep="last")
            .sort_values(["symbol", "concept", "end", "filed"])
            .reset_index(drop=True)
        )
        write_parquet(facts, lake.clean_dir(CLEAN_FACTS) / "part.parquet", FACTS_SCHEMA)
    events = read_parquet_dir(lake.raw_dir(EVENTS_DATASET), EVENTS_SCHEMA)
    if not events.empty:
        events = (
            events.sort_values("ingested_at")
            .drop_duplicates(["symbol", "accn"], keep="last")
            .sort_values(["symbol", "filed"])
            .reset_index(drop=True)
        )
        write_parquet(events, lake.clean_dir(CLEAN_EVENTS) / "part.parquet", EVENTS_SCHEMA)
    return facts, events


def load_facts(lake: Lake, symbols: Sequence[str] | None = None) -> pd.DataFrame:
    df = read_parquet_dir(lake.clean_dir(CLEAN_FACTS), FACTS_SCHEMA)
    return df[df["symbol"].isin(symbols)] if symbols is not None and len(df) else df


def load_events(lake: Lake, symbols: Sequence[str] | None = None) -> pd.DataFrame:
    df = read_parquet_dir(lake.clean_dir(CLEAN_EVENTS), EVENTS_SCHEMA)
    return df[df["symbol"].isin(symbols)] if symbols is not None and len(df) else df


# -- quarterly and point in time -----------------------------------------------------------------


def _pick(
    end: np.ndarray, rank: np.ndarray, filed: np.ndarray, first: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """One figure per period: the most preferred tag, from the latest filing (``first``: from
    the first). Returns the positions of the chosen rows (by period end) and each period's first
    filing date (when it became public)."""
    when = filed.astype("int64")
    order = np.lexsort((when if first else -when, rank, end.astype("int64")))
    ends_sorted = end[order]
    new_end = np.r_[True, ends_sorted[1:] != ends_sorted[:-1]]
    chosen = order[new_end]
    starts = np.flatnonzero(new_end)
    known = np.minimum.reduceat(filed[order].astype("int64"), starts).astype("datetime64[D]")
    return chosen, known


def _arrays(f: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "end": pd.to_datetime(f["end"]).to_numpy().astype("datetime64[D]"),
        "start": pd.to_datetime(f["start"]).to_numpy().astype("datetime64[D]"),
        "filed": pd.to_datetime(f["filed"]).to_numpy().astype("datetime64[D]"),
        "rank": f["rank"].to_numpy(dtype="int64"),
        "value": f["value"].to_numpy(dtype=float),
    }


def _select(
    facts: pd.DataFrame, symbol: str | None, concept: str, as_of: date | None
) -> pd.DataFrame:
    f = facts
    if symbol is not None:
        f = f[f["symbol"] == symbol]
    f = f[f["concept"] == concept]
    if as_of is not None:
        f = f[f["filed"] <= as_of]
    return f


def quarterly(
    facts: pd.DataFrame,
    symbol: str | None,
    concept: str,
    *,
    as_of: date | None = None,
    first_reported: bool = False,
) -> pd.DataFrame:
    """One row per fiscal quarter: ``end, start, value, known, derived`` (``derived``: the fourth
    quarter, the year less three quarters). With ``as_of``, only filings made by then count;
    with ``first_reported``, each quarter's value as first published (later restatements
    ignored), which is what the market saw on ``known``. ``symbol=None`` when ``facts`` holds one
    company already (faster)."""
    cols = ["end", "start", "value", "known", "derived"]
    f = _select(facts, symbol, concept, as_of)
    f = f[f["start"].notna()]
    if f.empty:
        return pd.DataFrame(columns=cols)
    a = _arrays(f)
    days = (a["end"] - a["start"]).astype(int)
    out_end, out_start, out_value, out_known, out_derived = [], [], [], [], []
    is_q = (days >= QUARTER_DAYS[0]) & (days <= QUARTER_DAYS[1])
    is_y = (days >= YEAR_DAYS[0]) & (days <= YEAR_DAYS[1])
    q_idx = np.flatnonzero(is_q)
    if len(q_idx):
        chosen, known = _pick(a["end"][q_idx], a["rank"][q_idx], a["filed"][q_idx], first_reported)
        rows = q_idx[chosen]
        q_end, q_start, q_val = a["end"][rows], a["start"][rows], a["value"][rows]
        out_end += list(q_end)
        out_start += list(q_start)
        out_value += list(q_val)
        out_known += list(known)
        out_derived += [False] * len(rows)
    else:
        q_end = np.array([], dtype="datetime64[D]")
        q_start, q_val = q_end, np.array([], dtype=float)
    y_idx = np.flatnonzero(is_y)
    if len(y_idx) and len(q_end):
        chosen, y_known = _pick(
            a["end"][y_idx], a["rank"][y_idx], a["filed"][y_idx], first_reported
        )
        rows = y_idx[chosen]
        y_end, y_start, y_val = a["end"][rows], a["start"][rows], a["value"][rows]
        inside = (
            (q_start[None, :] >= (y_start - np.timedelta64(7, "D"))[:, None])
            & (q_end[None, :] < y_end[:, None])
            & (q_end[None, :] <= (y_end - np.timedelta64(60, "D"))[:, None])
        )
        count = inside.sum(axis=1)
        total = (inside * q_val[None, :]).sum(axis=1)
        last_q = np.where(inside, q_end[None, :], np.datetime64("1900-01-01", "D")).max(axis=1)
        have = set(q_end.tolist())
        for i in np.flatnonzero(count == 3):
            if y_end[i].item() in have:
                continue
            out_end.append(y_end[i])
            out_start.append(last_q[i] + np.timedelta64(1, "D"))
            out_value.append(float(y_val[i] - total[i]))
            out_known.append(y_known[i])
            out_derived.append(True)
    out = pd.DataFrame(
        {
            "end": [x.item() for x in np.array(out_end, dtype="datetime64[D]")],
            "start": [x.item() for x in np.array(out_start, dtype="datetime64[D]")],
            "value": np.array(out_value, dtype=float),
            "known": [x.item() for x in np.array(out_known, dtype="datetime64[D]")],
            "derived": out_derived,
        },
        columns=cols,
    )
    return out.sort_values("end").reset_index(drop=True)


def balances(
    facts: pd.DataFrame,
    symbol: str | None,
    concept: str,
    *,
    as_of: date | None = None,
    first_reported: bool = False,
) -> pd.DataFrame:
    """A balance (equity, assets, shares) on each reporting date: ``end, value, known``."""
    f = _select(facts, symbol, concept, as_of)
    if f.empty:
        return pd.DataFrame(columns=["end", "value", "known"])
    a = _arrays(f.assign(start=f["end"]))
    chosen, known = _pick(a["end"], a["rank"], a["filed"], first_reported)
    out = pd.DataFrame(
        {
            "end": [x.item() for x in a["end"][chosen]],
            "value": a["value"][chosen],
            "known": [x.item() for x in known],
        }
    )
    return out.sort_values("end").reset_index(drop=True)


def trailing(q: pd.DataFrame, n: int = 4) -> float | None:
    """The sum of the last ``n`` consecutive quarters (each about three months after the one
    before), or None if they aren't all there."""
    if len(q) < n:
        return None
    last = q.tail(n)
    ends = pd.to_datetime(last["end"]).to_numpy()
    gaps = np.diff(ends).astype("timedelta64[D]").astype(int)
    if len(gaps) and (gaps.min() < 70 or gaps.max() > 110):
        return None
    return float(last["value"].sum())


def snapshot(facts: pd.DataFrame, symbols: Sequence[str], as_of: date) -> pd.DataFrame:
    """What was known on ``as_of`` for each symbol: trailing-twelve-month EPS, net income,
    revenue and gross profit, and the latest equity, assets and shares outstanding, with the
    end of the latest quarter and when it was filed. Values per share are as reported (not
    split adjusted)."""
    rows = []
    for s in symbols:
        row: dict[str, Any] = {"symbol": s}
        eps = quarterly(facts, s, "eps_diluted", as_of=as_of)
        for concept in FLOWS:
            q = eps if concept == "eps_diluted" else quarterly(facts, s, concept, as_of=as_of)
            row[f"ttm_{concept}"] = trailing(q)
        for concept in ("equity", "assets", "shares_outstanding"):
            b = balances(facts, s, concept, as_of=as_of)
            row[concept] = float(b["value"].iloc[-1]) if len(b) else None
        row["period_end"] = eps["end"].iloc[-1] if len(eps) else None
        row["known"] = eps["known"].iloc[-1] if len(eps) else None
        rows.append(row)
    return pd.DataFrame(rows)


HISTORY_COLUMNS = (
    "symbol", "known", "period_end", "ttm_eps_diluted", "ttm_net_income", "ttm_revenue",
    "ttm_gross_profit", "equity", "assets", "shares_outstanding", "shares_known",
)  # fmt: skip


def history(facts: pd.DataFrame) -> pd.DataFrame:
    """For every company, what was known after each of its filings, as first reported: one row
    per quarter, from the day it was filed (``known``), with the trailing twelve months of each
    flow and the latest balances known by then. Look a date up with ``as_known``. Restatements
    are ignored (the original figure is what the market saw); nothing appears before it was
    filed."""
    rows = []
    for symbol, company in facts.groupby("symbol"):
        flows = {c: _ttm(quarterly(company, None, c, first_reported=True)) for c in FLOWS}
        bals = {
            c: balances(company, None, c, first_reported=True)
            for c in ("equity", "assets", "shares_outstanding")
        }
        spine = flows["eps_diluted"] if len(flows["eps_diluted"][0]) else flows["net_income"]
        for end, known in zip(spine[0], spine[3], strict=True):
            row: dict[str, Any] = {
                "symbol": symbol,
                "known": known.item(),
                "period_end": end.item(),
            }
            for c, (ends, ttm, _, _, ttm_known) in flows.items():
                ok = np.flatnonzero((ends <= end) & (ttm_known <= known) & ~np.isnan(ttm))
                row[f"ttm_{c}"] = float(ttm[ok[-1]]) if len(ok) else None
            for c, b in bals.items():
                b_known = b["known"].to_numpy().astype("datetime64[D]")
                ok = np.flatnonzero(b_known <= known)
                row[c] = float(b["value"].iloc[ok[-1]]) if len(ok) else None
                if c == "shares_outstanding":
                    row["shares_known"] = b["known"].iloc[ok[-1]] if len(ok) else None
            rows.append(row)
    out = pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))
    return out.sort_values(["symbol", "known"]).reset_index(drop=True)


def _ttm(q: pd.DataFrame) -> tuple[np.ndarray, ...]:
    """Per quarter (by end): the trailing four quarters' sum (NaN unless all four are there,
    about three months apart) and the date the last of them became known."""
    ends = q["end"].to_numpy().astype("datetime64[D]") if len(q) else np.array([], "datetime64[D]")
    known = (
        q["known"].to_numpy().astype("datetime64[D]") if len(q) else np.array([], "datetime64[D]")
    )
    values = q["value"].to_numpy(dtype=float) if len(q) else np.array([], dtype=float)
    ttm = np.full(len(q), np.nan)
    ttm_known = known.copy()
    for i in range(3, len(q)):
        gaps = np.diff(ends[i - 3 : i + 1]).astype(int)
        if gaps.min() >= 70 and gaps.max() <= 110:
            ttm[i] = values[i - 3 : i + 1].sum()
            ttm_known[i] = known[i - 3 : i + 1].max()
    return ends, ttm, values, known, ttm_known


def as_known(hist: pd.DataFrame, when: date) -> pd.DataFrame:
    """Each company's latest row of ``history`` known on ``when``, indexed by symbol."""
    seen = hist[hist["known"] <= when]
    return seen.groupby("symbol").tail(1).set_index("symbol")


# -- earnings surprises -------------------------------------------------------------------------


def split_adjusted_eps(q: pd.DataFrame, split_factor: pd.Series) -> pd.DataFrame:
    """Scale each quarter's EPS into today's shares: times the split factor (``tp_core.adjust``,
    backward) of the last session on or before it was filed. ``split_factor`` is indexed by
    session."""
    if q.empty or split_factor.empty:
        return q
    f = split_factor.sort_index()
    idx = pd.DatetimeIndex(f.index)
    out = q.copy()
    factors = []
    for known in out["known"]:
        i = int(idx.searchsorted(pd.Timestamp(known), side="right")) - 1
        factors.append(float(f.iloc[max(i, 0)]))
    out["value"] = out["value"].to_numpy(dtype=float) * np.array(factors)
    return out


def sue(q: pd.DataFrame, window: int = 8, min_history: int = 6) -> pd.DataFrame:
    """Standardized unexpected earnings for each quarter of ``q`` (``end, value, known``): the
    change from the same quarter a year earlier, less the average such change over the previous
    ``window`` quarters, over their standard deviation. Needs ``min_history`` past changes."""
    q = q.sort_values("end").reset_index(drop=True)
    ends = pd.to_datetime(q["end"])
    values = q["value"].to_numpy(dtype=float)
    diffs: list[float] = []
    out = []
    for i in range(len(q)):
        year_ago = [
            j for j in range(i) if 340 <= (ends.iloc[i] - ends.iloc[j]).days <= 390
        ]  # the same fiscal quarter a year earlier
        if not year_ago:
            diffs.append(math.nan)
            continue
        change = values[i] - values[year_ago[-1]]
        past = [d for d in diffs[-window:] if d == d]
        score = None
        if len(past) >= min_history:
            sd = float(np.std(past, ddof=1))
            if sd > 0:
                score = (change - float(np.mean(past))) / sd
        out.append({"end": q["end"].iloc[i], "known": q["known"].iloc[i], "eps": values[i],
                    "change": change, "sue": score})  # fmt: skip
        diffs.append(change)
    return pd.DataFrame(out, columns=["end", "known", "eps", "change", "sue"])


def announcements(events: pd.DataFrame, symbol: str, q: pd.DataFrame) -> pd.DataFrame:
    """When each quarter's results became public: the first earnings release (8-K 2.02) accepted
    after the quarter ended and no later than its 10-Q or 10-K; else that filing. Adds
    ``announced`` (UTC) and ``session``: the trading session at whose close the news was out (a
    release after 4 PM New York, or on a holiday, counts for the next session)."""
    ev = events[events["symbol"] == symbol]
    releases = ev[ev["form"].isin(["8-K", "8-K/A"])].sort_values("accepted")
    out = q.copy()
    announced: list[pd.Timestamp | None] = []
    for _, r in out.iterrows():
        end, known = pd.Timestamp(r["end"]), pd.Timestamp(r["known"])
        window = releases[
            (releases["filed"] > r["end"])
            & (pd.to_datetime(releases["filed"]) <= known)
            & (pd.to_datetime(releases["filed"]) <= end + pd.Timedelta(days=100))
        ]
        if len(window):
            announced.append(pd.Timestamp(window["accepted"].iloc[0]))
        else:  # no release found: the filing itself, at the close
            announced.append(
                pd.Timestamp(datetime.combine(r["known"], time(16)), tz=NEW_YORK).tz_convert("UTC")
            )
    out["announced"] = announced
    out["session"] = [reaction_session(a) for a in announced]
    return out


def reaction_session(when: pd.Timestamp | None) -> date | None:
    """The session at whose close a release at ``when`` was public: the same day if it came
    before that day's close (4 PM New York, 1 PM on half days), else the next session."""
    if when is None or pd.isna(when):
        return None
    stamp = pd.Timestamp(when)
    local = stamp.tz_convert(NEW_YORK)
    day = local.date()
    if is_session(day) and stamp < xnys().session_close(pd.Timestamp(day)):
        return day
    ahead = sessions(day + timedelta(days=1), day + timedelta(days=10))
    return ahead[0] if ahead else None


def summary(
    facts: pd.DataFrame, events: pd.DataFrame, symbol: str, price: float | None, today: date
) -> dict[str, Any] | None:
    """What the filings say about one company today: trailing earnings and the valuation ratios
    the factor strategies use, the latest earnings surprise, and when results came out last and
    are likely next (a year after the same quarter's release; else three months after the
    last). None when EDGAR has nothing for it (funds)."""
    q = quarterly(facts, symbol, "eps_diluted", as_of=today)
    if q.empty:
        return None
    snap = snapshot(facts, [symbol], today).iloc[0]
    released = announcements(events, symbol, q) if len(events) else q.assign(session=q["known"])
    last = released.iloc[-1]
    past = pd.to_datetime(released["session"].dropna())
    next_est = None
    if len(past):
        year_ago = past[
            (past > pd.Timestamp(today) - pd.Timedelta(days=365))
            & (past <= pd.Timestamp(today) - pd.Timedelta(days=300))
        ]
        guess = (year_ago.min() + pd.Timedelta(days=364)) if len(year_ago) else None
        if guess is None or guess.date() <= today:
            guess = past.max() + pd.Timedelta(days=91)
        next_est = guess.date() if guess.date() > today else None
    surprises = sue(q)
    ttm_eps = _f(snap.get("ttm_eps_diluted"))
    equity, assets, shares = (_f(snap.get(k)) for k in ("equity", "assets", "shares_outstanding"))
    gross, net = _f(snap.get("ttm_gross_profit")), _f(snap.get("ttm_net_income"))
    cap = shares * price if shares and price else None
    return {
        "period_end": _iso(last["end"]),
        "last_release": _iso(last.get("session")),
        "next_release_estimate": _iso(next_est),
        "eps_last_quarter": _f(last["value"]),
        "eps_ttm": ttm_eps,
        "pe": price / ttm_eps if price and ttm_eps and ttm_eps > 0 else None,
        "earnings_yield": ttm_eps / price if price and ttm_eps is not None else None,
        "book_to_market": equity / cap if equity is not None and cap else None,
        "gross_profitability": gross / assets if gross is not None and assets else None,
        "roe": net / equity if net is not None and equity and equity > 0 else None,
        "market_cap": cap,
        "sue": _f(surprises["sue"].iloc[-1]) if len(surprises) else None,
    }


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _iso(value: Any) -> str | None:
    if value is None or (not isinstance(value, date) and pd.isna(value)):
        return None
    return value.isoformat() if isinstance(value, date) else str(value)
