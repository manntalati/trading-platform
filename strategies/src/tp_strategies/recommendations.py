"""Recommendations made on a schedule and kept, with a track record.

Each run stores the options research (swing calls, LEAPS, bearish puts, premium selling) and the
portfolio checks as they stood, so the dashboard opens on the latest at once, can say what is new
since the previous session, and can show how every past recommendation has done since:

    reports/recommendations/<run id>.json   the full run (kept KEEP_DAYS days)
    clean/recommendations_log/part.parquet  one row per idea per run, kept for good

A run is made whenever its inputs changed (new bars, a new option chain snapshot, a brokerage
sync, new macro data): ``tp-recs run``, which the job scheduler starts on its own.

The track record marks each recommended position at the latest quotes (the middle of the bid and
ask; entries were at the ask for a purchase and the bid for a sale, as the picks were priced), or
at its value at expiry once it has expired. It is the record of rules applied to past data, not
of trades anyone made.
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
from tp_core.storage import Lake, new_run_id, read_parquet_dir, write_json, write_parquet
from tp_core.volatility import latest_chains
from tp_strategies.ideas import ideas_from_lake
from tp_strategies.options_research import CHAIN_MAX_AGE_DAYS, DISCLAIMER, scan

log = logging.getLogger(__name__)

RUNS = "recommendations"
LOG = "recommendations_log"
KEEP_DAYS = 30
LISTS = ("swing", "leaps", "bearish", "income")
LIST_NAMES = {
    "swing": "Swing calls",
    "leaps": "LEAPS",
    "bearish": "Bearish",
    "income": "Premium selling",
}
HORIZONS = (5, 21)  # sessions after the recommendation, for the underlying's move
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
    ]
)


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
) -> dict[str, Any]:
    """Scan for ideas and check the portfolio, as of ``now``."""
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
) -> RunResult:
    """Make a run, mark what's new, store it and add it to the log."""
    run = make_run(
        lake, universes=universes, classifier=classifier, now=now, default_rate=default_rate
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
        if isinstance(node, Mapping):
            if "contract" in node and "side" in node:
                out.append(
                    {
                        k: node.get(k)
                        for k in ("contract", "side", "price", "expiration", "strike", "right")
                    }
                )
                return
            for value in node.values():
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
                    "entry": _entry(found),
                }
            )
    return pd.DataFrame(rows, columns=[f.name for f in LOG_SCHEMA])


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
    """Each distinct recommendation (list, symbol and the same contracts) once, at the first run
    it appeared in, with when it was last seen and in how many runs."""
    if log_df.empty:
        return log_df.assign(last_seen=pd.Series(dtype=object), runs=pd.Series(dtype=int))
    df = log_df.sort_values("run_at")
    keys = ["list", "symbol", "legs"]
    first = df.groupby(keys, sort=False).head(1).set_index(keys)
    grouped = df.groupby(keys, sort=False)
    first["last_seen"] = grouped["run_at"].max()
    first["runs"] = grouped["run_id"].nunique()
    return first.reset_index().sort_values("run_at", ascending=False).reset_index(drop=True)


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
    """How each recommendation has done: the underlying's move since (in the recommended
    direction, and against the S&P 500), and the position marked now or at expiry."""
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
    rows = []
    for (_, r), (_, found) in zip(recs.iterrows(), all_legs, strict=True):
        start = pd.Timestamp(r["bars_session"]) if pd.notna(r["bars_session"]) else None
        row: dict[str, Any] = {
            "list": r["list"],
            "symbol": r["symbol"],
            "structure": r["structure"],
            "recommended_at": pd.Timestamp(r["run_at"]).isoformat(),
            "last_seen": pd.Timestamp(r["last_seen"]).isoformat(),
            "runs": int(r["runs"]),
            "rank": int(r["rank"]),
            "spot": _float(r["spot"]),
            "legs": [
                leg | {"label": option_label(leg["contract"]) or leg["contract"]} for leg in found
            ],
            "entry": _float(r["entry"]),
        }
        sign = -1.0 if r["list"] == "bearish" else 1.0
        row |= _moves(closes, sessions, str(r["symbol"]), start, sign)
        row |= _mark(found, marks, raw, str(r["symbol"]), _float(r["entry"]), today)
        rows.append(row)
    return {
        "as_of": today.isoformat(),
        "recommendations": rows[:limit],
        "summary": _summary(rows),
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
            }
        )
    return out


def _median(values: list[float]) -> float | None:
    return float(pd.Series(values).median()) if values else None


def _share(values: Iterable[Any]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(1 for v in vals if v > 0) / len(vals) if vals else None
