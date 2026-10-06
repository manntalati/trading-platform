from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.calendar import sessions
from tp_core.portfolio import Classifier
from tp_risk import cli
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import FileRiskState
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine
from tp_trading.strategy import Context
from tp_trading.testing import Scripted

REPO = Path(__file__).resolve().parents[3]


def test_file_state_round_trips(tmp_path: Path) -> None:
    state = FileRiskState.under(tmp_path)
    assert state.kill_switch() is None
    state.set_kill_switch("testing")
    state.set_disabled("a", "drawdown")
    state.set_peak("a", 123.0)
    again = FileRiskState.under(tmp_path)
    kill = again.kill_switch()
    assert kill is not None
    assert kill.reason == "testing"
    disabled = again.disabled("a")
    assert disabled is not None
    assert disabled.reason == "drawdown"
    assert again.peak("a") == 123.0
    again.set_disabled("a", None)
    again.set_peak("a", None)
    assert state.disabled("a") is None
    assert state.peak("a") is None


def test_cli_switches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TP_RISK_FILE", str(REPO / "config" / "risk.toml"))
    run = CliRunner().invoke
    assert run(cli.app, ["kill", "--reason", "fat finger"]).exit_code == 0
    assert run(cli.app, ["disable", "ma-timing", "--reason", "review"]).exit_code == 0
    status = run(cli.app, ["status"])
    assert status.exit_code == 0, status.output
    assert "ENGAGED" in status.output
    assert "fat finger" in status.output
    assert "ma-timing            DISABLED review" in status.output
    assert "max_position                 0.1" in status.output
    run(cli.app, ["resume"])
    run(cli.app, ["enable", "ma-timing"])
    status = run(cli.app, ["status"])
    assert "kill switch: off" in status.output
    assert "ma-timing            enabled" in status.output


def test_backtest_records_rejections_from_the_risk_manager() -> None:
    days = pd.DatetimeIndex(sessions(date(2024, 1, 2), date(2024, 2, 29)))
    closes = pd.DataFrame(
        {"AAPL": np.linspace(100, 110, len(days)), "SPY": np.linspace(400, 420, len(days))},
        index=days,
    )

    def act(ctx: Context) -> None:
        if ctx.now == days[0].date():
            ctx.order_target_weights({"AAPL": 0.5, "SPY": 0.5}, reason="too concentrated")

    risk = RiskManager(Limits(), Classifier.load(REPO / "config" / "classifications.toml"))
    strategy = Scripted(universe=("AAPL", "SPY"), act=act)
    result = BacktestEngine(strategy, MarketData.from_closes(closes), risk=risk).run()
    rejected = result.orders[result.orders["status"] == "rejected"]
    assert rejected["symbol"].tolist() == ["AAPL"]
    assert rejected["note"].iloc[0].startswith("max_position: AAPL 50.0% of equity")
    assert result.positions["SPY"].iloc[-1] > 0
    assert result.positions["AAPL"].iloc[-1] == 0
