"""Live prices: one upstream quote stream, fanned out to every open dashboard.

    quote source (Alpaca IEX websocket, or a fake random walk)
        -> LiveHub (latest price per symbol, live portfolio valuation)
            -> every connected browser (JSON over WebSocket, batched every ``interval``)

The hub is the only consumer of the upstream feed, so opening more browser tabs never opens more
broker connections (Alpaca's free plan allows one stream and 30 symbols).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)
MAX_STREAM_SYMBOLS = 30


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    at: datetime


class QuoteSource(Protocol):
    name: str

    def stream(self, symbols: list[str]) -> AsyncIterator[Tick]: ...


class FakeQuoteSource:
    """Random-walk ticks around a starting price, so the dashboard works without keys and on
    weekends."""

    name = "fake"

    def __init__(
        self, start_prices: Mapping[str, float], *, interval: float = 1.0, seed: int = 7
    ) -> None:
        self.start = dict(start_prices)
        self.interval = interval
        self.rng = random.Random(seed)

    async def stream(self, symbols: list[str]) -> AsyncIterator[Tick]:
        prices = {s: self.start.get(s, 100.0) for s in symbols}
        while True:
            for symbol in symbols:
                prices[symbol] *= math.exp(self.rng.gauss(0.0, 0.0008))
                yield Tick(symbol, round(prices[symbol], 4), datetime.now(UTC))
            await asyncio.sleep(self.interval)


class AlpacaQuoteSource:
    """Alpaca's real-time trade stream (IEX on the free plan).

    alpaca-py's stream owns its own event loop, so it runs in a thread and hands ticks to ours
    through ``call_soon_threadsafe``.
    """

    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex") -> None:
        self._keys = (api_key, secret_key)
        self._feed = feed

    async def stream(self, symbols: list[str]) -> AsyncIterator[Tick]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live.stock import StockDataStream

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=10_000)
        stream = StockDataStream(*self._keys, feed=DataFeed(self._feed), raw_data=True)

        async def on_trade(trade: dict[str, Any]) -> None:
            tick = Tick(str(trade["S"]), float(trade["p"]), _parse_time(trade.get("t")))
            loop.call_soon_threadsafe(_put_nowait, queue, tick)

        stream.subscribe_trades(on_trade, *symbols)  # type: ignore[arg-type]  # raw_data dicts
        thread = threading.Thread(target=stream.run, name="alpaca-stream", daemon=True)
        thread.start()
        try:
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(Exception):
                stream.stop()


def _put_nowait(queue: asyncio.Queue[Tick], tick: Tick) -> None:
    with contextlib.suppress(asyncio.QueueFull):  # a stalled consumer drops ticks, never blocks
        queue.put_nowait(tick)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


@dataclass
class Position:
    symbol: str
    quantity: float
    prev_close: float  # last close in the lake: the reference for today's P&L


class LiveHub:
    """Keeps the latest price per symbol and broadcasts batched updates to subscribers."""

    def __init__(
        self,
        source: QuoteSource,
        symbols: list[str],
        *,
        prev_close: Mapping[str, float],
        positions: list[Position],
        static_value: float = 0.0,
        interval: float = 1.0,
    ) -> None:
        self.source = source
        self.symbols = symbols[:MAX_STREAM_SYMBOLS]
        self.prev_close = dict(prev_close)
        self.positions = positions
        self.static_value = static_value  # cash + holdings without a live price
        self.interval = interval
        self.latest: dict[str, Tick] = {}
        self._dirty: set[str] = set()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self.error: str | None = None
        self.portfolio_as_of: object = None  # the snapshot the positions come from

    def set_portfolio(self, positions: list[Position], static_value: float, as_of: object) -> None:
        """New positions after a brokerage sync (symbols already streamed stay live; others are
        valued at the broker's price until the next restart)."""
        self.positions = positions
        self.static_value = static_value
        self.portfolio_as_of = as_of
        self._dirty |= {p.symbol for p in positions}  # push the new total on the next tick

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume(), name="hub-consume"),
            asyncio.create_task(self._broadcast(), name="hub-broadcast"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _consume(self) -> None:
        if not self.symbols:
            return
        try:
            async for tick in self.source.stream(self.symbols):
                self.latest[tick.symbol] = tick
                self._dirty.add(tick.symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface feed problems in the UI instead of dying silently
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("quote stream stopped")

    async def _broadcast(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if not self._dirty or not self._subscribers:
                continue
            changed, self._dirty = self._dirty, set()
            message = self.message(changed)
            for queue in list(self._subscribers):
                _offer(queue, message)

    # -- subscribers ------------------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        _offer(queue, self.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    # -- messages ---------------------------------------------------------------------------------

    def quote(self, symbol: str) -> dict[str, Any]:
        tick = self.latest.get(symbol)
        prev = self.prev_close.get(symbol)
        price = tick.price if tick else prev
        change = (price / prev - 1) if price and prev else None
        return {
            "symbol": symbol,
            "price": price,
            "prev_close": prev,
            "change_pct": change,
            "at": tick.at.isoformat() if tick else None,
            "live": tick is not None,
        }

    def portfolio_value(self) -> dict[str, Any]:
        value = self.static_value
        day_pnl = 0.0
        for p in self.positions:
            tick = self.latest.get(p.symbol)
            price = tick.price if tick else p.prev_close
            value += p.quantity * price
            day_pnl += p.quantity * (price - p.prev_close)
        base = value - day_pnl
        return {
            "value": value,
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl / base if base else None,
        }

    def message(self, symbols: set[str]) -> dict[str, Any]:
        return {
            "type": "update",
            "at": datetime.now(UTC).isoformat(),
            "quotes": [self.quote(s) for s in sorted(symbols)],
            "portfolio": self.portfolio_value() if self.positions else None,
            "feed": {"source": self.source.name, "error": self.error},
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.message(set(self.symbols)), "type": "snapshot"}


def _offer(queue: asyncio.Queue[dict[str, Any]], message: dict[str, Any]) -> None:
    """Slow clients lose old updates rather than slowing everyone down."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(message)


HubFactory = Callable[[], LiveHub]
