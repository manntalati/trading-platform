"""Position statistics and notes, on made-up contracts (XYZ)."""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tp_core import greeks as bs
from tp_core.positions import (
    OptionPosition,
    OptionQuote,
    expiry_time,
    option_notes,
    option_stats,
    stock_notes,
    stock_stats,
    years_until,
)

NOW = datetime(2026, 1, 12, 15, 0, tzinfo=UTC)  # Monday 10:00 New York
FRIDAY = date(2026, 1, 16)


def call(
    strike: float = 100.0,
    cost: float | None = 2.5,
    quantity: float = 2.0,
    expiration: date = FRIDAY,
    broker_price: float | None = 1.2,
) -> OptionPosition:
    return OptionPosition(
        symbol="XYZ260116C00100000",
        label="XYZ Jan 16 '26 $100 Call",
        underlying="XYZ",
        quantity=quantity,
        strike=strike,
        expiration=expiration,
        call=True,
        cost=cost,
        broker_price=broker_price,
    )


def titles(notes: list[dict[str, str]]) -> list[str]:
    return [n["title"] for n in notes]


def test_expiry_is_the_close_of_the_expiration_day() -> None:
    assert expiry_time(FRIDAY) == datetime(2026, 1, 16, 21, 0, tzinfo=UTC)  # 4 PM EST
    assert years_until(FRIDAY, NOW) == pytest.approx((4 + 6 / 24) / 365)
    assert years_until(FRIDAY, datetime(2026, 1, 17, tzinfo=UTC)) == 0.0


def test_leaps_expiries_years_out_have_a_close() -> None:
    far = datetime.now(UTC).date().replace(month=1, day=15)
    far = far.replace(year=far.year + 3)
    assert expiry_time(far).astimezone(UTC).hour in (20, 21)  # 4 PM New York
    assert years_until(far, NOW) > 2


def test_out_of_the_money_call_a_few_days_out() -> None:
    pos = call()
    quote = OptionQuote(bid=1.00, ask=1.10, last=1.02)
    s = option_stats(pos, spot=97.0, quote=quote, now=NOW, rate=0.04, realized_vol=0.30)
    assert (s["mark"], s["mark_source"]) == (pytest.approx(1.05), "mid")
    assert s["value"] == pytest.approx(210.0)  # 2 contracts x 100 x 1.05
    assert s["pnl"] == pytest.approx((1.05 - 2.5) * 200)
    assert s["exit_value"] == pytest.approx(200.0)  # at the bid
    assert s["breakeven"] == pytest.approx(102.5)
    assert s["breakeven_move_pct"] == pytest.approx(102.5 / 97 - 1)
    assert not s["in_the_money"]
    assert s["intrinsic"] == 0.0
    assert s["extrinsic"] == pytest.approx(1.05)

    years = years_until(FRIDAY, NOW)
    # The implied vol reprices the mid, and the greeks follow from it.
    assert bs.price(97, 100, years, s["iv"], call=True, rate=0.04) == pytest.approx(1.05)
    assert 0 < s["delta"] < 0.5
    assert s["theta_day"] < 0
    assert s["delta_shares"] == pytest.approx(s["delta"] * 200)
    assert 0 < s["prob_profit"] < s["prob_itm"] < 0.5
    assert s["expected_move"] == pytest.approx(97 * s["iv"] * years**0.5)

    grid = s["scenarios"]
    assert grid["columns"][0] == "Now"
    assert grid["columns"][-1] == "Expiry Fri Jan 16"
    assert len(grid["columns"]) == 3  # now, midway, expiry
    flat = next(r for r in grid["rows"] if r["move"] == 0)
    assert flat["cells"][0]["value"] == pytest.approx(210.0)  # no move, now: today's value
    assert flat["cells"][-1]["value"] == 0.0  # no move by expiry: worthless
    up5 = next(r for r in grid["rows"] if r["move"] == 0.05)
    assert up5["cells"][-1]["value"] == pytest.approx((97 * 1.05 - 100) * 200)

    notes = option_notes(s, pos)
    assert titles(notes) == [
        "Out of the money",
        "Time decay",
        "What it needs",
        "Hold or sell",
        "The loss so far",
        "Implied vs actual volatility",
    ]
    text = " ".join(n["text"] for n in notes)
    assert "unless XYZ closes above $100.00 on Fri Jan 16" in text
    assert "has to rise 5.7% from $97.00" in text
    assert "Selling now gets about $200 at the bid" in text


def test_in_the_money_near_expiry_warns_about_exercise() -> None:
    pos = call(strike=95.0, cost=1.0, quantity=1.0)
    s = option_stats(pos, spot=100.0, quote=OptionQuote(bid=5.1, ask=5.3), now=NOW)
    assert s["in_the_money"]
    assert s["pnl"] > 0
    notes = option_notes(s, pos)
    exercise = next(n for n in notes if n["title"] == "Exercise at expiry")
    assert exercise["level"] == "risk"
    assert "you'd buy 100 shares for $9,500" in exercise["text"]
    assert "The gain so far" in titles(notes)
    assert "already passed" in next(n["text"] for n in notes if n["title"] == "What it needs")


