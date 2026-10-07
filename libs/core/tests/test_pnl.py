"""Realized P&L by average cost, on made-up transactions."""

from datetime import date
from typing import Any

import pandas as pd
import pytest

from tp_core.pnl import Fill, Position, broker_trades, fill_trades, option_label, summarize

CALL = "XYZ260116C00050000"  # XYZ 16 Jan 2026 $50 call
PUT = "XYZ260116P00040000"


def act(day: str, kind: str, symbol: str | None, units: float, price: float, amount: float,
        *, option_action: str = "", description: str = "", fee: float = 0.0,
        account: str = "acct-1") -> dict[str, Any]:  # fmt: skip
    return {
        "activity_id": f"{day}-{kind}-{symbol}-{units}-{amount}",
        "source": "fake",
        "account_id": account,
        "type": kind,
        "symbol": symbol,
        "trade_date": date.fromisoformat(day),
        "settlement_date": date.fromisoformat(day),
        "units": units,
        "price": price,
        "amount": amount,
        "fee": fee,
        "currency": "USD",
        "description": description,
        "option_action": option_action,
    }


def frame(*rows: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def realized(trades: pd.DataFrame) -> list[float | None]:
    return [None if v != v else round(v, 2) for v in trades["realized_pnl"]]


def test_position_long_short_and_through_zero() -> None:
    long = Position()
    assert long.trade(10, -100.0) is None
    assert long.trade(10, -200.0) is None  # average cost 15
    assert long.trade(-5, 150.0) == pytest.approx(75.0)
    assert long.quantity == 15
    assert long.cost == pytest.approx(225.0)  # still 15 a share

    short = Position()
    short.trade(-1, 120.0)  # sold to open for a 120 credit
    assert short.trade(1, -40.0) == pytest.approx(80.0)
    assert short.quantity == 0

    flip = Position()
    flip.trade(2, -20.0)
    assert flip.trade(-3, 45.0) == pytest.approx(30.0 - 20.0)  # 2 closed at 15 each
    assert flip.quantity == -1
    assert flip.cost == pytest.approx(-15.0)  # the third share opened a short


def test_stock_trades_realize_against_the_average_cost() -> None:
    trades = broker_trades(
        frame(
            act("2026-01-05", "BUY", "ABC", 10, 10.0, -100.0),
            act("2026-02-05", "BUY", "ABC", 10, 20.0, -200.0),
            act("2026-03-05", "SELL", "ABC", -5, 30.0, 150.0),
            act("2026-04-05", "SELL", "ABC", 15, 10.0, 150.0),  # sold units reported positive
        )
    )
    assert realized(trades) == [None, None, 75.0, -75.0]
    assert list(trades["action"]) == ["Buy", "Buy", "Sell", "Sell"]
    assert list(trades["quantity"]) == [10, 10, 5, 15]


def test_options_closed_expired_and_sold_to_open() -> None:
    trades = broker_trades(
        frame(
            act("2026-01-05", "BUY", CALL, 1, 3.0, -300.65, option_action="BUY_TO_OPEN"),
            act("2026-01-09", "SELL", CALL, -1, 4.5, 449.33, option_action="SELL_TO_CLOSE"),
            act("2026-01-05", "BUY", PUT, 2, 1.25, -251.3, option_action="BUY_TO_OPEN"),
            act("2026-01-20", "OPTIONEXPIRATION", PUT, -2, 0.0, 0.0),
        )
    )
    by_date = trades.set_index(["trade_date", "symbol"])["realized_pnl"]
    assert by_date[(date(2026, 1, 9), CALL)] == pytest.approx(449.33 - 300.65)
    assert by_date[(date(2026, 1, 20), PUT)] == pytest.approx(-251.3)  # the whole premium
    expired = trades[trades["type"] == "OPTIONEXPIRATION"].iloc[0]
    assert expired["action"] == "Expired"
    assert expired["label"] == "XYZ Jan 16 '26 $40 Put"
    assert set(trades["kind"]) == {"option"}

    short = broker_trades(
        frame(
            act("2026-01-05", "SELL", CALL, -1, 1.2, 119.35, option_action="SELL_TO_OPEN"),
            act("2026-01-16", "OPTIONEXPIRATION", CALL, 1, 0.0, 0.0),
        )
    )
    assert realized(short) == [None, 119.35]  # kept the whole credit
    assert list(short["action"]) == ["Sell to open", "Expired"]


def test_description_says_opening_or_closing_when_the_action_is_missing() -> None:
    opening, closing = "YOU SOLD OPENING TRANSACTION", "YOU BOUGHT CLOSING TRANSACTION"
    trades = broker_trades(
        frame(
            act("2026-01-05", "SELL", CALL, -1, 1.2, 119.0, description=opening),
            act("2026-01-07", "BUY", CALL, 1, 0.5, -50.0, description=closing),
        )
    )
    assert realized(trades) == [None, 69.0]
    assert list(trades["action"]) == ["Sell to open", "Buy to close"]


def test_sales_from_before_the_synced_history_have_no_realized_figure() -> None:
    trades = broker_trades(
        frame(
            act("2026-01-05", "SELL", "OLD", -3, 50.0, 150.0),
            act("2026-02-05", "BUY", "OLD", 2, 40.0, -80.0),
            act("2026-03-05", "SELL", "OLD", -4, 45.0, 180.0),  # 2 known, 2 from before
            act("2026-03-06", "OPTIONEXPIRATION", CALL, -1, 0.0, 0.0),
        )
    )
    assert realized(trades) == [None, None, 10.0, None]  # 90 for 2 known shares bought at 80
    assert list(trades["cost_known"]) == [False, True, False, False]


def test_same_day_round_trip_and_cash_sweep() -> None:
    trades = broker_trades(
        frame(
            act("2026-01-05", "SELL", "ABC", -1, 12.0, 12.0),  # listed first, happened second
            act("2026-01-05", "BUY", "ABC", 1, 10.0, -10.0),
            act("2026-01-05", "BUY", "MMF", 500, 1.0, -500.0),
        ),
        is_cash=lambda s: s == "MMF",
    )
    assert list(trades["symbol"]) == ["ABC", "ABC"]
    assert realized(trades) == [None, 2.0]


def test_accounts_are_kept_apart() -> None:
    trades = broker_trades(
        frame(
            act("2026-01-05", "BUY", "ABC", 1, 10.0, -10.0, account="a"),
            act("2026-01-06", "SELL", "ABC", -1, 12.0, 12.0, account="b"),
        )
    )
    assert realized(trades) == [None, None]


def test_summary_of_realized_income_fees_and_deposits() -> None:
    acts = frame(
        act("2025-06-02", "CONTRIBUTION", None, 0, 0, 1000.0),
        act("2025-07-01", "BUY", "ABC", 10, 10.0, -100.0),
        act("2025-12-01", "SELL", "ABC", -5, 14.0, 70.0),
        act("2026-02-02", "SELL", "ABC", -5, 8.0, 40.0),
        act("2026-03-02", "DIVIDEND", "ABC", 0, 0, 3.5),
        act("2026-03-03", "INTEREST", None, 0, 0, 1.25),
        act("2026-03-04", "FEE", None, 0, 0, -2.0),
        act("2026-04-01", "WITHDRAWAL", None, 0, 0, -300.0),
    )
    trades = broker_trades(acts)
    s = summarize(trades, acts, date(2026, 5, 1))
    assert s.realized == pytest.approx(20.0 - 10.0)
    assert s.realized_ytd == pytest.approx(-10.0)
    assert (s.closed_trades, s.winners, s.win_rate) == (2, 1, 0.5)
    assert s.income == pytest.approx(4.75)
    assert s.fees == pytest.approx(2.0)
    assert s.net_deposits == pytest.approx(700.0)
    assert s.since == date(2025, 6, 2)
    assert s.to_dict()["by_symbol"] == {"ABC": pytest.approx(10.0)}


def test_paper_fills_realize_per_book_and_symbol() -> None:
    day = date(2026, 1, 5)
    fills = [
        Fill("a", "SPY", "buy", 10, 100.0, 0.0, day, "1"),
        Fill("b", "SPY", "buy", 5, 90.0, 0.0, day, "2"),
        Fill("a", "SPY", "sell", 4, 110.0, 0.0, date(2026, 1, 6), "3"),
    ]
    out = {r["fill"].ref: r["realized_pnl"] for r in fill_trades(fills)}
    assert out == {"1": None, "2": None, "3": pytest.approx(40.0)}


def test_option_labels() -> None:
    assert option_label(CALL) == "XYZ Jan 16 '26 $50 Call"
    assert option_label("XYZ260116P00002500") == "XYZ Jan 16 '26 $2.5 Put"
    assert option_label("ABC") is None
