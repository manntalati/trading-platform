from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import is_last_session_of_month, sessions
from tp_core.config import load_universes
from tp_core.portfolio import Classifier
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_strategies.library import REGISTRY, build
from tp_strategies.library.dual_momentum import dual_momentum_pick
from tp_strategies.library.signals import annualized_volatility, trailing_return
from tp_strategies.library.ts_momentum import ts_momentum_targets
from tp_strategies.library.xs_momentum import LARGE_CAPS, xs_momentum_targets
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig

REPO = Path(__file__).resolve().parents[2]
DAYS = pd.DatetimeIndex(sessions(date(2021, 1, 4), date(2023, 12, 29)))


def trends(
    drifts: dict[str, float], vols: dict[str, float] | None = None, seed: int = 0
) -> pd.DataFrame:
    """Geometric random walks with the given annual drift and volatility per column."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, (symbol, mu) in enumerate(drifts.items()):
        sigma = (vols or {}).get(symbol, 0.15)
        steps = mu / 252 + sigma / np.sqrt(252) * rng.standard_normal(len(DAYS))
        out[symbol] = 100 * np.exp(np.cumsum(steps)) * (1 + i / 100)
    return pd.DataFrame(out, index=DAYS)


# -- configuration stays in step ----------------------------------------------------------------


def test_every_strategy_trades_symbols_the_ingest_job_collects_and_risk_can_classify() -> None:
    universe = set(load_universes(REPO / "config" / "universes.toml").bars)
    classifier = Classifier.load(REPO / "config" / "classifications.toml")
    for name, cls in REGISTRY.items():
        symbols = set(cls().symbols())
        assert symbols <= universe, f"{name} needs {sorted(symbols - universe)} in universes.toml"
        unclassified = {
            s for s in symbols if s not in classifier.funds and s not in classifier.sectors
        }
        assert not unclassified, f"{name}: classify {sorted(unclassified)}"


def test_large_caps_match_the_stock_universe() -> None:
    stocks = load_universes(REPO / "config" / "universes.toml").bars[-len(LARGE_CAPS) :]
    assert stocks == LARGE_CAPS


# -- signals ------------------------------------------------------------------------------------


def test_trailing_return_skips_the_most_recent_month() -> None:
    closes = pd.DataFrame({"A": [100.0] * 230 + [150.0] * 22 + [300.0]})
    assert trailing_return(closes, 252).iloc[0] == pytest.approx(2.0)
    assert trailing_return(closes, 252, skip=21).iloc[0] == pytest.approx(0.5)
    assert np.isnan(trailing_return(closes.iloc[:100], 252).iloc[0])
    with pytest.raises(ValueError, match="longer than skip"):
        trailing_return(closes, 21, skip=21)


def test_time_series_momentum_holds_assets_beating_t_bills_weighted_by_inverse_vol() -> None:
    closes = trends(
        {"UP": 0.25, "UPWILD": 0.90, "DOWN": -0.20, "SLOW": 0.02, "BIL": 0.04},
        vols={"UP": 0.10, "UPWILD": 0.30, "DOWN": 0.15, "SLOW": 0.01, "BIL": 0.002},
    )
    window = closes.iloc[:300]
    targets, reasons = ts_momentum_targets(window, ("UP", "UPWILD", "DOWN", "SLOW"), "BIL", 252, 63)
    assert targets["DOWN"] == 0
    assert targets["SLOW"] == 0  # trends up, but not past T-bills
    vol = annualized_volatility(window[["UP", "UPWILD"]], 63)
    assert targets["UP"] / targets["UPWILD"] == pytest.approx(vol["UPWILD"] / vol["UP"])
    assert 0 < sum(targets.values()) < 1  # the flat assets' slices stay in cash
    assert reasons["UP"].startswith("UP 12-month return +")
    assert "vs T-bills (BIL)" in reasons["UP"]
    assert reasons["DOWN"].endswith("flat")


def test_time_series_momentum_waits_for_history() -> None:
    closes = trends({"A": 0.2, "B": 0.1, "BIL": 0.03})
    closes.loc[: DAYS[200], "B"] = np.nan  # B listed later
    targets, reasons = ts_momentum_targets(closes.iloc[:260], ("A", "B"), "BIL", 252, 63)
    assert targets["B"] == 0
    assert "not enough history" in reasons["B"]
    assert targets["A"] == pytest.approx(1.0)


def test_cross_sectional_momentum_ranks_12_1_with_a_sector_cap() -> None:
    drifts = {"T1": 0.9, "T2": 0.8, "T3": 0.7, "F1": 0.5, "E1": 0.3, "E2": -0.2}
    closes = trends(drifts, vols=dict.fromkeys(drifts, 0.01)).iloc[:300]
    closes.loc[closes.index[-5:], "E2"] *= 3  # a last-month spike must not count
    sectors = {
        "T1": "Tech",
        "T2": "Tech",
        "T3": "Tech",
        "F1": "Fin",
        "E1": "Energy",
        "E2": "Energy",
    }
    targets, reasons = xs_momentum_targets(
        closes, lookback=252, skip=21, top_n=4, max_per_sector=2, require_positive=False,
        sector=sectors.get,
    )  # fmt: skip
    assert targets == {"T1": 0.25, "T2": 0.25, "F1": 0.25, "E1": 0.25}
    assert reasons["T3"].endswith("Tech already has 2 names")
    assert "rank 6 of 6" in reasons["E2"]
    positive_only, _ = xs_momentum_targets(
        closes, lookback=252, skip=21, top_n=6, max_per_sector=6, require_positive=True,
        sector=sectors.get,
    )  # fmt: skip
    assert "E2" not in positive_only
    assert sum(positive_only.values()) == pytest.approx(5 / 6)  # an empty slot stays cash


@pytest.mark.parametrize(
    ("drifts", "pick"),
    [
        ({"SPY": 0.30, "EFA": 0.10, "AGG": 0.02, "BIL": 0.03}, "SPY"),
        ({"SPY": 0.20, "EFA": 0.40, "AGG": 0.02, "BIL": 0.03}, "EFA"),
        ({"SPY": -0.20, "EFA": 0.40, "AGG": 0.02, "BIL": 0.03}, "AGG"),
    ],
)
def test_dual_momentum_absolute_then_relative(drifts: dict[str, float], pick: str) -> None:
    closes = trends(drifts, vols=dict.fromkeys(drifts, 0.001)).iloc[:300]
    chosen, why = dual_momentum_pick(
        closes, us="SPY", international="EFA", bonds="AGG", cash="BIL", lookback=252
    )
    assert chosen == pick
    assert "T-bills (BIL)" in why


# -- through the engine ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ts-momentum", "xs-momentum", "dual-momentum"])
def test_strategies_run_through_the_engine_and_risk_on_month_ends_only(name: str) -> None:
    strategy = build(name)
    symbols = strategy.symbols()
    rng = np.random.default_rng(len(name))
    drifts = {s: float(rng.uniform(-0.2, 0.4)) for s in symbols}
    closes = trends(drifts)
    classifier = Classifier.load(REPO / "config" / "classifications.toml")
    risk = RiskManager(Limits(), classifier)
    result = BacktestEngine(
        strategy, MarketData.from_closes(closes), EngineConfig(), risk, sectors=classifier.sectors
    ).run()
    assert len(result.fills) > 0
    sessions_ = pd.to_datetime(result.orders["session"]).dt.date
    assert all(is_last_session_of_month(d) for d in sessions_)
    assert (result.orders["reason"].str.len() > 0).all()
    assert set(result.orders["status"]) <= {"filled", "partial", "pending"}  # nothing rejected
    assert result.exposure.max() <= 1.0 + 1e-9
