"""The signal grammar, its genetic search, and strategy 14 that trades a rule (synthetic data)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.calendar import sessions
from tp_strategies import grammar, search
from tp_strategies.library import build
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine

DAYS = pd.DatetimeIndex(sessions(date(2015, 1, 2), date(2022, 12, 30)))


def market(seed: int = 3) -> pd.DataFrame:
    """Two assets that trend for long spells (momentum works) and two random walks."""
    rng = np.random.default_rng(seed)
    n = len(DAYS)
    regime = np.repeat(rng.choice([-1, 1], size=n // 120 + 1), 120)[:n]
    trending = 0.0012 * regime[:, None] + 0.01 * rng.standard_normal((n, 2))
    noise = 0.0002 + 0.012 * rng.standard_normal((n, 2))
    steps = np.hstack([trending, noise])
    return pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=DAYS,
                        columns=["SPY", "EFA", "IEF", "GLD"])  # fmt: skip


def test_rules_read_and_write_back() -> None:
    text = "and(ma(200),not(calm(21)))"
    assert grammar.to_text(grammar.parse(text)) == text
    assert grammar.size(grammar.parse(text)) == 4
    assert grammar.depth(grammar.parse(text)) == 3
    for bad in ("ma(13)", "foo(1)", "and(ma(200))", "ma(200", "ma(200))", "rsi(14,45)", ""):
        with pytest.raises(ValueError, match=r"."):  # any parse error
            grammar.parse(bad)


def test_simplify() -> None:
    p = grammar.parse
    assert grammar.simplify(p("not(not(ma(50)))")) == p("ma(50)")
    assert grammar.simplify(p("and(ma(50),ma(50))")) == p("ma(50)")
    assert grammar.simplify(p("or(mom(21),ma(20))")) == grammar.simplify(p("or(ma(20),mom(21))"))


def test_signals_use_only_the_past() -> None:
    px = market()
    rule = grammar.parse("or(and(ma(200),mom(126)),cross(20,50))")
    full = grammar.signal(rule, px)
    cut = grammar.signal(rule, px.iloc[:1000])
    pd.testing.assert_frame_equal(full.iloc[:1000], cut)  # later prices change nothing before
    ma = px > px.rolling(200).mean()
    assert (grammar.signal(grammar.parse("ma(200)"), px).iloc[200:] == ma.iloc[200:]).all().all()
    assert not grammar.signal(grammar.parse("ma(200)"), px).iloc[:199].any().any()  # warm-up: out


def test_monthly_weights_hold_between_month_ends() -> None:
    px = market()
    w = grammar.rule_weights(grammar.parse("mom(21)"), px, "monthly")
    assert set(np.unique(w.to_numpy())) <= {0.0, 0.25}
    month = pd.DatetimeIndex(w.index).to_period("M")
    inside = w.groupby(month).nunique()
    # one change at most per month: held from one month end to the next
    assert (inside.iloc[1:-1] <= 2).all().all()


def test_evolution_finds_trend_and_scores_everything() -> None:
    px = market()
    train = pd.Series(pd.Timestamp("2019-12-31") >= DAYS, index=DAYS)
    evo = grammar.evolve(px, train, lambda w: search.daily_returns(w, px, 5.0),
                         population=24, generations=6, seed=1)  # fmt: skip
    assert len(evo.evaluated) >= 24
    best, fitness = evo.population[0]
    assert fitness > 0  # the trending assets are there to be found
    assert all(grammar.depth(c.rule) <= grammar.MAX_DEPTH for c, _ in evo.population)
    again = grammar.evolve(px, train, lambda w: search.daily_returns(w, px, 5.0),
                           population=24, generations=6, seed=1)  # fmt: skip
    assert again.population[0][0].key == best.key  # seeded: reproducible


def test_strategy_14_trades_the_rule() -> None:
    px = market()
    strategy = build("rule-timing", {"rule": "mom(63)", "assets": "SPY,EFA,IEF,GLD",
                                     "rebalance": "monthly"})  # fmt: skip
    result = BacktestEngine(strategy, MarketData.from_closes(px)).run()
    held = result.weights.drop(columns="cash", errors="ignore")
    assert result.orders["target_weight"].max() <= 0.25 + 1e-9  # one quarter each, at most
    on = grammar.signal(grammar.parse("mom(63)"), px)
    month_end = DAYS[DAYS.to_series().groupby(DAYS.to_period("M")).transform("max") == DAYS]
    later = list(month_end[6:-1])
    for d in later[:10]:
        i = DAYS.get_loc(d)
        after = held.iloc[i + 2]  # filled at the next open, so held from the close after
        assert ((after > 0) == on.loc[d]).all(), d
    with pytest.raises(ValueError, match="allowed"):
        build("rule-timing", {"rule": "ma(13)"})
    with pytest.raises(ValueError, match="rebalance"):
        build("rule-timing", {"rebalance": "hourly"})


def test_the_search_breeds_rules_and_counts_them_all() -> None:
    from tp_strategies.validation import ValidationConfig

    px = market()
    periods = ValidationConfig(train_end=date(2019, 12, 31), validation_end=date(2021, 6, 30),
                               wf_train=252, wf_test=63)  # fmt: skip
    cfg = search.SearchConfig(families=("trend", "grammar"), population=20, generations=4, keep=8)
    counted: list[int] = []

    def ledger(rows: list[dict]) -> list[float]:  # type: ignore[type-arg]
        counted.append(len(rows))
        return [float(r["sharpe"]) for r in rows if r["sharpe"] == r["sharpe"]]

    report = search.search(px, cfg, periods, trial_sharpes=ledger)
    g = report["grammar"]
    assert g["kept"] == 8
    assert counted[0] >= g["scored"]  # every bred rule is a trial, kept or not
    grammar_rows = [i for i in report["table"].index if i.startswith("rule-timing")]
    assert len(grammar_rows) == 8
    windows = report["walk_forward"]
    assert "windows" not in windows  # summary only
    text = search.report_text(report)
    assert "genetic search scored" in text
    if report["chosen"].engine == "rule-timing":
        assert "-p 'rule=" in search.engine_command(report["chosen"])
