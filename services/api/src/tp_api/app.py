"""FastAPI application: JSON endpoints + one live WebSocket, and the built dashboard if present."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tp_api import __version__, broker, fills, paper, positions, queries, research, strategies
from tp_api.broker import BrokerSync
from tp_api.claude import ClaudeAdvisor
from tp_api.dashboard import bundle_state
from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_api.live import (
    AlpacaQuoteSource,
    FakeQuoteSource,
    LiveHub,
    Position,
    QuoteSource,
)
from tp_api.positions import AlpacaMarketData, MarketData
from tp_api.scheduler import Scheduler, start_in_background
from tp_core.calendar import NEW_YORK
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
    # Option underlyings too: their price shows how far each contract is from its strike.
    underlyings = table[table["kind"] == "option"]["underlying"].dropna()
    symbols = list(dict.fromkeys([*held["symbol"], *underlyings, *ctx.universes.watchlist]))
    now = now_utc()
    today = now.astimezone(NEW_YORK).date()
    prev_close = ctx.last_closes(symbols, before=today)
    positions, static_value, as_of = hub_portfolio(ctx, prev_close)
    source: QuoteSource
    if mode is QuoteMode.fake:
        source = FakeQuoteSource(prev_close, interval=interval / 2)
    else:
        key, secret = settings.require_alpaca_keys()
        source = AlpacaQuoteSource(key, secret, feed=settings.quotes_feed)
    hub = LiveHub(
        source,
        symbols,
        prev_close=prev_close,
        positions=positions,
        static_value=static_value,
        interval=interval,
    )
    hub.portfolio_as_of = as_of
    hub.closes_day, hub.closes_at = today, now
    return hub


def build_market_data(settings: Settings, mode: QuoteMode) -> MarketData | None:
    """Option quotes and stock prices fetched on demand (with live quotes from Alpaca only:
    the demo modes stay offline)."""
    if mode is not QuoteMode.alpaca:
        return None
    try:
        return AlpacaMarketData.from_settings(settings)
    except MissingCredentialsError:
        return None


def hub_portfolio(
    ctx: queries.Context, prev_close: dict[str, float]
) -> tuple[list[Position], float, object]:
    """The holdings with a market price, to be valued live, and the rest of the account's value
    (cash, options, anything unpriced), held as the broker had it; and which snapshot this is.

    Each holding moves from the price the broker valued it at in that snapshot, not from a close
    in the lake: those are different days whenever the bars or the server's start are older
    than the sync, and the gap would be counted in the value (twice, for each day it lasted)."""
    snap = latest_snapshot(ctx.lake)
    if snap.empty:
        return [], 0.0, None
    table = holdings_table(snap, ctx.classifier)
    held = table[table["kind"].isin(MARKET_KINDS) & table["price"].notna()]
    positions = [
        Position(str(r["symbol"]), float(r["quantity"]), float(r["price"]),
                 prev_close.get(str(r["symbol"])))
        for _, r in held.iterrows()
        if float(r["quantity"])
    ]  # fmt: skip
    marked = sum(p.quantity * p.mark for p in positions)
    return positions, snap.total_value - marked, snap.accounts["taken_at"].max()


CLOSES_TTL = timedelta(hours=1)  # re-read the reference closes this often (the bars may land late)


def update_hub_portfolio(app: FastAPI, now: datetime | None = None) -> None:
    """Re-read the positions if a sync stored a newer snapshot (from the dashboard or cron), and
    the reference closes when the day turns (or the last read is an hour old): a server left
    running for days otherwise measures "today" from the day it started."""
    hub: LiveHub | None = app.state.hub
    if hub is None:
        return
    ctx = queries.Context(app.state.settings)
    now = now or now_utc()
    today = now.astimezone(NEW_YORK).date()
    if hub.closes_day != today or hub.closes_at is None or now - hub.closes_at >= CLOSES_TTL:
        symbols = [*hub.symbols, *(p.symbol for p in hub.positions)]
        hub.set_closes(ctx.last_closes(symbols, before=today), today, now)
    snap_at = latest_snapshot(ctx.lake).accounts["taken_at"].max()
    if hub.portfolio_as_of is not None and snap_at == hub.portfolio_as_of:
        return
    positions, static_value, as_of = hub_portfolio(ctx, hub.prev_close)
    new = [p.symbol for p in positions if p.prev_close is None]
    if new:  # bought since the closes were read: theirs too
        hub.prev_close.update(ctx.last_closes(new, before=today))
        for p in positions:
            p.prev_close = hub.prev_close.get(p.symbol)
    hub.set_portfolio(positions, static_value, as_of)


def create_app(
    settings: Settings | None = None,
    *,
    quotes: QuoteMode = QuoteMode.fake,
    live_interval: float = 1.0,
    serve_dashboard: bool = True,
    jobs: bool = False,
) -> FastAPI:
    settings = settings or Settings()
    scheduler = Scheduler(settings)

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
        stop = start_in_background(scheduler)[1] if jobs else None
        yield
        if stop is not None:
            stop.set()
        if hub is not None:
            await hub.stop()

    app = FastAPI(title="trading-platform", version=__version__, lifespan=lifespan)
    hosts = [h.strip() for h in settings.dashboard_hosts.split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)  # DNS-rebinding guard
    app.state.settings = settings
    app.state.scheduler = scheduler
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
        out["dashboard"] = bundle_state().to_dict()
        hub: LiveHub | None = request.app.state.hub
        out["live"] = (
            {"source": hub.source.name, "symbols": hub.symbols, "error": hub.error}
            if hub
            else {"source": "off", "symbols": [], "error": None}
        )
        return out

    @app.get("/api/system/data", dependencies=auth)
    def data(c: Ctx) -> dict[str, Any]:
        return queries.data_map(c)

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
    def portfolio(c: Ctx, request: Request) -> dict[str, Any]:
        update_hub_portfolio(request.app)  # a sync since the last look: value the new positions
        return queries.portfolio(c, now_utc())

    @app.get("/api/trades", dependencies=auth)
    def trades(
        c: Ctx,
        source: Annotated[str, Query(pattern="^(all|mine|paper)$")] = "all",
        limit: Annotated[int, Query(ge=1, le=2000)] = 300,
    ) -> dict[str, Any]:
        return queries.trades(c, source, limit)

    @app.get("/api/trades/stats", dependencies=auth)
    def trade_stats(c: Ctx) -> dict[str, Any]:
        return queries.trade_stats(c)

    @app.get("/api/portfolio/performance", dependencies=auth)
    def performance(c: Ctx, days: Annotated[int, Query(ge=30, le=5000)] = 365) -> dict[str, Any]:
        return queries.performance(c, days)

    @app.get("/api/jobs", dependencies=auth)
    def job_status() -> dict[str, Any]:
        return scheduler.status()

    @app.post(
        "/api/jobs/{name}/run",
        dependencies=[*auth, Depends(require_dashboard_client)],
        status_code=202,
    )
    def run_job(name: str) -> dict[str, Any]:
        if not scheduler.acquire():
            raise HTTPException(
                409, f"the scheduler runs in another process: `uv run tp-jobs now {name}` there"
            )
        try:
            scheduler.request(name)
        except KeyError:
            raise HTTPException(404, f"no job {name!r}") from None
        if not jobs:  # no background loop here: run it now, then let go of the lock

            def once() -> None:
                try:
                    scheduler.step()
                finally:
                    scheduler.release()

            threading.Thread(target=once, name="job-now", daemon=True).start()
        return scheduler.status()

    @app.get("/api/models", dependencies=auth)
    def models(c: Ctx) -> dict[str, Any]:
        return queries.models(c)

    @app.get("/api/ideas", dependencies=auth)
    def ideas(c: Ctx) -> dict[str, Any]:
        return queries.ideas(c)

    @app.get("/api/strategies/ma-timing", dependencies=auth)
    def ma_timing(c: Ctx, universe: Annotated[str, Query(pattern="^(spy|gtaa)$")] = "spy") -> Any:
        return queries.ma_timing_view(c, universe)

    app.include_router(paper.router)
    app.state.market = build_market_data(settings, quotes)
    app.state.advisor = ClaudeAdvisor(settings)
    app.include_router(positions.router)
    app.state.research_cache = research.ResearchCache()
    app.state.ticker_fetch = research.TickerFetch(settings, on_done=app.state.research_cache.clear)
    app.include_router(research.router)
    app.state.validation_job = strategies.ValidationJob(settings)
    app.state.regime_cache = {}
    app.include_router(strategies.router)
    app.state.broker_sync = BrokerSync(settings, on_done=lambda: update_hub_portfolio(app))
    app.include_router(broker.router)
    app.include_router(fills.router)

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
