from datetime import UTC, date, datetime

import pytest

from tp_core.calendar import (
    is_session,
    last_completed_session,
    session_midnight_utc,
    sessions,
    sessions_back,
)


def test_sessions_skip_weekends_and_holidays() -> None:
    days = sessions(date(2024, 12, 20), date(2025, 1, 3))
    assert date(2024, 12, 25) not in days  # Christmas
    assert date(2025, 1, 1) not in days  # New Year
    assert date(2024, 12, 21) not in days  # Saturday
    assert days[0] == date(2024, 12, 20)
    assert days[-1] == date(2025, 1, 3)


def test_is_session() -> None:
    assert is_session(date(2024, 7, 3))
    assert not is_session(date(2024, 7, 4))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Wednesday 2024-07-03 is a half day: closes 13:00 ET (17:00 UTC).
        (datetime(2024, 7, 3, 16, 59, tzinfo=UTC), date(2024, 7, 2)),
        (datetime(2024, 7, 3, 17, 0, tzinfo=UTC), date(2024, 7, 3)),
        # Thursday 2024-07-04 holiday: still the 3rd.
        (datetime(2024, 7, 4, 23, 0, tzinfo=UTC), date(2024, 7, 3)),
        # Monday 2024-07-08 before the close (16:00 ET = 20:00 UTC): Friday.
        (datetime(2024, 7, 8, 19, 59, tzinfo=UTC), date(2024, 7, 5)),
        (datetime(2024, 7, 8, 20, 0, tzinfo=UTC), date(2024, 7, 8)),
    ],
)
def test_last_completed_session(now: datetime, expected: date) -> None:
    assert last_completed_session(now) == expected


def test_last_completed_session_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        last_completed_session(datetime(2024, 7, 8, 12, 0))  # noqa: DTZ001


def test_sessions_back_includes_end() -> None:
    assert sessions_back(date(2024, 7, 8), 3) == [
        date(2024, 7, 3),
        date(2024, 7, 5),
        date(2024, 7, 8),
    ]


def test_session_midnight_utc_tracks_dst() -> None:
    assert session_midnight_utc(date(2024, 1, 2)).hour == 5  # EST
    assert session_midnight_utc(date(2024, 7, 2)).hour == 4  # EDT
