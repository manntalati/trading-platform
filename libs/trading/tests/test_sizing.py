"""Position sizing: fixed fractional, volatility targeting, fractional Kelly."""

from collections.abc import Mapping
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_trading.data import Field, MarketData
from tp_trading.engine import BacktestEngine, EngineConfig
from tp_trading.portfolio import Position
from tp_trading.sizing import FixedFractional, FractionalKelly, VolatilityTarget, make
from tp_trading.strategy import Context
from tp_trading.testing import Scripted

DAYS = pd.DatetimeIndex(sessions(date(2023, 1, 3), date(2024, 6, 28)))


def prices(seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(DAYS)
    calm = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.005, n)))  # ~8% vol, drifting up
    wild = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.03, n)))  # ~48% vol
    loser = 100 * np.exp(np.cumsum(rng.normal(-0.002, 0.01, n)))  # falling
    return pd.DataFrame({"CALM": calm, "WILD": wild, "DOWN": loser}, index=DAYS)


class FakeContext(Context):
    def __init__(self, closes: pd.DataFrame) -> None:
        super().__init__("test")
        self._closes = closes

    @property
    def now(self) -> date:
        return self._closes.index[-1].date()

    def history(self, field: Field = "close", lookback: int | None = None) -> pd.DataFrame:
        frame = {
            "high": self._closes * 1.0 + self._closes.diff().abs().fillna(0) / 2,
            "low": self._closes - self._closes.diff().abs().fillna(0) / 2,
        }.get(field, self._closes)
        return frame.iloc[-lookback:] if lookback else frame

    def price(self, symbol: str) -> float:
        return float(self._closes[symbol].iloc[-1])

    @property
    def positions(self) -> Mapping[str, Position]:
        return {}

    @property
    def equity(self) -> float:
        return 100_000.0

    @property
    def cash(self) -> float:
        return 100_000.0


def test_make() -> None:
    assert make(None) is None
    assert make("strategy") is None
    assert isinstance(make("vol-target", target=0.1), VolatilityTarget)
    with pytest.raises(ValueError, match="unknown sizing"):
        make("martingale")


def test_fixed_fractional_risks_the_same_on_each_name() -> None:
    ctx = FakeContext(prices())
    out = FixedFractional(risk=0.01).resize({"CALM": 0.4, "WILD": 0.4}, ctx)
    assert out["WILD"] < out["CALM"] <= 0.4  # the wide-ranging name gets less
    assert sum(out.values()) <= 0.8 + 1e-12  # never more than the strategy's total


def book_vol(weights: dict[str, float]) -> float:
    rets = prices().pct_change().iloc[-60:]
    names = list(weights)
    w = np.array([weights[n] for n in names])
    return float(np.sqrt(w @ (rets[names].cov().to_numpy() * 252) @ w))


def test_volatility_target_equalizes_risk_and_caps_the_book() -> None:
    ctx = FakeContext(prices())
    out = VolatilityTarget(target=0.10).resize({"CALM": 0.5, "WILD": 0.5}, ctx)
    assert out["WILD"] < out["CALM"] <= 0.5
    assert book_vol(out) <= 0.10 + 1e-9
    tight = VolatilityTarget(target=0.03).resize({"CALM": 0.5, "WILD": 0.5}, ctx)
    assert book_vol(tight) == pytest.approx(0.03, rel=1e-6)  # scaled down to the target
    loose = VolatilityTarget(target=5.0).resize({"CALM": 0.5, "WILD": 0.5}, ctx)
    assert sum(loose.values()) <= 1.0 + 1e-12  # never levered up


def test_kelly_bets_nothing_on_a_negative_edge() -> None:
    ctx = FakeContext(prices())
    out = FractionalKelly(fraction=0.25).resize({"CALM": 0.3, "DOWN": 0.3}, ctx)
    assert out["DOWN"] == 0.0
    assert 0 < out["CALM"] <= 0.3


def test_the_engine_sizes_through_the_policy() -> None:
    closes = prices()
    frames = dict.fromkeys(("open", "high", "low", "close"), closes) | {"volume": closes * 0 + 1e6}
    data = MarketData(frames)  # type: ignore[arg-type]

    def act(ctx: Context) -> None:
        if ctx.now == DAYS[300].date():
            ctx.order_target_weights({"CALM": 0.5, "WILD": 0.5})

    strategy = Scripted(universe=("CALM", "WILD"), act=act)
    plain = BacktestEngine(strategy, data, EngineConfig()).run()
    sized = BacktestEngine(strategy, data, EngineConfig(sizing=VolatilityTarget(target=0.05))).run()
    assert sized.exposure.iloc[310] < plain.exposure.iloc[310]
    assert sized.weights["WILD"].iloc[310] < sized.weights["CALM"].iloc[310]
