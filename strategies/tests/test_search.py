from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.storage import Lake
from tp_ml.testing import write_lake
from tp_strategies import search
from tp_strategies.backtest_cli import app
from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_strategies.validation import ValidationConfig

NOW = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
PERIODS = ValidationConfig(train_end=date(2018, 12, 31), validation_end=date(2020, 12, 31),
                           wf_train=504, wf_test=126)  # fmt: skip
DAYS = pd.bdate_range("2013-01-02", "2022-12-30")


def closes(drift_spread: float, seed: int = 0) -> pd.DataFrame:
    """Large caps and index ETFs as random walks; with ``drift_spread`` the stocks' drifts
    differ (persistently), which momentum can find."""
    rng = np.random.default_rng(seed)
    symbols = ["SPY", "QQQ", "IWM", "DIA", *LARGE_CAPS]
    drift = rng.uniform(-drift_spread, drift_spread, len(symbols)) + 0.0002
    steps = drift + 0.015 * rng.standard_normal((len(DAYS), len(symbols)))
    return pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=symbols)


def test_rule_sets_map_to_engine_strategies() -> None:
    rules = search.rule_sets()
    assert len(rules) == 41
    assert {r.family for r in rules} == set(search.DEFAULT_FAMILIES)  # grammar: bred, not listed
    assert len({r.label for r in rules}) == len(rules)
    momentum = next(r for r in rules if r.engine == "xs-momentum")
    assert set(momentum.engine_params) == {"lookback", "skip", "top_n"}
    assert search.engine_command(momentum).startswith("tp-backtest validate xs-momentum -p ")


def test_weights_decide_at_the_close_and_earn_the_next_day() -> None:
    px = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0, 121.0]}, index=pd.bdate_range("2024-01-01", periods=4)
    )
    weights = pd.DataFrame({"A": [1.0, 1.0, 0.0, 0.0]}, index=px.index)
    r = search.daily_returns(weights, px, cost_bps=0.0)
    # bought at day 0's close: earns day 1 (+10%) and day 2 (+10%), sold at day 2's close
    assert r.tolist() == pytest.approx([0.0, 0.10, 0.10, 0.0])
    costly = search.daily_returns(weights, px, cost_bps=100.0)
    assert costly.iloc[1] == pytest.approx(0.10 - 0.01)  # the buy's cost lands the next day


def test_family_weights() -> None:
    px = closes(0.001)
    momentum = search.xs_momentum_weights(px[list(LARGE_CAPS)], 126, 21, 5)
    assert np.allclose(momentum.sum(axis=1).iloc[300:], 1.0)
    assert ((momentum > 0).sum(axis=1).iloc[300:] == 5).all()
    trend = search.ma_timing_weights(px[["SPY"]], 10)
    assert set(np.unique(trend.to_numpy())) <= {0.0, 1.0}
    rsi = search.rsi2_weights(px[list(LARGE_CAPS)], 10, 200, 5)
    assert (rsi.sum(axis=1) <= 1 + 1e-9).all()
    assert (rsi.to_numpy() > 0).any()


def test_pbo_low_for_a_real_edge_and_high_for_noise() -> None:
    rng = np.random.default_rng(1)
    noise = pd.DataFrame(rng.normal(0, 0.01, (2000, 20)))
    assert 0.3 < search.pbo(noise)["pbo"] < 0.75
    edge = noise.copy()
    edge[0] += 0.002  # one rule set really is better
    assert search.pbo(edge)["pbo"] < 0.1


def test_a_real_edge_is_found_and_noise_is_not_trusted() -> None:
    found = search.search(closes(0.0012, seed=2), search.SearchConfig(), PERIODS)
    assert found["chosen"].family == "momentum"
    assert found["pbo"]["pbo"] < 0.3
    assert found["deflated"]["trials"] == 41
    noise = search.search(closes(0.0, seed=5), search.SearchConfig(), PERIODS)
    ok, _ = search.verdict(noise)
    assert not ok
    assert noise["deflated"]["probability"] < 0.95
    text = search.report_text(noise, looks=1)
    for phrase in ("Best 5 on train", "Chosen:", "Deflated Sharpe", "overfitting", "Not yet."):
        assert phrase in text


def test_the_ledger_and_test_looks_accumulate(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    px = closes(0.0012, seed=2)
    cfg = search.SearchConfig(families=("momentum",))
    first, look1 = search.run_search(lake, px, cfg, PERIODS, now=NOW)
    second, look2 = search.run_search(lake, px, cfg, PERIODS, now=NOW)
    assert (look1, look2) == (1, 2)  # the same pick shown the test period twice
    assert first["deflated"]["trials"] == second["deflated"]["trials"] == 16
    wider, _ = search.run_search(lake, px, search.SearchConfig(), PERIODS, now=NOW)
    assert wider["deflated"]["trials"] == 41  # every distinct rule set ever tried
    assert len(list((tmp_path / "reports" / "search").glob("*.json"))) == 3
    _, none = search.run_search(lake, px, cfg, PERIODS, now=NOW, record=False)
    assert none is None


def test_cli_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lake"
    write_lake(root, ["SPY", *LARGE_CAPS[:12]], n=1500, seed=3)
    toml = tmp_path / "validation.toml"
    toml.write_text('[periods]\ntrain_end = "2020-12-31"\nvalidation_end = "2022-06-30"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(root))
    monkeypatch.setenv("TP_VALIDATION_FILE", str(toml))
    out = CliRunner().invoke(app, ["search", "--families", "momentum,reversion"])
    assert out.exit_code == 0, out.output
    for phrase in ("Strategy search: 32 rule sets", "Chosen:", "On the engine:", "logged"):
        assert phrase in out.output
    bad = CliRunner().invoke(app, ["search", "--families", "astrology"])
    assert bad.exit_code != 0
