from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tp_core.portfolio import Classifier
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import MemoryRiskState
from tp_trading.events import OrderIntent, OrderType, RiskDecision, Side
from tp_trading.risk import Book

REPO = Path(__file__).resolve().parents[3]
TODAY = date(2024, 7, 12)
IT = "Information Technology"
CLASSIFIER = Classifier(
    sectors={"AAPL": IT, "MSFT": IT, "NVDA": IT, "ORCL": IT, "JPM": "Financials"},
    funds={"SPY": "US Equity", "IEF": "US Treasuries"},
)
PRICES = {
    "AAPL": 200.0, "MSFT": 400.0, "NVDA": 100.0, "ORCL": 100.0, "JPM": 100.0,
    "SPY": 500.0, "IEF": 100.0, "NEW": 50.0,
}  # fmt: skip


def book(**overrides: Any) -> Book:
    base: dict[str, Any] = {
        "session": TODAY,
        "equity": 100_000.0,
        "cash": 100_000.0,
        "positions": {},
        "prices": PRICES,
        "previous_equity": 100_000.0,
        "average_volume": dict.fromkeys(PRICES, 10_000_000.0),
        "last_bar": dict.fromkeys(PRICES, TODAY),
    }
    return Book(**{**base, **overrides})


def buy(symbol: str, qty: float, **kw: Any) -> OrderIntent:
    return OrderIntent("s", symbol, Side.BUY, qty, TODAY, PRICES.get(symbol, 50.0), **kw)


def sell(symbol: str, qty: float, **kw: Any) -> OrderIntent:
    return OrderIntent("s", symbol, Side.SELL, qty, TODAY, PRICES.get(symbol, 50.0), **kw)


def manager(**limits: Any) -> RiskManager:
    return RiskManager(Limits(**limits), CLASSIFIER)


def failed(decision: RiskDecision) -> set[str]:
    return {c.check for c in decision.checks if not c.passed}


def test_repo_limits_match_the_plan() -> None:
    limits = Limits.load(REPO / "config" / "risk.toml")
    assert limits == Limits()
    assert limits.drawdown_limit("anything") == 0.10


def test_a_modest_order_passes_every_check_and_says_why() -> None:
    [d] = manager().review([buy("AAPL", 25)], book())
    assert d.approved
    checks = {c.check: c.detail for c in d.checks}
    assert set(checks) >= {"kill_switch", "daily_loss", "gross_exposure", "max_position",
                           "max_sector", "liquidity", "fresh_data", "strategy_enabled"}  # fmt: skip
    assert checks["max_position"] == "AAPL 5.0% of equity after this order (limit 10%)"


def test_single_names_are_capped_but_funds_are_not() -> None:
    m = manager()
    [stock] = m.review([buy("AAPL", 60)], book())  # 12%
    [fund] = m.review([buy("SPY", 150)], book())  # 75% of equity in one ETF
    assert failed(stock) == {"max_position"}
    assert fund.approved
    assert "max_position" not in {c.check for c in fund.checks}


def test_sector_cap_counts_existing_and_earlier_approved_orders() -> None:
    m = manager()
    held = book(positions={"MSFT": 25.0, "AAPL": 50.0}, cash=80_000.0)  # 20% IT already
    decisions = m.review([buy("NVDA", 90), buy("ORCL", 20), buy("JPM", 50)], held)
    # NVDA -> 29% IT; ORCL would make it 31% (each name under 10%); JPM is another sector
    assert [d.approved for d in decisions] == [True, False, True]
    assert failed(decisions[1]) == {"max_sector"}
    assert "Information Technology 31.0% of equity" in decisions[1].reasons[0]


def test_gross_exposure_blocks_leverage() -> None:
    [d] = manager().review([buy("SPY", 201)], book())
    assert failed(d) == {"gross_exposure"}


def test_selling_what_you_hold_skips_new_risk_checks_but_not_the_kill_switch() -> None:
    state = MemoryRiskState()
    m = RiskManager(Limits(), CLASSIFIER, state)
    crashed = book(positions={"AAPL": 100.0}, equity=90_000.0, previous_equity=100_000.0)
    [s] = m.review([sell("AAPL", 100)], crashed)
    assert s.approved
    assert {c.check for c in s.checks} == {"kill_switch", "liquidity"}
    [b] = m.review([buy("JPM", 10)], crashed)
    assert failed(b) == {"daily_loss"}
    state.set_kill_switch("broker outage")
    [s] = m.review([sell("AAPL", 100)], crashed)
    assert failed(s) == {"kill_switch"}
    assert "broker outage" in s.reasons[0]


def test_no_short_selling() -> None:
    [d] = manager().review([sell("AAPL", 5)], book(positions={"AAPL": 3.0}))
    assert "no_short" in failed(d)


