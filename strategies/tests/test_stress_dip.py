"""Strategy 16: the stress-dip rule, its study, and the playbook's 15:45 evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import NEW_YORK, session_close
from tp_core.indicators import wilder_rsi
from tp_strategies import playbook
from tp_strategies.library import stress_dip as sd


def walk(n: int = 400, seed: int = 0, drift: float = 0.0005) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2022-01-03", periods=n)
    close = 100 * np.exp(np.cumsum(drift + 0.01 * rng.standard_normal(n)))
    high = close * (1 + 0.005 * rng.random(n))
    low = close * (1 - 0.005 * rng.random(n))
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "raw_close": close / 1.01},
        index=days,
    )


def vix_series(days: pd.DatetimeIndex, level: float = 15.0) -> pd.Series:
    return pd.Series(level, index=days)


def test_rsi_matches_the_shared_indicator() -> None:
    ind = sd.daily(walk())
    shared = wilder_rsi(ind[["close"]], 2).iloc[:, 0]
    pd.testing.assert_series_equal(ind["rsi2"], shared, check_names=False)


def test_dip_needs_the_uptrend_and_a_pullback_or_a_weak_close() -> None:
    ind = pd.DataFrame(
        {
            "close": [110.0, 110.0, 90.0, 110.0],
            "ma200": [100.0, 100.0, 100.0, 100.0],
            "ma50": [105.0, 95.0, 105.0, 95.0],
            "rsi2": [5.0, 5.0, 5.0, 50.0],
            "ret1": [0.01, 0.01, -0.01, -0.01],
            "ibs": [0.5, 0.5, 0.1, 0.1],
        }
    )
    # pullback; RSI low but 50 < 200 and no weak close; under the 200-day; a weak down day
    assert sd.dip(ind).tolist() == [True, False, False, True]


def test_stress_reads_only_the_sessions_before() -> None:
    days = pd.bdate_range("2024-01-01", periods=15)
    vix = pd.Series([15.0] * 15, index=days)
    calm = sd.stress(vix, days)
    vix.iloc[-1] = 40.0  # a spike on the last session itself
    spiked = sd.stress(vix, days)
    assert not calm["stressed"].iloc[-1]
    assert not spiked["stressed"].iloc[-1]  # not known until the next session
    nxt = sd.stress(vix, days.append(pd.DatetimeIndex([days[-1] + timedelta(days=3)])))
    assert nxt["stressed"].iloc[-1]
    assert nxt["vix_prev"].iloc[-1] == 40.0
    level = sd.stress(pd.Series([21.0] * 15, index=days), days)  # 20+ is stress on its own
    assert level["stressed"].iloc[-1]


def test_provisional_equals_daily_with_the_bar_replaced() -> None:
    bars = walk()
    ind = sd.daily(bars)
    rng = np.random.default_rng(1)
    price = bars["close"].to_numpy() * (1 + 0.003 * rng.standard_normal(len(bars)))
    high = np.maximum(bars["high"].to_numpy(), price)
    low = np.minimum(bars["low"].to_numpy(), price)
    prov = sd.provisional(ind, price, high, low)
    for i in (250, 300, 399):
        replaced = bars.iloc[: i + 1].copy()
        replaced.iloc[-1, replaced.columns.get_indexer(["close", "high", "low"])] = [
            price[i],
            high[i],
            low[i],
        ]
        again = sd.daily(replaced).iloc[-1]
        for k in ("ma5", "ma50", "ma200", "rsi2", "ibs", "ret1"):
            assert prov[k][i] == pytest.approx(again[k], rel=1e-9), k


def paths_of(spot: list[list[float]], above5: list[list[bool]]) -> sd.Paths:
    n = len(spot)
    return sd.Paths(
        keys=pd.DataFrame(
            {
                "symbol": ["SPY"] * n,
                "session": pd.bdate_range("2024-01-01", periods=n),
                "rsi2": [5.0] * n,
            }
        ),
        spot=np.asarray(spot, dtype=float),
        above5=np.asarray(above5, dtype=bool),
        vix=np.full((n, 4), 0.2),
        days=np.tile([0.0, 1.0, 2.0, 3.0], (n, 1)),
        ratio=np.ones(n),
        rate=np.full(n, 0.04),
    )


def test_simulate_exits_on_the_stop_the_average_or_the_third_session() -> None:
    p = paths_of(
        [[100, 90, 90, 90], [100, 101, 102, 103], [100, 100.5, 100.5, 100.5]],
        [[False] * 4, [False, True, False, False], [False] * 4],
    )
    ret, held = sd.simulate(p, 21, 0.7, sd.SPEC_EXITS)
    assert held.tolist() == [1, 1, 3]
    assert ret[0] <= -0.4  # a 10% drop takes a 0.70-delta call through the 40% stop
    assert ret[1] > 0
    costed, _ = sd.simulate(p, 21, 0.7, sd.SPEC_EXITS, cost=0.02)
    assert costed == pytest.approx(ret - 0.02)


def test_as_traded_takes_one_a_day_and_two_at_most() -> None:
    days = pd.bdate_range("2024-01-01", periods=3)
    keys = pd.DataFrame(
        {
            "symbol": ["SPY", "QQQ", "IWM", "SPY"],
            "session": [days[0], days[0], days[1], days[2]],
            "rsi2": [8.0, 3.0, 5.0, 4.0],
        }
    )
    taken = sd.as_traded(keys, np.array([0.1, 0.2, 0.3, 0.4]), np.array([3, 3, 3, 3]))
    # QQQ (the lowest RSI) on day 1, IWM on day 2; SPY on day 3 would be a third open position
    assert taken["symbol"].tolist() == ["QQQ", "IWM"]


def test_the_late_price_is_the_bar_ending_15_minutes_before_the_close() -> None:
    def bars(day: str, last: str) -> pd.DataFrame:
        stamps = pd.date_range(f"{day} 09:30", f"{day} {last}", freq="5min", tz=NEW_YORK)
        n = len(stamps)
        return pd.DataFrame(
            {
                "symbol": "SPY",
                "timestamp": stamps.tz_convert(UTC),
                "session": pd.Timestamp(day).date(),
                "open": np.arange(n, dtype=float),
                "high": np.arange(n) + 0.5,
                "low": np.arange(n) - 0.5,
                "close": np.arange(n) + 0.25,
            }
        )

    late = sd.at_1545(pd.concat([bars("2024-07-12", "15:55"), bars("2024-11-29", "12:55")]))
    full, half = late.iloc[0], late.iloc[1]
    assert full["price"] == 74.25  # the 15:40 bar, the 75th of the day
    assert full["high"] == 74.5
    assert full["low"] == -0.5
    assert half["price"] == 38.25  # the 12:40 bar on a half day


def test_real_prices_follow_the_spec_exits() -> None:
    frame = sd.daily(walk(260))
    ev = sd.events({"SPY": frame}, vix_series(frame.index))
    day0, day1, day2 = ev["session"].iloc[-6:-3]
    spot = float(ev.loc[ev["session"] == day0, "raw_close"].iloc[0])
    contracts = pd.DataFrame(
        {
            "contract": ["C1"],
            "underlying": ["SPY"],
            "right": ["C"],
            "expiration": [day0 + timedelta(days=30)],
            "strike": [spot * 0.97],
        }
    )
    bars = pd.DataFrame(
        {"contract": "C1", "session": [day0, day1, day2], "close": [5.0, 2.5, 6.0], "volume": 100}
    )
    obs = sd.real_prices(bars, contracts, ev).set_index("session")
    assert obs.loc[day0, "ret"] == pytest.approx(-0.5)  # the stop on the first session
    assert obs.loc[day0, "held"] == 1
    assert day2 not in obs.index  # no bars after it to sell on


def test_the_study_runs_end_to_end() -> None:
    frames = {s: sd.daily(walk(700, seed=i)) for i, s in enumerate(["QQQ", "SPY", "IWM", "XLF"])}
    days = frames["SPY"].index
    vix = pd.Series(18 + 6 * np.sin(np.arange(len(days)) / 7), index=days)
    out = sd.study(frames, vix, ratios={"SPY": 1.0}, funds=["XLF"])
    assert out["test_from"] > out["start"]
    index = out["underlying"]["index (QQQ, SPY, IWM)"]["index (QQQ, SPY, IWM)"]
    assert set(index) == {"train", "test"}
    assert "dip, stressed" in index["test"]
    assert len(out["modeled"]["grid"]) == len(sd.DTES) * len(sd.DELTAS)
    assert set(out["modeled"]["spec"]["costs"]) == {"1%", "2%", "5%"}


# -- the playbook at 15:45 ------------------------------------------------------------------------


def test_the_late_window_is_the_last_minutes_of_a_session() -> None:
    friday = date(2024, 7, 12)
    close = session_close(friday)
    assert not playbook.late_window(close - timedelta(minutes=21))
    assert playbook.late_window(close - timedelta(minutes=15))
    assert not playbook.late_window(close - timedelta(minutes=1))
    assert not playbook.late_window(datetime(2024, 7, 13, 19, 50, tzinfo=UTC))  # Saturday


def test_the_contract_is_the_fixed_rule() -> None:
    rows = []
    for dte in (7, 18, 25, 45):
        for delta in (0.55, 0.68, 0.71, 0.80):
            rows.append(
                {
                    "right": "C",
                    "dte": dte,
                    "delta": delta,
                    "spread_pct": 0.02,
                    "open_interest": 1000,
                    "mid": 10.0,
                    "bid": 9.9,
                    "ask": 10.1,
                }
            )
    rows.append(
        {
            "right": "C",
            "dte": 21,
            "delta": 0.70,
            "spread_pct": 0.05,
            "open_interest": 1000,
            "mid": 10.0,
            "bid": 9.75,
            "ask": 10.25,
        }
    )  # too wide
    rows.append(
        {
            "right": "C",
            "dte": 21,
            "delta": 0.70,
            "spread_pct": 0.01,
            "open_interest": 100,
            "mid": 10.0,
            "bid": 9.95,
            "ask": 10.05,
        }
    )  # too thin
    row = playbook.stress_contract(pd.DataFrame(rows))
    assert row is not None
    assert (row["dte"], row["delta"]) == (18, 0.71)  # nearest 21 days, then nearest 0.70
    assert playbook.stress_contract(pd.DataFrame(rows[-2:])) is None


def test_the_late_entry_raises_once_and_never_past_the_ask() -> None:
    row = pd.Series({"mid": 10.0, "bid": 9.8, "ask": 10.2})
    plan = playbook.late_entry(row, date(2024, 7, 12))
    assert (plan["limit"], plan["worst"]) == (10.0, 10.1)
    assert (plan["reprice_at"], plan["cancel_at"]) == ("15:52", "15:58")
    half = playbook.late_entry(row, date(2024, 11, 29))
    assert half["cancel_at"] == "12:58"


def test_the_late_frame_appends_today_and_needs_yesterdays_vix() -> None:
    bars = walk(300)[["open", "high", "low", "close"]]
    today = (bars.index[-1] + timedelta(days=3)).date()  # the Monday after
    bar = pd.Series({"open": 100.0, "high": 101.0, "low": 95.0, "close": 95.5})
    vix = vix_series(bars.index, 25.0)
    ind = playbook.late_frame(bars, bar, vix, today)
    assert ind is not None
    assert ind.index[-1].date() == today
    assert ind["close"].iloc[-1] == 95.5
    assert ind["stressed"].iloc[-1]
    stale = playbook.late_frame(bars, bar, vix.iloc[:-3], today)  # VIX three sessions old
    assert stale is not None
    assert not stale["stressed"].iloc[-1]


def test_one_entry_a_day_goes_to_the_lowest_rsi() -> None:
    def idea(symbol: str, tier: int, rsi: float) -> dict[str, object]:
        return {"symbol": symbol, "play": {"tier": tier, "rsi2": rsi}, "risks": []}

    ranked = playbook._one_a_day([idea("XLF", 2, 1.0), idea("SPY", 1, 8.0), idea("QQQ", 1, 4.0)])
    assert [i["symbol"] for i in ranked] == ["QQQ", "SPY", "XLF"]
    assert ranked[0]["play"]["todays_entry"]
    assert not ranked[1]["play"]["todays_entry"]
    assert "QQQ has the lowest RSI(2)" in ranked[1]["risks"][0]
