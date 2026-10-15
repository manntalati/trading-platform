"""Portfolio sync: store a snapshot of balances/positions and new transactions in the lake; and
``check_sync``, which compares what the aggregator has with what the lake stored."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tp_broker.base import BrokerSource, RefreshResult
from tp_core.calendar import NEW_YORK
from tp_core.portfolio import (
    account_label,
    connection_problems,
    latest_snapshot,
    read_activities,
)
from tp_core.schemas import (
    RAW_BROKER_ACCOUNTS,
    RAW_BROKER_ACCOUNTS_SCHEMA,
    RAW_BROKER_ACTIVITIES,
    RAW_BROKER_ACTIVITIES_SCHEMA,
    RAW_BROKER_HOLDINGS,
    RAW_BROKER_HOLDINGS_SCHEMA,
    conform,
)
from tp_core.storage import Lake, new_run_id, write_raw

log = logging.getLogger(__name__)

# Re-fetch this much transaction history each sync so late-posting activity is picked up.
ACTIVITY_OVERLAP = timedelta(days=30)


@dataclass
class SyncResult:
    run_id: str
    source: str
    accounts: int = 0
    holdings: int = 0
    activities: int = 0
    files: list[Path] = field(default_factory=list)
    refresh: str | None = None  # what asking the aggregator for fresh data achieved
    warnings: list[str] = field(default_factory=list)  # why data may have stopped updating


def run_sync(
    lake: Lake, source: BrokerSource, *, now: datetime, refresh: bool = False
) -> SyncResult:
    """Snapshot balances and positions and fetch new transactions. With ``refresh``, first have
    the aggregator pull fresh positions from the brokerage (its data is otherwise up to a day
    old; SnapTrade charges a small fee per refresh)."""
    run_id = new_run_id(now)
    result = SyncResult(run_id=run_id, source=source.name)
    if refresh:
        refresher = getattr(source, "refresh", None)
        if refresher is None:
            result.refresh = f"{source.name} can't be refreshed on demand"
        else:
            outcome: RefreshResult = refresher()
            result.refresh = outcome.message
            log.info("%s refresh: %s (%.0fs)", source.name, outcome.message, outcome.waited)
    snapshot_date = now.astimezone(NEW_YORK).date()
    stamp: dict[str, Any] = {
        "taken_at": pd.Timestamp(now),
        "snapshot_date": snapshot_date,
        "source": source.name,
        "ingested_at": pd.Timestamp(now),
        "run_id": run_id,
    }
    partition = {"source": source.name, "snapshot_date": snapshot_date.isoformat()}

    snap = source.snapshot()
    result.accounts, result.holdings = len(snap.accounts), len(snap.holdings)
    result.warnings = connection_problems(snap.accounts.assign(source=source.name), now)
    for warning in result.warnings:
        log.warning("%s", warning)
    if not snap.accounts.empty:
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_ACCOUNTS,
                conform(snap.accounts.assign(**stamp), RAW_BROKER_ACCOUNTS_SCHEMA),
                run_id=run_id,
                partition=partition,
                schema=RAW_BROKER_ACCOUNTS_SCHEMA,
            )
        )
    if not snap.holdings.empty:
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_HOLDINGS,
                conform(snap.holdings.assign(**stamp), RAW_BROKER_HOLDINGS_SCHEMA),
                run_id=run_id,
                partition=partition,
                schema=RAW_BROKER_HOLDINGS_SCHEMA,
            )
        )

    acts = source.activities(_activities_since(lake, source.name))
    result.activities = len(acts)
    if not acts.empty:
        stamped = acts.assign(source=source.name, ingested_at=pd.Timestamp(now), run_id=run_id)
        result.files.append(
            write_raw(
                lake,
                RAW_BROKER_ACTIVITIES,
                conform(stamped, RAW_BROKER_ACTIVITIES_SCHEMA),
                run_id=run_id,
                partition={"source": source.name, "ingest_date": snapshot_date.isoformat()},
                schema=RAW_BROKER_ACTIVITIES_SCHEMA,
            )
        )
    log.info(
        "%s sync: %d accounts, %d holdings, %d activities",
        source.name,
        result.accounts,
        result.holdings,
        result.activities,
    )
    return result


def _activities_since(lake: Lake, source: str) -> dict[str, date]:
    """Per account: re-fetch from a month before its latest stored transaction. An account with
    none stored (newly linked) isn't in the result, so its whole history is fetched."""
    acts = read_activities(lake)
    acts = acts[acts["source"] == source] if not acts.empty else acts
    acts = acts.dropna(subset=["trade_date"]) if not acts.empty else acts
    if acts.empty:
        return {}
    latest = acts.groupby("account_id")["trade_date"].max()
    return {str(account): day - ACTIVITY_OVERLAP for account, day in latest.items()}


