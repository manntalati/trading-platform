"""Property tests: random order flow must never break the book's invariants."""

from collections import defaultdict
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from tp_research.phase0.matching_engine import (
    Cancelled,
    Event,
    Fill,
    OrderBook,
    Rejected,
    Rested,
    Side,
    TimeInForce,
)

submits = st.tuples(
    st.just("submit"),
    st.sampled_from(Side),
    st.integers(min_value=1, max_value=20),  # qty
    st.one_of(st.none(), st.integers(min_value=95, max_value=105)),  # price (None = market)
    st.sampled_from(TimeInForce),
)
cancels = st.tuples(st.just("cancel"), st.integers(min_value=1, max_value=60))
operations = st.lists(st.one_of(submits, submits, submits, cancels), max_size=60)


@settings(max_examples=300, deadline=None)
@given(operations)
def test_invariants_hold_under_random_flow(ops: list[tuple]) -> None:  # type: ignore[type-arg]
    book = OrderBook()
    original: dict[int, int] = {}
    limit: dict[int, tuple[Side, int | None]] = {}
    filled: dict[int, int] = defaultdict(int)
    cancelled: dict[int, int] = defaultdict(int)

    for op in ops:
        if op[0] == "submit":
            _, side, qty, price, tif = op
            events: list[Event] = book.submit(side, qty, price, tif)
            order_id = events[0].order_id
            original[order_id] = qty
            limit[order_id] = (side, price)
            _check_submit_events(book, events, side, qty, price, tif)
        else:
            events = book.cancel(op[1])

        for e in events:
            if isinstance(e, Fill):
                filled[e.taker_id] += e.qty
                filled[e.maker_id] += e.qty
            elif isinstance(e, Cancelled):
                cancelled[e.order_id] += e.qty

        # 1. Never crossed.
        bid, ask = book.best_bid(), book.best_ask()
        assert bid is None or ask is None or bid < ask
        # 2. No empty or negative levels.
        for side in Side:
            assert all(q > 0 for _, q in book.depth(side, levels=100))

    # 3. Every share of every order is accounted for exactly once.
    for order_id, qty in original.items():
        resting = book.resting(order_id)
        open_qty = resting.remaining if resting else 0
        assert filled[order_id] + cancelled[order_id] + open_qty == qty, order_id


def _check_submit_events(
    book: OrderBook,
    events: list[Event],
    side: Side,
    qty: int,
    price: int | None,
    tif: TimeInForce,
) -> None:
    fills = [e for e in events if isinstance(e, Fill)]
    # Fill prices respect the taker's limit and never get worse as the order walks the book.
    for f in fills:
        if price is not None:
            assert f.price <= price if side is Side.BUY else f.price >= price
    ordered = [f.price for f in fills]
    assert ordered == sorted(ordered, reverse=side is Side.SELL)
    # Time priority: at one price, makers fill in arrival (= id) order.
    for a, b in pairwise(fills):
        if a.price == b.price:
            assert a.maker_id < b.maker_id
    total = sum(f.qty for f in fills)
    if tif is TimeInForce.FOK:
        assert total in (0, qty)
    # Only GTC limits may rest, and only after taking everything they could.
    rested = [e for e in events if isinstance(e, Rested)]
    if rested:
        assert price is not None
        assert tif is TimeInForce.GTC
        contra_best = book.best_ask() if side is Side.BUY else book.best_bid()
        assert contra_best is None or (
            contra_best > price if side is Side.BUY else contra_best < price
        )
    assert not any(isinstance(e, Rejected) for e in events)
