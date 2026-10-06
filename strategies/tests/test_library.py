from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import is_last_session_of_month, sessions
from tp_strategies.library import REGISTRY, build, parameters
from tp_strategies.library.ma_timing import GTAA, MaTiming
from tp_strategies.ma_timing import ma_timing
from tp_trading.costs import CostModel
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig
from tp_trading.execution import ExecutionConfig

DAYS = pd.DatetimeIndex(sessions(date(2015, 1, 2), date(2021, 12, 31)))
RESEARCH = ExecutionConfig(
    fill_at="next_close", size_at="fill", fractional=True, costs=CostModel.free()
)


def prices(columns: tuple[str, ...], seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0002, 0.01, (len(DAYS), len(columns)))
    return pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=list(columns))


@pytest.mark.parametrize("assets", [("SPY",), GTAA])
def test_event_driven_ma_timing_reproduces_the_vectorised_backtest(assets: tuple[str, ...]) -> None:
    px = prices(assets)
    reference = ma_timing(px, lag_days=1, cost_bps=0.0)
    result = BacktestEngine(
        MaTiming(assets=assets),
        MarketData.from_closes(px),
        EngineConfig(initial_cash=1e6, execution=RESEARCH),
    ).run()
    engine = result.returns.loc[reference.returns.index]
    np.testing.assert_allclose(engine.to_numpy(), reference.returns.to_numpy(), atol=1e-12)
    assert len(result.fills) == len(reference.trades)
    assert (result.returns.loc[: reference.returns.index[0]].iloc[:-1] == 0).all()


def test_ma_timing_trades_only_on_month_ends_and_explains_each_order() -> None:
    px = prices(GTAA, seed=5)
    result = BacktestEngine(MaTiming(), MarketData.from_closes(px), EngineConfig()).run()
    signal_days = pd.to_datetime(result.orders["session"]).dt.date
    assert all(is_last_session_of_month(d) for d in signal_days)
    assert signal_days.min() >= DAYS[0].date() + pd.Timedelta(days=270)  # 10 month ends first
    first = result.orders.iloc[0]
    assert "10-month average" in first["reason"]
    assert first["reason"].startswith(first["symbol"])


def test_build_converts_string_parameters_to_field_types() -> None:
    s = build("ma-timing", {"assets": "spy, efa", "months": "12", "min-change": "0.02"})
    assert s == MaTiming(assets=("SPY", "EFA"), months=12, min_change=0.02)
    assert build("ma-timing", {"assets": ["SPY"]}) == MaTiming(assets=("SPY",))
    assert parameters(MaTiming)["months"] == 10
    assert "ma-timing" in REGISTRY


@pytest.mark.parametrize(
    ("name", "params", "message"),
    [
        ("nope", {}, "unknown strategy"),
        ("ma-timing", {"window": "3"}, "has no parameter"),
        ("ma-timing", {"months": "ten"}, "expected int"),
    ],
)
def test_build_rejects_bad_input(name: str, params: dict[str, str], message: str) -> None:
    with pytest.raises((KeyError, ValueError), match=message):
        build(name, params)
