from __future__ import annotations

import pandas as pd

from tp_core.versions import data_version, git_version


def test_data_version_names_the_rows_and_last_session() -> None:
    bars = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB"],
            "session": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-03"]).date,
            "ingested_at": pd.Timestamp("2024-01-04", tz="UTC"),
        }
    )
    version = data_version(bars)
    assert version["rows"] == 3
    assert version["symbols"] == 2
    assert version["last_session"] == "2024-01-03"
    assert data_version(bars.iloc[:0]) == {"rows": 0}


def test_git_version_reports_a_commit_or_says_it_cannot() -> None:
    version = git_version()
    assert set(version) >= {"commit", "dirty"}