# -- check: the aggregator against the lake ----------------------------------------------------

CHECK_COLUMNS = ["trade_date", "type", "symbol", "units", "amount", "description"]


@dataclass
class AccountCheck:
    account_id: str
    label: str
    disabled: bool | None
    disabled_since: str | None
    positions_as_of: pd.Timestamp | None
    transactions_through: date | None
    remote: int  # transactions the aggregator has in the window
    stored: int  # of those, how many the lake has
    missing: pd.DataFrame  # the aggregator's transactions the lake doesn't have (CHECK_COLUMNS)
    orphaned: int  # the lake's transactions in the window the aggregator no longer lists


@dataclass
class SyncCheck:
    start: date
    last_sync: pd.Timestamp | None
    accounts: list[AccountCheck]
    unlinked: list[str]  # accounts in the lake that the aggregator no longer returns


def check_sync(lake: Lake, source: Any, *, now: datetime, days: int = 45) -> SyncCheck:
    """Compare the aggregator's transactions over the last ``days`` with the lake's, per
    account, along with the connection's state and freshness. Read-only on both sides."""
    start = now.astimezone(NEW_YORK).date() - timedelta(days=days)
    live = source.snapshot().accounts
    remote = source.activities(start)
    links: dict[str, dict[str, Any]] = (
        source.account_connections() if hasattr(source, "account_connections") else {}
    )
    stored = read_activities(lake)
    if not stored.empty:
        stored = stored[(stored["source"] == source.name) & (stored["trade_date"] >= start)]
    snap = latest_snapshot(lake)
    synced = snap.accounts[snap.accounts["source"] == source.name] if not snap.empty else None
    last_sync = (
        pd.Timestamp(synced["taken_at"].max()) if synced is not None and len(synced) else None
    )
    checks = []
    for a in live.to_dict("records"):
        account_id = str(a["account_id"])
        theirs = remote[remote["account_id"] == account_id] if not remote.empty else remote
        ours = stored[stored["account_id"] == account_id] if not stored.empty else stored
        have = set(ours["activity_id"].astype(str)) if not ours.empty else set()
        listed = set(theirs["activity_id"].astype(str)) if not theirs.empty else set()
        missing = theirs[~theirs["activity_id"].astype(str).isin(have)] if len(theirs) else theirs
        link = links.get(account_id) or {}
        disabled = a.get("connection_disabled")
        checks.append(
            AccountCheck(
                account_id=account_id,
                label=account_label(a),
                disabled=None if disabled is None or pd.isna(disabled) else bool(disabled),
                disabled_since=str(link["disabled_date"])[:10]
                if disabled and link.get("disabled_date")
                else None,
                positions_as_of=a.get("holdings_as_of"),
                transactions_through=a.get("transactions_as_of"),
                remote=len(theirs),
                stored=len(listed & have),
                missing=missing.reindex(columns=CHECK_COLUMNS).sort_values("trade_date"),
                orphaned=len(have - listed),
            )
        )
    linked = {str(a) for a in live["account_id"]}
    unlinked = (
        [account_label(a) for a in synced.to_dict("records") if str(a["account_id"]) not in linked]
        if synced is not None
        else []
    )
    return SyncCheck(start=start, last_sync=last_sync, accounts=checks, unlinked=unlinked)


