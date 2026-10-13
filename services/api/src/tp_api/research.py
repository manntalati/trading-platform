"""Options research: swing, LEAPS and bearish ideas; one symbol in depth; fetching a new ticker;
and Claude to pressure-test an idea.

``GET /api/research`` ranks every eligible name with daily bars (your research list, the options
universe, your holdings and the bars universe) and picks contracts from the latest chains (see
``tp_strategies.options_research``). It's cached for a few minutes: the inputs change daily.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tp_api import queries
from tp_api.claude import AskRequest, ask_claude, rounded
from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_core.calendar import NEW_YORK
from tp_core.config import MissingCredentialsError, Settings, load_universes
from tp_core.pnl import option_label
from tp_core.portfolio import CASH_KIND, held_symbols, holdings_table, latest_snapshot
from tp_core.storage import Lake
from tp_core.volatility import iv_history, latest_chains
from tp_strategies.options_research import (
    CHAIN_MAX_AGE_DAYS,
    DISCLAIMER,
    analyze,
    closes_from_lake,
    scan,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", dependencies=[Depends(require_token)])
CACHE_SECONDS = 300.0
FETCH_YEARS = 2


def now_utc() -> datetime:
    return datetime.now(UTC)


def research_symbols(ctx: queries.Context) -> list[str]:
    u = ctx.universes
    return list(
        dict.fromkeys([*u.research, *u.options_underlyings, *held_symbols(ctx.lake), *u.bars])
    )


def held_summary(ctx: queries.Context) -> dict[str, str]:
    """What you hold per underlying, in words ("40 shares, 2 calls")."""
    snap = latest_snapshot(ctx.lake)
    if snap.empty:
        return {}
    table = holdings_table(snap, ctx.classifier)
    out: dict[str, list[str]] = {}
    for _, r in table[table["kind"] != CASH_KIND].iterrows():
        underlying = str(r["underlying"])
        qty = float(r["quantity"])
        if r["kind"] == "option":
            label = option_label(str(r["symbol"])) or str(r["symbol"])
            out.setdefault(underlying, []).append(f"{qty:g} x {label}")
        else:
            out.setdefault(underlying, []).append(f"{qty:g} shares")
    return {k: ", ".join(v) for k, v in out.items()}


def research_view(ctx: queries.Context, now: datetime) -> dict[str, Any]:
    today = now.astimezone(NEW_YORK).date()
    chains = latest_chains(ctx.lake, since=today - timedelta(days=CHAIN_MAX_AGE_DAYS))
    report = scan(
        ctx.lake,
        research_symbols(ctx),
        classifier=ctx.classifier,
        chains=chains,
        now=now,
        rate=ctx.settings.risk_free_rate,
        held=held_summary(ctx),
    )
    out: dict[str, Any] = queries.clean(report)
    return out


def symbol_view(ctx: queries.Context, symbol: str, now: datetime) -> dict[str, Any]:
    """One symbol in depth, whether or not it ranks: the stock, its options' pricing and the
    contract each approach would use. Chains of any age are used (and dated)."""
    today = now.astimezone(NEW_YORK).date()
    closes = closes_from_lake(ctx.lake, [symbol, "SPY"], today)
    if symbol not in closes:
        raise HTTPException(404, f"no daily bars for {symbol}: fetch it first")
    chain = latest_chains(ctx.lake, [symbol]).get(symbol)
    history = iv_history(ctx.lake)
    mine = history[history["underlying"] == symbol].set_index("snapshot_date")["iv30"]
    analysis = analyze(
        symbol,
        closes[symbol],
        chain=chain,
        benchmark=closes.get("SPY"),
        iv_hist=mine,
        now=now,
        rate=ctx.settings.risk_free_rate,
    )
    age = None
    if chain is not None and not chain.empty:
        age = (today - chain["snapshot_at"].max().tz_convert(NEW_YORK).date()).days
    analysis |= {
        "chain_age_days": age,
        "held": held_summary(ctx).get(symbol),
        "ask": {
            "available": ctx.settings.anthropic_api_key is not None,
            "model": ctx.settings.ask_model,
        },
    }
    out: dict[str, Any] = queries.clean(analysis | {"disclaimer": DISCLAIMER})
    return out


# -- fetching a ticker that isn't in the lake yet -------------------------------------------------


class FetchRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9.]{0,9}$")


class TickerFetch:
    """Backfill two years of bars and store today's option chain for one symbol, in the
    background (a minute or so), one at a time."""

    def __init__(self, settings: Settings, on_done: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.on_done = on_done
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False}

    def source(self) -> Any:
        from tp_ingest.sources.alpaca import AlpacaSource

        return AlpacaSource.from_settings(self.settings)

    def start(self, symbol: str) -> bool:
        with self._lock:
            if self.state.get("running"):
                return False
            source = self.source()  # raises without keys, before starting
            self.state = {"running": True, "symbol": symbol, "started_at": now_utc().isoformat()}
        threading.Thread(
            target=self._run, args=(source, symbol), name="ticker-fetch", daemon=True
        ).start()
        return True

    def _run(self, source: Any, symbol: str) -> None:
        from tp_ingest.jobs.bars import run_backfill
        from tp_ingest.jobs.options import run_snapshot

        lake = Lake(self.settings.data_root)
        now = now_utc()
        try:
            bars = run_backfill(lake, source, [symbol], now=now, years=FETCH_YEARS)
            try:
                max_dte = load_universes(self.settings.universes_file).options_max_dte
            except (OSError, ValueError):
                max_dte = 800
            chain = run_snapshot(lake, source, [symbol], now=now, max_dte=max_dte)
            got = bars.bars_fetched
            if not got:
                outcome = {
                    "ok": False,
                    "message": f"no daily bars for {symbol}: is the symbol right?",
                }
            else:
                outcome = {"ok": True, "message": f"{got} daily bars; options: {chain.summary()}"}
        except Exception as exc:  # shown on the page
            log.exception("fetching %s failed", symbol)
            outcome = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if self.on_done is not None:
            self.on_done()
        with self._lock:
            self.state = self.state | outcome | {"running": False,
                                                  "finished_at": now_utc().isoformat()}  # fmt: skip


# -- endpoints ------------------------------------------------------------------------------------


class ResearchCache:
    def __init__(self) -> None:
        self.at = 0.0
        self.value: dict[str, Any] | None = None
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.value = None


def _cache(request: Request) -> ResearchCache:
    cache: ResearchCache = request.app.state.research_cache
    return cache


@router.get("")
def research(c: Ctx, request: Request, refresh: bool = False) -> dict[str, Any]:
    cache = _cache(request)
    with cache.lock:
        if not refresh and cache.value is not None and time.monotonic() - cache.at < CACHE_SECONDS:
            return cache.value
    value = research_view(c, now_utc())
    with cache.lock:
        cache.value, cache.at = value, time.monotonic()
    return value


@router.get("/symbol/{symbol}")
def research_symbol(c: Ctx, symbol: str) -> dict[str, Any]:
    return symbol_view(c, symbol.upper(), now_utc())


@router.get("/fetch")
def fetch_status(request: Request) -> dict[str, Any]:
    fetcher: TickerFetch = request.app.state.ticker_fetch
    return dict(fetcher.state)


@router.post("/fetch", dependencies=[Depends(require_dashboard_client)], status_code=202)
def fetch(request: Request, body: FetchRequest) -> dict[str, Any]:
    fetcher: TickerFetch = request.app.state.ticker_fetch
    try:
        started = fetcher.start(body.symbol.upper())
    except MissingCredentialsError as exc:
        raise HTTPException(503, f"market data not configured: {exc}") from exc
    if not started:
        raise HTTPException(409, "a fetch is already running")
    return dict(fetcher.state)


RESEARCH_INSTRUCTIONS = """\
You are a candid, numerate options trading assistant inside the owner's personal trading \
dashboard. They trade swings (weeks to a few months) and LEAPS. Below is what the dashboard's \
rules found for one stock: its trend and volatility, how its options are priced (implied \
volatility, its rank, implied vs realized, any event the term structure shows), the contracts \
the rules picked (priced at the ask, with breakeven, odds and greeks), and the owner's portfolio.

