from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_strategies import cli

NOW = datetime(2024, 7, 12, 22, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


def test_tp_ideas_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XOM", "UNH", "EFA", "IEF"]
    run_backfill(lake, FakeSource(), symbols, now=NOW, years=3)
    run_sync(lake, FakeBroker(as_of=NOW.date()), now=NOW)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(lake.root))
    monkeypatch.setenv("TP_CLASSIFICATIONS_FILE", str(REPO / "config" / "classifications.toml"))

    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert "Not investment advice" in result.output
    assert "[portfolio]" in result.output
    assert "[strategy] 10-month MA timing on your current mix" in result.output

    only = CliRunner().invoke(cli.app, ["--kind", "candidate"])
    assert "[portfolio]" not in only.output