def render_check(check: SyncCheck, now: datetime, *, show: int = 15) -> str:
    """The check in plain words, ending with what to do about each problem."""
    lines = []
    if check.last_sync is None:
        lines.append("Never synced here: run `tp-broker sync` (and schedule it, docs/setup.md).")
    else:
        ago = (pd.Timestamp(now) - check.last_sync).total_seconds() / 3600
        late = ". Is the scheduled `tp-broker sync` running? See data/logs/broker.log"
        lines.append(
            f"Last sync here: {check.last_sync.tz_convert(NEW_YORK):%Y-%m-%d %H:%M} ET "
            f"({ago:.0f} hours ago){late if ago > 26 else ''}"
        )
    for c in check.accounts:
        lines.append(f"\n{c.label}")
        positions = (
            f"{pd.Timestamp(c.positions_as_of).tz_convert(NEW_YORK):%Y-%m-%d %H:%M} ET"
            if c.positions_as_of is not None and not pd.isna(c.positions_as_of)
            else "unknown"
        )
        through = c.transactions_through or "unknown"
        state = {True: "DISABLED", False: "connected", None: "status unknown"}[c.disabled]
        lines.append(
            f"  SnapTrade: {state}; positions as of {positions}; transactions through {through}"
        )
        lines.append(
            f"  Since {check.start}: SnapTrade lists {c.remote} transactions, "
            f"the lake has {c.stored} of them"
        )
        if c.disabled:
            since = f" since {c.disabled_since}" if c.disabled_since else ""
            lines.append(
                f"  ✗ The connection to the broker is disabled{since}: SnapTrade keeps serving "
                "the last data it got, so newer trades never arrive. Reconnect with "
                "`tp-broker link --reconnect` (log in to the broker again in the page it opens)."
            )
        if len(c.missing):
            lines.append(
                f"  ✗ {len(c.missing)} of SnapTrade's transactions aren't in the lake. Run "
                "`tp-broker sync`; if they stay missing, send the output of `tp-broker check`."
            )
            for m in c.missing.head(show).to_dict("records"):
                units = "" if pd.isna(m["units"]) else f" {float(m['units']):g}"
                lines.append(f"      {m['trade_date']}  {m['type']:<16} {m['symbol'] or ''}{units}")
            if len(c.missing) > show:
                lines.append(f"      ... and {len(c.missing) - show} more")
        if c.orphaned:
            lines.append(
                f"  ! {c.orphaned} stored transactions since {check.start} are no longer in "
                "SnapTrade's list (it may have re-issued them under new ids, which would show "
                "them twice)."
            )
        stale = c.transactions_through is not None and now.astimezone(
            NEW_YORK
        ).date() - c.transactions_through > timedelta(days=4)
        if stale and not c.disabled:
            lines.append(
                f"  ! SnapTrade's transactions stop at {c.transactions_through}: it hasn't pulled "
                "new ones from the broker since. If this lasts, reconnect with "
                "`tp-broker link --reconnect`."
            )
        if not c.disabled and not len(c.missing) and not stale:
            lines.append(
                "  ✓ Every transaction SnapTrade has is stored. Trades made after "
                f"{through} arrive with the broker's overnight posting; until then the "
                "dashboard shows them as pending from the change in positions, once SnapTrade "
                "has refreshed those (daily, or now with `tp-broker sync --refresh`, for a "
                "small SnapTrade fee)."
            )
    for label in check.unlinked:
        lines.append(
            f"\n! {label} is in the lake but SnapTrade no longer returns it (unlinked, or "
            "re-linked under a new id): it no longer shows on the dashboard."
        )
    return "\n".join(lines)