def test_a_disabled_strategy_may_sell_but_not_buy() -> None:
    state = MemoryRiskState()
    state.set_disabled("s", "drawdown")
    m = RiskManager(Limits(), CLASSIFIER, state)
    held = book(positions={"AAPL": 10.0})
    buy_d, sell_d = m.review([buy("JPM", 1), sell("AAPL", 10)], held)
    assert failed(buy_d) == {"strategy_enabled"}
    assert sell_d.approved


def test_stale_or_missing_data_blocks_new_positions() -> None:
    stale = book(last_bar={**dict.fromkeys(PRICES, TODAY), "AAPL": date(2024, 7, 10)})
    [d] = manager().review([buy("AAPL", 1)], stale)
    assert failed(d) == {"fresh_data"}
    [ok] = manager(require_current_bar=False).review([buy("AAPL", 1)], stale)
    assert ok.approved


def test_liquidity_and_unknown_volume() -> None:
    thin = book(average_volume={"AAPL": 1_000.0})
    big, unknown = manager().review([buy("AAPL", 21), buy("JPM", 1)], thin)
    assert failed(big) == {"liquidity"}
    assert failed(unknown) == {"liquidity"}
    assert "no volume history" in unknown.reasons[0]


def test_limit_price_must_be_near_the_last_close() -> None:
    far = buy("AAPL", 1, order_type=OrderType.LIMIT, limit_price=180.0)
    near = buy("AAPL", 1, order_type=OrderType.LIMIT, limit_price=195.0)
    a, b = manager().review([far, near], book())
    assert failed(a) == {"price_sanity"}
    assert b.approved


def test_risk_per_trade_applies_to_orders_with_a_stop() -> None:
    m = manager()
    [wide] = m.review([buy("JPM", 50, stop_price=70.0)], book())  # 50 * 30 = 1.5%
    [tight] = m.review([buy("JPM", 50, stop_price=85.0)], book())  # 0.75%
    [wrong] = m.review([buy("JPM", 1, stop_price=101.0)], book())
    assert failed(wide) == {"risk_per_trade"}
    assert tight.approved
    assert "not below the price" in wrong.reasons[0]


def test_options_are_not_supported_yet() -> None:
    [d] = manager().review([buy("SPY241220C00450000", 1)], book())
    assert "instrument" in failed(d)


def test_unclassified_names_get_their_own_sector_bucket() -> None:
    [d] = manager().review([buy("NEW", 100)], book())  # 5%
    assert d.approved
    assert any("Unclassified (NEW)" in c.detail for c in d.checks)


def test_drawdown_breach_is_recorded_once_and_disables_when_enforced() -> None:
    state = MemoryRiskState()
    m = RiskManager(Limits(), CLASSIFIER, state, enforce_drawdown=True)
    for day, equity in enumerate([100.0, 110.0, 101.0, 98.0, 97.0], start=1):
        m.end_of_day(date(2024, 7, day), "s", equity)
    assert len(m.events) == 1
    assert m.events[0]["session"] == "2024-07-04"
    assert m.events[0]["drawdown"] == pytest.approx(98 / 110 - 1)
    disabled = state.disabled("s")
    assert disabled is not None
    assert "breached the 10% limit" in disabled.reason


def test_drawdown_breach_in_research_mode_only_records() -> None:
    state = MemoryRiskState()
    m = RiskManager(Limits(strategy_drawdown_overrides={"s": 0.05}), CLASSIFIER, state,
                    enforce_drawdown=False)  # fmt: skip
    for day, equity in enumerate([100.0, 94.0, 99.0, 93.0, 101.0, 95.0], start=1):
        m.end_of_day(date(2024, 7, day), "s", equity)
    # one event per drawdown: a bounce to 99 doesn't re-arm it, the new peak at 101 does
    assert [e["session"] for e in m.events] == ["2024-07-02", "2024-07-06"]
    assert state.disabled("s") is None


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[pre_trade]\nmax_postion = 0.1\n", "unknown \\[pre_trade\\] key"),
        ("[pre_trade]\nmax_position = 1.5\n", "must be in"),
        ("[limits]\n", "unknown section"),
        ("[strategy_drawdown]\ndefault = 0.1\nextra = 1\n", "unknown \\[strategy_drawdown\\]"),
    ],
)
def test_limits_file_typos_are_errors(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        Limits.load(path)


def test_limits_overrides_load(tmp_path: Path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(
        '[strategy_drawdown]\ndefault = 0.15\n[strategy_drawdown.overrides]\n"x" = 0.25\n'
    )
    limits = Limits.load(path)
    assert limits.drawdown_limit("x") == 0.25
    assert limits.drawdown_limit("y") == 0.15
