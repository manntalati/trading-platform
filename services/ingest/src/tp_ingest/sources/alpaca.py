"""Alpaca adapter (alpaca-py SDK).

Clients run with ``raw_data=True`` so we map Alpaca's JSON ourselves into our schemas instead of
depending on the SDK's model classes. The SDK still handles auth, pagination and retries.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pandas as pd
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed, OptionsFeed
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    CorporateActionsRequest,
    OptionChainRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings
from tp_ingest.sources.base import (
    BAR_COLUMNS,
    CORPORATE_ACTION_COLUMNS,
    OPTION_CONTRACT_COLUMNS,
    OPTION_QUOTE_COLUMNS,
    UNDERLYING_COLUMNS,
)

# Alpaca's response keys → our action_type values.
_CA_TYPES: dict[str, str] = {
    "forward_splits": "forward_split",
    "reverse_splits": "reverse_split",
    "stock_dividends": "stock_dividend",
    "cash_dividends": "cash_dividend",
}
# Keep each corporate-actions request to one year so no single call gets huge.
_CA_WINDOW = timedelta(days=365)
_SIP_DELAY = timedelta(minutes=16)


class AlpacaSource:
    name = "alpaca"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: str = "sip",
        options_feed: str = "indicative",
        quotes_feed: str = "iex",
        paper: bool = True,
        data_url: str | None = None,
    ) -> None:
        self.feed = feed
        self.options_feed = options_feed
        self.quotes_feed = quotes_feed
        self._paper = paper
        self._keys = (api_key, secret_key)
        self._stocks = StockHistoricalDataClient(
            api_key, secret_key, raw_data=True, url_override=data_url
        )
        self._corporate = CorporateActionsClient(
            api_key, secret_key, raw_data=True, url_override=data_url
        )
        self._options = OptionHistoricalDataClient(
            api_key, secret_key, raw_data=True, url_override=data_url
        )
        self._trading = TradingClient(api_key, secret_key, paper=paper, raw_data=True)

    @classmethod
    def from_settings(cls, settings: Settings) -> AlpacaSource:
        key, secret = settings.require_alpaca_keys()
        return cls(
            key,
            secret,
            feed=settings.bars_feed,
            options_feed=settings.options_feed,
            quotes_feed=settings.quotes_feed,
            paper=settings.alpaca_paper,
        )

    def trading_client(self) -> TradingClient:
        return self._trading

    # -- bars ---------------------------------------------------------------------------------

    def daily_bars(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        # Daily bars are stamped at midnight New York; query the whole local days, but never
        # closer to "now" than the free plan's 15-minute SIP delay allows (Alpaca rejects
        # requests for recent SIP data on the Basic plan).
        end_at = min(
            datetime.combine(end, time.max, tzinfo=NEW_YORK),
            datetime.now(UTC) - _SIP_DELAY,
        )
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, time.min, tzinfo=NEW_YORK),
            end=end_at,
            adjustment=Adjustment.RAW,
            feed=DataFeed(self.feed),
        )
        raw = cast(dict[str, list[dict[str, Any]]], self._stocks.get_stock_bars(request))
        return bars_from_raw(raw)

    # -- corporate actions -------------------------------------------------------------------

    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        frames = []
        window_start = start
        while window_start <= end:
            window_end = min(window_start + _CA_WINDOW, end)
            request = CorporateActionsRequest(
                symbols=list(symbols),
                types=[CorporateActionsType(t) for t in _CA_TYPES.values()],
                start=window_start,
                end=window_end,
            )
            raw = cast(
                dict[str, list[dict[str, Any]]], self._corporate.get_corporate_actions(request)
            )
            frames.append(corporate_actions_from_raw(raw))
            window_start = window_end + timedelta(days=1)
        out = pd.concat(frames, ignore_index=True) if frames else corporate_actions_from_raw({})
        return out[(out["ex_date"] >= start) & (out["ex_date"] <= end)].reset_index(drop=True)

    # -- options -----------------------------------------------------------------------------

    def underlying_snapshots(self, symbols: Sequence[str]) -> pd.DataFrame:
        request = StockSnapshotRequest(
            symbol_or_symbols=list(symbols), feed=DataFeed(self.quotes_feed)
        )
        raw = cast(dict[str, dict[str, Any]], self._stocks.get_stock_snapshot(request))
        return underlying_snapshots_from_raw(raw)

    def option_chain(self, underlying: str, *, expiration_lte: date) -> pd.DataFrame:
        request = OptionChainRequest(
            underlying_symbol=underlying,
            feed=OptionsFeed(self.options_feed),
            expiration_date_lte=expiration_lte,
        )
        raw = cast(dict[str, dict[str, Any]], self._options.get_option_chain(request))
        return option_chain_from_raw(raw)

    def option_contracts(self, underlying: str, *, expiration_lte: date) -> pd.DataFrame:
        contracts: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:  # this endpoint is not auto-paginated by the SDK
            request = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_lte=expiration_lte,
                limit=10_000,
                page_token=page_token,
            )
            page = cast(dict[str, Any], self._trading.get_option_contracts(request))
            contracts.extend(page.get("option_contracts") or [])
            page_token = page.get("next_page_token")
            if not page_token:
                break
        return option_contracts_from_raw(contracts)


def bars_from_raw(raw: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    """Map Alpaca's ``{symbol: [{t, o, h, l, c, v, n, vw}, ...]}`` to ``BAR_COLUMNS``."""
    rows = [
        {
            "symbol": symbol,
            "timestamp": bar["t"],
            "open": bar["o"],
            "high": bar["h"],
            "low": bar["l"],
            "close": bar["c"],
            "volume": bar["v"],
            "trade_count": bar.get("n"),
            "vwap": bar.get("vw"),
        }
        for symbol, bars in raw.items()
        for bar in bars
    ]
    df = pd.DataFrame(rows, columns=list(BAR_COLUMNS))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume", "trade_count", "vwap"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


