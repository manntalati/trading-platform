"""Strategy 6: daily leveraged momentum rotation."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_core.portfolio import Classifier
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_strategies.library import build
from tp_strategies.library.leveraged_momentum import LEVERAGED, leveraged_momentum_targets
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig

REPO = Path(__file__).resolve().parents[2]
DAYS = pd.DatetimeIndex(sessions(date(2022, 1, 3), date(2023, 12, 29)))


def leveraged_walks(seed: int = 3) -> pd.DataFrame:
    """Correlated random walks at 3x-fund volatility (about 60% a year)."""
    rng = np.random.default_rng(seed)
    market = rng.standard_normal(len(DAYS))
    out = {}
    for i, symbol in enumerate(LEVERAGED):
        own = rng.standard_normal(len(DAYS))
        shock = 0.7 * market + np.sqrt(1 - 0.7**2) * own
        steps = 0.10 / 252 + 0.60 / np.sqrt(252) * shock
        out[symbol] = 50 * np.exp(np.cumsum(steps)) * (1 + i / 10)
    return pd.DataFrame(out, index=DAYS)


def lines(slopes: dict[str, float], days: int = 30) -> pd.DataFrame:
    index = pd.RangeIndex(days)
    return pd.DataFrame({s: 100 * (1 + k) ** np.arange(days) for s, k in slopes.items()}, index)


def test_holds_the_two_strongest_risers_and_nothing_falling() -> None:
    closes = lines({"A": 0.010, "B": 0.020, "C": 0.005, "D": -0.010})
    targets, why = leveraged_momentum_targets(
        closes, ("A", "B", "C", "D"), lookback=10, trend=20, top_n=2
    )
    assert targets == {"A": 0.5, "B": 0.5, "C": 0.0, "D": 0.0}
    assert why["B"].endswith("rank 1, held at 50%")
    assert why["C"].endswith("rank 3, outside the top 2")
    assert why["D"].endswith(": out")
    assert "10-day return" in why["A"]
    assert "20-day average" in why["A"]


def test_cash_when_too_few_qualify() -> None:
    closes = lines({"A": 0.010, "B": -0.020, "C": -0.005})
    targets, _ = leveraged_momentum_targets(closes, ("A", "B", "C"), lookback=10, trend=20, top_n=2)
    assert targets == {"A": 0.5, "B": 0.0, "C": 0.0}  # half the sleeve stays in cash
    falling, _ = leveraged_momentum_targets(closes, ("B", "C"), lookback=10, trend=20, top_n=2)
    assert sum(falling.values()) == 0.0


def test_short_history_waits() -> None:
    closes = lines({"A": 0.01, "B": 0.02}, days=15)
    targets, why = leveraged_momentum_targets(closes, ("A", "B"), lookback=10, trend=20, top_n=2)
    assert sum(targets.values()) == 0.0
    assert "not enough history" in why["A"]


def test_trades_most_days_within_the_risk_limits() -> None:
    strategy = build("leveraged-momentum")
    closes = leveraged_walks()
    classifier = Classifier.load(REPO / "config" / "classifications.toml")
    assert set(strategy.symbols()) <= set(classifier.funds)  # funds: no single-name caps
    limits = Limits.load(REPO / "config" / "risk.toml")
    assert limits.drawdown_limit(strategy.name) == 0.50  # its own, wider drawdown limit
    risk = RiskManager(limits, classifier)
    result = BacktestEngine(
        strategy, MarketData.from_closes(closes), EngineConfig(), risk, sectors=classifier.sectors
    ).run()
    traded_days = pd.to_datetime(result.orders["session"]).dt.date.nunique()
    active_days = len(DAYS) - strategy.trend - 1
    assert traded_days / active_days > 0.6  # a daily strategy, not a monthly one
    rejected = result.orders[result.orders["status"] == "rejected"]
    # In a backtest the daily loss limit applies to the sleeve itself, so it blocks buys after a
    # bad day; nothing else ever rejects it (in paper trading the limit is the whole account's).
    assert set(rejected["note"].str.split(":").str[0]) <= {"daily_loss"}
    assert len(rejected) < 0.15 * len(result.orders)
    assert result.exposure.max() <= 1.0 + 1e-9  # leverage comes from the funds, not borrowing
    assert result.positions.gt(0).sum(axis=1).max() <= strategy.top_n


@pytest.mark.parametrize("param", ["top_n=1", "lookback=5", "assets=TQQQ,TMF"])
def test_parameters_from_the_command_line(param: str) -> None:
    key, value = param.split("=")
    strategy = build("leveraged-momentum", {key: value})
    assert strategy.symbols()