def test_wide_market_and_expiry_day() -> None:
    pos = call()
    at_open = datetime(2026, 1, 16, 15, 0, tzinfo=UTC)
    s = option_stats(pos, spot=99.0, quote=OptionQuote(bid=0.20, ask=0.40), now=at_open)
    assert s["spread_pct"] == pytest.approx(0.2 / 0.3)
    notes = option_notes(s, pos)
    assert titles(notes)[0] == "Expires today"
    wide = next(n for n in notes if n["title"] == "Wide market")
    assert "gives up about $20" in wide["text"]  # (0.30 - 0.20) x 200
    assert s["scenarios"]["columns"] == ["Now", "Expiry Fri Jan 16"]


def test_without_a_quote_it_falls_back_to_the_broker_price() -> None:
    pos = call(broker_price=1.2)
    s = option_stats(pos, spot=97.0, quote=None, now=NOW)
    assert (s["mark"], s["mark_source"]) == (1.2, "broker")
    assert s["iv"] is not None
    assert titles(option_notes(s, pos))[0] == "No live quote"

    unknown = option_stats(pos, spot=None, quote=None, now=NOW)
    assert "iv" not in unknown
    assert titles(option_notes(unknown, pos)) == ["No live quote", "No price for the underlying"]


def test_a_price_below_exercise_value_is_flagged_as_stale() -> None:
    pos = call(strike=90.0, broker_price=3.0)  # 7 in the money, priced at 3: an old price
    s = option_stats(pos, spot=97.0, quote=None, now=NOW)
    assert s["iv"] is None
    notes = option_notes(s, pos)
    stale = next(n for n in notes if n["title"] == "Price below exercise value")
    assert "worth $7.00 if exercised now" in stale["text"]
    hold = next(n for n in notes if n["title"] == "Hold or sell")
    assert "there are no odds to weigh" in hold["text"]


def test_short_call_earns_the_decay() -> None:
    pos = call(quantity=-1.0, cost=2.0)
    s = option_stats(pos, spot=97.0, quote=OptionQuote(bid=1.0, ask=1.1), now=NOW)
    assert s["pnl"] == pytest.approx((1.05 - 2.0) * -100)  # bought back cheaper: a gain
    assert s["theta_day"] > 0
    assert s["exit_value"] == pytest.approx(-110.0)  # buying back at the ask
    assert s["prob_profit"] > 0.5
    notes = option_notes(s, pos)
    assert "Earning about" in next(n["text"] for n in notes if n["title"] == "Time decay")
    assert "Hold or sell" not in titles(notes)


def test_stock_stats_and_notes() -> None:
    days = pd.bdate_range("2025-01-01", periods=300)
    rng = np.random.default_rng(3)
    closes = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, len(days)))), index=days)
    spy = pd.Series(400 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, len(days)))), index=days)
    last = float(closes.iloc[-1])
    s = stock_stats(closes, price=last * 1.01, benchmark=spy)
    assert s["day_change_pct"] == pytest.approx(0.01)
    assert s["returns"]["1W"] == pytest.approx(last * 1.01 / closes.iloc[-5] - 1)
    assert s["ytd"] == pytest.approx(last * 1.01 / closes[closes.index.year == 2025].iloc[-1] - 1)
    assert s["low_52w"] <= s["price"] <= s["high_52w"]
    assert s["vol_20d"] > 0
    assert s["ma200"] == pytest.approx(
        pd.concat([closes, pd.Series([last * 1.01])]).iloc[-200:].mean()
    )
    assert "beta_1y" in s

    notes = stock_notes(s, symbol="XYZ", weight=0.25, pnl=-50.0, pnl_pct=-0.05)
    assert titles(notes)[:3] == ["Position", "Concentration", "Volatility"]
    assert "Down $50 (-5.0%)" in notes[0]["text"]

    # Without a live price, the last two closes give the day's change.
    assert stock_stats(closes)["day_change"] == pytest.approx(last - closes.iloc[-2])
    assert stock_stats(pd.Series(dtype=float), price=10.0) == {"price": 10.0}


def test_deep_in_the_money_put_is_read_as_american() -> None:
    from tp_core import american

    far = date(2026, 12, 18)
    years = years_until(far, NOW)
    price = american.price(100.0, 140.0, years, 0.30, call=False, rate=0.05)
    pos = OptionPosition(
        symbol="XYZ261218P00140000",
        label="XYZ Dec 18 '26 $140 Put",
        underlying="XYZ",
        quantity=1.0,
        strike=140.0,
        expiration=far,
        call=False,
        cost=30.0,
        broker_price=price,
    )
    quote = OptionQuote(bid=price - 0.05, ask=price + 0.05, last=price)
    s = option_stats(pos, spot=100.0, quote=quote, now=NOW, rate=0.05, realized_vol=0.3)
    european = bs.implied_vol(price, 100.0, 140.0, years, call=False, rate=0.05)
    assert european is not None
    assert european > 0.33  # Black-Scholes reads the early-exercise right as volatility
    assert s["iv"] == pytest.approx(0.30, abs=0.01)
    assert s["early_exercise_value"] > 0.5
    # an out-of-the-money put is barely affected
    otm = option_stats(
        OptionPosition(**{**pos.__dict__, "strike": 90.0, "broker_price": 2.0}),
        spot=100.0, quote=OptionQuote(bid=1.95, ask=2.05, last=2.0), now=NOW, rate=0.05,
        realized_vol=0.3,
    )  # fmt: skip
    euro_otm = bs.implied_vol(2.0, 100.0, 90.0, years, call=False, rate=0.05)
    assert euro_otm is not None
    assert otm["iv"] == pytest.approx(euro_otm, abs=0.01)
