"""A toy limit order book and matching engine (Phase 0, module 2).

What it models, in the order an exchange would apply it:

- **Price-time priority.** Better prices trade first; at the same price, earlier orders trade
  first (FIFO queue per price level).
- **Trades print at the resting (maker) order's price.** An aggressive buy limit at 101 against
  a resting ask at 100 fills at 100: the taker gets price improvement.
- **Partial fills.** An order fills against as many resting orders and levels as it needs; any
  remainder rests (GTC limit) or is cancelled (IOC, market).
- **Order types / time in force:**
    - ``GTC`` limit: match what crosses, rest the remainder.
    - ``IOC`` limit: match what crosses, cancel the remainder.
    - ``FOK`` limit: fill completely right now or not at all.
    - market: match at any price, cancel any remainder (never rests).

Prices are integer ticks (e.g. cents) so there is no floating-point equality anywhere.

Not modelled: stop orders, hidden/iceberg quantity, self-trade prevention, auctions, multiple
venues or fees. Those are good extensions to try.
"""

from __future__ import annotations

import bisect
import itertools
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class TimeInForce(StrEnum):
    GTC = "gtc"  # good till cancelled: rests on the book
    IOC = "ioc"  # immediate or cancel: partial fills allowed
    FOK = "fok"  # fill or kill: all or nothing, immediately


@dataclass
class Order:
    order_id: int
    side: Side
    qty: int
    price: int | None  # None = market order
    tif: TimeInForce
    seq: int  # arrival sequence number: the "time" in price-time priority
    remaining: int = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.qty

    @property
    def is_market(self) -> bool:
        return self.price is None


# -- events -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Accepted:
    order_id: int


@dataclass(frozen=True)
class Rejected:
    order_id: int
    reason: str


@dataclass(frozen=True)
class Fill:
    taker_id: int
    maker_id: int
    taker_side: Side
    price: int  # always the maker's price
    qty: int


@dataclass(frozen=True)
class Rested:
    order_id: int
    side: Side
    price: int
    qty: int


@dataclass(frozen=True)
class Cancelled:
    order_id: int
    qty: int  # quantity removed from consideration
    reason: str  # "ioc", "fok", "market", "user"


Event = Accepted | Rejected | Fill | Rested | Cancelled


# -- book ---------------------------------------------------------------------------------------


class _BookSide:
    """One side of the book: FIFO queues per price, plus a sorted list of active prices."""

    def __init__(self, side: Side) -> None:
        self.side = side
        self.levels: dict[int, deque[Order]] = {}
        self._prices: list[int] = []  # ascending

    def best(self) -> int | None:
        if not self._prices:
            return None
        return self._prices[-1] if self.side is Side.BUY else self._prices[0]

    def prices_best_first(self) -> Iterator[int]:
        return reversed(self._prices) if self.side is Side.BUY else iter(self._prices)

    def add(self, order: Order) -> None:
        assert order.price is not None
        if order.price not in self.levels:
            self.levels[order.price] = deque()
            bisect.insort(self._prices, order.price)
        self.levels[order.price].append(order)

    def remove(self, order: Order) -> None:
        assert order.price is not None
        queue = self.levels[order.price]
        queue.remove(order)
        if not queue:
            self._drop_level(order.price)

    def pop_front(self, price: int) -> None:
        queue = self.levels[price]
        queue.popleft()
        if not queue:
            self._drop_level(price)

    def volume(self, price: int) -> int:
        return sum(o.remaining for o in self.levels.get(price, ()))

    def _drop_level(self, price: int) -> None:
        del self.levels[price]
        self._prices.pop(bisect.bisect_left(self._prices, price))


