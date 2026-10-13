"""Factor portfolios on synthetic prices and a stub reference (made-up symbols)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import is_last_session_of_month, sessions
from tp_strategies.library.factors import FactorPortfolio, factor_scores, pick
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig

DAYS = pd.DatetimeIndex(sessions(date(2019, 1, 2), date(2021, 12, 31)))
NAMES = [f"S{i:02d}" for i in range(12)]


def prices() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    vols = np.linspace(0.005, 0.03, len(NAMES))  # S00 the calmest
    steps = rng.normal(0.0003, 1.0, (len(DAYS), len(NAMES))) * vols
    return pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS, columns=NAMES)


def valuation() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "earnings_yield": np.linspace(0.12, 0.01, len(NAMES)),  # S00 the cheapest
            "book_to_market": np.linspace(0.9, 0.1, len(NAMES)),
            "gross_profitability": [np.nan] * 2 + list(np.linspace(0.1, 0.5, len(NAMES) - 2)),
            "roe": np.linspace(0.05, 0.3, len(NAMES)),
        },
        index=NAMES,
    )


class StubReference:
    def __init__(self, table: pd.DataFrame) -> None:
        self.table = table
        self.calls: list[tuple[str, date]] = []

    def get(self, name: str, as_of: date) -> pd.DataFrame:
        self.calls.append((name, as_of))
        return self.table if as_of >= date(2020, 1, 1) else self.table.iloc[0:0]


def test_scores() -> None:
    px = prices()
    low = factor_scores("low-vol", px, None)
    assert list(low.index[:3]) == ["S00", "S01", "S02"]
    value = factor_scores("value", px, valuation())
    assert value.index[0] == "S00"
    quality = factor_scores("quality", px, valuation())
    assert quality.index[0] == "S11"  # the most profitable
    assert "S00" in quality.index  # ranked on return on equity: no gross profit reported
    with pytest.raises(LookupError):
        factor_scores("value", px, None)
    with pytest.raises(ValueError, match="unknown factor"):
        factor_scores("momentum", px, None)
    sectors = dict.fromkeys(NAMES[:5], "Same")
    assert pick(low, 4, 2, sectors.get) == ["S00", "S01", "S05", "S06"]


def test_monthly_portfolio_uses_fundamentals_only_once_known() -> None:
    ref = StubReference(valuation())
    result = BacktestEngine(
        FactorPortfolio(universe=tuple(NAMES), factor="value", top=5),
        MarketData.from_closes(prices()),
        EngineConfig(initial_cash=100_000),
        reference=ref,
    ).run()
    first = pd.to_datetime(result.orders["session"]).min()
    assert first >= pd.Timestamp("2020-01-01")  # nothing to rank before the first filings
    held = result.positions.iloc[-1]
    assert set(held[held > 0].index) == {f"S0{i}" for i in range(5)}
    assert {name for name, _ in ref.calls} == {"valuation"}
    assert all(is_last_session_of_month(d) for _, d in ref.calls)  # read on rebalance days only
