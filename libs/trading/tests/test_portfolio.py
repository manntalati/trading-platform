from datetime import date

import pytest

from tp_trading.costs import CostModel
from tp_trading.events import Fill, Side
from tp_trading.portfolio import Portfolio, Position

D1, D2, D3 = date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3)


def fill(side: Side, qty: float, price: float, fees: float = 0.0, day: date = D1) -> Fill:
    return Fill("o", "s", "AAA", side, qty, price, fees, day)


def test_buy_moves_cash_into_a_position_at_cost_including_fees() -> None:
    p = Portfolio(10_000)
    p.apply(fill(Side.BUY, 10, 100, fees=5))
    assert p.cash == pytest.approx(10_000 - 1_000 - 5)
    assert p.positions["AAA"] == Position("AAA", 10, 100.5, D1)
    assert p.equity({"AAA": 110}) == pytest.approx(8_995 + 1_100)


def test_adding_averages_the_cost_and_keeps_the_open_date() -> None:
    p = Portfolio(10_000)
    p.apply(fill(Side.BUY, 10, 100))
    p.apply(fill(Side.BUY, 10, 120, day=D2))
    pos = p.positions["AAA"]
    assert pos.quantity == 20
    assert pos.avg_price == pytest.approx(110)
    assert pos.opened == D1


def test_selling_realizes_pnl_net_of_fees_and_closing_removes_the_position() -> None:
    p = Portfolio(10_000)
    p.apply(fill(Side.BUY, 10, 100))
    p.apply(fill(Side.SELL, 4, 130, fees=2, day=D2))
    assert p.realized_pnl == pytest.approx(4 * 30 - 2)
    assert p.positions["AAA"].quantity == 6
    p.apply(fill(Side.SELL, 6, 90, day=D3))
    assert "AAA" not in p.positions
    assert p.realized_pnl == pytest.approx(118 - 60)
    assert p.cash == pytest.approx(10_000 - 1_000 + 520 - 2 + 540)
    assert p.fees_paid == pytest.approx(2)


def test_selling_through_zero_opens_the_other_side_at_the_fill_price() -> None:
    p = Portfolio(10_000)
    p.apply(fill(Side.BUY, 5, 100))
    p.apply(fill(Side.SELL, 8, 110, day=D2))
    pos = p.positions["AAA"]
    assert pos.quantity == -3
    assert pos.avg_price == pytest.approx(110)
    assert pos.opened == D2
    assert p.realized_pnl == pytest.approx(50)


def test_valuation_without_a_price_holds_at_cost() -> None:
    p = Portfolio(1_000)
    p.apply(fill(Side.BUY, 5, 100))
    assert p.equity({}) == pytest.approx(1_000)
    assert p.weights({"AAA": 100}) == {"AAA": pytest.approx(0.5)}
    assert p.gross_exposure({"AAA": 120}) == pytest.approx(600)
    assert p.unrealized_pnl({"AAA": 120}) == pytest.approx(100)


def test_costs_slip_against_you_and_fees_fall_on_sales() -> None:
    costs = CostModel(slippage_bps=10)
    assert costs.fill_price(Side.BUY, 100) == pytest.approx(100.1)
    assert costs.fill_price(Side.SELL, 100) == pytest.approx(99.9)
    assert costs.fees(Side.BUY, 100, 50) == 0
    sell = costs.fees(Side.SELL, 100, 50)
    assert sell == pytest.approx(5_000 * 27.80 / 1_000_000 + 100 * 0.000166)
    assert costs.fees(Side.SELL, 1_000_000, 1) == pytest.approx(27.80 + 8.30)  # TAF capped
    free = CostModel.free()
    assert free.fill_price(Side.BUY, 100) == 100
    assert free.fees(Side.SELL, 100, 50) == 0
    assert CostModel(commission_per_share=0.005, commission_min=1).fees(Side.BUY, 10, 5) == 1
