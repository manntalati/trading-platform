from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_ml.meta import MetaModel, bet_size
from tp_strategies.library import REGISTRY, build, rsi2_meta
from tp_strategies.library.rsi2_meta import Rsi2Meta, retrain_date
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, BacktestResult

FUNDS = ("SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "IEF", "TLT")
DAYS = pd.DatetimeIndex(sessions(date(2021, 1, 4), date(2023, 12, 29)))


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    rsi2_meta._MODELS.clear()
    yield
    rsi2_meta._MODELS.clear()


def market(seed: int = 11) -> MarketData:
    rng = np.random.default_rng(seed)
    steps = 0.0006 + 0.012 * rng.standard_normal((len(DAYS), len(FUNDS)))
    closes = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=FUNDS)
    return MarketData.from_closes(closes)


def run(strategy: Any, data: MarketData | None = None) -> BacktestResult:
    return BacktestEngine(strategy, data or market()).run()


class ByName:
    """A stand-in classifier: a fixed probability per symbol (features indexed by symbol)."""

    def __init__(self, probabilities: dict[str, float]) -> None:
        self.probabilities = probabilities

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        p = np.array([self.probabilities.get(str(s), 0.0) for s in x.index])
        return np.column_stack([1 - p, p])


def fake_model(probabilities: dict[str, float]) -> MetaModel:
    return MetaModel(
        estimator=ByName(probabilities), columns=("ret_5", "rsi_2"), events=321,
        base_rate=0.6, trained_to=pd.Timestamp("2021-12-31"),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("now", "months", "expected"),
    [
        ("2024-02-15", 3, "2024-01-01"),
        ("2024-04-01", 3, "2024-04-01"),
        ("2024-12-31", 1, "2024-12-01"),
        ("2024-09-10", 6, "2024-07-01"),
        ("2024-06-30", 12, "2024-01-01"),
    ],
)
def test_retrain_dates(now: str, months: int, expected: str) -> None:
    assert retrain_date(pd.Timestamp(now), months) == pd.Timestamp(expected)


def test_registered_and_buildable() -> None:
    assert REGISTRY["rsi2-meta"] is Rsi2Meta
    strategy = build("rsi2-meta", {"threshold": "0.6", "model": "logistic", "sized": "true"})
    assert isinstance(strategy, Rsi2Meta)
    assert (strategy.threshold, strategy.model, strategy.sized) == (0.6, "logistic", True)
    assert strategy.config().label == "trade"


def test_without_a_model_it_trades_exactly_like_rsi2() -> None:
    params = {"universe": ",".join(FUNDS), "slots": "4"}
    plain = run(build("rsi2", params))
    meta = run(build("rsi2-meta", {**params, "min_events": "1000000"}))
    columns = ["session", "symbol", "side", "quantity"]
    pd.testing.assert_frame_equal(plain.orders[columns], meta.orders[columns])
    buys = meta.orders[meta.orders["side"] == "buy"]
    assert len(buys) > 20
    assert buys["reason"].str.contains("no model yet").all()


def model_buys(result: BacktestResult) -> pd.DataFrame:
    """Buys made with a model (before the first retrain date there is none: plain RSI(2))."""
    buys = result.orders[result.orders["side"] == "buy"]
    warm_up = buys["reason"].str.contains("no model yet")
    if warm_up.any():
        assert buys[warm_up]["session"].max() < buys[~warm_up]["session"].min()
    return buys[~warm_up]


def test_the_model_decides_which_signals_to_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    good = {"SPY": 0.9, "QQQ": 0.8, "IWM": 0.7, "DIA": 0.65}  # the rest score 0
    monkeypatch.setattr(rsi2_meta, "train", lambda *a, **k: fake_model(good))
    buys = model_buys(run(Rsi2Meta(universe=FUNDS, slots=4, threshold=0.6)))
    assert len(buys) > 10
    assert set(buys["symbol"]) <= set(good)
    assert buys["reason"].str.contains("the model gives it").all()
    assert buys["reason"].str.contains("trained on 321 signals to 2021-12-31").all()
    assert (buys["target_weight"] == 0.25).all()


def test_sized_positions_scale_with_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    probabilities = dict.fromkeys(FUNDS, 0.7)
    monkeypatch.setattr(rsi2_meta, "train", lambda *a, **k: fake_model(probabilities))
    buys = model_buys(run(Rsi2Meta(universe=FUNDS, slots=4, threshold=0.6, sized=True)))
    assert len(buys) > 10
    assert buys["target_weight"].to_numpy() == pytest.approx(float(bet_size(0.7)) / 4)


def test_models_see_only_the_bars_before_each_retrain_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[pd.Timestamp] = []

    def recording_train(past: dict[str, pd.DataFrame], *args: Any, **kwargs: Any) -> MetaModel:
        assert {"open", "high", "low", "close", "volume"} <= set(past)
        seen.append(pd.Timestamp(past["close"].index.max()))
        return fake_model(dict.fromkeys(FUNDS, 0.9))

    monkeypatch.setattr(rsi2_meta, "train", recording_train)
    run(Rsi2Meta(universe=FUNDS, slots=4, retrain=3))
    assert len(seen) == len(set(seen)) >= 8  # trained once per quarter, cached in between
    for last in seen:
        following = DAYS[DAYS.searchsorted(last, side="right")]
        assert following.month in (1, 4, 7, 10)  # the next session starts a new quarter
        assert following.month != last.month


def test_trains_a_real_model_and_runs_through_the_engine() -> None:
    strategy = Rsi2Meta(universe=FUNDS, slots=4, model="logistic", min_events=40, retrain=6,
                        threshold=0.5)  # fmt: skip
    result = run(strategy, market(seed=5))
    buys = result.orders[result.orders["side"] == "buy"]
    assert buys["reason"].str.contains("no model yet").any()
    assert buys["reason"].str.contains("the model gives it").any()
    assert result.exposure.max() <= 1.0 + 1e-9
    assert int((result.positions > 0).sum(axis=1).max()) <= 4
