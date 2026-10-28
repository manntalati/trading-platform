"""Recommendations made on a schedule and kept, with a track record.

Each run stores the options research (swing calls, LEAPS, bearish puts, premium selling), the
stock ideas and the portfolio checks as they stood, so the dashboard opens on the latest at
once, can say what is new since the previous session, and can show how every past
recommendation has done since:

    reports/recommendations/<run id>.json   the full run (kept KEEP_DAYS days)
    clean/recommendations_log/part.parquet  one row per idea per run, kept for good

A run is made whenever its inputs changed (new bars, a new option chain snapshot, a brokerage
sync, new macro data, new headlines or their scores, a new playbook calibration): ``tp-recs
run``, which the job scheduler starts on its own.

The track record counts each idea once (a name on a list for one setup; listed again within a
week, it's the same idea), with the contracts the first run that listed it picked. It marks each
position at the latest quotes (the middle of the bid and ask; entries were at the ask for a
purchase and the bid for a sale, as the picks were priced), or at its value at expiry once it
has expired. It also follows each idea's exit plan through the daily chain snapshots: the first
session its profit target, stop, stock-side exit, time stop or days-to-expiry limit was
reached, and what the position was worth then, which is what following the plan would have
made. A stock idea is followed the same way on the daily bars: its stop and target against each
session's open, low and high, then its stock-side rule and time stop on the close. It is the
record of rules applied to past data, not of trades anyone made. Each idea keeps the news
sentiment it was listed with, so the record can say whether ideas the news agreed with did
better than the ones it ran against (``by_news``).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from tp_core.bars import close_matrix, load_bars
from tp_core.calendar import NEW_YORK
from tp_core.config import Universes
from tp_core.macro import risk_free_rate
from tp_core.pnl import option_label
from tp_core.portfolio import CASH_KIND, Classifier, held_symbols, holdings_table, latest_snapshot
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA
from tp_core.storage import Lake, new_run_id, read_parquet_dir, write_json, write_parquet
from tp_core.volatility import latest_chains, snapshot_partitions
from tp_strategies.budget import Budget
from tp_strategies.ideas import ideas_from_lake
from tp_strategies.options_research import CHAIN_MAX_AGE_DAYS, DISCLAIMER
from tp_strategies.playbook import load_calibration, scan
from tp_strategies.setups import exit_signal, indicators

log = logging.getLogger(__name__)

RUNS = "recommendations"
LOG = "recommendations_log"
KEEP_DAYS = 30
LISTS = ("swing", "leaps", "bearish", "income", "volatility", "stocks")
LIST_NAMES = {
    "swing": "Swing calls",
    "leaps": "LEAPS",
    "bearish": "Bearish",
    "income": "Premium selling",
    "volatility": "Volatility",
    "stocks": "Stocks",
}
HORIZONS = (5, 21)  # sessions after the recommendation, for the underlying's move
EPISODE_GAP = timedelta(days=7)  # listed again within a week: the same idea
LOG_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("run_at", pa.timestamp("us", tz="UTC")),
        ("bars_session", pa.date32()),  # the last daily bar the run saw
        ("list", pa.string()),
        ("symbol", pa.string()),
        ("rank", pa.int32()),
        ("score", pa.float64()),
        ("spot", pa.float64()),
        ("structure", pa.string()),
        ("legs", pa.string()),  # JSON: [{contract, side, price, expiration, strike, right}]
        ("entry", pa.float64()),  # per share: + paid (a debit), - received (a credit)
        ("setup", pa.string()),  # the playbook setup (tp_strategies.setups)
        ("plan", pa.string()),  # JSON: the exit plan (targets per share, dates, stock rule)
        ("news", pa.float64()),  # the name's mean news sentiment when listed (tp_core.news)
        ("headlines", pa.int32()),  # how many headlines that was from
    ]
)
DIRECTION = {"bearish": -1, "income": 0, "volatility": 0}  # the rest: +1


# -- what to scan and what you hold ---------------------------------------------------------------


def research_symbols(lake: Lake, universes: Universes) -> list[str]:
    """Your research list, the options universe, your holdings and the bars universe."""
    return list(
        dict.fromkeys(
            [
                *universes.research,
                *universes.options_underlyings,
                *held_symbols(lake),
                *universes.bars,
            ]
        )
    )


def held_summary(lake: Lake, classifier: Classifier) -> dict[str, str]:
    """What you hold per underlying, in words ("40 shares, 2 x XYZ Jan 16 '26 $50 Call")."""
    snap = latest_snapshot(lake)
    if snap.empty:
        return {}
    table = holdings_table(snap, classifier)
    out: dict[str, list[str]] = {}
    for _, r in table[table["kind"] != CASH_KIND].iterrows():
        underlying = str(r["underlying"])
        qty = float(r["quantity"])
        if r["kind"] == "option":
            label = option_label(str(r["symbol"])) or str(r["symbol"])
            out.setdefault(underlying, []).append(f"{qty:g} x {label}")
        else:
            out.setdefault(underlying, []).append(f"{qty:g} shares")
    return {k: ", ".join(v) for k, v in out.items()}


# -- inputs: has anything changed since the last run? ---------------------------------------------


def _newest(directory: Path, pattern: str = "*") -> str | None:
    """The newest entry's name and modification time under ``directory`` (one level)."""
    try:
        entries = [p for p in directory.glob(pattern) if not p.name.startswith(".")]
    except OSError:
        return None
    if not entries:
        return None
    newest = max(entries, key=lambda p: (p.stat().st_mtime, p.name))
    return f"{newest.name}@{newest.stat().st_mtime:.0f}"


