from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tp_trading.costs import CostModel
from tp_trading.data import MarketData
from tp_trading.events import Fill, Order, OrderIntent, OrderStatus, OrderType, Side
from tp_trading.execution import ExecutionConfig, SimulatedExecution
from tp_trading.portfolio import Portfolio

DAYS = pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-03"])


def data(**bars: tuple[float, float, float, float]) -> MarketData:
    """Second-session OHLC per symbol; the first session closes at 100 for everyone."""
    frames = {}
    for i, name in enumerate(["open", "high", "low", "close"]):
        frames[name] = pd.DataFrame({s: [100.0, b[i], b[3]] for s, b in bars.items()}, index=DAYS)
    frames["volume"] = pd.DataFrame(1e6, index=DAYS, columns=list(bars))
    return MarketData(frames)  # type: ignore[arg-type]


def intent(
    symbol: str,
    side: Side,
    qty: float,
    *,
    order_type: OrderType = OrderType.MARKET,
    limit: float | None = None,
    target: float | None = None,
) -> Order:
    i = OrderIntent(
        strategy="s",
        symbol=symbol,
        side=side,
        quantity=qty,
        session=date(2024, 7, 1),
        reference_price=100.0,
        order_type=order_type,
        limit_price=limit,
        target_weight=target,
    )
    return Order(i.order_id(1), i)


def execution(md: MarketData, **config: Any) -> SimulatedExecution:
    return SimulatedExecution(md, ExecutionConfig(**config))


def test_market_buy_fills_at_the_next_open_plus_slippage() -> None:
    md = data(AAA=(102, 105, 99, 104))
    p = Portfolio(10_000)
    order = intent("AAA", Side.BUY, 10)
    [fill] = execution(md, costs=CostModel(slippage_bps=10)).execute([order], 1, p)
    assert fill.price == pytest.approx(102 * 1.001)
    assert fill.session == date(2024, 7, 2)
    assert fill.slippage == pytest.approx(10 * 0.102)
    assert order.status is OrderStatus.FILLED
    assert p.quantity("AAA") == 10


def test_next_close_fills_at_the_close() -> None:
    md = data(AAA=(102, 105, 99, 104))
    [fill] = execution(md, fill_at="next_close", costs=CostModel.free()).execute(
        [intent("AAA", Side.BUY, 1)], 1, Portfolio(1_000)
    )
    assert fill.price == 104


@pytest.mark.parametrize(
    ("bar", "limit", "price"),
    [
        ((98, 101, 97, 100), 99.0, 98.0),  # opens below the limit: the open
        ((101, 102, 98, 100), 99.0, 99.0),  # trades down through it: the limit
        ((101, 102, 99.5, 100), 99.0, None),  # never reaches it: expires
    ],
)
def test_buy_limit(
    bar: tuple[float, float, float, float], limit: float, price: float | None
) -> None:
    md = data(AAA=bar)
    order = intent("AAA", Side.BUY, 5, order_type=OrderType.LIMIT, limit=limit)
    fills = execution(md).execute([order], 1, Portfolio(10_000))
    if price is None:
        assert fills == []
        assert order.status is OrderStatus.EXPIRED
    else:
        assert fills[0].price == price
        assert fills[0].slippage == 0


def test_sell_limit_fills_at_the_limit_when_the_high_reaches_it() -> None:
    md = data(AAA=(100, 106, 99, 101))
    p = Portfolio(450)
    p.apply(_buy("AAA", 5, 90))
    order = intent("AAA", Side.SELL, 5, order_type=OrderType.LIMIT, limit=105)
    [fill] = execution(md).execute([order], 1, p)
    assert fill.price == 105


def test_sells_settle_before_buys_so_the_proceeds_fund_them() -> None:
    md = data(AAA=(100, 100, 100, 100), BBB=(50, 50, 50, 50))
    p = Portfolio(0)
    p.apply(_buy("AAA", 10, 100))  # all cash now in AAA
    p.cash = 0
    buy = intent("BBB", Side.BUY, 20)
    sell = intent("AAA", Side.SELL, 10)
    fills = execution(md, costs=CostModel.free()).execute([buy, sell], 1, p)
    assert [f.symbol for f in fills] == ["AAA", "BBB"]
    assert p.quantity("BBB") == 20
    assert p.cash == pytest.approx(0)


def test_a_buy_is_cut_to_the_cash_available_and_whole_shares() -> None:
    md = data(AAA=(30, 30, 30, 30))
    p = Portfolio(100)
    order = intent("AAA", Side.BUY, 10)
    [fill] = execution(md, costs=CostModel.free()).execute([order], 1, p)
    assert fill.quantity == 3
    assert order.status is OrderStatus.PARTIAL
    assert p.cash == pytest.approx(10)


def test_no_cash_or_no_bar_expires_the_order() -> None:
    md = data(AAA=(30, 30, 30, 30), BBB=(np.nan, np.nan, np.nan, np.nan))
    broke = intent("AAA", Side.BUY, 1)
    missing = intent("BBB", Side.BUY, 1)
    fills = execution(md).execute([broke, missing], 1, Portfolio(5))
    assert fills == []
    assert broke.status is OrderStatus.EXPIRED
    assert missing.status is OrderStatus.EXPIRED
    assert missing.note == "no bar on the fill session"


def test_a_sell_never_exceeds_the_shares_held() -> None:
    md = data(AAA=(100, 100, 100, 100))
    p = Portfolio(1_000)
    p.apply(_buy("AAA", 3, 100))
    order = intent("AAA", Side.SELL, 5)
    [fill] = execution(md).execute([order], 1, p)
    assert fill.quantity == 3
    assert order.status is OrderStatus.PARTIAL
    assert "AAA" not in p.positions


def test_size_at_fill_lands_target_weight_orders_on_target() -> None:
    md = data(AAA=(120, 120, 120, 120))
    p = Portfolio(1_000)
    order = intent("AAA", Side.BUY, 5, target=0.6)  # sized at 100, but it opens at 120
    execution(md, size_at="fill", fractional=True, costs=CostModel.free()).execute([order], 1, p)
    assert p.quantity("AAA") * 120 == pytest.approx(600)


def _buy(symbol: str, qty: float, price: float) -> Fill:
    return Fill("seed", "s", symbol, Side.BUY, qty, price, 0.0, date(2024, 6, 28))
