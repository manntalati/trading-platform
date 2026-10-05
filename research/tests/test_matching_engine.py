import doctest

import pytest

from tp_research.phase0 import matching_engine
from tp_research.phase0.matching_engine import (
    Accepted,
    Cancelled,
    Event,
    Fill,
    OrderBook,
    Rejected,
    Rested,
    Side,
    TimeInForce,
)

BUY, SELL = Side.BUY, Side.SELL


def fills(events: list[Event]) -> list[tuple[int, int, int]]:
    """(maker_id, price, qty) for each fill."""
    return [(e.maker_id, e.price, e.qty) for e in events if isinstance(e, Fill)]


@pytest.fixture
def book() -> OrderBook:
    """Asks: 101 x [#1 5, #2 3], 102 x [#3 4]. Bids: 99 x [#4 2], 98 x [#5 6]."""
    b = OrderBook()
    b.submit(SELL, 5, 101)
    b.submit(SELL, 3, 101)
    b.submit(SELL, 4, 102)
    b.submit(BUY, 2, 99)
    b.submit(BUY, 6, 98)
    return b


def test_docstring_example() -> None:
    assert doctest.testmod(matching_engine).failed == 0


def test_resting_orders_build_the_book(book: OrderBook) -> None:
    assert (book.best_bid(), book.best_ask(), book.spread()) == (99, 101, 2)
    assert book.depth(SELL) == [(101, 8), (102, 4)]
    assert book.depth(BUY) == [(99, 2), (98, 6)]
    assert book.queue(SELL, 101) == [1, 2]


def test_non_crossing_limit_rests(book: OrderBook) -> None:
    events = book.submit(BUY, 1, 100)
    assert events == [Accepted(6), Rested(6, BUY, 100, 1)]
    assert book.best_bid() == 100


def test_time_priority_within_a_level(book: OrderBook) -> None:
    events = book.submit(BUY, 6, 101)
    assert fills(events) == [(1, 101, 5), (2, 101, 1)]  # #1 arrived first, fills first
    assert book.queue(SELL, 101) == [2]
    resting = book.resting(2)
    assert resting is not None
    assert resting.remaining == 2


def test_price_priority_and_sweep_with_price_improvement(book: OrderBook) -> None:
    events = book.submit(BUY, 10, 105)
    # Fills walk the book best price first and print at each maker's price, not at 105.
    assert fills(events) == [(1, 101, 5), (2, 101, 3), (3, 102, 2)]
    assert book.depth(SELL) == [(102, 2)]
    assert book.best_bid() == 99  # fully filled: nothing rested


def test_partial_fill_then_rest(book: OrderBook) -> None:
    events = book.submit(BUY, 12, 101)
    assert fills(events) == [(1, 101, 5), (2, 101, 3)]
    assert events[-1] == Rested(6, BUY, 101, 4)
    assert (book.best_bid(), book.best_ask()) == (101, 102)


def test_sell_side_mirrors(book: OrderBook) -> None:
    events = book.submit(SELL, 5, 98)
    assert fills(events) == [(4, 99, 2), (5, 98, 3)]
    assert book.depth(BUY) == [(98, 3)]


def test_market_order_sweeps_and_never_rests(book: OrderBook) -> None:
    events = book.submit(BUY, 20)
    assert fills(events) == [(1, 101, 5), (2, 101, 3), (3, 102, 4)]
    assert events[-1] == Cancelled(6, 8, "market")
    assert book.best_ask() is None


def test_market_order_on_empty_side_is_cancelled() -> None:
    events = OrderBook().submit(SELL, 5)
    assert events == [Accepted(1), Cancelled(1, 5, "market")]


def test_ioc_cancels_remainder(book: OrderBook) -> None:
    events = book.submit(BUY, 10, 101, TimeInForce.IOC)
    assert fills(events) == [(1, 101, 5), (2, 101, 3)]
    assert events[-1] == Cancelled(6, 2, "ioc")
    assert book.best_bid() == 99


def test_fok_all_or_nothing(book: OrderBook) -> None:
    killed = book.submit(BUY, 13, 102, TimeInForce.FOK)  # only 12 available up to 102
    assert killed == [Accepted(6), Cancelled(6, 13, "fok")]
    assert book.depth(SELL) == [(101, 8), (102, 4)]  # untouched

    filled = book.submit(BUY, 12, 102, TimeInForce.FOK)
    assert sum(q for _, _, q in fills(filled)) == 12
    assert book.best_ask() is None


def test_fok_respects_limit_price(book: OrderBook) -> None:
    assert book.submit(BUY, 9, 101, TimeInForce.FOK)[-1] == Cancelled(6, 9, "fok")


def test_cancel(book: OrderBook) -> None:
    assert book.cancel(1) == [Cancelled(1, 5, "user")]
    assert book.queue(SELL, 101) == [2]
    assert book.cancel(1) == [Rejected(1, "unknown or already done")]
    book.cancel(2)
    assert book.best_ask() == 102  # empty level removed


def test_filled_orders_cannot_be_cancelled(book: OrderBook) -> None:
    book.submit(BUY, 5, 101)
    assert isinstance(book.cancel(1)[0], Rejected)


@pytest.mark.parametrize(("qty", "price"), [(0, 100), (-1, 100), (1, 0), (1, -5)])
def test_rejects_bad_orders(qty: int, price: int) -> None:
    book = OrderBook()
    events = book.submit(BUY, qty, price)
    assert len(events) == 1
    assert isinstance(events[0], Rejected)
    assert book.best_bid() is None


def test_ladder_renders() -> None:
    book = OrderBook()
    book.submit(SELL, 3, 101)
    book.submit(BUY, 2, 99)
    text = book.ladder()
    assert text.index("101") < text.index("99")