def input_stamp(lake: Lake) -> dict[str, str | None]:
    """A cheap fingerprint of what a run reads: when any of these change, a new run is due."""
    accounts = lake.raw_dir("broker/accounts")
    newest_day = max(accounts.glob("*/snapshot_date=*"), default=None, key=lambda p: p.name)
    return {
        "bars": _newest(lake.reports_dir("validation"), "stock_bars_1d-*.json"),
        "options": _newest(lake.reports_dir("options_snapshot"), "*.json"),
        "macro": _newest(lake.reports_dir("macro"), "*.json"),
        "fundamentals": _newest(lake.reports_dir("fundamentals"), "*.json"),
        "calibration": _newest(lake.reports_dir("playbook"), "calibration.json"),
        "news": _newest(lake.reports_dir("news"), "*.json"),
        "news_scores": _newest(lake.reports_dir("news_scores"), "*.json"),
        "broker": _newest(newest_day, "*.parquet") if newest_day is not None else None,
    }


# -- one run --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    run_id: str
    path: Path
    ideas: dict[str, int]  # per list
    portfolio_ideas: int
    new: int  # ideas not in the previous session's last run

    def summary(self) -> str:
        lists = ", ".join(f"{LIST_NAMES[k].lower()} {n}" for k, n in self.ideas.items())
        return (
            f"run {self.run_id}: {lists}; {self.portfolio_ideas} portfolio checks; "
            f"{self.new} new since the previous session"
        )


def make_run(
    lake: Lake,
    *,
    universes: Universes,
    classifier: Classifier,
    now: datetime,
    default_rate: float = 0.04,
    budget: Budget | None = None,
) -> dict[str, Any]:
    """Scan for ideas (fitted to ``budget``: the account's buying power) and check the
    portfolio, as of ``now``."""
    today = now.astimezone(NEW_YORK).date()
    chains = latest_chains(lake, since=today - timedelta(days=CHAIN_MAX_AGE_DAYS))
    research = scan(
        lake,
        research_symbols(lake, universes),
        classifier=classifier,
        chains=chains,
        now=now,
        rate=risk_free_rate(lake, default_rate),
        held=held_summary(lake, classifier),
        calibration=load_calibration(lake),
        budget=budget,
    )
    portfolio = ideas_from_lake(lake, classifier).to_dict()
    bars = load_bars(lake, ["SPY"])
    return {
        "run_id": new_run_id(now),
        "as_of": now.isoformat(),
        "bars_session": bars["session"].max().isoformat() if len(bars) else None,
        "inputs": input_stamp(lake),
        "research": research,
        "portfolio": portfolio,
        "disclaimer": DISCLAIMER,
    }


def run_recommendations(
    lake: Lake,
    *,
    universes: Universes,
    classifier: Classifier,
    now: datetime,
    default_rate: float = 0.04,
    budget: Budget | None = None,
) -> RunResult:
    """Make a run, mark what's new, store it and add it to the log."""
    run = make_run(
        lake,
        universes=universes,
        classifier=classifier,
        now=now,
        default_rate=default_rate,
        budget=budget,
    )
    return store_run(lake, run, now=now)


def store_run(lake: Lake, run: dict[str, Any], *, now: datetime) -> RunResult:
    previous = previous_session_run(lake, run)
    mark_new(run, previous)
    run["previous_run"] = previous["run_id"] if previous else None
    path = write_json(_jsonable(run), lake.reports_dir(RUNS) / f"{run['run_id']}.json")
    _append_log(lake, run)
    _prune(lake, now)
    research = run["research"]
    return RunResult(
        run_id=run["run_id"],
        path=path,
        ideas={k: len(research.get(k) or []) for k in LISTS},
        portfolio_ideas=len(run["portfolio"].get("ideas") or []),
        new=sum(1 for k in LISTS for i in research.get(k) or [] if i.get("new")),
    )


