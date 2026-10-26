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
    calendar_pick,
    cash_secured_put_pick,
    covered_call_pick,
    credit_spread_pick,
    holding_actions,
    iron_condor_pick,
    leaps_pick,
    leaps_view,
    priced,
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

    # options are expensive on XYZ and it's rising: sell a put, or a put spread to cap the risk
    assert {"cash_secured_put", "put_credit_spread", "iron_condor", "calendar"} <= set(ups["picks"])
    assert ups["vol"]["skew_25d"] == pytest.approx(0.0, abs=0.01)  # the synthetic smile is flat
    assert [i["symbol"] for i in ideas["income"]] == ["XYZ"]  # never the downtrend
    income = ideas["income"][0]
    assert income["pick"]["cash_required"] == income["pick"]["strike"] * 100
    assert income["alternative"]["kind"] == "put credit spread"
    assert any("the seller's" in w for w in income["why"])
    assert any("caps the gain" in r for r in income["risks"])


def test_lists_skip_names_without_a_contract_and_cap_each_sector() -> None:
    spy = closes(0.0003, seed=9)
    names = ["AAA", "BBB", "CCC", "DDD"]
    analyses = []
    for i, name in enumerate(names):
        series = closes(0.003 + 0.0005 * i, seed=20 + i)
        with_chain = chain(series.iloc[-1], symbol=name) if name != "DDD" else None
        analyses.append(analyze(name, series, chain=with_chain, benchmark=spy, iv_hist=None,
                                now=NOW, rate=0.04))  # fmt: skip
    assert analyses[3]["picks"] == {}  # DDD: no chain, so nothing to trade
    groups = {"AAA": "Tech", "BBB": "Tech", "CCC": "Tech", "DDD": "Energy"}
    ideas = rank_ideas(analyses, held={}, groups=groups)
    for k in ("swing", "leaps"):
        assert "DDD" not in {i["symbol"] for i in ideas[k]}
        assert len(ideas[k]) == RULES.per_group  # the third tech name waits its turn
        assert {i["group"] for i in ideas[k]} == {"Tech"}
    one_each = rank_ideas(analyses, held={}, rules=Rules(per_group=1), groups=groups)
    assert len(one_each["swing"]) == 1


def test_premium_selling_structures() -> None:
    c = chain()
    csp = cash_secured_put_pick(c, 100.0, NOW, 0.04, RULES)
    assert csp is not None
    assert csp["dte"] == 46
    assert 0.15 <= abs(csp["delta"]) <= 0.35
    assert csp["strike"] < 100
    assert csp["premium"] == pytest.approx(csp["bid"] * 100)
    assert csp["net_entry"] == pytest.approx(csp["strike"] - csp["bid"])

    puts = credit_spread_pick(c, 100.0, NOW, 0.04, "P", RULES)
    calls = credit_spread_pick(c, 100.0, NOW, 0.04, "C", RULES)
    assert puts is not None
    assert calls is not None
    for s, sign in ((puts, -1), (calls, 1)):
        assert (s["long"]["strike"] - s["short"]["strike"]) * sign > 0  # the wing is further out
        assert 0 < s["credit"] < s["width"]
        assert s["max_loss"] == pytest.approx((s["width"] - s["credit"]) * 100)
        assert s["breakeven"] == pytest.approx(s["short"]["strike"] + sign * s["credit"])
        assert 0.5 < s["prob_profit"] < 0.95
        assert s["theta_day"] > 0  # the seller earns the decay

    condor = iron_condor_pick(c, 100.0, NOW, 0.04, RULES)
    assert condor is not None
    assert condor["put_spread"]["short"]["strike"] < 100 < condor["call_spread"]["short"]["strike"]
    assert condor["put_spread"]["dte"] == condor["call_spread"]["dte"]
    assert condor["lower_breakeven"] < 100 < condor["upper_breakeven"]
    assert 0 < condor["prob_profit"] < 1

    cal = calendar_pick(c, 100.0, NOW, 0.04, RULES)
    assert cal is not None
    assert cal["front"]["dte"] < cal["back"]["dte"]
    assert cal["front"]["strike"] == cal["back"]["strike"] == 100
    assert cal["debit"] > 0
    assert cal["theta_day"] > 0
    assert cal["vega"] > 0  # long the later expiry's volatility
    lo, hi = cal["profit_range"]
    assert lo < 100 < hi
    assert cal["max_profit_est"] > 0


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


def test_a_chain_missing_whole_expiry_windows_still_analyzes() -> None:
    # only short-dated expiries: nothing for the calendar's back month, LEAPS or swing window
    short = chain(days=(4, 11, 18))
    rising, spy = closes(0.003, seed=2), closes(0.0003, seed=9)
    a = analyze("XYZ", rising, chain=short, benchmark=spy, iv_hist=None, now=NOW, rate=0.04)
    assert "calendar" not in a["picks"]
    assert "leaps_call" not in a["picks"]
    empty = priced(short, 100.0, NOW, 0.04, right="C", dte=(50, 120))
    assert empty.empty
    assert {"mid", "spread_pct", "years", "iv", "delta", "theta", "vega"} <= set(empty.columns)


def test_expensive_is_judged_against_the_volatility_forecast() -> None:
    """Implied 30% looks rich against a quiet month's 10% realized; not against a forecast that
    the next month will be 28% (a calm stretch that won't last)."""
    rising = closes(0.003, seed=2)
    c = chain(rising.iloc[-1])
    trailing = analyze("XYZ", rising, chain=c, benchmark=None, iv_hist=None, now=NOW, rate=0.04)
    assert trailing["vol"]["rich"] is True
    assert trailing["vol"]["iv_rv"] is None
    ahead = analyze("XYZ", rising, chain=c, benchmark=None, iv_hist=None, now=NOW, rate=0.04,
                    forecast=0.28)  # fmt: skip
    vol = ahead["vol"]
    assert vol["rv_forecast"] == 0.28
    assert vol["iv_rv"] == pytest.approx(0.3 / 0.28, abs=0.05)
    assert vol["iv_hv"] > 2  # still reported
    assert vol["rich"] is False
    cheap = analyze("XYZ", rising, chain=c, benchmark=None, iv_hist=None, now=NOW, rate=0.04,
                    forecast=0.15)  # fmt: skip
    assert cheap["vol"]["rich"] is True  # 2x the forecast


def test_rv_forecast_from_bars() -> None:
    from tp_ml.testing import random_bars
    from tp_strategies.options_research import rv_forecast

    bars = random_bars(["XYZ"], n=300)
    frame = pd.DataFrame({k: v["XYZ"] for k, v in bars.items() if k != "volume"})
    value = rv_forecast(frame)
    assert value is not None
    assert 0.1 < value < 0.4  # the walk's volatility is about 24% a year
    assert rv_forecast(frame.iloc[:100]) is None  # too little history
    assert rv_forecast(None) is None
