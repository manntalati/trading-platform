import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_paper import cli
from tp_paper.config import PaperBook
from tp_paper.store import PaperStore, Proposal, now_iso
from tp_strategies.library import REGISTRY

REPO = Path(__file__).resolve().parents[3]


def test_repo_book_runs_every_library_strategy_inside_a_default_paper_account() -> None:
    book = PaperBook.load(REPO / "config" / "paper.toml")
    assert {s.name for s in book.sleeves} == set(REGISTRY)
    assert book.capital <= 100_000  # Alpaca paper accounts start at $100k
    assert {s.approval for s in book.sleeves} == {"manual"}


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[strategies.nope]\ncapital = 1\n", "unknown strategy"),
        ("[strategies.rsi2]\ncapital = 0\n", "positive capital"),
        ('[strategies.rsi2]\ncapital = 1\napproval = "yolo"\n', "manual or auto"),
        ("[strategies.rsi2]\ncapital = 1\ncapitol = 2\n", "unknown key"),
        ('[strategies.rsi2]\ncapital = 1\nparams = { slots = "x" }\n', "expected int"),
        ("[account]\n", "unknown section"),
    ],
)
def test_book_errors(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "paper.toml"
    path.write_text(toml)
    with pytest.raises((KeyError, ValueError), match=message):
        PaperBook.load(path)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    book = tmp_path / "paper.toml"
    book.write_text('[strategies.ma-timing]\ncapital = 20000\napproval = "manual"\n')
    run_backfill(Lake(root), FakeSource(), ["SPY", "EFA", "IEF", "VNQ", "DBC"],
                 now=datetime.now(UTC), years=2)  # fmt: skip
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(root))
    monkeypatch.setenv("TP_PAPER_FILE", str(book))
    monkeypatch.setenv("TP_RISK_FILE", str(REPO / "config" / "risk.toml"))
    monkeypatch.setenv("TP_CLASSIFICATIONS_FILE", str(REPO / "config" / "classifications.toml"))
    monkeypatch.setenv("TP_PAPER_BROKER", "fake")
    return root


def pending(store: PaperStore, pid: str, quantity: float = 10) -> None:
    now = now_iso()
    store.add_proposals(
        [
            Proposal(
                pid,
                "ma-timing",
                "2024-07-31",
                "SPY",
                "buy",
                quantity,
                "market",
                500.0,
                "test",
                [],
                "pending",
                now,
                now,
            )
        ]
    )


def test_cli_cycle_with_the_simulated_broker(cli_env: Path) -> None:
    run = CliRunner().invoke
    proposed = run(cli.app, ["propose"])
    assert proposed.exit_code == 0, proposed.output
    assert "session " in proposed.output

    store = PaperStore.under(cli_env)
    pending(store, "a")
    pending(store, "b")
    pending(store, "c")
    listed = run(cli.app, ["proposals"])
    assert "pending           a" in listed.output
    assert run(cli.app, ["approve", "a", "--quantity", "4"]).exit_code == 0
    assert run(cli.app, ["reject", "b", "--note", "no"]).exit_code == 0
    assert run(cli.app, ["approve", "--all"]).exit_code == 0
    assert store.proposal("a").order_quantity == 4
    assert store.proposal("b").status == "rejected"
    assert store.proposal("c").status == "approved"
    again = run(cli.app, ["approve", "b"])
    assert again.exit_code == 1
    assert "is rejected, not pending" in again.output

    status = run(cli.app, ["status", "--json"])
    assert status.exit_code == 0, status.output
    report = json.loads(status.output)
    assert report["broker"] == "fake-paper"
    assert report["sleeves"][0]["strategy"] == "ma-timing"

    killed = run(cli.app, ["kill", "--reason", "drill"])
    assert "kill switch ENGAGED" in killed.output
    refused = run(cli.app, ["submit", "--tif", "day"])
    assert refused.exit_code == 1
    assert "kill switch engaged (drill)" in refused.output


def test_cli_without_keys_points_at_the_fake_broker(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(cli.app, ["--broker", "alpaca", "status"])
    assert result.exit_code == 2
    assert "--broker fake" in result.output