class OrderBook:
    """A single-instrument limit order book.

    >>> book = OrderBook()
    >>> _ = book.submit(Side.SELL, 5, price=101)
    >>> events = book.submit(Side.BUY, 3, price=102)
    >>> [e for e in events if isinstance(e, Fill)]
    [Fill(taker_id=2, maker_id=1, taker_side=<Side.BUY: 'buy'>, price=101, qty=3)]
    >>> book.best_ask(), book.volume_at(Side.SELL, 101)
    (101, 2)
    """

    def __init__(self) -> None:
        self._sides = {Side.BUY: _BookSide(Side.BUY), Side.SELL: _BookSide(Side.SELL)}
        self._resting: dict[int, Order] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count()

    # -- queries --------------------------------------------------------------------------------

    def best_bid(self) -> int | None:
        return self._sides[Side.BUY].best()

    def best_ask(self) -> int | None:
        return self._sides[Side.SELL].best()

    def spread(self) -> int | None:
        bid, ask = self.best_bid(), self.best_ask()
        return None if bid is None or ask is None else ask - bid

    def volume_at(self, side: Side, price: int) -> int:
        return self._sides[side].volume(price)

    def depth(self, side: Side, levels: int = 5) -> list[tuple[int, int]]:
        """``[(price, total qty), ...]`` best price first."""
        book_side = self._sides[side]
        prices = itertools.islice(book_side.prices_best_first(), levels)
        return [(p, book_side.volume(p)) for p in prices]

    def queue(self, side: Side, price: int) -> list[int]:
        """Order ids resting at ``price``, front of the queue first."""
        return [o.order_id for o in self._sides[side].levels.get(price, ())]

    def resting(self, order_id: int) -> Order | None:
        return self._resting.get(order_id)

    def ladder(self, levels: int = 5) -> str:
        """Text view of the top of the book, asks above bids."""
        asks = self.depth(Side.SELL, levels)[::-1]
        bids = self.depth(Side.BUY, levels)
        rows = [f"{'':>8} {p:>8} {q:<8}" for p, q in asks]
        rows.append("-" * 26)
        rows += [f"{q:>8} {p:>8}" for p, q in bids]
        return "\n".join(rows)

    # -- commands -------------------------------------------------------------------------------

    def submit(
        self,
        side: Side,
        qty: int,
        price: int | None = None,
        tif: TimeInForce = TimeInForce.GTC,
    ) -> list[Event]:
        """Submit a new order. ``price=None`` is a market order. Returns what happened."""
        order = Order(next(self._ids), side, qty, price, tif, next(self._seq))
        reason = self._reject_reason(order)
        if reason:
            return [Rejected(order.order_id, reason)]
        events: list[Event] = [Accepted(order.order_id)]

        if order.tif is TimeInForce.FOK and self._fillable_qty(order) < order.qty:
            events.append(Cancelled(order.order_id, order.qty, "fok"))
            return events

        events += self._match(order)

        if order.remaining:
            if order.is_market:
                events.append(Cancelled(order.order_id, order.remaining, "market"))
            elif order.tif is TimeInForce.IOC:
                events.append(Cancelled(order.order_id, order.remaining, "ioc"))
            else:
                assert order.price is not None
                self._sides[side].add(order)
                self._resting[order.order_id] = order
                events.append(Rested(order.order_id, side, order.price, order.remaining))
        return events

    def cancel(self, order_id: int) -> list[Event]:
        order = self._resting.pop(order_id, None)
        if order is None:
            return [Rejected(order_id, "unknown or already done")]
        self._sides[order.side].remove(order)
        return [Cancelled(order_id, order.remaining, "user")]

    # -- internals ------------------------------------------------------------------------------

    @staticmethod
    def _reject_reason(order: Order) -> str | None:
        if order.qty <= 0:
            return "qty must be positive"
        if order.price is not None and order.price <= 0:
            return "price must be positive"
        return None  # note: a market order never rests, so GTC on one behaves like IOC

    def _crosses(self, order: Order, resting_price: int) -> bool:
        if order.price is None:
            return True
        if order.side is Side.BUY:
            return resting_price <= order.price
        return resting_price >= order.price

    def _fillable_qty(self, order: Order) -> int:
        """How much could fill right now at acceptable prices (for FOK)."""
        contra = self._sides[order.side.opposite]
        total = 0
        for price in contra.prices_best_first():
            if not self._crosses(order, price) or total >= order.qty:
                break
            total += contra.volume(price)
        return total

    def _match(self, order: Order) -> list[Event]:
        contra = self._sides[order.side.opposite]
        fills: list[Event] = []
        while order.remaining:
            price = contra.best()
            if price is None or not self._crosses(order, price):
                break
            maker = contra.levels[price][0]
            qty = min(order.remaining, maker.remaining)
            order.remaining -= qty
            maker.remaining -= qty
            fills.append(Fill(order.order_id, maker.order_id, order.side, price, qty))
            if maker.remaining == 0:
                contra.pop_front(price)
                del self._resting[maker.order_id]
        return fills
