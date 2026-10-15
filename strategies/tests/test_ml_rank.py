from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_ml.ranking import RankModel
from tp_strategies.library import REGISTRY, build, ml_rank
from tp_strategies.library.ml_rank import MlRank, top_by_score
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, BacktestResult

STOCKS = tuple(f"S{i:02d}" for i in range(12))
SECTORS = {s: ("Tech" if i < 6 else "Energy") for i, s in enumerate(STOCKS)}
DAYS = pd.DatetimeIndex(sessions(date(2019, 1, 2), date(2023, 12, 29)))


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    ml_rank._MODELS.clear()
    yield
    ml_rank._MODELS.clear()


def market(seed: int = 7) -> MarketData:
    rng = np.random.default_rng(seed)
    drift = np.linspace(-0.0004, 0.0012, len(STOCKS))
    steps = drift + 0.012 * rng.standard_normal((len(DAYS), len(STOCKS)))
    closes = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=STOCKS)
    return MarketData.from_closes(closes)


def run(strategy: Any) -> BacktestResult:
    return BacktestEngine(strategy, market(), sectors=SECTORS).run()


class ByName:
    """A stand-in regressor: a fixed score per symbol (rows come indexed by symbol)."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.index: list[str] = []

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.array([self.scores.get(str(s), 0.0) for s in x.index])


class FakeRank(RankModel):
    def score(self, latest: pd.DataFrame) -> pd.Series:
        return pd.Series(self.estimator.predict(latest), index=latest.index, dtype=float)


def fake_model(scores: dict[str, float]) -> RankModel:
    return FakeRank(
        estimator=ByName(scores), columns=("ret_21",), dates=48,
        trained_to=pd.Timestamp("2020-12-31"),
    )  # fmt: skip


def test_registered_and_buildable() -> None:
    assert REGISTRY["ml-rank"] is MlRank
    strategy = build("ml-rank", {"model": "gbm", "top_n": "8", "retrain": "6"})
    assert isinstance(strategy, MlRank)
    assert (strategy.model, strategy.top_n, strategy.retrain) == ("gbm", 8, 6)


def test_top_by_score_respects_the_sector_cap() -> None:
    scores = pd.Series({"S00": 0.9, "S01": 0.8, "S02": 0.7, "S06": 0.1, "S07": -0.2})
    targets, reasons = top_by_score(scores, top_n=3, max_per_sector=2, sector=SECTORS.get)
    assert targets == {"S00": pytest.approx(1 / 3), "S01": pytest.approx(1 / 3),
                       "S06": pytest.approx(1 / 3)}  # fmt: skip
    assert "Tech already has 2 names" in reasons["S02"]
    assert "outside the top 3" in reasons["S07"]
    assert "rank 1 of 5" in reasons["S00"]


def test_without_a_model_it_trades_like_xs_momentum() -> None:
    params = {"universe": ",".join(STOCKS), "top_n": "4", "max_per_sector": "4"}
    plain = run(build("xs-momentum", params))
    ml = run(build("ml-rank", {**params, "min_dates": "100000"}))
    columns = ["session", "symbol", "side", "quantity"]
    pd.testing.assert_frame_equal(plain.orders[columns], ml.orders[columns])
    buys = ml.orders[ml.orders["side"] == "buy"]
    assert len(buys) > 10
    assert buys["reason"].str.contains("no model yet").all()


def test_the_model_picks_the_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    favourites = {"S03": 0.9, "S04": 0.8, "S09": 0.7, "S10": 0.6}
    monkeypatch.setattr(ml_rank, "train_ranker", lambda *a, **k: fake_model(favourites))
    result = run(MlRank(universe=STOCKS, top_n=4, max_per_sector=4))
    buys = result.orders[result.orders["side"] == "buy"]
    with_model = buys[~buys["reason"].str.contains("no model yet")]
    assert len(with_model) > 0
    assert set(with_model["symbol"]) <= set(favourites)
    assert with_model["reason"].str.contains("model score").all()
    assert with_model["reason"].str.contains("trained on 48 month ends to 2020-12-31").all()
    held = result.positions.iloc[-1]
    assert set(held[held > 0].index) == set(favourites)


def test_models_see_only_the_bars_before_each_retrain_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[pd.Timestamp] = []

    def recording(past: dict[str, pd.DataFrame], *args: Any, **kwargs: Any) -> RankModel:
        seen.append(pd.Timestamp(past["close"].index.max()))
        return fake_model({})

    monkeypatch.setattr(ml_rank, "train_ranker", recording)
    run(MlRank(universe=STOCKS, top_n=4, retrain=6))
    assert len(seen) == len(set(seen)) >= 6  # twice a year, cached in between
    for last in seen:
        following = DAYS[DAYS.searchsorted(last, side="right")]
        assert following.month in (1, 7)
        assert following.month != last.month


def test_trains_a_real_ranker_and_runs_through_the_engine() -> None:
    result = run(MlRank(universe=STOCKS, top_n=4, max_per_sector=4, min_dates=12, retrain=6))
    buys = result.orders[result.orders["side"] == "buy"]
    assert buys["reason"].str.contains("no model yet").any()
    assert buys["reason"].str.contains("ridge trained on").any()
    assert int((result.positions > 0).sum(axis=1).max()) <= 4
    assert result.exposure.max() <= 1.0 + 1e-9
