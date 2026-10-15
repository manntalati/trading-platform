from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tp_ml import ranking
from tp_ml.cv import oof_regress, walk_forward
from tp_ml.features import compute_features
from tp_ml.models import make_regressor
from tp_ml.testing import random_bars

SYMBOLS = [f"S{i:02d}" for i in range(20)]


def drifting_bars(strength: float, seed: int = 0, n: int = 1200) -> dict[str, pd.DataFrame]:
    """Random walks where each stock has its own steady drift (``strength`` scales how much
    they differ): past returns then say something about future ones."""
    bars = random_bars(SYMBOLS, n=n, seed=seed)
    drift = np.linspace(-1, 1, len(SYMBOLS)) * strength
    trend = pd.DataFrame(np.outer(np.arange(n), drift), index=bars["close"].index,
                         columns=SYMBOLS)  # fmt: skip
    growth = np.exp(trend)
    return {k: v * growth if k != "volume" else v for k, v in bars.items()}


def test_rebalance_dates_are_month_ends() -> None:
    index = pd.bdate_range("2024-01-01", "2024-04-15")
    dates = ranking.rebalance_dates(index)
    assert [str(d.date()) for d in dates] == ["2024-01-31", "2024-02-29", "2024-03-29",
                                              "2024-04-15"]  # fmt: skip


def test_cross_rank_ranks_within_each_date() -> None:
    days = pd.to_datetime(["2024-01-31", "2024-02-29"])
    index = pd.MultiIndex.from_product([days, ["A", "B", "C", "D"]], names=["date", "symbol"])
    panel = pd.DataFrame({"x": [1.0, 2.0, 3.0, np.nan, 40.0, 30.0, 20.0, 10.0]}, index=index)
    ranked = ranking.cross_rank(panel)["x"]
    assert ranked.loc["2024-01-31"].tolist()[:3] == pytest.approx([1 / 3 - 0.5, 2 / 3 - 0.5, 0.5])
    assert np.isnan(ranked.loc[("2024-01-31", "D")])
    assert ranked.loc[("2024-02-29", "A")] == pytest.approx(0.5)
    assert ranked.loc[("2024-02-29", "D")] == pytest.approx(0.25 - 0.5)


def test_dataset_labels_the_next_months_return_from_the_next_open() -> None:
    bars = random_bars(SYMBOLS, n=300)
    cfg = ranking.RankConfig(horizon=21)
    data = ranking.ranking_dataset(bars, compute_features(bars), cfg)
    opens = bars["open"]
    day, symbol = data.index[40]
    i = opens.index.get_loc(day)
    expected = opens[symbol].iloc[i + 22] / opens[symbol].iloc[i + 1] - 1
    assert data.loc[(day, symbol), "fwd"] == pytest.approx(expected)
    assert data.loc[(day, symbol), "start"] == opens.index[i + 1]
    assert data.loc[(day, symbol), "end"] == opens.index[i + 22]
    # every label ends inside the bars; the last month end (no next month yet) is left out
    assert data["end"].max() <= opens.index[-1]
    assert data["target"].between(-0.5, 0.5).all()
    assert set(data.index.get_level_values("date")) <= set(ranking.rebalance_dates(opens.index))


@pytest.mark.parametrize("model", ["ridge", "gbm"])
def test_a_planted_signal_is_found_out_of_sample(model: str) -> None:
    bars = drifting_bars(0.004)
    cfg = ranking.RankConfig(model=model)
    data = ranking.ranking_dataset(bars, compute_features(bars), cfg)
    report = ranking.evaluate_ranking(data, cfg)
    wf = report["walk_forward"]
    assert wf["rank_ic"] > 0.1
    assert wf["rank_ic_t"] > 2
    assert wf["buckets"][-1] > wf["buckets"][0]
    assert wf["spread"] > 0


@pytest.mark.parametrize("model", ["ridge", "gbm"])
def test_noise_ranks_at_chance(model: str) -> None:
    """The leak check: with nothing to find, the out-of-sample rank IC stays near zero."""
    bars = drifting_bars(0.0, seed=3)
    cfg = ranking.RankConfig(model=model)
    data = ranking.ranking_dataset(bars, compute_features(bars), cfg)
    wf = ranking.evaluate_ranking(data, cfg)["walk_forward"]
    assert abs(wf["rank_ic"]) < 0.05
    assert abs(wf["rank_ic_t"]) < 2.5


def test_signal_report_by_hand() -> None:
    dates = pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-28"])
    rows = []
    for d in dates:
        for k in range(10):
            rows.append({"date": d, "symbol": f"S{k}", "score": float(k), "fwd": 0.01 * k})
    frame = pd.DataFrame(rows).set_index(["date", "symbol"])
    report = ranking.signal_report(frame, "score", buckets=5)
    assert report["dates"] == 3
    assert report["rank_ic"] == pytest.approx(1.0)
    assert report["ic"] == pytest.approx(1.0)
    assert report["rank_ic_positive"] == 1.0
    assert report["buckets"] == pytest.approx([0.005, 0.025, 0.045, 0.065, 0.085])
    assert report["spread"] == pytest.approx(0.08)
    assert report["top_turnover"] == 0.0  # the same two names stay on top


def test_fit_and_score_rank_the_days_stocks() -> None:
    bars = drifting_bars(0.004)
    cfg = ranking.RankConfig(model="ridge")
    features = compute_features(bars)
    model = ranking.fit_ranker(ranking.ranking_dataset(bars, features, cfg), cfg)
    assert model.dates > 40
    latest = features.loc[bars["close"].index[-1]]
    scores = model.score(latest)
    assert list(scores.index) == SYMBOLS
    # the steadily rising stocks score higher than the falling ones
    assert scores[SYMBOLS[-5:]].mean() > scores[SYMBOLS[:5]].mean()
    assert ranking.train_ranker(random_bars(SYMBOLS, n=300), cfg, min_dates=24) is None


def test_oof_regress_leaves_untested_rows_empty() -> None:
    rng = np.random.default_rng(0)
    n = 200
    start = pd.Series(pd.bdate_range("2024-01-01", periods=n))
    x = pd.DataFrame({"a": rng.normal(size=n)})
    y = pd.Series(2 * x["a"] + rng.normal(scale=0.1, size=n))
    p = oof_regress(make_regressor("ridge"), x, y, walk_forward(start, start, n_splits=4))
    assert p.iloc[:80].isna().all()
    assert p.iloc[80:].notna().all()
    assert np.corrcoef(p.iloc[80:], y.iloc[80:])[0, 1] > 0.9
    with pytest.raises(ValueError, match="unknown model"):
        make_regressor("forest")


def test_report_text() -> None:
    bars = drifting_bars(0.004)
    cfg = ranking.RankConfig()
    report = ranking.evaluate_ranking(
        ranking.ranking_dataset(bars, compute_features(bars), cfg), cfg
    )
    text = ranking.report_text(report)
    for phrase in ("20 stocks", "Model, walk-forward", "12-1 momentum (baseline)", "by bucket"):
        assert phrase in text
