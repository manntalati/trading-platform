from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tp_ml.cli import app, default_symbols
from tp_ml.testing import write_lake

runner = CliRunner()
SYMBOLS = ["SPY", *(f"S{i:02d}" for i in range(19))]
CONFIG = Path(__file__).parents[3] / "config"


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ml")
    write_lake(path, SYMBOLS, n=800, seed=4)
    return path


@pytest.fixture(autouse=True)
def env(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(root))
    monkeypatch.setenv("TP_UNIVERSES_FILE", str(CONFIG / "universes.toml"))
    monkeypatch.setenv("TP_CLASSIFICATIONS_FILE", str(CONFIG / "classifications.toml"))


def test_default_symbols_are_the_index_etfs_and_shared_large_caps() -> None:
    symbols = default_symbols(CONFIG / "universes.toml")
    assert symbols[:4] == ["SPY", "QQQ", "IWM", "DIA"]
    assert len(symbols) == len(set(symbols)) > 30
    assert "TQQQ" not in symbols  # no leveraged or bond ETFs


def test_features_build_and_list() -> None:
    out = runner.invoke(app, ["features"])
    assert out.exit_code == 0, out.output
    assert "daily" in out.output
    assert "20 symbols" in out.output
    listed = runner.invoke(app, ["features", "--list"])
    assert listed.exit_code == 0, listed.output
    assert "daily" in listed.output


def test_meta_evaluates_and_logs_to_mlflow() -> None:
    symbols = ",".join(SYMBOLS)
    out = runner.invoke(app, ["meta", "--symbols", symbols, "--model", "logistic"])
    assert out.exit_code == 0, out.output
    assert "RSI(2) signals" in out.output
    assert "Walk-forward" in out.output
    assert "logged to MLflow" in out.output
    runs = runner.invoke(app, ["runs"])
    assert runs.exit_code == 0, runs.output
    assert "rsi2-trade-logistic" in runs.output


def test_meta_without_logging_and_bad_options() -> None:
    symbols = ",".join(SYMBOLS)
    out = runner.invoke(
        app, ["meta", "--symbols", symbols, "--model", "logistic", "--label", "barrier", "--no-log"]
    )
    assert out.exit_code == 0, out.output
    assert "reached the target first" in out.output
    assert "logged" not in out.output
    bad = runner.invoke(app, ["meta", "--model", "forest"])
    assert bad.exit_code == 2
    none = runner.invoke(app, ["meta"])  # the default universe isn't in this lake
    assert none.exit_code == 2
    assert "tp-data bars backfill" in none.output


def test_runs_on_an_empty_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "empty"))
    out = runner.invoke(app, ["runs"])
    assert out.exit_code == 0, out.output
    assert "no runs yet" in out.output


def test_rank_evaluates_and_logs_to_mlflow() -> None:
    symbols = ",".join(SYMBOLS[1:])
    out = runner.invoke(app, ["rank", "--symbols", symbols, "--model", "ridge"])
    assert out.exit_code == 0, out.output
    for phrase in ("19 stocks", "Model, walk-forward", "12-1 momentum (baseline)", "logged"):
        assert phrase in out.output
    runs = runner.invoke(app, ["runs", "--experiment", "cross-sectional-ranking"])
    assert runs.exit_code == 0, runs.output
    assert "rank-ridge-21d" in runs.output
    both = runner.invoke(app, ["runs"])
    assert "cross-sectional-ranking" in both.output


def test_rank_without_logging_and_bad_options() -> None:
    symbols = ",".join(SYMBOLS[1:])
    out = runner.invoke(app, ["rank", "--symbols", symbols, "--model", "gbm", "--no-log"])
    assert out.exit_code == 0, out.output
    assert "Model: gbm" in out.output
    assert "logged" not in out.output
    assert runner.invoke(app, ["rank", "--model", "forest"]).exit_code == 2
    none = runner.invoke(app, ["rank"])  # the default large caps aren't in this lake
    assert none.exit_code == 2
    assert "tp-data bars backfill" in none.output


@pytest.fixture(scope="module")
def spy_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("spy")
    write_lake(path, ["SPY", "XYZ"], n=1400, seed=6)
    return path


def test_vol_and_regime_report_and_log(spy_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TP_DATA_ROOT", str(spy_root))
    out = runner.invoke(app, ["vol"])
    assert out.exit_code == 0, out.output
    for phrase in ("SPY", "GARCH(1,1)", "HAR-RV", "Now (", "logged"):
        assert phrase in out.output
    assert "Implied now" not in out.output  # no VIX in this lake
    other = runner.invoke(app, ["vol", "--symbol", "xyz", "--no-log"])
    assert other.exit_code == 0, other.output
    assert other.output.startswith("XYZ")
    regimes = runner.invoke(app, ["regime"])
    assert regimes.exit_code == 0, regimes.output
    for phrase in ("calm", "stressed", "Today:", "logged"):
        assert phrase in regimes.output
    runs = runner.invoke(app, ["runs"])
    assert "volatility" in runs.output
    assert "vol-SPY-21d" in runs.output
    assert "hmm-2" in runs.output


def test_vol_and_regime_need_enough_history() -> None:
    short = runner.invoke(app, ["vol", "--symbol", "S00"])  # 800 sessions in this lake
    assert short.exit_code == 2
    assert "sessions of S00 bars" in short.output
    assert runner.invoke(app, ["regime"]).exit_code == 2
    assert runner.invoke(app, ["regime", "--states", "4"]).exit_code == 2
