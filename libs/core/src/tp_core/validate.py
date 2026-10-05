"""Data validation for daily bars.

Errors mean a row cannot be trusted and is quarantined (kept out of the clean table but saved for
inspection). Warnings flag rows that are probably fine but worth a look; they stay in.

| check            | severity | meaning                                                        |
|------------------|----------|----------------------------------------------------------------|
| bad_timestamp    | error    | not midnight New York, not an XNYS session, or in the future   |
| duplicate        | error    | the same (symbol, session) twice within one ingest run        |
| bad_price        | error    | an OHLC value is missing, zero or negative                     |
| ohlc_inconsistent| error    | low/high do not bracket open and close                         |
| negative_volume  | error    | volume below zero                                              |
| zero_volume      | warning  | no shares traded (halt, or a feed gap)                         |
| missing_sessions | warning  | XNYS sessions with no bar between a symbol's first and last bar|
| price_jump       | warning  | adjusted close-to-close move above the threshold (default 20%) |
| adjustment       | warning  | a corporate action that could not be applied                   |
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from tp_core.calendar import NEW_YORK, sessions

PRICE_COLS = ("open", "high", "low", "close")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    symbol: str
    session: date | None
    check: str
    severity: Severity
    detail: str


@dataclass
class ValidationReport:
    rows_in: int = 0
    rows_clean: int = 0
    rows_quarantined: int = 0
    symbols: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            key = f"{issue.severity}:{issue.check}"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> str:
        return (
            f"{self.rows_in} rows / {self.symbols} symbols in, {self.rows_clean} clean, "
            f"{self.rows_quarantined} quarantined; {len(self.errors)} errors, "
            f"{len(self.warnings)} warnings {self.counts()}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_clean": self.rows_clean,
            "rows_quarantined": self.rows_quarantined,
            "symbols": self.symbols,
            "ok": self.ok,
            "counts": self.counts(),
            "issues": [asdict(i) for i in self.issues],
        }


def _issues_for(
    rows: pd.DataFrame, check: str, severity: Severity, detail: str | pd.Series
) -> list[Issue]:
    details = detail if isinstance(detail, pd.Series) else pd.Series(detail, index=rows.index)
    return [
        Issue(str(sym), sess, check, severity, str(details[idx]))
        for idx, sym, sess in zip(rows.index, rows["symbol"], rows["session"], strict=True)
    ]


def check_structure(
    bars: pd.DataFrame, *, now: datetime
) -> tuple[pd.DataFrame, pd.DataFrame, list[Issue]]:
    """Row-level checks. Returns (good rows, quarantined rows with a ``reason``, issues).

    ``bars`` needs ``symbol``, ``session``, ``timestamp`` (tz-aware), OHLC, ``volume`` and
    ``run_id``. Duplicates are judged within a run: re-fetching a session in a later run is
    normal and is resolved before this check by keeping the latest run.
    """
    if bars.empty:
        return bars, bars.assign(reason=pd.Series(dtype=object)), []
    issues: list[Issue] = []
    bad = pd.Series(False, index=bars.index)
    reason = pd.Series("", index=bars.index, dtype=object)

    def flag(mask: pd.Series, check: str, detail: str | pd.Series) -> None:
        nonlocal bad
        if mask.any():
            issues.extend(_issues_for(bars[mask], check, Severity.ERROR, detail))
            reason[mask & ~bad] = check
            bad = bad | mask

    ts_ny = bars["timestamp"].dt.tz_convert(NEW_YORK)
    not_midnight = (ts_ny.dt.hour != 0) | (ts_ny.dt.minute != 0) | (ts_ny.dt.second != 0)
    days = bars["session"]
    valid_days = set(sessions(min(days), max(days))) if len(days) else set()
    not_session = ~days.isin(valid_days)
    in_future = bars["timestamp"] > pd.Timestamp(now)
    flag(
        not_midnight | not_session | in_future,
        "bad_timestamp",
        "timestamp "
        + bars["timestamp"].astype(str)
        + " is not midnight New York on a past session",
    )

    dupes = bars.duplicated(subset=["symbol", "session", "run_id"], keep=False)
    flag(dupes, "duplicate", "same symbol/session more than once in one run")

    prices = bars[list(PRICE_COLS)]
    flag((prices.isna() | (prices <= 0)).any(axis=1), "bad_price", "OHLC missing or <= 0")

    tol = 1e-9 * bars["high"].abs()
    inconsistent = (
        (bars["low"] > bars[["open", "close"]].min(axis=1) + tol)
        | (bars["high"] < bars[["open", "close"]].max(axis=1) - tol)
        | (bars["low"] > bars["high"])
    )
    ohlc_text = bars[list(PRICE_COLS)].apply(
        lambda row: "/".join(f"{v:g}" for v in row), axis=1, result_type="reduce"
    )
    flag(inconsistent & ~bad, "ohlc_inconsistent", "o/h/l/c = " + ohlc_text)

    flag(bars["volume"] < 0, "negative_volume", "volume < 0")

    good = bars[~bad]
    zero_vol = good["volume"] == 0
    issues.extend(_issues_for(good[zero_vol], "zero_volume", Severity.WARNING, "volume == 0"))

    quarantined = bars[bad].assign(reason=reason[bad])
    return good, quarantined, issues


def check_missing_sessions(bars: pd.DataFrame) -> list[Issue]:
    """One warning per run of consecutive missing sessions, per symbol."""
    issues: list[Issue] = []
    for symbol, group in bars.groupby("symbol"):
        have = set(group["session"])
        expected = sessions(min(have), max(have))
        missing = [s for s in expected if s not in have]
        if not missing:
            continue
        run: list[date] = [missing[0]]
        index = {s: i for i, s in enumerate(expected)}
        for s in missing[1:]:
            if index[s] == index[run[-1]] + 1:
                run.append(s)
                continue
            issues.append(_gap_issue(str(symbol), run))
            run = [s]
        issues.append(_gap_issue(str(symbol), run))
    return issues


def _gap_issue(symbol: str, run: list[date]) -> Issue:
    span = f"{run[0]}" if len(run) == 1 else f"{run[0]}..{run[-1]}"
    return Issue(
        symbol, run[0], "missing_sessions", Severity.WARNING, f"{len(run)} session(s): {span}"
    )


def check_price_jumps(adjusted: pd.DataFrame, *, threshold: float = 0.20) -> list[Issue]:
    """Warn on adjusted close-to-close moves larger than ``threshold`` (0.20 = 20%)."""
    issues: list[Issue] = []
    for _, group in adjusted.groupby("symbol"):
        group = group.sort_values("session")
        change = group["adj_close"].pct_change()
        jumps = group[np.abs(change) > threshold]
        detail = change.loc[jumps.index].map(lambda c: f"adjusted close moved {c:+.1%}")
        issues.extend(_issues_for(jumps, "price_jump", Severity.WARNING, detail))
    return issues
