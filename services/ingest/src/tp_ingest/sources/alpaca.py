"""Alpaca adapter (alpaca-py SDK).

Clients run with ``raw_data=True`` so we map Alpaca's JSON ourselves into our schemas instead of
depending on the SDK's model classes. The SDK still handles auth, pagination and retries.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pandas as pd
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings
from tp_ingest.sources.base import BAR_COLUMNS, CORPORATE_ACTION_COLUMNS

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
        paper: bool = True,
        data_url: str | None = None,
    ) -> None:
        self.feed = feed
        self._paper = paper
        self._keys = (api_key, secret_key)
        self._stocks = StockHistoricalDataClient(
            api_key, secret_key, raw_data=True, url_override=data_url
        )
        self._corporate = CorporateActionsClient(
            api_key, secret_key, raw_data=True, url_override=data_url
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> AlpacaSource:
        key, secret = settings.require_alpaca_keys()
        return cls(key, secret, feed=settings.bars_feed, paper=settings.alpaca_paper)

    def trading_client(self) -> TradingClient:
        return TradingClient(*self._keys, paper=self._paper, raw_data=True)

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
