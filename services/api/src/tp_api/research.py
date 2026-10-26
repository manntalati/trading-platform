"""Options research: swing, LEAPS and bearish ideas; one symbol in depth; fetching a new ticker;
and Claude to pressure-test an idea.

``GET /api/research`` serves the latest stored run of the recommendations (see
``tp_strategies.recommendations``): every eligible name with daily bars (your research list, the
options universe, your holdings and the bars universe) ranked, with contracts picked from the
latest chains (``tp_strategies.options_research``). The job scheduler makes a new run whenever new
data lands; if the stored run is older than the data (the scheduler is off), the request makes
one. ``GET /api/research/track-record`` shows how past recommendations have done.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tp_api import queries
from tp_api.claude import AskRequest, ask_claude, rounded
from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_core.calendar import NEW_YORK
from tp_core.config import MissingCredentialsError, Settings, load_universes
from tp_core.fundamentals import load_events, load_facts
from tp_core.fundamentals import summary as fundamentals_summary
from tp_core.portfolio import holdings_table, latest_snapshot
from tp_core.storage import Lake
from tp_core.volatility import iv_history, latest_chains
from tp_strategies import playbook
from tp_strategies import recommendations as recs
from tp_strategies.budget import budget_from_settings
from tp_strategies.options_research import DISCLAIMER, bars_from_lake

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", dependencies=[Depends(require_token)])
CACHE_SECONDS = 30.0  # the stored run is re-read at most this often
FETCH_YEARS = 2


def now_utc() -> datetime:
    return datetime.now(UTC)


def held_summary(ctx: queries.Context) -> dict[str, str]:
    """What you hold per underlying, in words ("40 shares, 2 calls")."""
    return recs.held_summary(ctx.lake, ctx.classifier)


def new_run(ctx: queries.Context, now: datetime) -> dict[str, Any]:
    """Make and store a run now."""
    result = recs.run_recommendations(
        ctx.lake,
        universes=ctx.universes,
        classifier=ctx.classifier,
        now=now,
        default_rate=ctx.settings.risk_free_rate,
        budget=budget_from_settings(ctx.lake, ctx.settings),
    )
    return recs.load_run(result.path)


def research_view(ctx: queries.Context, now: datetime, *, fresh: bool = False) -> dict[str, Any]:
    """The latest stored run's ideas, with when it was made and what changed; a new run first when
    asked, when there is none, or when the data has moved on since."""
    run = None if fresh else recs.latest_run(ctx.lake)
    if run is None or run.get("inputs") != recs.input_stamp(ctx.lake):
        run = new_run(ctx, now)
    meta = {k: run.get(k) for k in ("run_id", "as_of", "bars_session", "previous_run", "dropped")}
    out: dict[str, Any] = queries.clean({**run["research"], "run": meta})
    return out


def symbol_view(ctx: queries.Context, symbol: str, now: datetime) -> dict[str, Any]:
    """One symbol in depth, whether or not it ranks: the stock, its options' pricing and the
    contract each approach would use. Chains of any age are used (and dated)."""
    today = now.astimezone(NEW_YORK).date()
    bars = bars_from_lake(ctx.lake, [symbol, "SPY"], today)
    closes = {s: b["close"] for s, b in bars.items()}
    if symbol not in closes:
        raise HTTPException(404, f"no daily bars for {symbol}: fetch it first")
    chain = latest_chains(ctx.lake, [symbol]).get(symbol)
    history = iv_history(ctx.lake)
    mine = history[history["underlying"] == symbol].set_index("snapshot_date")["iv30"]
    name = playbook.prepare(
        symbol,
        bars[symbol],
        chain=chain,
        benchmark=closes.get("SPY"),
        iv_hist=mine,
        now=now,
        rate=ctx.risk_free_rate(),
        earnings=playbook.earnings_by_symbol(ctx.lake).get(symbol),
    )
    analysis = name.analysis
    analysis["playbook"] = playbook.for_symbol(
        name, playbook.load_calibration(ctx.lake), budget_from_settings(ctx.lake, ctx.settings)
    )
    age = None
    if chain is not None and not chain.empty:
        age = (today - chain["snapshot_at"].max().tz_convert(NEW_YORK).date()).days
    facts = load_facts(ctx.lake, [symbol])
    analysis |= {
        "fundamentals": (
            fundamentals_summary(
                facts, load_events(ctx.lake, [symbol]), symbol, analysis.get("spot"), today
            )
            if len(facts)
            else None
        ),
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
    value = research_view(c, now_utc(), fresh=refresh)
    with cache.lock:
        cache.value, cache.at = value, time.monotonic()
    return value


@router.post("/run", dependencies=[Depends(require_dashboard_client)])
def run_now(c: Ctx, request: Request) -> dict[str, Any]:
    """Scan and store a new run now (a minute or less), and return it."""
    value = research_view(c, now_utc(), fresh=True)
    cache = _cache(request)
    with cache.lock:
        cache.value, cache.at = value, time.monotonic()
    return value


@router.get("/track-record")
def track_record(c: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = queries.clean(
        recs.track_record(c.lake, now_utc().astimezone(NEW_YORK).date())
    )
    return out


@router.get("/calibration")
def calibration(c: Ctx) -> dict[str, Any]:
    """The playbook backtest's per-setup results (``tp-recs calibrate``); empty before its first
    run."""
    found = playbook.load_calibration(c.lake)
    out: dict[str, Any] = queries.clean(found or {"setups": {}})
    return out


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
volatility, its rank, implied vs realized, the smile's 25-delta skew, the term structure's \
slope and any event it shows), what the company's SEC filings say (trailing EPS, P/E, book to \
market, the last earnings surprise, the last results date and an estimate of the next), the \
contracts the rules picked (bought at the ask, sold at the \
bid, with breakeven, odds and greeks; premium-selling structures and a calendar too), the \
playbook's view (the entry setups live on it, the contract each is best expressed with under \
the dashboard's volatility forecast, its entry limit and exit plan), and the owner's portfolio.

How to answer:
- Give a straight view on the setup and the question asked, using the numbers. Lead with the \
bottom line.
- Use web search for what the numbers can't show: the next earnings date, recent news, and \
anything scheduled before the expiry. Say what you found and when it was published.
- Compare structures where it matters (shares, long call, debit spread, LEAPS, and when \
options are expensive the seller's side: cash-secured put, credit spread, iron condor, calendar): \
cost or credit, maximum loss, breakeven, what a volatility drop does, and how each behaves if \
the stock goes nowhere.
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
