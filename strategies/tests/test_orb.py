"""Opening-range breakout on hand-made 5-minute bars."""

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from tp_core.calendar import NEW_YORK
from tp_strategies.library.orb import OrbConfig, daily_returns, orb_trades, summary

DAY = date(2024, 3, 5)


def day(
    path: list[tuple[float, float, float, float]], symbol: str = "XYZ", d: date = DAY
) -> pd.DataFrame:
    start = datetime.combine(d, time(9, 30), tzinfo=NEW_YORK)
    return pd.DataFrame(
        [
            {"symbol": symbol, "session": d, "timestamp": start + timedelta(minutes=5 * i),
             "open": o, "high": h, "low": lo, "close": c, "volume": 1.0, "vwap": c}
            for i, (o, h, lo, c) in enumerate(path)
        ]
    )  # fmt: skip


RANGE = [(100, 101, 99, 100)] * 6  # the first 30 minutes: 99 to 101


def test_breakout_held_to_the_close() -> None:
    bars = day(
        [*RANGE, (100.5, 101.5, 100.2, 101.2), (101.2, 102.5, 101, 102.4), (102.4, 103, 102, 103)]
    )
    t = orb_trades(bars, OrbConfig(cost_bps=0, slip_bps=0)).iloc[0]
    assert t["side"] == "long"
    assert t["entry"] == 101
    assert t["stop"] == 99
    assert (t["exit"], t["exit_reason"]) == (103, "close")
    assert t["r"] == pytest.approx(1.0)
    assert t["ret"] == pytest.approx(103 / 101 - 1)


def test_stopped_out_and_gap_through_the_stop() -> None:
    stopped = day(
        [*RANGE, (100.5, 101.5, 100.2, 101.2), (101, 101.1, 98.5, 98.7), (98.7, 99, 98, 98.5)]
    )
    t = orb_trades(stopped, OrbConfig(cost_bps=0, slip_bps=0)).iloc[0]
    assert (t["exit"], t["exit_reason"]) == (99, "stop")
    gapped = day([*RANGE, (100.5, 101.5, 100.2, 101.2), (97, 97.5, 96.5, 97)])
    assert orb_trades(gapped, OrbConfig(cost_bps=0, slip_bps=0)).iloc[0]["exit"] == 97
    same_bar = day([*RANGE, (100, 101.5, 98.5, 100), (100, 100.5, 99.5, 100)])
    assert orb_trades(same_bar, OrbConfig(cost_bps=0, slip_bps=0)).iloc[0]["exit_reason"] == "stop"


def test_no_breakout_shorts_and_the_midpoint_stop() -> None:
    quiet = day(RANGE + [(100, 100.8, 99.2, 100)] * 3)
    assert orb_trades(quiet).empty
    down = day([*RANGE, (99.5, 99.8, 98.5, 98.6), (98.6, 98.7, 97, 97.2)])
    assert orb_trades(down).empty  # long only by default
    short = orb_trades(down, OrbConfig(direction="both", stop="mid", cost_bps=0, slip_bps=0)).iloc[
        0
    ]
    assert (short["side"], short["entry"], short["stop"]) == ("short", 99, 100)
    assert short["ret"] == pytest.approx(1 - 97.2 / 99)
    with pytest.raises(ValueError, match="direction"):
        orb_trades(down, OrbConfig(direction="sideways"))


def test_daily_returns_average_over_symbols_and_summary() -> None:
    win = day([*RANGE, (100.5, 101.5, 100.2, 101.2), (101.2, 103, 101, 103)], "AAA")
    flat = day(RANGE + [(100, 100.8, 99.2, 100)] * 2, "BBB")
    bars = pd.concat([win, flat], ignore_index=True)
    trades = orb_trades(bars, OrbConfig(cost_bps=0, slip_bps=0))
    daily = daily_returns(trades, bars)
    assert daily.iloc[0] == pytest.approx((103 / 101 - 1) / 2)  # BBB's half of the capital idle
    s = summary(trades)
    assert (s["trades"], s["hit_rate"]) == (1, 1.0)
    assert summary(trades.iloc[0:0]) == {"trades": 0}
