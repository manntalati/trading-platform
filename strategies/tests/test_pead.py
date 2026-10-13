"""Post-earnings drift on a stub earnings table (made-up symbols)."""

from datetime import date

import numpy as np
import pandas as pd

from tp_core.calendar import sessions
from tp_strategies.library.pead import EarningsDrift
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, EngineConfig

DAYS = pd.DatetimeIndex(sessions(date(2022, 1, 3), date(2022, 12, 30)))
NAMES = ("AAA", "BBB", "CCC")


class Earnings:
    def __init__(self, rows: list[tuple[str, date, float]]) -> None:
        self.table = pd.DataFrame(
            [
                {"symbol": s, "end": d, "known": d, "eps": 1.0, "change": 0.1, "sue": sue,
                 "session": d}
                for s, d, sue in rows
            ]
        )  # fmt: skip

    def get(self, name: str, as_of: date) -> pd.DataFrame:
        assert name == "earnings"
        out: pd.DataFrame = self.table[self.table["session"] <= as_of]
        return out


def run(rows: list[tuple[str, date, float]], **params: object) -> pd.DataFrame:
    px = pd.DataFrame(100.0 * np.ones((len(DAYS), len(NAMES))), index=DAYS, columns=list(NAMES))
    result = BacktestEngine(
        EarningsDrift(universe=NAMES, **params),  # type: ignore[arg-type]
        MarketData.from_closes(px),
        EngineConfig(initial_cash=100_000),
        reference=Earnings(rows),
    ).run()
    orders: pd.DataFrame = result.orders
    return orders


def test_buys_big_surprises_and_holds_for_the_window() -> None:
    orders = run(
        [
            ("AAA", date(2022, 2, 1), 3.5),
            ("BBB", date(2022, 2, 1), 1.0),
            ("CCC", date(2022, 2, 1), 2.4),
        ],
        hold=20,
    )
    buys = orders[orders["side"] == "buy"]
    assert list(buys["symbol"]) == ["AAA", "CCC"]  # the largest surprise first; BBB too small
    assert (pd.to_datetime(buys["session"]) == pd.Timestamp("2022-02-01")).all()
    assert "SUE +3.5" in buys["reason"].iloc[0]
    sells = orders[orders["side"] == "sell"]
    assert set(sells["symbol"]) == {"AAA", "CCC"}
    assert "drift window is over" in sells["reason"].iloc[0]
    held = (pd.to_datetime(sells["session"]).iloc[0] - pd.Timestamp("2022-02-01")).days
    assert 25 <= held <= 32  # 20 sessions


def test_a_negative_surprise_ends_it_early_and_slots_limit_entries() -> None:
    orders = run(
        [("AAA", date(2022, 2, 1), 3.0), ("AAA", date(2022, 3, 1), -1.0),
         ("BBB", date(2022, 2, 1), 2.5), ("CCC", date(2022, 2, 1), 2.2)],
        slots=2,
    )  # fmt: skip
    buys = orders[orders["side"] == "buy"]
    assert list(buys["symbol"]) == ["AAA", "BBB"]  # two slots
    early = orders[(orders["side"] == "sell") & (orders["symbol"] == "AAA")]
    assert pd.to_datetime(early["session"]).iloc[0] == pd.Timestamp("2022-03-01")
    assert "SUE -1.0" in early["reason"].iloc[0]
