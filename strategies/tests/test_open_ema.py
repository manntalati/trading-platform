"""Strategy 15, the first 5-minute candle against the 12-period EMA, on hand-built sessions (the
fills of each exit) and on synthetic markets with and without an edge planted (S00, S01, ...)."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import NEW_YORK, sessions
from tp_strategies.library import open_ema as oe

DAYS = sessions(date(2024, 1, 2), date(2024, 12, 31))


def day_bars(symbol: str, day: date, closes: list[float], *, opens: list[float] | None = None,
             highs: list[float] | None = None, lows: list[float] | None = None,
             volume: float = 1e5) -> list[dict[str, object]]:  # fmt: skip
    """78 five-minute bars: the ones given, then flat at the last close."""
    closes = closes + [closes[-1]] * (78 - len(closes))
    opens = (opens or [closes[0]]) + closes[:77]  # each bar opens at the previous close
    opens = opens[:78]
    first = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK)
    rows = []
    for i in range(78):
        o, c = opens[i], closes[i]
        rows.append({
            "symbol": symbol, "timestamp": (first + timedelta(minutes=5 * i)).astimezone(UTC),
            "session": day, "open": o, "close": c,
            "high": highs[i] if highs and i < len(highs) else max(o, c),
            "low": lows[i] if lows and i < len(lows) else min(o, c),
            "volume": volume, "vwap": (o + c) / 2,
        })  # fmt: skip
    return rows


def frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def flat(symbol: str, days: list[date], level: float = 100.0) -> list[dict[str, object]]:
    return [r for d in days for r in day_bars(symbol, d, [level])]


def one_trade(rows: list[dict[str, object]], cfg: oe.Config) -> pd.Series:
    s = oe.sessions_from_bars(frame(rows))
    trades = oe.simulate(s, cfg)
    assert len(trades) == 1
    out: pd.Series = trades.iloc[0]
    return out


def test_the_first_candle_against_the_ema_picks_the_side() -> None:
    warm = flat("XYZ", DAYS[:2])
    up = day_bars("XYZ", DAYS[2], [102.0, 102.5], opens=[101.0])  # opens up, holds
    s = oe.sessions_from_bars(frame(warm + up))
    assert len(s) == 1  # the first two sessions only warm the EMA up
    c = oe.candle(s, oe.Config())
    assert c["close"][0] == 102.0
    assert 100.0 < c["ema"][0] < 101.0  # mostly yesterday: 100 + (102 - 100) * 2 / 13
    assert oe.sides(s, oe.Config())[0] == 1
    trade = oe.simulate(s, oe.Config()).iloc[0]
    assert trade["entry"] == 102.0  # the open of the 9:35 bar, not the candle's close
    assert trade["stop"] == 101.0  # the candle's low
    down = day_bars("XYZ", DAYS[2], [98.0, 97.0], opens=[99.0])
    s = oe.sessions_from_bars(frame(warm + down))
    assert oe.sides(s, oe.Config())[0] == -1
    assert oe.sides(s, oe.Config(side="long"))[0] == 0  # long only: no trade


def test_each_exit_fills_where_an_order_would() -> None:
    warm = flat("XYZ", DAYS[:2])
    path = [102.0, 102.5, 103.0, 101.5, 100.0, 104.0]  # the 9:30 candle closes at 102 (low 101)
    lows = [101.0, 102.0, 102.4, 100.8, 99.5]
    day = day_bars("XYZ", DAYS[2], path, opens=[101.0], lows=lows)
    exact = oe.Config(cost_bps=0, stop_slip_bps=0)
    stopped = one_trade(warm + day, exact)
    assert (stopped["reason"], stopped["exit"]) == ("stop", 101.0)  # the 9:45 bar's low
    assert stopped["ret"] == pytest.approx(101.0 / 102.0 - 1)
    assert stopped["r"] == pytest.approx(-1.0)
    slipped = one_trade(warm + day, oe.Config(cost_bps=0))
    assert slipped["exit"] == pytest.approx(101.0 - 2e-4 * 102.0)  # a stop slips 2 bp
    target = one_trade(warm + day, oe.Config(cost_bps=0, target_r=1.0))
    assert (target["reason"], target["exit"]) == ("target", 103.0)  # 1R: 102 + (102 - 101)
    ridden = one_trade(warm + day, oe.Config(cost_bps=0, stop="none"))
    assert (ridden["reason"], ridden["exit"]) == ("close", 104.0)  # flat at the last close
    crossed = one_trade(warm + day, oe.Config(cost_bps=0, stop="none", ema_exit=True))
    assert crossed["reason"] == "ema"

    gap = day_bars("XYZ", DAYS[2], [102.0, 102.5, 99.0], opens=[101.0])  # 9:40 opens at 102.5,
    gap[2]["open"] = 100.0  # then gaps under the stop: filled at that bar's open
    through = one_trade(warm + gap, exact)
    assert (through["reason"], through["exit"]) == ("stop", 100.0)
    costs = one_trade(warm + day, oe.Config(cost_bps=5, stop_slip_bps=0))
    assert costs["ret"] == pytest.approx(101.0 / 102.0 - 1 - 10 / 1e4)


def test_a_short_mirrors_a_long() -> None:
    warm = flat("XYZ", DAYS[:2])
    path = [98.0, 97.5, 97.0, 96.0]
    highs = [99.0, 98.0, 97.6, 97.2]
    day = day_bars("XYZ", DAYS[2], path, opens=[99.0], highs=highs)
    short = one_trade(warm + day, oe.Config(cost_bps=0, target_r=1.0))
    assert short["side"] == -1
    assert short["stop"] == 99.0  # the candle's high
    assert (short["reason"], short["exit"]) == ("target", 97.0)  # 98 - (99 - 98)
    assert short["ret"] == pytest.approx(1 - 97.0 / 98.0)


def market(symbols: int, days: list[date], edge: float, seed: int) -> pd.DataFrame:
    """Random walks sampled 25 times a bar (each bar's high and low are its path's), gapping each
    morning. With ``edge``, the rest of the day drifts the way the open went (about ``edge`` a
    bar), three times as hard on the days with heavy opening volume."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for k in range(symbols):
        symbol, price = f"S{k:02d}", 50.0 + 10 * k
        for day in days:
            gap = rng.normal(0, 0.01)
            heavy = rng.random() < 0.15
            drift = edge * np.sign(gap) * (3 if heavy else 1) / 25
            steps = rng.normal(drift, 0.002 / 5, 78 * 25)
            steps[0] += gap / 4  # the opening bar leans the gap's way
            path = price * (1 + gap) * np.exp(np.r_[0.0, np.cumsum(steps)])
            first = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK)
            volume = rng.lognormal(13, 0.3, 78) * 20
            volume[0] *= 4 if heavy else 1
            for i in range(78):
                bar = path[25 * i : 25 * i + 26]
                rows.append({
                    "symbol": symbol, "session": day, "open": bar[0], "close": bar[-1],
                    "timestamp": (first + timedelta(minutes=5 * i)).astimezone(UTC),
                    "high": bar.max(), "low": bar.min(), "volume": volume[i], "vwap": bar.mean(),
                })  # fmt: skip
            price = float(path[-1])
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def edged() -> oe.Sessions:
    return oe.sessions_from_bars(market(12, DAYS[:120], 0.0004, seed=1))