def _jsonable(value: Any) -> Any:
    """Plain JSON: NaN and infinities become null, tuples lists, anything else a string."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is pd.NaT:
        return None
    if isinstance(value, date):  # dates, datetimes and pandas timestamps
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalars
        return _jsonable(value.item())
    return str(value)


# -- reading runs ---------------------------------------------------------------------------------


def run_paths(lake: Lake) -> list[Path]:
    """Stored runs, oldest first (run ids sort by time)."""
    return sorted(lake.reports_dir(RUNS).glob("*.json"))


def load_run(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(path.read_text())
    return out


def latest_run(lake: Lake) -> dict[str, Any] | None:
    paths = run_paths(lake)
    return load_run(paths[-1]) if paths else None


def _session_of(run: Mapping[str, Any]) -> date:
    return datetime.fromisoformat(str(run["as_of"])).astimezone(NEW_YORK).date()


def previous_session_run(lake: Lake, run: Mapping[str, Any]) -> dict[str, Any] | None:
    """The last run made on an earlier day than ``run``: what "new" is measured against, so a
    second run the same day doesn't make the morning's new ideas old."""
    day = _session_of(run)
    for path in reversed(run_paths(lake)):
        other = load_run(path)
        if other.get("run_id") != run.get("run_id") and _session_of(other) < day:
            return other
    return None


def _keys(run: Mapping[str, Any] | None) -> dict[str, set[str]]:
    research = (run or {}).get("research") or {}
    return {k: {str(i["symbol"]) for i in research.get(k) or []} for k in LISTS}


def mark_new(run: dict[str, Any], previous: Mapping[str, Any] | None) -> None:
    """Flag each idea that wasn't on the same list in ``previous``, and list the ones that left."""
    before = _keys(previous)
    research = run["research"]
    for k in LISTS:
        for idea in research.get(k) or []:
            idea["new"] = previous is not None and idea["symbol"] not in before[k]
    now_keys = _keys(run)
    run["dropped"] = (
        {k: sorted(before[k] - now_keys[k]) for k in LISTS} if previous is not None else {}
    )


# -- the log and the track record -----------------------------------------------------------------


