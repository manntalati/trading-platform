"""The playbook on synthetic names (S01, S02, ...): live setups become ideas with a contract, an
entry and an exit plan; lists are capped per sector; a calibration changes drift and exits."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tp_core.portfolio import Classifier
from tp_core.testing import synthetic_chain
from tp_ml.testing import write_lake
from tp_strategies import playbook
from tp_strategies.playbook import build_lists, for_symbol, ideas_for, prepare

NOW = datetime(2026, 1, 12, 20, 45, tzinfo=UTC)  # Monday, 3:45 PM New York
EXPIRIES = (4, 11, 18, 25, 32, 46, 60, 88, 116, 151, 207, 270, 361, 452, 543)


def path(n: int, drift: float, *, seed: int, dip: float | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + 0.008 * rng.standard_normal(n)))
    if dip is not None:  # three down days in a row
        close[-3:] = close[-4] * np.cumprod([1 - dip] * 3)
    return close


def frame(close: np.ndarray) -> pd.DataFrame:
    days = pd.bdate_range(end=NOW.date() - timedelta(days=3), periods=len(close))
    c = pd.Series(close, index=days)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c})


def chain(spot: float, iv: float, symbol: str = "S01", now: datetime = NOW) -> pd.DataFrame:
    strikes = sorted({round(spot * k / 40, 2) for k in range(20, 65)})
    expirations = [now.date() + timedelta(days=d) for d in EXPIRIES]
    return synthetic_chain(symbol, spot, now, expirations=expirations, iv=iv, skew=-0.1,
                           strikes=strikes, half_spread=0.01)  # fmt: skip


def name(close: np.ndarray, iv: float, symbol: str = "S01") -> playbook.Name:
    bars = frame(close)
    return prepare(symbol, bars, chain=chain(float(close[-1]), iv, symbol), benchmark=None,
                   iv_hist=None, now=NOW, rate=0.0)  # fmt: skip


def only(ideas: list[dict[str, Any]], setup: str) -> dict[str, Any]:
    (idea,) = [i for i in ideas if i["play"]["name"] == setup]
    return idea


def test_a_pullback_becomes_a_call_idea_with_an_entry_and_exit_plan() -> None:
    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.2)
    idea = only(ideas_for(n, None, playbook.calibrated_setups(None)), "pullback")
    assert idea["list"] == "swing"
    pick = idea["pick"]
    assert pick["legs"][0]["right"] == "call"
    assert pick["legs"][0]["side"] == "buy"
    assert pick["model"]["horizon"] == 10
    assert pick["model"]["drift"] == pytest.approx(0.07 * 10 / 252)  # no calibration yet
    assert {"delta", "gamma", "theta_day", "vega", "theta_pct"} <= set(pick["greeks"])
    entry = pick["entry"]
    assert entry["side"] == "debit"
    assert entry["limit"] == pytest.approx(abs(pick["mid"]), abs=0.01)
    assert entry["valid_until"] > n.ind.index[-1].date().isoformat()  # two sessions to act
    plan = pick["exit"]
    paid = pick["model"]["entry"]
    assert plan["take_profit"] == pytest.approx(paid * 1.5, abs=0.01)
    assert plan["stop"] == pytest.approx(paid * 0.5, abs=0.01)
    expiry = datetime.fromisoformat(pick["expiration"]).date()
    assert plan["out_by"] == (expiry - timedelta(days=21)).isoformat()
    assert plan["sell_by"] <= plan["out_by"]
    assert plan["stock"]["text"] == "a close above the 5-day average"
    assert any("Pullback in an uptrend (today)" in w for w in idea["why"])
    # implied 20% against a forecast near 9%: the spread's sold leg pays for the volatility
    assert pick["structure"] == "call debit spread"
    assert pick["breakeven_move"] is None  # short gamma overall: no decay to pay back
    cheap = only(ideas_for(name(path(320, 0.002, seed=4, dip=0.03), iv=0.07), None,
                           playbook.calibrated_setups(None)), "pullback")  # fmt: skip
    assert cheap["pick"]["structure"] == "call"
    assert any("Gamma vs theta" in w for w in cheap["why"])
    if pick["model"]["expected_return"] <= 0:
        assert any("expects a loss" in r for r in idea["risks"])


def test_expensive_options_are_sold_and_cheap_ones_bought() -> None:
    calm = path(320, 0.0005, seed=4)
    rich = ideas_for(name(calm, iv=0.6), None, playbook.calibrated_setups(None))
    income = only(rich, "rich_premium")
    assert income["list"] == "income"
    pick = income["pick"]
    assert pick["credit"] is not None
    assert pick["entry"]["side"] == "credit"
    assert pick["model"]["expected_return"] > 0  # implied 60% against ~19% that's coming
    received = -pick["model"]["entry"]
    assert pick["exit"]["take_profit"] == pytest.approx(received * 0.5, abs=0.01)
    assert pick["exit"]["stop"] == pytest.approx(received * 3, abs=0.01)  # down twice the credit
    assert any("caps the gain" in r for r in income["risks"])

    cheap = ideas_for(name(calm, iv=0.08), None, playbook.calibrated_setups(None))
    vol = only(cheap, "cheap_vol")
    assert vol["list"] == "volatility"
    assert vol["pick"]["structure"] == "straddle"
    assert {leg["right"] for leg in vol["pick"]["legs"]} == {"call", "put"}
    assert "rich_premium" not in {i["play"]["name"] for i in cheap}


def test_a_calibration_sets_the_drift_and_the_exits() -> None:
    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.2)
    plain = only(ideas_for(n, None, playbook.calibrated_setups(None)), "pullback")
    calibration = {"setups": {"pullback": {"drift": 0.03, "events": 400,
                                           "exits": {"take_profit": 0.3, "time_stop": 5},
                                           "underlying": {"hit_rate": 0.6, "median": 0.01},
                                           "option": {"trades": 380, "win_rate": 0.55,
                                                      "median": 0.08}}}}  # fmt: skip
    tuned = only(ideas_for(n, calibration, playbook.calibrated_setups(calibration)), "pullback")
    assert tuned["play"]["drift_source"] == "backtest"
    assert tuned["pick"]["model"]["drift"] == pytest.approx(0.03)
    assert tuned["pick"]["model"]["expected_return"] > plain["pick"]["model"]["expected_return"]
    assert tuned["pick"]["exit"]["rules"]["take_profit"] == 0.3
    assert tuned["pick"]["exit"]["sell_by"] < plain["pick"]["exit"]["sell_by"]
    assert any("Backtest: fired 400 times" in w for w in tuned["why"])


def test_lists_take_one_idea_per_name_and_two_per_sector() -> None:
    names = [name(path(320, 0.002, seed=4, dip=0.03), iv=0.2, symbol=f"S0{i}") for i in range(4)]
    groups = {"S00": "Tech", "S01": "Tech", "S02": "Tech", "S03": "Energy"}
    lists = build_lists(names, held={"S03": "40 shares"}, groups=groups, calibration=None)
    swing = lists["swing"]
    assert [i["group"] for i in swing].count("Tech") == 2
    assert len({i["symbol"] for i in swing}) == len(swing)
    held = next(i for i in swing if i["symbol"] == "S03")
    assert held["held"] == "40 shares"
    assert set(lists) == {*playbook.LISTS, "watch"}


def test_a_leader_above_its_50_day_is_one_to_watch() -> None:
    leader = name(path(320, 0.003, seed=7), iv=0.3)
    lists = build_lists([leader], held={}, groups={}, calibration=None)
    assert [w["symbol"] for w in lists["watch"]] == ["S01"]
    watch = lists["watch"][0]
    assert watch["entry_level"] == pytest.approx(watch["ma50"] * 1.01)
    no_chain = prepare("S02", frame(path(320, 0.003, seed=7)), chain=None, benchmark=None,
                       iv_hist=None, now=NOW, rate=0.0)  # fmt: skip
    assert build_lists([no_chain], held={}, groups={}, calibration=None)["watch"] == []
    view = for_symbol(leader, None)
    assert view["watch"] is True
    assert any("RSI(2)" in line for line in view["levels"])


def test_scan_a_lake(tmp_path: Path) -> None:
    lake = write_lake(tmp_path, ["SPY", "S01", "S02"], n=400, seed=2)  # bars to 2023-12-29
    now = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)
    chains = {s: chain(100.0, 0.3, s, now) for s in ("S01", "S02")}
    out = playbook.scan(lake, ["S01", "S02"], classifier=Classifier(sectors={}, funds={}),
                        chains=chains, now=now, rate=0.0)  # fmt: skip
    assert out["scanned"] == 2
    assert out["calibration"] is None
    assert set(out) >= {*playbook.LISTS, "watch", "live_setups", "disclaimer"}
    for k in playbook.LISTS:
        for idea in out[k]:
            assert idea["pick"]["legs"]
