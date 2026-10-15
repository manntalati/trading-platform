from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tp_ml import regime
from tp_ml.testing import ohlc_from_returns, simulate_regimes


@pytest.fixture(scope="module")
def switching() -> tuple[pd.DataFrame, np.ndarray]:
    """Ten years of returns that switch between a calm (0.6%/day) and a stressed (2%/day)
    state, each lasting about 50 days."""
    returns, state, sigma = simulate_regimes(2520, stay=0.98, seed=4)
    return ohlc_from_returns(returns, sigma, seed=4), state


def test_the_hmm_recovers_the_states(switching: tuple[pd.DataFrame, np.ndarray]) -> None:
    bars, truth = switching
    obs = regime.observations(bars)
    model = regime.fit_regimes(obs, 2)
    probs = model.probabilities(obs)
    assert list(probs.columns) == ["calm", "stressed"]
    assert np.allclose(probs.sum(axis=1), 1.0)
    found = probs.to_numpy().argmax(axis=1)
    true = pd.Series(truth, index=bars.index).reindex(obs.index).to_numpy()
    assert (found == true).mean() > 0.85
    desc = regime.describe(model, obs)
    calm, stressed = desc["states"]
    assert calm["ann_vol"] < 0.5 * stressed["ann_vol"]
    assert calm["expected_days"] > 10
    assert stressed["expected_days"] > 10
    assert sum(desc["now"].values()) == pytest.approx(1.0)


def test_filtered_probabilities_use_only_the_past(
    switching: tuple[pd.DataFrame, np.ndarray],
) -> None:
    bars, _ = switching
    obs = regime.observations(bars)
    model = regime.fit_regimes(obs, 2)
    full = model.probabilities(obs)
    cut = obs.index[1200]
    changed = obs.copy()
    changed.loc[changed.index > cut, "log_vol"] += 2.0  # a crisis after the cut
    again = model.probabilities(changed)
    upto = full.index <= cut
    pd.testing.assert_frame_equal(full[upto], again[upto])


def test_out_of_sample_states_predict_next_months_volatility(
    switching: tuple[pd.DataFrame, np.ndarray],
) -> None:
    bars, _ = switching
    ev = regime.evaluate_regimes(bars, 2, min_years=3)
    assert ev["days"] > 1000
    calm, stressed = ev["by_state"]
    assert stressed["next_vol"] > 1.5 * calm["next_vol"]
    assert 0 < ev["time_in_market"] < 1
    assert ev["timed"]["max_drawdown"] > ev["buy_and_hold"]["max_drawdown"]  # shallower
    text = regime.report_text(regime.describe(regime.fit_regimes(regime.observations(bars)),
                                              regime.observations(bars)), ev)  # fmt: skip
    for phrase in ("calm", "stressed", "Out of sample", "SPY only while calm"):
        assert phrase in text


def test_three_states_are_ordered_calm_to_stressed() -> None:
    returns, _, sigma = simulate_regimes(2000, vols=(0.005, 0.012, 0.03), stay=0.985, seed=2)
    obs = regime.observations(ohlc_from_returns(returns, sigma, seed=2))
    model = regime.fit_regimes(obs, 3)
    vols = [s["ann_vol"] for s in regime.describe(model, obs)["states"]]
    assert vols == sorted(vols)
    assert list(model.probabilities(obs).columns) == ["calm", "normal", "stressed"]


def test_hmm_on_known_parameters() -> None:
    """Two well-separated Gaussian states with a sticky switch: Baum-Welch finds them."""
    rng = np.random.default_rng(0)
    state = np.repeat([0, 1, 0, 1, 0], 200)
    x = rng.normal(np.where(state == 1, 3.0, -1.0), 1.0)[:, None]
    hmm = regime.GaussianHMM(n_states=2, seed=1).fit(x)
    assert hmm.means_ is not None
    assert hmm.trans_ is not None
    assert hmm.means_[:, 0] == pytest.approx([-1.0, 3.0], abs=0.2)
    assert np.diag(hmm.trans_).min() > 0.97
    assert (hmm.filter(x).argmax(axis=1) == state).mean() > 0.97
