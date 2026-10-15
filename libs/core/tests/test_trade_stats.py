"""Round trips and trading statistics from synthetic trades (XYZ, ABC, AAA)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from tp_core.pnl import broker_trades
from tp_core.trade_stats import round_trips, stats_from_activities, trade_stats

D0 = date(2024, 1, 2)
CALL = "XYZ240315C00050000"
PUT = "ABC240315P00040000"


def act(i: int, kind: str, symbol: str, day: int, units: float, amount: float,
        option_action: str | None = None) -> dict[str, object]:  # fmt: skip
    return {
        "source": "fake", "account_id": "a1", "activity_id": f"x{i}", "type": kind,
        "symbol": symbol, "trade_date": D0 + timedelta(days=day), "settlement_date": None,
        "units": units, "price": None, "amount": amount, "fee": 0.0, "currency": "USD",
        "description": "", "option_action": option_action,
    }  # fmt: skip


def frame(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def trips_of(*rows: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    return round_trips(broker_trades(frame(*rows)))


def test_shares_match_first_in_first_out() -> None:
    trips, open_lots, unknown = trips_of(
        act(1, "BUY", "AAA", 0, 10, -1000.0),  # 10 @ 100
        act(2, "BUY", "AAA", 10, 10, -1200.0),  # 10 @ 120
        act(3, "SELL", "AAA", 30, -15, 1650.0),  # 15 @ 110
    )
    assert len(trips) == 2
    first, second = trips.iloc[0], trips.iloc[1]
    assert (first["quantity"], first["days"]) == (10, 30)
    assert first["pnl"] == pytest.approx(1100 - 1000)
    assert first["return_pct"] == pytest.approx(0.10)
    assert (second["quantity"], second["days"]) == (5, 20)
    assert second["pnl"] == pytest.approx(550 - 600)
    assert open_lots["quantity"].tolist() == [5]  # 5 of the second lot still held
    assert unknown == 0


def test_long_calls_sold_or_expired() -> None:
    trips, _, _ = trips_of(
        act(1, "BUY", CALL, 0, 2, -400.0, "BUY_TO_OPEN"),
        act(2, "SELL", CALL, 5, -1, 350.0, "SELL_TO_CLOSE"),
        act(3, "OPTIONEXPIRATION", CALL, 73, -1, 0.0),
    )
    sold, expired = trips.iloc[0], trips.iloc[1]
    assert (sold["kind"], sold["side"], sold["exit"]) == ("call", "long", "sold")
    assert sold["return_pct"] == pytest.approx(350 / 200 - 1)
    assert sold["underlying"] == "XYZ"
    assert (expired["exit"], expired["days"]) == ("expired", 73)
    assert expired["return_pct"] == pytest.approx(-1.0)


def test_options_sold_to_open_keep_the_premium() -> None:
    trips, _, _ = trips_of(
        act(1, "SELL", PUT, 0, -1, 150.0, "SELL_TO_OPEN"),
        act(2, "BUY", PUT, 20, 1, -50.0, "BUY_TO_CLOSE"),
        act(3, "SELL", PUT, 25, -1, 120.0, "SELL_TO_OPEN"),
        act(4, "OPTIONEXPIRATION", PUT, 73, 1, 0.0),
    )
    bought_back, expired = trips.iloc[0], trips.iloc[1]
    assert (bought_back["side"], bought_back["exit"]) == ("short", "bought back")
    assert bought_back["pnl"] == pytest.approx(100.0)
    assert bought_back["return_pct"] == pytest.approx(100 / 150)
    assert (expired["side"], expired["exit"]) == ("short", "expired")
    assert expired["return_pct"] == pytest.approx(1.0)


def test_sales_before_the_history_are_counted_not_guessed() -> None:
    trips, _, unknown = trips_of(act(1, "SELL", "AAA", 0, -5, 500.0))
    assert trips.empty
    assert unknown == 1


def test_stats_by_kind_holding_times_and_observations() -> None:
    rows = []
    i = 0
    # 6 call trades: 2 winners held 3 days, 4 losers held 30 days (two expired)
    for k, (days, cash_back, exit_) in enumerate(
        [(3, 300.0, "sell"), (3, 260.0, "sell"), (30, 50.0, "sell"), (30, 80.0, "sell"),
         (30, 0.0, "expire"), (30, 0.0, "expire")]
    ):  # fmt: skip
        start = 40 * k
        symbol = f"XYZ2412{20 + k}C00050000"
        rows.append(act(i := i + 1, "BUY", symbol, start, 1, -200.0, "BUY_TO_OPEN"))
        if exit_ == "sell":
            rows.append(act(i := i + 1, "SELL", symbol, start + days, -1, cash_back,
                            "SELL_TO_CLOSE"))  # fmt: skip
        else:
            rows.append(act(i := i + 1, "OPTIONEXPIRATION", symbol, start + days, -1, 0.0))
    rows += [act(90, "BUY", "AAA", 0, 10, -1000.0), act(91, "SELL", "AAA", 200, -10, 1300.0)]
    stats = stats_from_activities(frame(*rows), D0 + timedelta(days=400))
    calls, shares = stats["by_kind"]["calls"], stats["by_kind"]["shares"]
    assert calls["trades"] == 6
    assert calls["win_rate"] == pytest.approx(2 / 6)
    assert calls["median_days_winners"] == 3
    assert calls["median_days_losers"] == 30
    assert calls["expired"] == 2
    assert shares["trades"] == 1
    assert shares["median_return"] == pytest.approx(0.30)
    assert stats["by_kind"]["all"]["trades"] == 7
    calls_hold = {b["bucket"]: b["trades"] for b in stats["holding"]["calls"]}
    assert calls_hold["1-7 days"] == 2
    assert calls_hold["1-4 weeks"] == 0
    assert calls_hold["1-3 months"] == 4
    # only winners among the best and only losers among the worst
    assert [t["return_pct"] for t in stats["best"]] == pytest.approx([0.5, 0.3, 0.3])
    assert [t["return_pct"] for t in stats["worst"]] == pytest.approx([-1.0, -1.0, -0.75, -0.6])
    text = " ".join(stats["observations"])
    assert "You hold losing calls 10.0x longer than winning ones" in text
    assert "expired worthless" in text
    assert sum(m["trades"] for m in stats["monthly"]) == 7


def test_empty_history() -> None:
    trips, open_lots, unknown = round_trips(broker_trades(pd.DataFrame()))
    stats = trade_stats(trips, open_lots, unknown, D0)
    assert stats["by_kind"]["all"] == {"trades": 0}
    assert stats["best"] == [] and stats["monthly"] == []  # noqa: PT018
