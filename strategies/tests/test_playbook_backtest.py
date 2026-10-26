"""Backtesting the playbook on a synthetic lake (S00..S11): events, exits on hand-built paths,
the walk-forward choice, the calibration the live ideas read, and real option bars."""

import json
import math
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tp_core import greeks as bs
from tp_core import option_history
from tp_core.storage import Lake
from tp_ml.testing import write_lake
from tp_strategies import playbook
from tp_strategies import playbook_backtest as pb
from tp_strategies.setups import SETUPS

NOW = datetime(2024, 1, 6, 15, 0, tzinfo=UTC)
NAMES = ["SPY", *[f"S{i:02d}" for i in range(12)]]


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    drifts = [0.0003, *np.linspace(-0.001, 0.0015, 12)]
    return write_lake(tmp_path_factory.mktemp("pb"), NAMES, n=1100, seed=11, drifts=drifts)


def test_the_strike_for_a_delta() -> None:
    for delta in (0.55, 0.8, -0.55):
        k = float(pb.strike_for(100.0, 0.3, 0.2, delta, 0.04))
        assert bs.greeks(100.0, k, 0.2, 0.3, call=delta > 0, rate=0.04).delta == pytest.approx(
            delta, abs=1e-6
        )


def paths(ret: list[list[float]], stock: list[list[bool]] | None = None) -> pb.Paths:
    r = np.array([[math.nan, *row] for row in ret])
    s = np.array([[False, *row] for row in stock]) if stock else np.zeros_like(r, dtype=bool)
    return pb.Paths(r, s, np.zeros_like(r, dtype=bool), np.zeros(len(r), dtype=bool))


def test_exits_take_the_first_rule_that_fires() -> None:
    p = paths([[0.1, 0.3, 0.6, 0.2], [-0.2, -0.6, 0.5, 0.9], [0.0, 0.05, 0.1, 0.2]],
              stock=[[False] * 4, [False] * 4, [False, True, False, False]])  # fmt: skip
    result, held = pb.apply_exits(p, take_profit=0.5, stop_loss=-0.5, time_stop=10,
                                  underlying=True)  # fmt: skip
    assert result == pytest.approx([0.6, -0.6, 0.05])  # target, stop, the stock-side rule
    assert list(held) == [3, 2, 2]
    timed, held = pb.apply_exits(p, take_profit=None, stop_loss=None, time_stop=2,
                                 underlying=False)  # fmt: skip
    assert timed == pytest.approx([0.3, -0.6, 0.05])
    ridden, _ = pb.apply_exits(p, take_profit=None, stop_loss=None, time_stop=99, underlying=False)
    assert ridden == pytest.approx([0.2, 0.9, 0.2])  # the last mark
    gap = paths([[0.1, math.nan, math.nan, 0.4]])  # no marks on the time stop's session
    late, _ = pb.apply_exits(gap, take_profit=None, stop_loss=None, time_stop=2, underlying=False)
    assert late == pytest.approx([0.4])  # the next mark after it


def test_the_walk_forward_choice_is_judged_on_events_it_never_saw() -> None:
    rng = np.random.default_rng(0)
    p = paths(rng.normal(0.02, 0.3, (200, 20)).cumsum(axis=1).tolist())
    train = np.arange(200) < 120
    out = pb.choose_exits(p, SETUPS["breakout"], train)
    assert out["combinations"] == 72
    assert set(out["exits"]) == {"take_profit", "stop_loss", "time_stop", "underlying"}
    assert out["train"]["trades"] == 120
    assert out["test"]["trades"] == 80
    assert out["default"]["trades"] == 80
    few = pb.choose_exits(paths([[0.1, 0.2]] * 10), SETUPS["breakout"], np.arange(10) < 6)
    assert few["exits"] is None  # too few trades to choose: the defaults stand


def test_calibrate_a_lake_and_the_live_playbook_reads_it(lake: Lake) -> None:
    report = pb.calibrate(lake, NAMES, now=NOW)
    assert report["names"] == 12
    path = playbook.calibration_path(lake)
    assert json.loads(path.read_text())["generated_at"] == NOW.isoformat()
    assert path.with_name("calibration-20240106.json").exists()
    pull = report["setups"]["pullback"]
    assert pull["events"] > 50
    assert pull["horizon"] == 10
    under = pull["underlying"]
    n = pull["events"]
    shrunk = (n * under["mean"] + pb.SHRINK * pull["baseline"]) / (n + pb.SHRINK)
    assert pull["drift"] == pytest.approx(shrunk, rel=0.05)  # events past the end don't count
    assert 0 <= under["hit_rate"] <= 1
    assert pull["option"]["trades"] > 0
    assert "default" in pull["option"]["detail"]
    lines = pb.report_lines(report)
    assert any(line.startswith("Pullback in an uptrend (pullback)") for line in lines)

    loaded = playbook.load_calibration(lake)
    assert loaded is not None
    setups = playbook.calibrated_setups(loaded)
    if "exits" in pull:
        assert setups["pullback"].exits.time_stop == pull["exits"]["time_stop"]
    drift, source = playbook.setup_drift(SETUPS["pullback"], loaded)
    assert (drift, source) == (pytest.approx(pull["drift"]), "backtest")
    assert report["setups"]["earnings_up"]["events"] == 0  # no fundamentals in this lake


def test_real_option_bars_replace_the_model(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "REAL_SINCE", date(2023, 6, 1))  # the synthetic bars end in 2023
    pb.calibrate(lake, NAMES, now=NOW)
    wanted = option_history.wanted(lake)
    assert wanted, "the trades since REAL_SINCE should ask for contracts"
    assert all(w["start"] >= "2023-06-01" for w in wanted)
    w = next(x for x in wanted if x["key"].split("|")[0] != "SPY")
    # pretend the contract was fetched: flat at 2.00, then worth 3.00 from the third session
    days = pd.bdate_range(w["start"], periods=30)
    closes = [2.0, 2.0] + [3.0] * 28
    option_history.save_bars(lake, pd.DataFrame({
        "contract": "XYZ240216C00100000", "session": [d.date() for d in days], "open": closes,
        "high": closes, "low": closes, "close": closes, "volume": 1.0, "trade_count": 1.0,
        "vwap": closes, "ingested_at": pd.Timestamp(NOW)}))  # fmt: skip
    option_history.save_matches(lake, pd.DataFrame([{
        "key": w["key"], "contract": "XYZ240216C00100000", "underlying": w["underlying"],
        "right": w["right"], "expiration": date(2024, 2, 16), "strike": 100.0,
        "matched_at": pd.Timestamp(NOW)}]))  # fmt: skip
    report = pb.calibrate(lake, NAMES, now=NOW)
    real = [s["real"] for s in report["setups"].values() if s.get("real")]
    assert real, "the matched contract's bars should be used"
    assert real[0]["trades"] >= 1
    assert real[0]["model_bias"] is not None
