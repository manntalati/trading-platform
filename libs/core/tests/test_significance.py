"""Probabilistic and deflated Sharpe ratios, and the block bootstrap."""

import math

import numpy as np
import pandas as pd
import pytest

from tp_core.significance import (
    block_bootstrap,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
)


def returns(mean: float, sd: float = 0.01, n: int = 1000, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, sd, n))


def test_probabilistic_sharpe_grows_with_the_edge_and_the_sample() -> None:
    assert probabilistic_sharpe(returns(0.002)) > 0.99  # Sharpe ~3 a year over 4 years
    flat = returns(0.0)
    assert probabilistic_sharpe(flat - flat.mean()) == pytest.approx(0.5)  # Sharpe exactly 0
    assert probabilistic_sharpe(returns(0.001, n=60)) < probabilistic_sharpe(returns(0.001))
    assert math.isnan(probabilistic_sharpe(pd.Series([0.01, 0.02])))


def test_expected_max_sharpe_rises_with_trials() -> None:
    assert expected_max_sharpe(1, 0.01) == 0.0
    assert 0 < expected_max_sharpe(5, 0.01) < expected_max_sharpe(100, 0.01)
    # about 2.5 standard deviations for 100 trials
    assert expected_max_sharpe(100, 1.0) == pytest.approx(2.53, abs=0.05)


def test_deflated_sharpe_is_harder_to_pass_after_many_trials() -> None:
    r = returns(0.0005)
    one = deflated_sharpe(r, [0.8])
    assert one.trials == 1
    assert one.threshold == 0.0
    assert one.probability == pytest.approx(probabilistic_sharpe(r))
    many = deflated_sharpe(r, [0.8, 0.2, -0.3, 1.1, 0.5, -0.1, 0.9, 0.0, 0.4, 0.6])
    assert many.trials == 10
    assert many.threshold > 0
    assert many.probability < one.probability
    assert many.excess == pytest.approx(many.sharpe - many.threshold)


def test_block_bootstrap_brackets_the_outcome() -> None:
    r = returns(0.0015, n=756)
    mc = block_bootstrap(r, paths=500)
    assert mc["paths"] == 500
    assert mc["cagr_p5"] < mc["cagr_p50"] < mc["cagr_p95"]
    assert mc["max_drawdown_p5"] <= mc["max_drawdown_p50"] <= 0
    assert 0 <= mc["prob_loss"] < 0.5
    assert block_bootstrap(r.iloc[:30]) == {}
