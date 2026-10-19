"""Regime gates from the lake: the trend and VIX rules, with their bands."""

import numpy as np
import pandas as pd
import pytest

from tp_core.bars import close_matrix, load_bars
from tp_ml.testing import write_lake
from tp_strategies.regime_gate import hysteresis, make_gate, regime_series


def test_hysteresis_needs_a_clear_move_each_way() -> None:
    x = pd.Series([0.0, 0.8, 0.6, 0.4, 0.2, 0.5, 0.75, np.nan, 0.1])
    assert hysteresis(x, 0.7, 0.3).tolist() == [
        False, True, True, True, False, False, True, True, False
    ]  # fmt: skip


def test_trend_gate_follows_spy_against_its_200_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lake = write_lake(tmp_path, ["SPY"], n=500, seed=4, drifts=[-0.002])  # a falling market
    off, detail = regime_series(lake, "trend")
    assert "200-day" in detail
    close = close_matrix(load_bars(lake))["SPY"]
    below = close / close.rolling(200).mean() - 1
    assert off[below < -0.05].all()  # well below the average: out
    assert not off.iloc[:199].any()  # no average yet: in
    gate = make_gate(lake, "trend")
    assert gate is not None
    assert gate.off(off.index[-1].date()) == bool(off.iloc[-1])
    assert make_gate(lake, "off") is None


def test_gates_say_what_data_they_need(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lake = write_lake(tmp_path, ["SPY"], n=300, seed=1)
    with pytest.raises(ValueError, match="five years"):
        make_gate(lake, "hmm")
    with pytest.raises(ValueError, match="macro backfill"):
        make_gate(lake, "vix")
    with pytest.raises(ValueError, match="unknown regime gate"):
        make_gate(lake, "astrology")