def corporate_actions_from_raw(raw: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    """Map Alpaca's ``{forward_splits: [...], cash_dividends: [...], ...}`` to our columns."""
    rows = []
    for key, actions in raw.items():
        action_type = _CA_TYPES.get(key)
        if action_type is None:
            continue
        for action in actions:
            rows.append(
                {
                    "id": action.get("id"),
                    "symbol": action.get("symbol"),
                    "action_type": action_type,
                    "ex_date": action.get("ex_date"),
                    "process_date": action.get("process_date"),
                    "record_date": action.get("record_date"),
                    "payable_date": action.get("payable_date"),
                    "new_rate": action.get("new_rate"),
                    "old_rate": action.get("old_rate"),
                    "rate": action.get("rate"),
                    "special": action.get("special"),
                    "foreign": action.get("foreign"),
                }
            )
    df = pd.DataFrame(rows, columns=list(CORPORATE_ACTION_COLUMNS))
    for col in ("ex_date", "process_date", "record_date", "payable_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    for col in ("new_rate", "old_rate", "rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def underlying_snapshots_from_raw(raw: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Map ``{symbol: {latestTrade: {p, t}, latestQuote: {bp, ap, t}, ...}}``."""
    rows = []
    for symbol, snap in raw.items():
        trade = snap.get("latestTrade") or {}
        quote = snap.get("latestQuote") or {}
        rows.append(
            {
                "symbol": symbol,
                "price": _num(trade.get("p")),
                "bid": _num(quote.get("bp")),
                "ask": _num(quote.get("ap")),
                "quote_at": quote.get("t"),
                "trade_at": trade.get("t"),
            }
        )
    df = pd.DataFrame(rows, columns=list(UNDERLYING_COLUMNS))
    for col in ("quote_at", "trade_at"):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    return df


def option_chain_from_raw(raw: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Map ``{OCC: {latestQuote, latestTrade, impliedVolatility, greeks}}``."""
    rows = []
    for contract, snap in raw.items():
        quote = snap.get("latestQuote") or {}
        trade = snap.get("latestTrade") or {}
        greeks = snap.get("greeks") or {}
        rows.append(
            {
                "contract": contract,
                "bid": _num(quote.get("bp")),
                "ask": _num(quote.get("ap")),
                "bid_size": _num(quote.get("bs")),
                "ask_size": _num(quote.get("as")),
                "quote_at": quote.get("t"),
                "last_price": _num(trade.get("p")),
                "last_size": _num(trade.get("s")),
                "trade_at": trade.get("t"),
                "implied_volatility": _num(snap.get("impliedVolatility")),
                **{g: _num(greeks.get(g)) for g in ("delta", "gamma", "theta", "vega", "rho")},
            }
        )
    df = pd.DataFrame(rows, columns=list(OPTION_QUOTE_COLUMNS))
    for col in ("quote_at", "trade_at"):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    return df


def option_contracts_from_raw(contracts: list[dict[str, Any]]) -> pd.DataFrame:
    """Map the contracts endpoint (numbers arrive as strings) to ``OPTION_CONTRACT_COLUMNS``."""
    rows = [
        {
            "contract": c["symbol"],
            "style": c.get("style"),
            "open_interest": _num(c.get("open_interest")),
            "open_interest_date": c.get("open_interest_date"),
            "close_price": _num(c.get("close_price")),
            "close_price_date": c.get("close_price_date"),
        }
        for c in contracts
    ]
    df = pd.DataFrame(rows, columns=list(OPTION_CONTRACT_COLUMNS))
    for col in ("open_interest_date", "close_price_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


def describe_connection(source: AlpacaSource) -> dict[str, Any]:
    """Cheap authenticated calls used by ``tp-data check``."""
    client = source.trading_client()
    clock = cast(dict[str, Any], client.get_clock())
    account = cast(dict[str, Any], client.get_account())
    return {
        "paper": source._paper,
        "account_status": account.get("status"),
        "market_open": clock.get("is_open"),
        "next_open": clock.get("next_open"),
        "server_time": clock.get("timestamp"),
        "local_time_utc": datetime.now(UTC).isoformat(),
    }
