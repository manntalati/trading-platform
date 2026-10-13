"""Options research on synthetic prices and chains (XYZ, ABC): setups, contract picks, ranking,
and what to do with positions already held."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tp_core.testing import synthetic_chain
from tp_strategies.options_research import (
    Rules,
    analyze,
    covered_call_pick,
    holding_actions,
    leaps_pick,
    leaps_view,
    rank_ideas,
    spread_pick,
    stock_setup,
    swing_pick,
)

NOW = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)  # Monday, 3:45 PM New York
DAY = NOW.date()
RULES = Rules()


def expiries(*days: int) -> list[date]:
    return [DAY + timedelta(days=d) for d in days]


def chain(spot: float = 100.0, iv: float | list[float] = 0.3, symbol: str = "XYZ",
          days: tuple[int, ...] = (18, 46, 60, 88, 340, 522)) -> pd.DataFrame:  # fmt: skip
    return synthetic_chain(symbol, spot, NOW, expirations=expiries(*days), iv=iv)


def closes(drift: float, *, seed: int = 1, n: int = 300) -> pd.Series:
    days = pd.bdate_range(end=DAY - timedelta(days=1), periods=n)
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, 0.006, n))), index=days)


def test_swing_pick_is_near_60_days_and_055_delta() -> None:
    c = chain()
    row = swing_pick(c, 100.0, NOW, 0.04, "C", RULES)
    assert row is not None
    assert 30 <= row["dte"] <= 100
    assert row["dte"] == 60
    assert 0.45 <= row["delta"] <= 0.70
    put = swing_pick(c, 100.0, NOW, 0.04, "P", RULES)
    assert put is not None
    assert -0.70 <= put["delta"] <= -0.45


def test_debit_spread_caps_the_cost_and_the_gain() -> None:
    c = chain()
    long = swing_pick(c, 100.0, NOW, 0.04, "C", RULES)
    assert long is not None
    s = spread_pick(c, 100.0, NOW, 0.04, long, RULES)
    assert s is not None
    assert s["short"]["strike"] > s["long"]["strike"]
    assert s["short"]["delta"] == pytest.approx(0.30, abs=0.08)
    width = s["short"]["strike"] - s["long"]["strike"]
    assert 0 < s["debit"] < width
    assert s["max_gain"] == pytest.approx((width - s["debit"]) * 100)
    assert s["cost"] < long["ask"] * 100  # cheaper than the call alone
    assert s["breakeven"] == pytest.approx(s["long"]["strike"] + s["debit"])


def test_leaps_pick_replaces_shares_at_the_least_time_value() -> None:
    c = chain()
    row = leaps_pick(c, 100.0, NOW, 0.04, "C", RULES)
    assert row is not None
    assert row["dte"] >= 270
    assert 0.65 <= row["delta"] <= 0.90
    v = leaps_view(row, 100.0, 0.04)
    assert v["capital_freed"] == pytest.approx(100 * 100 - v["cost"])
    assert v["shares_equivalent"] == pytest.approx(row["delta"] * 100)
    assert 0 < v["carry_per_year_pct"] < 0.2
    assert v["breakeven_cagr"] is not None


def test_covered_call_on_250_shares() -> None:
    cc = covered_call_pick(chain(), 100.0, NOW, 0.04, 250, RULES)
    assert cc is not None
    assert cc["contracts"] == 2
    assert cc["strike"] > 100
    assert 21 <= cc["dte"] <= 50
    assert cc["premium"] == pytest.approx(cc["bid"] * 200)
    assert cc["annualized_pct"] == pytest.approx(cc["yield_pct"] * 365 / cc["dte"])
    assert covered_call_pick(chain(), 100.0, NOW, 0.04, 99, RULES) is None


def test_stock_setup_tags() -> None:
    up = {"vs_ma20": -0.01, "vs_ma50": 0.03, "vs_ma200": 0.12, "ma50": 105, "ma200": 95,
          "from_high_pct": -0.02, "returns": {"1M": 0.02}}  # fmt: skip
    s = stock_setup(up, 0.08)
    assert s["direction"] == "bullish"
    assert s["tags"] == ["uptrend", "pullback", "near 52-week high", "beating the S&P 500"]
    down = {"vs_ma20": -0.05, "vs_ma50": -0.06, "vs_ma200": -0.15, "ma50": 90, "ma200": 100,
            "from_high_pct": -0.3, "returns": {"1M": -0.08}}  # fmt: skip
    assert stock_setup(down, -0.1)["direction"] == "bearish"
    hot = up | {"vs_ma20": 0.12}
    assert "extended" in stock_setup(hot, None)["tags"]


def test_analyze_and_rank_ideas() -> None:
    spy = closes(0.0003, seed=9)
    rising, falling = closes(0.003, seed=2), closes(-0.003, seed=3)
    ups = analyze("XYZ", rising, chain=chain(rising.iloc[-1]), benchmark=spy, iv_hist=None,
                  now=NOW, rate=0.04)  # fmt: skip
    downs = analyze("ABC", falling, chain=chain(falling.iloc[-1], symbol="ABC"), benchmark=spy,
                    iv_hist=None, now=NOW, rate=0.04)  # fmt: skip
    assert ups["setup"]["direction"] == "bullish"
    assert set(ups["picks"]) >= {"swing_call", "call_spread", "leaps_call", "swing_put"}
    assert ups["vol"]["iv30"] == pytest.approx(0.3, abs=0.01)
    assert ups["vol"]["rich"] is True  # 30% implied vs ~10% realized

    ideas = rank_ideas([ups, downs], held={"XYZ": "2 calls"})
    swing = ideas["swing"][0]
    assert swing["symbol"] == "XYZ"
    assert (
        "long" in swing["pick"]
    )  # options are rich: the spread leads, the call is the alternative
    assert swing["alternative"]["right"] == "call"
    assert any("Uptrend" in w for w in swing["why"])
    assert any("Already held: 2 calls" in r for r in swing["risks"])
    assert ideas["leaps"][0]["pick"]["dte"] >= 270
    assert [i["symbol"] for i in ideas["bearish"]] == ["ABC"]
    assert ideas["bearish"][0]["pick"]["long"]["right"] == "put"


def test_event_in_the_term_structure_is_a_risk() -> None:
    rising, spy = closes(0.003, seed=2), closes(0.0003, seed=9)
    spot = float(rising.iloc[-1])
    bumpy = chain(spot, iv=[0.25, 0.45, 0.30, 0.29, 0.28, 0.28])
    a = analyze("XYZ", rising, chain=bumpy, benchmark=spy, iv_hist=None, now=NOW, rate=0.04)
    assert a["vol"]["event"]["by"] == expiries(46)[0].isoformat()
    idea = rank_ideas([a], held={})["swing"][0]
    assert any("pricing an event" in r for r in idea["risks"])


def option_row(**stats: Any) -> dict[str, Any]:
    return {"kind": "option", "symbol": "XYZ260313C00100000", "quantity": 2, "stats": stats}


def titles(actions: list[dict[str, Any]]) -> list[str]:
    return [a["title"] for a in actions]


def test_swing_option_close_to_expiry_gets_a_roll() -> None:
    c = chain()
    actions = holding_actions(
        option_row(days_left=15, pnl_pct=-0.2, mark=1.5, bid=1.45, delta=0.35, spot=100.0,
                   right="call", strike=102.0, expiration=expiries(18)[0].isoformat(),
                   extrinsic=1.5, theta_day=-12.0, prob_profit=0.3),
        c, now=NOW, rate=0.04,
    )  # fmt: skip
    roll = next(a for a in actions if a["title"] == "Close or roll before the decay speeds up")
    assert roll["trade"]["dte"] == 60
    assert roll["trade"]["strike"] == 100.0  # the listed strike nearest 102
    assert "Closing returns about $290" in roll["text"]


def test_leaps_winner_and_low_odds_loser() -> None:
    c = chain()
    winner = holding_actions(
        option_row(days_left=520, pnl_pct=1.4, mark=45.0, bid=44.5, delta=0.93, spot=100.0,
                   right="call", strike=60.0, expiration=expiries(522)[0].isoformat()),
        c, now=NOW, rate=0.04,
    )  # fmt: skip
    lock = next(a for a in winner if a["title"] == "Lock in some of the gain")
    assert lock["trade"]["delta"] == pytest.approx(0.70, abs=0.06)
    assert "off the table" in lock["text"]

    loser = holding_actions(
        option_row(days_left=10, pnl_pct=-0.75, mark=0.3, bid=0.25, delta=0.1, spot=100.0,
                   right="call", strike=110.0, expiration=expiries(18)[0].isoformat(),
                   extrinsic=0.3, prob_profit=0.05),
        c, now=NOW, rate=0.04,
    )  # fmt: skip
    assert "Low odds of recovering" in titles(loser)
    assert holding_actions({"kind": "option", "quantity": -1, "stats": {}}, c, now=NOW,
                           rate=0.04) == []  # fmt: skip


def test_stock_actions_for_a_large_winner() -> None:
    stock = {"kind": "stock", "symbol": "XYZ", "quantity": 250, "price": 100.0, "weight": 0.2,
             "pnl_pct": 0.8, "stats": {"vs_ma200": 0.1}}  # fmt: skip
    actions = holding_actions(stock, chain(), now=NOW, rate=0.04)
    assert titles(actions) == [
        "Covered call income",
        "LEAPS in place of 100 shares",
        "Insure the gain",
    ]
    assert "Selling 2 " in actions[0]["text"]
    assert holding_actions(stock, None, now=NOW, rate=0.04) == []
