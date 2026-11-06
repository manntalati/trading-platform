"""Which of the day's snapshots a stored one is (``snapshot_kind``), stored or worked out."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tp_core.chains import infer_kind, load_chain_snapshots
from tp_core.schemas import RAW_OPTION_CHAIN_SCHEMA, RAW_OPTION_CHAIN_SNAPSHOTS
from tp_core.storage import Lake, write_json, write_raw
from tp_core.testing import synthetic_chain


@pytest.mark.parametrize(
    ("at", "kind"),
    [
        (datetime(2026, 11, 5, 5, 52, tzinfo=UTC), "before_open"),  # 00:52 New York
        (datetime(2026, 11, 5, 14, 45, tzinfo=UTC), "morning"),  # 09:45
        (datetime(2026, 11, 5, 17, 50, tzinfo=UTC), "midday"),  # 12:50
        (datetime(2026, 11, 5, 20, 53, tzinfo=UTC), "close"),  # 15:53
        (datetime(2026, 11, 6, 1, 45, tzinfo=UTC), "after_close"),  # 20:45 (cron, Chicago time)
        (datetime(2026, 11, 7, 15, 0, tzinfo=UTC), "after_close"),  # a Saturday
        (datetime(2024, 11, 29, 17, 50, tzinfo=UTC), "close"),  # 12:50 on a half day
    ],
)
def test_kind_from_the_clock(at: datetime, kind: str) -> None:
    assert infer_kind(at) == kind


def test_older_files_get_a_kind_and_late_runs_are_told_apart(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    at = datetime(2026, 11, 5, 20, 45, tzinfo=UTC)
    day = at.date()
    for run_id, when in (("r-late", at), ("r-close", at + timedelta(minutes=8))):
        chain = synthetic_chain("SPY", 700.0, when, expirations=[day + timedelta(days=30)])
        chain["run_id"] = run_id
        write_raw(lake, RAW_OPTION_CHAIN_SNAPSHOTS, chain, run_id=run_id,
                  partition={"snapshot_date": day.isoformat(), "underlying": "SPY"},
                  schema=RAW_OPTION_CHAIN_SCHEMA)  # fmt: skip
    write_json({}, lake.reports_dir("options_late") / f"{day}-r-late.json")
    chains = load_chain_snapshots(lake)
    kinds = chains.drop_duplicates("run_id").set_index("run_id")["snapshot_kind"]
    assert kinds.to_dict() == {"r-late": "late", "r-close": "close"}