How to answer:
- Give a straight view on the setup and the question asked, using the numbers. Lead with the \
bottom line.
- Use web search for what the numbers can't show: the next earnings date, recent news, and \
anything scheduled before the expiry. Say what you found and when it was published.
- Compare structures where it matters (shares, long call, debit spread, LEAPS): cost, \
breakeven, what a volatility drop does, and how each behaves if the stock goes nowhere.
- Propose a concrete plan: entry, a profit target, an exit if wrong (price or time), and a size \
that fits the portfolio (a swing risking more than 1-2% of the account is large).
- Say what would invalidate the idea. If the setup is weak, say so.
- Plain text, short paragraphs or bullets. Under about 300 words unless asked for more.
- You can't place trades; the owner decides. One short line of caveat at most.
"""


def research_prompt(
    analysis: dict[str, Any], portfolio: list[dict[str, Any]], now: datetime
) -> str:
    context = {
        "now": now.astimezone(NEW_YORK).strftime("%A %Y-%m-%d %H:%M %Z"),
        "portfolio": portfolio,
    }
    return "\n".join(
        [
            RESEARCH_INSTRUCTIONS,
            "<idea>",
            json.dumps(rounded(_without_scenarios(analysis))),
            "</idea>",
            "<portfolio>",
            json.dumps(rounded(context)),
            "</portfolio>",
        ]
    )


def _without_scenarios(value: Any) -> Any:
    """The scenario grids are long and the model can price moves itself."""
    if isinstance(value, dict):
        return {k: _without_scenarios(v) for k, v in value.items() if k != "scenarios"}
    if isinstance(value, list):
        return [_without_scenarios(v) for v in value]
    return value


def portfolio_brief(ctx: queries.Context) -> list[dict[str, Any]]:
    snap = latest_snapshot(ctx.lake)
    if snap.empty:
        return []
    table = holdings_table(snap, ctx.classifier)
    cols = ["symbol", "kind", "quantity", "market_value", "weight", "unrealized_pct"]
    return queries.records(table[cols])


@router.post("/ask", dependencies=[Depends(require_dashboard_client)])
def ask(c: Ctx, request: Request, body: AskRequest) -> dict[str, Any]:
    if not body.symbol:
        raise HTTPException(422, "which symbol?")
    now = now_utc()
    analysis = symbol_view(c, body.symbol.upper(), now)
    return ask_claude(request, research_prompt(analysis, portfolio_brief(c), now), body, now)
