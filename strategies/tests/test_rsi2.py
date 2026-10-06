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
from tp_strategies.library.rsi2 import rsi2_decisions, wilder_rsi
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig
from tp_trading.portfolio import Position

REPO = Path(__file__).resolve().parents[2]
DAYS = pd.DatetimeIndex(sessions(date(2023, 1, 3), date(2024, 6, 28)))
PARAMS = {"period": 2, "entry": 10.0, "trend": 200, "exit_ma": 5, "slots": 3, "max_hold": 10,
          "max_per_sector": 2}  # fmt: skip


def test_wilder_rsi_by_hand() -> None:
    closes = pd.DataFrame({"A": [1.0, 2.0, 1.0, 2.0, 1.0], "UP": [1.0, 2.0, 3.0, 4.0, 5.0]})
    rsi = wilder_rsi(closes, 2)
    # gains 1,0,1,0 and losses 0,1,0,1 smoothed with alpha 1/2: 0.75/0.25 then 0.375/0.625
    assert rsi["A"].iloc[3] == pytest.approx(75.0)
    assert rsi["A"].iloc[4] == pytest.approx(37.5)
    assert rsi["UP"].iloc[-1] == 100.0
    assert rsi["A"].iloc[:2].isna().all()


def uptrend_then(drop: list[float], n: int = 230, start: float = 100.0) -> list[float]:
    """A steady uptrend followed by the given closes."""
    return [start * 1.002**i for i in range(n)] + drop


def frame(**columns: list[float]) -> pd.DataFrame:
    length = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=DAYS[:length])


def no_sector(symbol: str) -> str | None:
    return None


def test_buys_sharp_dips_in_uptrends_only() -> None:
    top = 100 * 1.002**229
    closes = frame(
        DIP=uptrend_then([top * 0.97, top * 0.94]),  # two hard down days, still above the 200-day
        CRASH=uptrend_then([top * 0.6, top * 0.5]),  # oversold but now below the 200-day average
        CALM=uptrend_then([top * 1.001, top * 1.002]),
    )
    decision = rsi2_decisions(closes, {}, sector=no_sector, **PARAMS)  # type: ignore[arg-type]
    assert [s for s, _ in decision.buys] == ["DIP"]
    assert "RSI(2)" in decision.buys[0][1]
    assert "above its 200-day average" in decision.buys[0][1]
    assert decision.sells == {}


def test_exits_on_the_bounce_or_the_time_stop() -> None:
    top = 100 * 1.002**229
    closes = frame(
        BOUNCED=uptrend_then([top * 0.95, top * 0.93, top * 1.01]),
        STUCK=uptrend_then([top * 0.95, top * 0.94, top * 0.93]),
        FRESH=uptrend_then([top * 0.95, top * 0.94, top * 0.93]),
    )
    opened_long_ago = closes.index[-12].date()
    opened_today = closes.index[-1].date()
    positions = {
        "BOUNCED": Position("BOUNCED", 10, 95.0, opened_today),
        "STUCK": Position("STUCK", 10, 95.0, opened_long_ago),
        "FRESH": Position("FRESH", 10, 95.0, opened_today),
    }
    decision = rsi2_decisions(closes, positions, sector=no_sector, **PARAMS)  # type: ignore[arg-type]
    assert set(decision.sells) == {"BOUNCED", "STUCK"}
    assert "take the bounce" in decision.sells["BOUNCED"]
    assert "time stop" in decision.sells["STUCK"]
    assert "held 11 sessions" in decision.sells["STUCK"]


def test_fills_free_slots_most_oversold_first_within_sector_limits() -> None:
    top = 100 * 1.002**229
    closes = frame(
        HELD=uptrend_then([top * 0.99, top * 0.98]),
        T1=uptrend_then([top * 0.96, top * 0.92]),
        T2=uptrend_then([top * 0.96, top * 0.91]),
        T3=uptrend_then([top * 0.96, top * 0.90]),
        F1=uptrend_then([top * 0.97, top * 0.95]),
    )
    sectors = {"HELD": "Tech", "T1": "Tech", "T2": "Tech", "T3": "Tech", "F1": "Fin"}
    positions = {"HELD": Position("HELD", 1, 100.0, closes.index[-1].date())}
    decision = rsi2_decisions(closes, positions, sector=sectors.get, **PARAMS)  # type: ignore[arg-type]
    # 3 slots, 1 held: two free. T3 is the most oversold; Tech then has 2 (HELD + T3), so F1.
    assert [s for s, _ in decision.buys] == ["T3", "F1"]


def test_rsi2_through_the_engine_trades_often_and_stays_within_its_slots() -> None:
    # Funds only: at 4 slots each position is 25%, fine for ETFs, over the 10% cap for stocks.
    strategy = build("rsi2", {"universe": "SPY,QQQ,IWM,DIA,EFA,EEM,IEF,TLT", "slots": "4"})
    rng = np.random.default_rng(11)
    days = pd.DatetimeIndex(sessions(date(2021, 1, 4), date(2023, 12, 29)))
    steps = 0.0006 + 0.012 * rng.standard_normal((len(days), 8))
    closes = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=days,
                          columns=strategy.symbols())  # fmt: skip
    classifier = Classifier.load(REPO / "config" / "classifications.toml")
    result = BacktestEngine(
        strategy, MarketData.from_closes(closes), EngineConfig(),
        RiskManager(Limits(), classifier), sectors=classifier.sectors,
    ).run()  # fmt: skip
    assert len(result.fills) > 40
    assert int((result.positions > 0).sum(axis=1).max()) <= 4
    assert set(result.orders["status"]) <= {"filled", "partial", "pending"}
    assert result.exposure.max() <= 1.0 + 1e-9
