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
from tp_strategies.budget import Budget
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
    assert set(lists) == {*playbook.LISTS, "watch", "out_of_reach", "budget", "holding_alerts",
                          "model_rank"}  # fmt: skip
    assert lists["budget"] is None  # no account given: nothing filtered


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
            assert (
                idea["pick"]["legs"] if k != playbook.STOCKS else idea["pick"]["kind"] == "shares"
            )


def test_a_pullback_is_a_stock_idea_too_with_a_stop_and_a_size() -> None:
    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.2)
    setups = playbook.calibrated_setups(None)
    idea = only(playbook.stock_ideas_for(n, None, setups), "pullback")
    assert idea["list"] == playbook.STOCKS
    pick = idea["pick"]
    atr = float(n.ind["atr14"].iloc[-1])
    assert pick["kind"] == "shares"
    assert pick["entry"]["limit"] == pytest.approx(n.spot, abs=0.01)
    assert pick["exit"]["stop"] == pytest.approx(n.spot - 2.5 * atr, abs=0.01)
    assert pick["exit"]["target"] is None
    assert pick["exit"]["stock"]["rule"] == "above_ma5"
    assert pick["exit"]["sell_by"] > NOW.date().isoformat()
    assert any("The stop is 2.5 ATR" in w for w in idea["why"])

    budget = Budget(buying_power=5000.0, account_value=50_000.0, account="A", risk_per_trade=0.01)
    sized = only(playbook.stock_ideas_for(n, None, setups, budget), "pullback")
    size = sized["pick"]["size"]
    risk = sized["pick"]["risk_per_share"]
    assert size["shares"] == min(int(5000 // n.spot), int(500 // risk))
    assert size["max_loss"] <= 500 + 1e-9

    missed: list[dict[str, Any]] = []
    broke = Budget(buying_power=10.0, account_value=1000.0, account="A", risk_per_trade=0.01)
    assert not playbook.stock_ideas_for(n, None, setups, broke, missed)
    assert {m["structure"] for m in missed} == {"shares"}


def test_calibrated_share_exits_and_the_ml_rank_move_a_stock_idea() -> None:
    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.2)
    calibration = {"setups": {"pullback": {"drift": 0.02, "events": 400, "stock": {
        "exits": {"stop_atr": 1.5, "target_atr": 4.0, "time_stop": 5, "underlying": None},
        "test": {"trades": 120, "win_rate": 0.58, "mean": 0.011, "avg_sessions": 4.2,
                 "excess": 0.008, "mean_r": 0.31}}}}}  # fmt: skip
    setups = playbook.calibrated_setups(calibration)
    rank = playbook.ModelRank({"S01": 0.95}, NOW.date(), 40, "v1", 0.04)
    idea = only(playbook.stock_ideas_for(n, calibration, setups, ranks=rank), "pullback")
    pick = idea["pick"]
    atr = float(n.ind["atr14"].iloc[-1])
    assert pick["exit"]["stop"] == pytest.approx(n.spot - 1.5 * atr, abs=0.01)
    assert pick["exit"]["target"] == pytest.approx(n.spot + 4 * atr, abs=0.01)
    assert pick["exit"]["stock"] is None
    assert any("won 58%" in w for w in idea["why"])
    assert any("top 5% of 40 names" in w for w in idea["why"])
    untrusted = playbook.ModelRank({"S01": 0.95}, NOW.date(), 40, "v1", -0.01)
    plain = only(playbook.stock_ideas_for(n, calibration, setups, ranks=untrusted), "pullback")
    assert idea["score"] == pytest.approx(plain["score"] + playbook.RANK_TILT * 0.45)
    assert idea["model_rank"]["counted"] is True
    assert plain["model_rank"]["counted"] is False


def test_a_downward_setup_on_a_stock_you_hold_is_an_alert() -> None:
    rng = np.random.default_rng(3)
    fall = 100 * np.exp(np.cumsum(-0.003 + 0.006 * rng.standard_normal(320)))
    fall[-1] = fall[-2] * 0.9  # a new 50-day low in a downtrend
    n = name(fall, iv=0.3)
    lists = build_lists([n], held={"S01": "40 shares"}, groups={}, calibration=None)
    (alert,) = [a for a in lists["holding_alerts"] if a["setup"] == "breakdown"]
    assert alert["held"] == "40 shares"
    assert alert["text"].startswith("Breakdown to a new low (today)")
    options_only = build_lists([n], held={"S01": "2 x S01 call"}, groups={}, calibration=None)
    assert options_only["holding_alerts"] == []  # only for shares held


def news(sentiment: float, headlines: int) -> dict[str, Any]:
    return {"sentiment": sentiment, "headlines": headlines, "claude": 0, "sessions": 5,
            "since": "2026-01-06", "latest": [{"headline": "S01 news", "score": sentiment,
                                               "scored_by": "keywords"}]}  # fmt: skip


def test_news_moves_an_idea_its_way_and_flags_it_when_against() -> None:
    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.2)
    base = only(ideas_for(n, None, playbook.calibrated_setups(None)), "pullback")
    plain = playbook.with_news(dict(base, why=list(base["why"]), risks=list(base["risks"])), None)
    assert plain["news"] is None
    good = playbook.with_news(dict(base, why=list(base["why"]), risks=list(base["risks"])),
                              news(0.5, 6))  # fmt: skip
    assert good["score"] == pytest.approx(base["score"] + playbook.NEWS_WEIGHT * 0.5)
    assert any(w.startswith("The news agrees: +0.50 mean sentiment from 6 headlines") for w in
               good["why"])  # fmt: skip
    bad = playbook.with_news(dict(base, why=list(base["why"]), risks=list(base["risks"])),
                             news(-0.6, 2))  # fmt: skip
    assert bad["score"] == pytest.approx(base["score"] - playbook.NEWS_WEIGHT * 0.6 * 2 / 4)
    assert any("runs against it" in r and '"S01 news"' in r for r in bad["risks"])


def test_bad_news_on_a_stock_you_hold_is_an_alert() -> None:
    calm = name(path(320, 0.0005, seed=4), iv=0.2)
    lists = build_lists([calm], held={"S01": "40 shares"}, groups={}, calibration=None,
                        news={"S01": news(-0.55, 4)})  # fmt: skip
    (alert,) = [a for a in lists["holding_alerts"] if a["setup"] == "news"]
    assert alert["text"].startswith("Bad news: -0.55 mean sentiment from 4 headlines")
    mild = build_lists([calm], held={"S01": "40 shares"}, groups={}, calibration=None,
                       news={"S01": news(-0.2, 4)})  # fmt: skip
    assert not [a for a in mild["holding_alerts"] if a["setup"] == "news"]