@pytest.fixture(scope="module")
def noise() -> oe.Sessions:
    return oe.sessions_from_bars(market(12, DAYS[:120], 0.0, seed=2))


def test_the_morning_features_use_only_earlier_sessions(edged: oe.Sessions) -> None:
    k = edged.keys
    first = k[k["symbol"] == "S00"].iloc[0]
    assert np.isnan(first["atr"])  # 14 sessions are needed first
    later = k[k["symbol"] == "S00"].iloc[20]
    assert later["atr"] > 0
    assert later["rvol"] > 0
    assert later["gap"] == pytest.approx(
        edged.open[20, 0] / edged.close[19, int(np.isfinite(edged.close[19]).nonzero()[0][-1])] - 1
    )
    chosen = oe.in_play(edged, 3, min_volume=0)
    per_day = pd.Series(chosen).groupby(k["session"].to_numpy()).sum()
    assert per_day.max() == 3
    picked = k[chosen]
    best = k[oe.in_play(edged, None, min_volume=0)].groupby("session")["rvol"].max()
    top = picked.groupby("session")["rvol"].max()
    assert (top == best.loc[top.index]).all()  # the day's highest relative volume is in


def test_the_study_finds_a_planted_edge_and_not_a_missing_one(
    edged: oe.Sessions, noise: oe.Sessions
) -> None:
    found = oe.study(edged, tops=(3,))
    test = found["rule"]["test"]
    assert test["trades"] > 300
    assert test["mean_bps"] > 10
    assert test["sharpe"] > 2
    assert found["controls"]["always long"]["test"]["mean_bps"] < test["mean_bps"]
    assert found["tickers"]["top 3 by relative volume"]["mean_bps"] > test["mean_bps"]  # in play
    assert set(found["lengths"]) >= {"EMA 12, 5-minute candle", "EMA 26, 15-minute candle"}
    assert found["costs"]["5 bp"]["mean_bps"] == pytest.approx(
        found["costs"]["0 bp"]["mean_bps"] - 10, abs=1e-6
    )
    assert "chosen" in found["exits"]
    assert len(found["tickers"]["per_symbol"]) == 12

    none = oe.study(noise, tops=(3,))["rule"]["test"]
    assert abs(none["mean_bps"]) < 8  # nothing there: zero less the cost, give or take noise


def test_stops_pay_for_the_overshoot_so_a_random_walk_makes_nothing() -> None:
    """Filling a stop exactly at its level credits each stopped trade with the jump past it; the
    stop's slippage takes that back, so on driftless paths the rule makes about nothing."""
    means = []
    for seed in range(5):
        s = oe.sessions_from_bars(market(8, DAYS[:60], 0.0, seed=100 + seed))
        means.append(oe.simulate(s, oe.Config(cost_bps=0))["ret"].mean() * 1e4)
    assert abs(float(np.mean(means))) < 2.5


def test_bad_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="ema is one of"):
        oe.Config(ema=10)
    with pytest.raises(ValueError, match="needs a stop"):
        oe.Config(stop="none", target_r=2.0)
    with pytest.raises(LookupError):
        oe.sessions_from_bars(pd.DataFrame())
