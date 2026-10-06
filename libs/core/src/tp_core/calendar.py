"""NYSE (XNYS) trading calendar helpers.

Sessions are represented as plain ``datetime.date`` values: the exchange-local trading day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_START = "2000-01-01"


@cache
def xnys() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS", start=CALENDAR_START)


def sessions(start: date, end: date) -> list[date]:
    """All XNYS sessions in ``[start, end]`` (inclusive)."""
    if end < start:
        return []
    cal = xnys()
    lo = max(pd.Timestamp(start), cal.first_session)
    hi = min(pd.Timestamp(end), cal.last_session)
    if hi < lo:
        return []
    return [ts.date() for ts in cal.sessions_in_range(lo, hi)]


def is_session(day: date) -> bool:
    return bool(xnys().is_session(pd.Timestamp(day)))


def last_completed_session(now: datetime) -> date:
    """The most recent session whose close is at or before ``now`` (handles half days)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cal = xnys()
    now_utc = pd.Timestamp(now.astimezone(UTC))
    today_ny = now.astimezone(NEW_YORK).date()
    for session in reversed(cal.sessions_in_range(today_ny - timedelta(days=14), today_ny)):
        if cal.session_close(session) <= now_utc:
            return session.date()  # type: ignore[no-any-return]
    raise LookupError(f"no completed XNYS session in the 14 days before {now}")


def sessions_back(end: date, count: int) -> list[date]:
    """The ``count`` sessions ending at (and including, if a session) ``end``."""
    if count <= 0:
        return []
    window = sessions(end - timedelta(days=count * 2 + 10), end)
    return window[-count:]


def session_midnight_utc(day: date) -> pd.Timestamp:
    """Midnight New York time on ``day`` expressed in UTC: Alpaca's daily-bar timestamp."""
    return pd.Timestamp(datetime(day.year, day.month, day.day, tzinfo=NEW_YORK)).tz_convert(UTC)


@cache
def last_session_of_month(year: int, month: int) -> date:
    """The final XNYS session of a calendar month (the exchange calendar is known in advance,
    so using it is not look-ahead)."""
    first = date(year, month, 1)
    nxt = date(year + month // 12, month % 12 + 1, 1)
    days = sessions(first, nxt - timedelta(days=1))
    if not days:
        raise LookupError(f"no XNYS sessions in {year}-{month:02d}")
    return days[-1]


def is_last_session_of_month(day: date) -> bool:
    return day == last_session_of_month(day.year, day.month)


def next_session(day: date) -> date:
    """The first session strictly after ``day``."""
    upcoming = sessions(day + timedelta(days=1), day + timedelta(days=14))
    if not upcoming:
        raise LookupError(f"no XNYS session in the 14 days after {day}")
    return upcoming[0]


def previous_session(day: date) -> date:
    """The last session strictly before ``day``."""
    earlier = sessions(day - timedelta(days=14), day - timedelta(days=1))
    if not earlier:
        raise LookupError(f"no XNYS session in the 14 days before {day}")
    return earlier[-1]


def session_open(day: date) -> datetime:
    """Regular-hours open of session ``day`` (UTC-aware)."""
    ts = xnys().session_open(pd.Timestamp(day))
    return ts.to_pydatetime().astimezone(UTC)  # type: ignore[no-any-return]


def session_close(day: date) -> datetime:
    """Regular-hours close of session ``day`` (UTC-aware); 13:00 New York on half days."""
    ts = xnys().session_close(pd.Timestamp(day))
    return ts.to_pydatetime().astimezone(UTC)  # type: ignore[no-any-return]
