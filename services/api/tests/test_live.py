"""Live prices: why the stream isn't live reaches the dashboard."""

import logging
from collections.abc import AsyncIterator

from tp_api.live import (
    CONNECTION_LIMIT,
    STREAM_LOGGER,
    AlpacaQuoteSource,
    LiveHub,
    Tick,
    _StreamErrors,
)


def test_alpaca_s_logged_connection_errors_become_the_feed_error() -> None:
    source = AlpacaQuoteSource("key", "secret")
    handler = _StreamErrors(source)
    logger = logging.getLogger(STREAM_LOGGER)
    logger.addHandler(handler)
    try:
        logger.info("starting data websocket connection")
        assert source.error is None
        logger.error("error during websocket communication: connection limit exceeded")
        assert source.error == CONNECTION_LIMIT
        logger.warning("data websocket error, restarting connection: timed out")
        assert source.error == "data websocket error, restarting connection: timed out"
    finally:
        logger.removeHandler(handler)


class Quiet:
    name = "alpaca"
    error: str | None = "connection limit exceeded"

    async def stream(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if False:  # pragma: no cover - never yields
            yield Tick("SPY", 1.0, None)  # type: ignore[arg-type]


def test_the_hub_reports_the_source_s_error() -> None:
    hub = LiveHub(Quiet(), ["SPY"], prev_close={"SPY": 500.0}, positions=[])
    snapshot = hub.snapshot()
    assert snapshot["feed"] == {"source": "alpaca", "error": "connection limit exceeded"}
    assert snapshot["quotes"][0]["live"] is False  # the last close stands in
    hub.error = "RuntimeError: stream stopped"
    assert hub.feed_error() == "RuntimeError: stream stopped"
