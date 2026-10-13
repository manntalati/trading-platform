"""Every open position with its statistics, and Claude to talk them through.

``GET /api/positions``: each holding with a current price and P&L, plus

- stocks and funds: day change, returns over 1 week to 1 year, 52-week range, moving averages,
  volatility and beta (from the daily bars in the lake);
- options: a live quote (Alpaca), implied volatility, greeks, time decay in dollars a day, the
  move needed to break even against the move the market prices in, the odds, and a scenario grid;

and notes on what each position is up against. ``POST /api/positions/ask`` sends a question
about one position (or the account) to Claude with those figures attached. That needs an
Anthropic API key; the positions are sent to Anthropic's API with each question.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar

import anthropic
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tp_api import queries
from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_core.bars import load_bars, missing_symbols
from tp_core.calendar import NEW_YORK
from tp_core.config import MissingCredentialsError, Settings
from tp_core.pnl import option_label
from tp_core.portfolio import CASH_KIND, MARKET_KINDS, holdings_table, latest_snapshot
from tp_core.positions import (
    OptionPosition,
    OptionQuote,
    option_notes,
    option_stats,
    stock_notes,
    stock_stats,
)
from tp_ingest.sources.alpaca import AlpacaSource

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/positions", dependencies=[Depends(require_token)])
QUOTE_TTL = 30.0  # seconds a fetched quote is reused
HISTORY_DAYS = 400  # calendar days of bars: a year of sessions plus the 200-day average's warmup
T = TypeVar("T")


def now_utc() -> datetime:
    return datetime.now(UTC)


# -- market data ----------------------------------------------------------------------------------


class MarketData(Protocol):
    name: str
    error: str | None

    def option_quotes(self, contracts: Sequence[str]) -> dict[str, OptionQuote]: ...

    def stock_prices(self, symbols: Sequence[str]) -> dict[str, float]: ...


class AlpacaMarketData:
    """Option quotes and latest stock trades from Alpaca (the free plan: options from the
    indicative feed, stocks from IEX), cached for ``ttl`` seconds so a dashboard refreshing every
    minute doesn't ask again each time. A failed fetch is reported in ``error``; the page then
    falls back to the broker's prices."""

    name = "alpaca"

    def __init__(
        self,
        source: AlpacaSource,
        *,
        ttl: float = QUOTE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.ttl = ttl
        self.clock = clock
        self.error: str | None = None
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> AlpacaMarketData:
        return cls(AlpacaSource.from_settings(settings))

    def option_quotes(self, contracts: Sequence[str]) -> dict[str, OptionQuote]:
        return self._cached("option", contracts, self._fetch_options)

    def stock_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        return self._cached("stock", symbols, self._fetch_stocks)

    def _fetch_options(self, contracts: list[str]) -> dict[str, OptionQuote]:
        df = self.source.option_snapshots(contracts)
        return {
            str(r["contract"]): OptionQuote(
                bid=_float(r["bid"]),
                ask=_float(r["ask"]),
                last=_float(r["last_price"]),
                implied_volatility=_float(r["implied_volatility"]),
                quote_at=r["quote_at"].to_pydatetime() if pd.notna(r["quote_at"]) else None,
            )
            for _, r in df.iterrows()
        }

    def _fetch_stocks(self, symbols: list[str]) -> dict[str, float]:
        df = self.source.underlying_snapshots(symbols)
        return {str(r["symbol"]): float(r["price"]) for _, r in df.iterrows() if _float(r["price"])}

    def _cached(
        self, kind: str, keys: Sequence[str], fetch: Callable[[list[str]], dict[str, T]]
    ) -> dict[str, T]:
        now = self.clock()
        wanted = list(dict.fromkeys(keys))
        with self._lock:
            stale = [
                k
                for k in wanted
                if (hit := self._cache.get((kind, k))) is None or now - hit[0] >= self.ttl
            ]
        if stale:
            try:
                got = fetch(stale)
            except Exception as exc:  # show it on the page; the broker's prices still work
                log.warning("market data from Alpaca failed: %s", exc)
                self.error = f"{type(exc).__name__}: {exc}"
            else:
                self.error = None
                with self._lock:
                    for k in stale:  # remember misses too: no quote is an answer
                        self._cache[(kind, k)] = (now, got.get(k))
        with self._lock:
            hits = {k: self._cache.get((kind, k)) for k in wanted}
        return {k: hit[1] for k, hit in hits.items() if hit is not None and hit[1] is not None}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# -- the view -------------------------------------------------------------------------------------


def positions_view(
    ctx: queries.Context,
    now: datetime,
    *,
    market: MarketData | None = None,
    live: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Every holding of the latest brokerage sync with statistics and notes (see module doc).

    Prices, freshest first: ``live`` (the dashboard's quote stream), ``market`` (fetched now),
    the last daily close in the lake, then the broker's own price from the sync.
    """
    snap = latest_snapshot(ctx.lake)
    if snap.empty:
        return {"synced": False, "positions": []}
    today = now.astimezone(NEW_YORK).date()
    table = holdings_table(snap, ctx.classifier, today)
    held = table[table["kind"] != CASH_KIND]
    is_option = held["kind"] == "option"
    live_options = held[is_option & (held["days_to_expiry"].fillna(-1) >= 0)]
    contracts = [str(s) for s in live_options["symbol"]]  # expired ones have no market
    underlyings = [str(u) for u in held.loc[is_option, "underlying"].dropna()]
    stocks = [str(s) for s in held.loc[held["kind"].isin(MARKET_KINDS), "symbol"]]
    priced = list(dict.fromkeys([*stocks, *underlyings]))

    prices = {s: float(p) for s, p in (live or {}).items() if s in priced and p}
    if market is not None and (missing := [s for s in priced if s not in prices]):
        prices |= market.stock_prices(missing)
    quotes = market.option_quotes(contracts) if market is not None and contracts else {}
    history = _history(ctx, [*priced, "SPY"], today)
    bench = history.get("SPY")

    rows: list[dict[str, Any]] = []
    for _, r in held.iterrows():
        if r["kind"] == "option":
            rows.append(_option_row(ctx, r, now, prices, quotes, history))
        else:
            rows.append(_stock_row(r, prices, history, bench))
    rows.sort(key=lambda p: -abs(p.get("market_value") or 0.0))
    total_value = snap.total_value
    for p in rows:
        mv = p.get("market_value")
        p["weight"] = mv / total_value if mv is not None and total_value else None
        if p["kind"] != "option":
            p["notes"] = stock_notes(
                p["stats"],
                symbol=p["symbol"],
                weight=p["weight"],
                pnl=p.get("pnl"),
                pnl_pct=p.get("pnl_pct"),
            )
    view: dict[str, Any] = queries.clean(
        {
            "synced": True,
            "now": now.isoformat(),
            "freshness": queries.freshness(snap.accounts),
            "total_value": total_value,
            "cash": queries.cash_summary(snap),
            "market_data": {
                "source": market.name if market is not None else None,
                "error": market.error if market is not None else None,
                "option_quotes": len(quotes),
                "options": len(contracts),
            },
            "risk_free_rate": ctx.settings.risk_free_rate,
            "totals": _totals(rows),
            "exposure": _exposure(rows),
            "positions": rows,
            "ask": {
                "available": ctx.settings.anthropic_api_key is not None,
                "model": ctx.settings.ask_model,
            },
        }
    )
    return view


def _history(ctx: queries.Context, symbols: list[str], today: date) -> dict[str, pd.Series]:
    """Adjusted daily closes of each symbol the lake has bars for."""
    wanted = list(dict.fromkeys(symbols))
    present = [s for s in wanted if s not in missing_symbols(ctx.lake, [s])]
    if not present:
        return {}
    bars = load_bars(ctx.lake, present, start=today - timedelta(days=HISTORY_DAYS))
    out = {}
    for symbol, g in bars.groupby("symbol"):
        out[str(symbol)] = pd.Series(
            g["adj_close"].to_numpy(dtype=float), index=pd.DatetimeIndex(g["session"])
        )
    return out


def _stock_row(
    r: pd.Series,
    prices: Mapping[str, float],
    history: Mapping[str, pd.Series],
    bench: pd.Series | None,
) -> dict[str, Any]:
    symbol = str(r["symbol"])
    qty = float(r["quantity"])
    cost = _float(r["cost_basis_per_unit"])
    broker = _float(r["price"])
    closes = history.get(symbol)
    if closes is not None and len(closes):
        stats = stock_stats(closes, prices.get(symbol), bench)
        source = "live" if symbol in prices else "last close"
    else:
        stats = {"price": prices.get(symbol) or broker}
        source = "live" if symbol in prices else "broker"
    price = _float(stats.get("price")) or broker
    value = qty * price if price is not None else _float(r["market_value"])
    pnl = (price - cost) * qty if price is not None and cost is not None else None
    day = _float(stats.get("day_change"))
    return {
        "symbol": symbol,
        "label": symbol,
        "kind": str(r["kind"]),
        "description": r["description"] if pd.notna(r["description"]) else None,
        "underlying": symbol,
        "quantity": qty,
        "price": price,
        "price_source": source,
        "broker_price": broker,
        "cost": cost,
        "market_value": value,
        "pnl": pnl,
        "pnl_pct": (price / cost - 1) if price is not None and cost else None,
        "day_pnl": day * qty if day is not None else None,
        "sector": r["sector"],
        "asset_class": r["asset_class"],
        "stats": stats,
    }


def _option_row(
    ctx: queries.Context,
    r: pd.Series,
    now: datetime,
    prices: Mapping[str, float],
    quotes: Mapping[str, OptionQuote],
    history: Mapping[str, pd.Series],
) -> dict[str, Any]:
    symbol, underlying = str(r["symbol"]), str(r["underlying"])
    multiplier = float(r["multiplier"]) or 100.0
    cost = _float(r["cost_basis_per_unit"])
    broker = _float(r["price"])
    expiration = r["expiration"]
    expiration = expiration if isinstance(expiration, date) else pd.Timestamp(expiration).date()
    pos = OptionPosition(
        symbol=symbol,
        label=option_label(symbol) or symbol,
        underlying=underlying,
        quantity=float(r["quantity"]),
        strike=float(r["strike"]),
        expiration=expiration,
        call=r["right"] == "C",
        cost=cost / multiplier if cost is not None else None,
        broker_price=broker / multiplier if broker is not None else None,
        multiplier=multiplier,
    )
    closes = history.get(underlying)
    spot = prices.get(underlying)
    under: dict[str, Any] = {}
    if closes is not None and len(closes):
        under = stock_stats(closes, spot)
        spot = spot or _float(under.get("price"))
    stats = option_stats(
        pos,
        spot=spot,
        quote=quotes.get(symbol),
        now=now,
        rate=ctx.settings.risk_free_rate,
        realized_vol=_float(under.get("vol_20d")),
    )
    value = _float(stats.get("value"))
    return {
        "symbol": symbol,
        "label": pos.label,
        "kind": "option",
        "description": r["description"] if pd.notna(r["description"]) else None,
        "underlying": underlying,
        "quantity": pos.quantity,
        "price": stats.get("mark"),
        "price_source": stats.get("mark_source"),
        "broker_price": pos.broker_price,
        "cost": pos.cost,
        "market_value": value if value is not None else _float(r["market_value"]),
        "pnl": stats.get("pnl"),
        "pnl_pct": stats.get("pnl_pct"),
        "day_pnl": None,
        "sector": r["sector"],
        "asset_class": r["asset_class"],
        "stats": stats,
        "underlying_stats": {
            k: under.get(k)
            for k in ("price", "day_change_pct", "returns", "vol_20d", "high_52w", "low_52w")
        },
        "notes": option_notes(stats, pos),
    }


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    options = [p for p in rows if p["kind"] == "option"]

    def total(items: list[dict[str, Any]], key: str, inner: str | None = None) -> float:
        values = [(p.get(inner) or {}).get(key) if inner else p.get(key) for p in items]
        return float(sum(v for v in values if v is not None and v == v))

    soon = [p for p in options if (p["stats"].get("days_left") or 0) <= 7]
    return {
        "value": total(rows, "market_value"),
        "pnl": total(rows, "pnl"),
        "day_pnl": total(rows, "day_pnl"),
        "options_value": total(options, "market_value"),
        "options_pnl": total(options, "pnl"),
        "theta_day": total(options, "theta_day", "stats"),
        "expiring_within_7d": len(soon),
    }


def _exposure(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per underlying: shares held, the share-equivalent of its options (delta) and the net, in
    shares and dollars. Shows when calls and stock on the same name stack up."""
    out: dict[str, dict[str, Any]] = {}
    for p in rows:
        u = str(p["underlying"])
        e = out.setdefault(u, {"underlying": u, "shares": 0.0, "option_delta_shares": 0.0,
                               "price": None, "options": 0})  # fmt: skip
        if p["kind"] == "option":
            e["options"] += 1
            e["option_delta_shares"] += p["stats"].get("delta_shares") or 0.0
            e["price"] = e["price"] or p["stats"].get("spot")
        else:
            e["shares"] += p["quantity"]
            e["price"] = p.get("price") or e["price"]
    result = []
    for e in out.values():
        if not e["options"]:
            continue  # stock alone: its value is its exposure
        net = e["shares"] + e["option_delta_shares"]
        result.append(
            e | {"net_shares": net, "net_value": net * e["price"] if e["price"] else None}
        )
    return sorted(result, key=lambda e: -abs(e["net_value"] or 0.0))


# -- asking Claude --------------------------------------------------------------------------------

INSTRUCTIONS = """\
You are a candid, numerate trading assistant inside the owner's personal trading dashboard. \
Below are their brokerage positions (read-only data from the broker) with live market data and \
statistics the dashboard computed: Black-Scholes implied volatility, greeks, risk-neutral odds \
and scenario values for options; trend, range and volatility for stocks.

How to answer:
- Answer the question asked, using this position's actual numbers. Lead with the bottom line, \
then the reasoning.
- For options, especially near expiry, weigh what selling now recovers (at the bid, not the \
mid), the time decay per day, the move needed to break even against the move the market prices \
in, the odds, the scenario grid, and any catalyst before expiry (earnings, scheduled events). \
Lay out the realistic choices (sell now, hold to a set date or price with an exit rule, sell \
part, roll) and what each costs and gains.
- What was already paid is spent either way; judge from here.
- Say so when something material is missing or stale (no live quote, an old broker price).
- When it matters, use web search to check for earnings dates or major news on the underlying \
before expiry, and say what you found.
- Plain text, short paragraphs or bullets, no wide tables. Keep it under about 250 words unless \
asked for more.
- You can't place trades; the owner decides. Skip boilerplate disclaimers: one short line at \
most, and only when the call is genuinely close.
"""
MAX_TOKENS = 16_000
MAX_CONTINUATIONS = 3  # web searches can pause a turn; resume it at most this often
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    symbol: str | None = Field(default=None, max_length=40)
    history: list[Turn] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class Answer:
    text: str
    model: str
    stop_reason: str | None
    sources: list[dict[str, str]] = field(default_factory=list)
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    note: str | None = None


class Advisor(Protocol):
    def ask(self, system: str, messages: list[dict[str, Any]]) -> Answer: ...


class ClaudeAdvisor:
    """Claude through the Anthropic API: adaptive thinking at the configured effort, streamed
    (long answers can take a while), with server-side fallback to another model if a request
    is declined, and optional web search."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            key = self.settings.require_anthropic_key()
            self._client = anthropic.Anthropic(api_key=key, timeout=300.0)
        return self._client

    def ask(self, system: str, messages: list[dict[str, Any]]) -> Answer:
        tools = (
            [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
            if self.settings.ask_web_search
            else []
        )
        try:
            return self._ask(system, messages, tools)
        except anthropic.BadRequestError as exc:
            if not tools or "web_search" not in str(exc):
                raise
            log.warning("web search unavailable, asking without it: %s", exc)
            answer = self._ask(system, messages, [])
            return replace(answer, note="Web search isn't enabled for this API key.")

    def _ask(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Answer:
        convo = list(messages)
        blocks: list[Any] = []
        usage = [0, 0]
        message: Any = None
        for _ in range(MAX_CONTINUATIONS + 1):
            extra: dict[str, Any] = {"tools": tools} if tools else {}
            with self.client().beta.messages.stream(
                model=self.settings.ask_model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=convo,
                output_config={"effort": self.settings.ask_effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **extra,
            ) as stream:
                message = stream.get_final_message()
            blocks += message.content
            usage[0] += message.usage.input_tokens
            usage[1] += message.usage.output_tokens
            if message.stop_reason != "pause_turn":
                break
            convo = [*convo, {"role": "assistant", "content": message.content}]
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            why = getattr(details, "explanation", None) if details else None
            text = "Claude declined to answer this one." + (f" ({why})" if why else "")
            return Answer(text, message.model, "refusal", input_tokens=usage[0],
                          output_tokens=usage[1])  # fmt: skip
        return Answer(
            text="".join(b.text for b in blocks if b.type == "text").strip(),
            model=message.model,
            stop_reason=message.stop_reason,
            sources=_sources(blocks),
            searches=sum(
                1 for b in blocks if b.type == "server_tool_use" and b.name == "web_search"
            ),
            input_tokens=usage[0],
            output_tokens=usage[1],
        )


def _sources(blocks: list[Any]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for b in blocks:
        for c in getattr(b, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen[url] = getattr(c, "title", None) or url
    return [{"url": u, "title": t} for u, t in seen.items()]


def ask_system_prompt(view: dict[str, Any], symbol: str | None, now: datetime) -> str:
    """The instructions, then the account and the position in question as JSON."""
    focus = next((p for p in view.get("positions", []) if p["symbol"] == symbol), None)
    account = {
        "now": now.astimezone(NEW_YORK).strftime("%A %Y-%m-%d %H:%M %Z"),
        "data_freshness": view.get("freshness"),
        "total_value": view.get("total_value"),
        "cash": view.get("cash"),
        "totals": view.get("totals"),
        "exposure_by_underlying": view.get("exposure"),
        "risk_free_rate": view.get("risk_free_rate"),
        "positions": [_brief(p) for p in view.get("positions", [])],
    }
    parts = [INSTRUCTIONS, "<account>", json.dumps(_rounded(account)), "</account>"]
    if focus is not None:
        parts += ["<position_in_question>", json.dumps(_rounded(focus)), "</position_in_question>"]
    else:
        parts.append("The question is about the account as a whole.")
    return "\n".join(parts)


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    keep = ("symbol", "label", "kind", "quantity", "price", "cost", "market_value", "pnl",
            "pnl_pct", "weight")  # fmt: skip
    out = {k: p.get(k) for k in keep}
    s = p.get("stats") or {}
    if p["kind"] == "option":
        for k in ("days_left", "spot", "iv", "delta", "theta_day", "prob_profit", "breakeven"):
            out[k] = s.get(k)
    else:
        out["day_change_pct"] = s.get("day_change_pct")
    return out


def _rounded(value: Any) -> Any:
    """Four significant figures is plenty, and keeps the context (and the bill) small."""
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, float):
        return float(f"{value:.4g}") if math.isfinite(value) else None
    return value


# -- endpoints ------------------------------------------------------------------------------------


def _market(request: Request) -> MarketData | None:
    market: MarketData | None = getattr(request.app.state, "market", None)
    return market


def _live(request: Request) -> dict[str, float]:
    hub = getattr(request.app.state, "hub", None)
    return {s: t.price for s, t in hub.latest.items()} if hub is not None else {}


@router.get("")
def positions(c: Ctx, request: Request) -> dict[str, Any]:
    return positions_view(c, now_utc(), market=_market(request), live=_live(request))


@router.post("/ask", dependencies=[Depends(require_dashboard_client)])
def ask(c: Ctx, request: Request, body: AskRequest) -> dict[str, Any]:
    roles = [t.role for t in body.history]
    if roles != ["user", "assistant"] * (len(roles) // 2):
        raise HTTPException(422, "history must alternate user, assistant, starting with user")
    now = now_utc()
    view = positions_view(c, now, market=_market(request), live=_live(request))
    if not view.get("synced"):
        raise HTTPException(409, "no brokerage data yet: run a sync first")
    symbol = body.symbol.upper() if body.symbol else None
    if symbol and not any(p["symbol"] == symbol for p in view["positions"]):
        raise HTTPException(404, f"no open position in {symbol}")
    messages = [{"role": t.role, "content": t.content} for t in body.history]
    messages.append({"role": "user", "content": body.question})
    advisor: Advisor = request.app.state.advisor
    try:
        answer = advisor.ask(ask_system_prompt(view, symbol, now), messages)
    except MissingCredentialsError as exc:
        raise HTTPException(503, str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(502, f"couldn't reach the Claude API: {exc}") from exc
    return {
        "answer": answer.text,
        "model": answer.model,
        "stop_reason": answer.stop_reason,
        "sources": answer.sources,
        "searches": answer.searches,
        "usage": {"input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens},
        "note": answer.note,
        "as_of": now.isoformat(),
    }