def legs(pick: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every contract in a pick (one for a call, two for a spread, four for an iron condor), with
    the side it was priced for."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, Mapping):
            if "contract" in node and "side" in node:
                out.append(
                    {
                        k: node.get(k)
                        for k in ("contract", "side", "price", "expiration", "strike", "right")
                    }
                )
                return
            for key, value in node.items():
                if key not in ("entry", "exit", "model"):  # plans and numbers, not legs
                    walk(value)

    walk(pick)
    return out


def _structure(pick: Mapping[str, Any] | None, idea_list: str) -> str:
    if not pick:
        return "no contract"
    if pick.get("kind"):
        return str(pick["kind"])
    found = legs(pick)
    if len(found) == 2:
        return "debit spread"
    if len(found) == 1:
        leg = found[0]
        if leg["side"] == "sell":
            return f"short {leg['right']}" if idea_list != "income" else "cash-secured put"
        return "LEAPS call" if idea_list == "leaps" else f"long {leg['right']}"
    return "options"


def _entry(found: list[dict[str, Any]]) -> float | None:
    if not found or any(leg.get("price") is None for leg in found):
        return None
    return sum((1 if leg["side"] == "buy" else -1) * float(leg["price"]) for leg in found)


def log_rows(run: Mapping[str, Any]) -> pd.DataFrame:
    rows = []
    research = run.get("research") or {}
    session = run.get("bars_session")
    for k in LISTS:
        for rank, idea in enumerate(research.get(k) or [], start=1):
            found = legs(idea.get("pick"))
            shares = (idea.get("pick") or {}).get("kind") == "shares"
            entry = _float(idea["pick"]["entry"].get("limit")) if shares else _entry(found)
            rows.append(
                {
                    "run_id": run["run_id"],
                    "run_at": pd.Timestamp(run["as_of"]).tz_convert("UTC"),
                    "bars_session": date.fromisoformat(session) if session else None,
                    "list": k,
                    "symbol": idea["symbol"],
                    "rank": rank,
                    "score": idea.get("score"),
                    "spot": idea.get("spot"),
                    "structure": _structure(idea.get("pick"), k),
                    "legs": json.dumps(found),
                    "entry": entry,
                    "setup": (idea.get("play") or {}).get("name"),
                    "plan": _plan(idea.get("pick")),
                    "news": (idea.get("news") or {}).get("sentiment"),
                    # 0: no headlines; null: listed before the news was kept
                    "headlines": (idea.get("news") or {}).get(
                        "headlines", 0 if "news" in idea else None
                    ),
                }
            )
    return pd.DataFrame(rows, columns=[f.name for f in LOG_SCHEMA])


def _plan(pick: Mapping[str, Any] | None) -> str | None:
    if not pick or not pick.get("exit"):
        return None
    if pick.get("kind") == "shares":
        held = ("stop", "target", "sell_by", "stock")
        return json.dumps({k: pick["exit"].get(k) for k in held} | {"shares": True})
    keep = ("take_profit", "stop", "sell_by", "out_by", "stock")
    plan = {k: pick["exit"].get(k) for k in keep}
    return json.dumps(plan | {"credit": pick.get("credit") is not None})


def load_log(lake: Lake) -> pd.DataFrame:
    path = lake.clean_dir(LOG)
    if not (path / "part.parquet").exists():
        return pd.DataFrame(columns=[f.name for f in LOG_SCHEMA])
    return read_parquet_dir(path, LOG_SCHEMA)


def _append_log(lake: Lake, run: Mapping[str, Any]) -> None:
    fresh = log_rows(run)
    if fresh.empty:
        return
    old = load_log(lake)
    old = old[old["run_id"] != run["run_id"]]
    out = pd.concat([old, fresh], ignore_index=True) if len(old) else fresh
    write_parquet(out, lake.clean_dir(LOG) / "part.parquet", LOG_SCHEMA)


def _prune(lake: Lake, now: datetime) -> None:
    """Full runs are kept KEEP_DAYS days (the log keeps the essentials for good); the newest is
    always kept."""
    cutoff = now - timedelta(days=KEEP_DAYS)
    for path in run_paths(lake)[:-1]:
        try:
            at = datetime.strptime(path.stem.split("-")[0] + "+0000", "%Y%m%dT%H%M%SZ%z")
        except ValueError:
            continue
        if at < cutoff:
            path.unlink(missing_ok=True)


def recommended(log_df: pd.DataFrame) -> pd.DataFrame:
    """Each idea once: a name on a list for one setup, from the first run that listed it until it
    went ``EPISODE_GAP`` without being listed (listed after that, it's a new idea). Runs come
    many times a day and the contract picked shifts with the prices, so an idea is tracked with
    the contracts the first run picked, at its prices, as if entered then; with when it was last
    seen, in how many runs, and how many different contracts it was shown with."""
    extra = {"last_seen": object, "runs": int, "contracts": int}
    if log_df.empty:
        return log_df.assign(**{k: pd.Series(dtype=t) for k, t in extra.items()})
    df = log_df.sort_values("run_at").reset_index(drop=True)
    df["_setup"] = df["setup"].fillna("")
    keys = ["list", "symbol", "_setup"]
    gap = df.groupby(keys)["run_at"].diff()
    df["_episode"] = (gap.isna() | (gap > EPISODE_GAP)).groupby([df[k] for k in keys]).cumsum()
    keys.append("_episode")
    grouped = df.groupby(keys, sort=False)
    first = grouped.head(1).set_index(keys)
    first["last_seen"] = grouped["run_at"].max()
    first["runs"] = grouped["run_id"].nunique()
    first["contracts"] = grouped["legs"].nunique()
    out = first.reset_index().drop(columns=["_setup", "_episode"])
    return out.sort_values("run_at", ascending=False).reset_index(drop=True)


def _contract_marks(
    lake: Lake, contracts: Iterable[tuple[str, str]]
) -> dict[str, tuple[float, str]]:
    """Latest mid (and its snapshot date) of each ``(underlying, contract)`` from the newest chain
    of its underlying."""
    wanted: dict[str, set[str]] = {}
    for underlying, contract in contracts:
        wanted.setdefault(underlying, set()).add(contract)
    out: dict[str, tuple[float, str]] = {}
    for underlying, chain in latest_chains(lake, sorted(wanted)).items():
        rows = chain[chain["contract"].isin(wanted[underlying])]
        for _, r in rows.iterrows():
            bid, ask = r["bid"], r["ask"]
            if pd.notna(bid) and pd.notna(ask) and ask > 0:
                when = pd.Timestamp(r["snapshot_at"]).tz_convert(NEW_YORK).date().isoformat()
                out[str(r["contract"])] = ((float(bid) + float(ask)) / 2, when)
    return out


def _intrinsic(right: str, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)


def track_record(lake: Lake, today: date, *, limit: int = 200) -> dict[str, Any]:
    """How each idea (``recommended``) has done, newest first: the underlying's move since (in
    the recommended direction, and against the S&P 500), the position marked now or at expiry,
    and its exit plan followed; ``state`` is closed once the plan closed it or it expired."""
    recs = recommended(load_log(lake))
    if recs.empty:
        return {"recommendations": [], "summary": [], "as_of": today.isoformat()}
    symbols = sorted({*recs["symbol"], "SPY"})
    bars = load_bars(lake, symbols)
    closes = close_matrix(bars) if len(bars) else pd.DataFrame()
    raw = close_matrix(bars, adjusted=False) if len(bars) else pd.DataFrame()
    sessions = list(closes.index)
    all_legs = [(r["symbol"], json.loads(r["legs"])) for _, r in recs.iterrows()]
    marks = _contract_marks(lake, [(s, leg["contract"]) for s, found in all_legs for leg in found])
    plans = [json.loads(p) if isinstance(p, str) else None for p in recs["plan"]]
    followed = [
        (str(r["symbol"]), found, _run_day(r["run_at"]))
        for (_, r), (_, found), plan in zip(recs.iterrows(), all_legs, plans, strict=True)
        if plan is not None and found
    ]
    history = _daily_marks(lake, followed)
    stock = _stock_indicators(bars)
    ohlc = _ohlc(bars)
    rows = []
    for (_, r), (_, found), plan in zip(recs.iterrows(), all_legs, plans, strict=True):
        start = pd.Timestamp(r["bars_session"]) if pd.notna(r["bars_session"]) else None
        shares = r["structure"] == "shares"
        row: dict[str, Any] = {
            "list": r["list"],
            "symbol": r["symbol"],
            "setup": r["setup"] if isinstance(r["setup"], str) else None,
            "structure": r["structure"],
            "recommended_at": pd.Timestamp(r["run_at"]).isoformat(),
            "last_seen": pd.Timestamp(r["last_seen"]).isoformat(),
            "runs": int(r["runs"]),
            "contracts": int(r["contracts"]),
            "rank": int(r["rank"]),
            "spot": _float(r["spot"]),
            "legs": [
                leg | {"label": option_label(leg["contract"]) or leg["contract"]} for leg in found
            ],
            "entry": _float(r["entry"]),
            "news": (
                {"sentiment": _float(r["news"]), "headlines": int(r["headlines"])}
                if pd.notna(r.get("headlines"))
                else None
            ),
        }
        sign = -1.0 if r["list"] == "bearish" else 1.0
        row |= _moves(closes, sessions, str(r["symbol"]), start, sign)
        if shares:
            row |= _mark_shares(row, closes, str(r["symbol"]))
            row["plan"] = plan
            row["followed"] = (
                follow_shares(
                    plan,
                    _float(r["entry"]),
                    ohlc.get(str(r["symbol"])),
                    stock.get(str(r["symbol"])),
                    start,
                    _run_day(r["run_at"]),
                )
                if plan is not None
                else None
            )
            row["state"] = "closed" if (row["followed"] or {}).get("status") == "closed" else "open"
            row["news_group"] = news_group(row)
            rows.append(row)
            continue
        row |= _mark(found, marks, raw, str(r["symbol"]), _float(r["entry"]), today)
        row["plan"] = plan
        row["followed"] = (
            follow_plan(
                found,
                plan,
                _float(r["entry"]),
                history,
                stock.get(str(r["symbol"])),
                _run_day(r["run_at"]),  # the snapshot it was priced from is its entry
                raw,
                str(r["symbol"]),
                today,
            )
            if plan is not None
            else None
        )
        closed = (row["followed"] or {}).get("status") == "closed" or row.get("status") == "expired"
        row["state"] = "closed" if closed else "open"
        row["news_group"] = news_group(row)
        rows.append(row)
    return {
        "as_of": today.isoformat(),
        "total": len(rows),
        "recommendations": rows[:limit],
        "summary": _summary(rows),
        "by_setup": _by_setup(rows),
        "by_news": _by_news(rows),
    }


# -- following the exit plans ---------------------------------------------------------------------


def _run_day(run_at: Any) -> date:
    return pd.Timestamp(run_at).tz_convert(NEW_YORK).date()


def _daily_marks(
    lake: Lake, followed: list[tuple[str, list[dict[str, Any]], Any]]
) -> dict[str, pd.Series]:
    """Each contract's mid at the last snapshot of each session after the day it was
    recommended (index: snapshot dates)."""
    wanted: dict[str, set[str]] = {}
    since: dict[str, date] = {}
    for underlying, found, session in followed:
        wanted.setdefault(underlying, set()).update(leg["contract"] for leg in found)
        day = pd.Timestamp(session).date()
        since[underlying] = min(since.get(underlying, day), day)
    points: dict[str, dict[date, float]] = {}
    for (day, underlying), path in sorted(snapshot_partitions(lake).items()):
        if underlying not in wanted or day <= since[underlying]:
            continue
        chain = read_parquet_dir(path, RAW_OPTION_CHAIN_SCHEMA)
        chain = chain[chain["contract"].isin(wanted[underlying])]
        if chain.empty:
            continue
        last = chain[chain["snapshot_at"] == chain["snapshot_at"].max()]
        for contract, bid, ask in zip(last["contract"], last["bid"], last["ask"], strict=True):
            if pd.notna(bid) and pd.notna(ask) and ask > 0:
                points.setdefault(str(contract), {})[day] = (float(bid) + float(ask)) / 2
    return {c: pd.Series(v).sort_index() for c, v in points.items()}


def _ohlc(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each symbol's adjusted daily open, high, low and close (index: sessions)."""
    out = {}
    for symbol, g in bars.groupby("symbol"):
        frame = g[["adj_open", "adj_high", "adj_low", "adj_close"]].astype(float)
        frame.columns = ["open", "high", "low", "close"]
        frame.index = pd.DatetimeIndex(g["session"])
        out[str(symbol)] = frame.sort_index()
    return out


def _mark_shares(row: Mapping[str, Any], closes: pd.DataFrame, symbol: str) -> dict[str, Any]:
    """Shares marked at the latest close: the entry grown by the stock's adjusted move since the
    session the run saw (so a split or a dividend since doesn't count as a move)."""
    out: dict[str, Any] = {"value": None, "pnl": None, "result": None, "marked": None}
    entry, move = _float(row.get("entry")), row.get("move")
    if entry is None or move is None or symbol not in closes:
        return out
    value = entry * (1 + float(move))
    marked = closes[symbol].dropna().index[-1].date().isoformat()
    return out | {
        "value": value,
        "pnl": (value - entry) * 100,
        "result": float(move),
        "marked": marked,
        "status": "open",
    }


def follow_shares(
    plan: Mapping[str, Any],
    entry: float | None,
    bars: pd.DataFrame | None,
    ind: pd.DataFrame | None,
    session: pd.Timestamp | None,
    start: date,
) -> dict[str, Any] | None:
    """Walk the sessions after the recommendation and sell where the plan says: the stop or the
    target (an open past it fills at the open; the stop first when a session touches both),
    then the stock-side rule or the time stop on the close. Prices are the entry grown by the
    adjusted bars from the session the run saw (``session``); P&L is for 100 shares."""
    if entry is None or bars is None or session is None or session not in bars.index:
        return None
    base = float(bars["close"].loc[session])
    stop, target = _float(plan.get("stop")), _float(plan.get("target"))
    sell_by = date.fromisoformat(str(plan["sell_by"])) if plan.get("sell_by") else None
    rule = (plan.get("stock") or {}).get("rule")
    stock_exit = exit_signal(ind, rule) if ind is not None and rule else None
    last: dict[str, Any] | None = None
    days = [d for d in pd.DatetimeIndex(bars.index) if d.date() > start]
    for n, day in enumerate(days, start=1):
        o, h, lo, c = (
            entry * float(bars[k].loc[day]) / base for k in ("open", "high", "low", "close")
        )
        fill, reason = None, None
        if stop is not None and o <= stop:
            fill, reason = o, "stop"
        elif target is not None and o >= target:
            fill, reason = o, "target"
        elif stop is not None and lo <= stop:
            fill, reason = stop, "stop"
        elif target is not None and h >= target:
            fill, reason = target, "target"
        elif stock_exit is not None and bool(stock_exit.get(day)):
            fill, reason = c, "stock"
        elif sell_by is not None and day.date() >= sell_by:
            fill, reason = c, "time"
        last = _closed(fill if fill is not None else c, entry, day.date(), n, reason)
        if reason is not None:
            return last
    return last


def _stock_indicators(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol, g in bars.groupby("symbol"):
        frame = g[["adj_high", "adj_low", "adj_close"]].astype(float)
        frame.columns = ["high", "low", "close"]
        frame.index = pd.DatetimeIndex(g["session"])
        out[str(symbol)] = indicators(frame.sort_index())
    return out


def follow_plan(
    found: list[dict[str, Any]],
    plan: Mapping[str, Any],
    entry: float | None,
    marks: Mapping[str, pd.Series],
    ind: pd.DataFrame | None,
    start: date,
    raw: pd.DataFrame,
    symbol: str,
    today: date,
) -> dict[str, Any] | None:
    """Walk the sessions after the recommendation and close where the exit plan says: the
    profit target or the stop on the position's value (per share; for a credit, what it costs
    to buy back), the stock-side rule, the time stop, the days-to-expiry limit, or expiry. Marks
    are the middle of the bid and ask at each session's last snapshot."""
    if entry is None or not found:
        return None
    credit = bool(plan.get("credit"))
    target, stop = _float(plan.get("take_profit")), _float(plan.get("stop"))
    sell_by = date.fromisoformat(str(plan["sell_by"])) if plan.get("sell_by") else None
    out_by = date.fromisoformat(str(plan["out_by"])) if plan.get("out_by") else None
    expiry = min(date.fromisoformat(str(leg["expiration"])[:10]) for leg in found)
    rule = (plan.get("stock") or {}).get("rule")
    stock_exit = exit_signal(ind, rule) if ind is not None and rule else None
    days = sorted({d for leg in found for d in marks.get(leg["contract"], pd.Series()).index})
    last: dict[str, Any] | None = None
    for n, day in enumerate((d for d in days if d > start), start=1):
        value = 0.0
        for leg in found:
            series = marks.get(leg["contract"])
            if series is None or day not in series.index:
                break
            value += (1.0 if leg["side"] == "buy" else -1.0) * float(series[day])
        else:
            reason = _exit_reason(value, credit, target, stop)
            if reason is None and stock_exit is not None:
                hit = stock_exit.get(pd.Timestamp(day))
                reason = "stock" if bool(hit) else None
            if reason is None and sell_by is not None and day >= sell_by:
                reason = "time"
            if reason is None and out_by is not None and day >= out_by:
                reason = "days to expiry"
            last = _closed(value, entry, day, n, reason)
            if reason is not None:
                return last
    if today > expiry:
        spot = _close_on(raw, symbol, expiry)
        if spot is not None:
            value = sum(
                (1.0 if leg["side"] == "buy" else -1.0)
                * _intrinsic(str(leg["right"]), float(leg["strike"]), spot)
                for leg in found
            )
            return _closed(value, entry, expiry, last["sessions"] + 1 if last else 0, "expiry")
    return last | {"status": "open", "reason": None} if last is not None else None


def _exit_reason(
    value: float, credit: bool, target: float | None, stop: float | None
) -> str | None:
    if credit:  # what buying it back would cost
        owed = -value
        if target is not None and owed <= target:
            return "target"
        if stop is not None and owed >= stop:
            return "stop"
        return None
    if target is not None and value >= target:
        return "target"
    if stop is not None and value <= stop:
        return "stop"
    return None


def _closed(
    value: float, entry: float, day: date, sessions: int, reason: str | None
) -> dict[str, Any]:
    return {
        "status": "closed" if reason else "open",
        "reason": reason,
        "exit": day.isoformat(),
        "sessions": sessions,
        "value": value,
        "pnl": (value - entry) * 100,
        "result": (value - entry) / abs(entry) if entry else None,
    }


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _moves(
    closes: pd.DataFrame,
    sessions: list[pd.Timestamp],
    symbol: str,
    start: pd.Timestamp | None,
    sign: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {"move": None, "move_vs_spy": None}
    out |= {f"move_{h}d": None for h in HORIZONS}
    if start is None or symbol not in closes or start not in closes.index:
        return out
    i = sessions.index(start)
    s = closes[symbol]
    spy = closes.get("SPY")
    base = s.iloc[i]
    last = s.dropna()
    if last.empty or base != base or i == len(sessions) - 1:
        return out  # no session has closed since it was recommended
    out["move"] = sign * (float(last.iloc[-1]) / float(base) - 1)
    out["sessions"] = len(sessions) - 1 - i
    if spy is not None and spy.iloc[i] == spy.iloc[i]:
        spy_move = float(spy.dropna().iloc[-1]) / float(spy.iloc[i]) - 1
        out["move_vs_spy"] = out["move"] - sign * spy_move
    for h in HORIZONS:
        if i + h < len(sessions) and s.iloc[i + h] == s.iloc[i + h]:
            out[f"move_{h}d"] = sign * (float(s.iloc[i + h]) / float(base) - 1)
    return out


def _mark(
    found: list[dict[str, Any]],
    marks: Mapping[str, tuple[float, str]],
    raw: pd.DataFrame,
    symbol: str,
    entry: float | None,
    today: date,
) -> dict[str, Any]:
    """The position's value now per share (+ worth having, - owed), and its P&L."""
    out: dict[str, Any] = {"value": None, "pnl": None, "result": None, "marked": None}
    if not found or entry is None:
        return out
    value, dates, status = 0.0, [], "open"
    for leg in found:
        expiration = date.fromisoformat(str(leg["expiration"])[:10])
        sign = 1.0 if leg["side"] == "buy" else -1.0
        if expiration < today:
            status = "expired"
            spot = _close_on(raw, symbol, expiration)
            if spot is None:
                return out
            value += sign * _intrinsic(str(leg["right"]), float(leg["strike"]), spot)
            dates.append(expiration.isoformat())
        elif leg["contract"] in marks:
            mid, when = marks[leg["contract"]]
            value += sign * mid
            dates.append(when)
        else:
            return out
    pnl = (value - entry) * 100
    out |= {"value": value, "pnl": pnl, "status": status, "marked": max(dates)}
    # a debit: return on what was paid; a credit: the share of the premium kept
    out["result"] = (value - entry) / abs(entry) if entry else None
    return out


def _close_on(raw: pd.DataFrame, symbol: str, day: date) -> float | None:
    if symbol not in raw:
        return None
    s = raw[symbol].dropna()
    s = s[s.index <= pd.Timestamp(day)]
    return float(s.iloc[-1]) if len(s) else None


def _plan_stats(mine: list[dict[str, Any]]) -> dict[str, Any]:
    """How the ideas did following their exit plans: the closed ones' win rate and median."""
    closed = [
        r["followed"]["result"]
        for r in mine
        if (r.get("followed") or {}).get("status") == "closed"
        and r["followed"].get("result") is not None
    ]
    return {
        "plan_closed": len(closed),
        "plan_win_rate": (sum(1 for x in closed if x > 0) / len(closed)) if closed else None,
        "plan_median": _median(closed),
    }


def _by_setup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name in sorted({r["setup"] for r in rows if r.get("setup")}):
        mine = [r for r in rows if r.get("setup") == name]
        out.append({"setup": name, "recommendations": len(mine), **_plan_stats(mine)})
    return out


NEWS_GROUPS = (
    ("agreed", "The news agreed"),
    ("against", "The news ran against it"),
    ("neutral", "Mixed or neutral news"),
    ("quiet", "Little or no news"),
)


def news_group(row: Mapping[str, Any]) -> str | None:
    """How the news stood toward an idea when it was listed: its mean sentiment times the idea's
    direction, from two headlines or more (None for ideas with no direction, and for ideas
    listed before the news was kept)."""
    direction = DIRECTION.get(str(row["list"]), 1)
    news = row.get("news")
    if not direction or news is None:
        return None
    if news["headlines"] < 2 or news["sentiment"] is None:
        return "quiet"
    lean = direction * news["sentiment"]
    return "agreed" if lean >= 0.15 else "against" if lean <= -0.15 else "neutral"


def _by_news(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The directional ideas by how the news stood toward them when listed: did following the
    news's lead help?"""
    out = []
    for key, name in NEWS_GROUPS:
        mine = [r for r in rows if r.get("news_group") == key]
        if not mine:
            continue
        moved = [r["move_21d"] for r in mine if r.get("move_21d") is not None]
        out.append(
            {
                "group": key,
                "name": name,
                "recommendations": len(mine),
                "right_way_21d": (sum(1 for x in moved if x > 0) / len(moved)) if moved else None,
                "median_move_21d": _median(moved),
                **_plan_stats(mine),
            }
        )
    return out


def _summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for k in LISTS:
        mine = [r for r in rows if r["list"] == k]
        if not mine:
            continue
        marked = [r["result"] for r in mine if r.get("result") is not None]
        moved = {
            h: [r[f"move_{h}d"] for r in mine if r.get(f"move_{h}d") is not None] for h in HORIZONS
        }
        out.append(
            {
                "list": k,
                "name": LIST_NAMES[k],
                "recommendations": len(mine),
                "marked": len(marked),
                "profitable": (sum(1 for x in marked if x > 0) / len(marked)) if marked else None,
                "median_result": _median(marked),
                **{
                    f"right_way_{h}d": (sum(1 for x in v if x > 0) / len(v)) if v else None
                    for h, v in moved.items()
                },
                **{f"median_move_{h}d": _median(v) for h, v in moved.items()},
                "beat_spy": _share(r.get("move_vs_spy") for r in mine),
                **_plan_stats(mine),
            }
        )
    return out


def _median(values: list[float]) -> float | None:
    return float(pd.Series(values).median()) if values else None


def _share(values: Iterable[Any]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(1 for v in vals if v > 0) / len(vals) if vals else None
