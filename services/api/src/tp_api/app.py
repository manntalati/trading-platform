"""FastAPI application: JSON endpoints + one live WebSocket, and the built dashboard if present."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tp_api import __version__, queries
from tp_api.live import (
    AlpacaQuoteSource,
    FakeQuoteSource,
    LiveHub,
    Position,
    QuoteSource,
)
from tp_core.config import MissingCredentialsError, Settings
from tp_core.portfolio import MARKET_KINDS, holdings_table, latest_snapshot

log = logging.getLogger(__name__)
DASHBOARD_DIST = Path(__file__).resolve().parents[4] / "apps" / "dashboard" / "dist"


class QuoteMode(StrEnum):
    alpaca = "alpaca"
    fake = "fake"
    off = "off"


def now_utc() -> datetime:
    return datetime.now(UTC)


def build_hub(settings: Settings, mode: QuoteMode, interval: float = 1.0) -> LiveHub | None:
    """Stream the watchlist plus every market-priced holding; value the portfolio live."""
    if mode is QuoteMode.off:
        return None
    ctx = queries.Context(settings)
    snap = latest_snapshot(ctx.lake)
    table = holdings_table(snap, ctx.classifier)
    held = table[table["kind"].isin(MARKET_KINDS)]
    symbols = list(dict.fromkeys([*held["symbol"], *ctx.universes.watchlist]))
    prices = ctx.prices(None)
    last = prices.ffill().iloc[-1] if not prices.empty else None
    prev_close: dict[str, float] = (
        {str(s): float(v) for s, v in last.items() if s in symbols and v == v}
        if last is not None
        else {}
    )
    positions = [
        Position(str(r["symbol"]), float(r["quantity"]), prev_close[str(r["symbol"])])
        for _, r in held.iterrows()
        if str(r["symbol"]) in prev_close
    ]
    live_value = sum(p.quantity * p.prev_close for p in positions)
    static_value = snap.total_value - live_value if not snap.empty else 0.0
    source: QuoteSource
    if mode is QuoteMode.fake:
        source = FakeQuoteSource(prev_close, interval=interval / 2)
    else:
        key, secret = settings.require_alpaca_keys()
        source = AlpacaQuoteSource(key, secret, feed=settings.quotes_feed)
    return LiveHub(
        source,
        symbols,
        prev_close=prev_close,
        positions=positions,
        static_value=static_value,
        interval=interval,
    )


def require_token(request: Request) -> None:
    token: str | None = request.app.state.token
    if token is None:
        return
    header = request.headers.get("authorization", "")
    if not secrets.compare_digest(header, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="missing or wrong dashboard token")


def get_context(request: Request) -> queries.Context:
    return queries.Context(request.app.state.settings)


Ctx = Annotated[queries.Context, Depends(get_context)]


def create_app(
    settings: Settings | None = None,
    *,
    quotes: QuoteMode = QuoteMode.fake,
    live_interval: float = 1.0,
    serve_dashboard: bool = True,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            hub = build_hub(settings, quotes, live_interval)
        except MissingCredentialsError as exc:
            log.warning("live quotes disabled: %s", exc)
            hub = None
        app.state.hub = hub
        if hub is not None:
            hub.start()
        yield
        if hub is not None:
            await hub.stop()

    app = FastAPI(title="trading-platform", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.token = (
        settings.dashboard_token.get_secret_value() if settings.dashboard_token else None
    )
    auth = [Depends(require_token)]

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/status", dependencies=auth)
    def status(c: Ctx, request: Request) -> dict[str, Any]:
        out = queries.status(c, now_utc())
        hub: LiveHub | None = request.app.state.hub
        out["live"] = (
            {"source": hub.source.name, "symbols": hub.symbols, "error": hub.error}
            if hub
            else {"source": "off", "symbols": [], "error": None}
        )
        return out

    @app.get("/api/bars/{symbol}", dependencies=auth)
    def bars(c: Ctx, symbol: str, days: Annotated[int, Query(ge=5, le=5000)] = 365) -> Any:
        try:
            return queries.bars_for(c, symbol, days)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/options/{underlying}", dependencies=auth)
    def options(c: Ctx, underlying: str) -> dict[str, Any]:
        return queries.option_summary(c, underlying)

    @app.get("/api/portfolio", dependencies=auth)
    def portfolio(c: Ctx) -> dict[str, Any]:
        return queries.portfolio(c)

    @app.get("/api/portfolio/performance", dependencies=auth)
    def performance(c: Ctx, days: Annotated[int, Query(ge=30, le=5000)] = 365) -> dict[str, Any]:
        return queries.performance(c, days)

    @app.get("/api/ideas", dependencies=auth)
    def ideas(c: Ctx) -> dict[str, Any]:
        return queries.ideas(c)

    @app.get("/api/strategies/ma-timing", dependencies=auth)
    def ma_timing(c: Ctx, universe: Annotated[str, Query(pattern="^(spy|gtaa)$")] = "spy") -> Any:
        return queries.ma_timing_view(c, universe)

    @app.websocket("/ws/live")
    async def live(websocket: WebSocket) -> None:
        token: str | None = websocket.app.state.token
        if token is not None and not secrets.compare_digest(
            websocket.query_params.get("token", ""), token
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub: LiveHub | None = websocket.app.state.hub
        if hub is None:
            await websocket.send_json({"type": "snapshot", "quotes": [], "portfolio": None,
                                       "feed": {"source": "off", "error": None}})  # fmt: skip
            await websocket.close()
            return
        queue = hub.subscribe()
        try:
            while True:
                message = await queue.get()
                await websocket.send_json(queries.clean(message))
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            hub.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await websocket.close()

    if serve_dashboard and DASHBOARD_DIST.exists():
        app.mount("/assets", StaticFiles(directory=DASHBOARD_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = DASHBOARD_DIST / path
            if path and candidate.is_file() and DASHBOARD_DIST in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(DASHBOARD_DIST / "index.html")

    return app
