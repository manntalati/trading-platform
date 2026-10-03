"""Deterministic synthetic market data.

Used by the test suite and by ``tp-data ... --source fake`` to exercise the whole pipeline
without API keys. Prices are a seeded random walk per symbol on real XNYS sessions; splits and
dividends can be injected so the adjustment logic sees realistic discontinuities, and individual
bars can be corrupted to exercise validation.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from tp_core.calendar import session_midnight_utc, sessions
from tp_core.occ import Right, format_occ
from tp_ingest.sources.base import (
    BAR_COLUMNS,
    CORPORATE_ACTION_COLUMNS,
    OPTION_CONTRACT_COLUMNS,
    OPTION_QUOTE_COLUMNS,
    UNDERLYING_COLUMNS,
)

HISTORY_START = date(2015, 1, 2)


@dataclass(frozen=True)
class FakeSplit:
    ex_date: date
    ratio: float  # shares after / shares before: 4.0 for 4-for-1, 0.1 for 1-for-10


@dataclass(frozen=True)
class FakeDividend:
    ex_date: date
    amount: float  # cash per share


@dataclass
class FakeSource:
    """Synthetic bars + corporate actions. History always starts at ``HISTORY_START`` so the
    same (symbol, session) has the same price no matter what window is requested."""

    splits: Mapping[str, Sequence[FakeSplit]] = field(default_factory=dict)
    dividends: Mapping[str, Sequence[FakeDividend]] = field(default_factory=dict)
    # Hook to corrupt rows in tests: receives and returns the bars DataFrame.
    mutate: Callable[[pd.DataFrame], pd.DataFrame] | None = None
    # Options: the "today" the synthetic chain is built around, and symbols that should fail.
    as_of: date = date(2024, 7, 12)
    failing_underlyings: frozenset[str] = frozenset()
    name: str = "fake"
    feed: str = "synthetic"
    options_feed: str = "synthetic"
    calls: list[tuple[str, tuple[str, ...], date, date]] = field(default_factory=list)

    def daily_bars(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        self.calls.append(("daily_bars", tuple(symbols), start, end))
        frames = [self._history(s, end) for s in symbols]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=list(BAR_COLUMNS))
        df = df[(df["session"] >= start) & (df["session"] <= end)].drop(columns="session")
        if self.mutate is not None:
            df = self.mutate(df)
        return df.reset_index(drop=True)[list(BAR_COLUMNS)]

    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pd.DataFrame:
        self.calls.append(("corporate_actions", tuple(symbols), start, end))
        rows: list[dict[str, object]] = []
        for symbol in symbols:
            for s in self.splits.get(symbol, ()):
                forward = s.ratio >= 1
                rows.append(
                    {
                        "id": f"{symbol}-split-{s.ex_date}",
                        "symbol": symbol,
                        "action_type": "forward_split" if forward else "reverse_split",
                        "ex_date": s.ex_date,
                        "new_rate": s.ratio if forward else 1.0,
                        "old_rate": 1.0 if forward else 1.0 / s.ratio,
                    }
                )
            for d in self.dividends.get(symbol, ()):
                rows.append(
                    {
                        "id": f"{symbol}-div-{d.ex_date}",
                        "symbol": symbol,
                        "action_type": "cash_dividend",
                        "ex_date": d.ex_date,
                        "rate": d.amount,
                        "special": False,
                        "foreign": False,
                    }
                )
        df = pd.DataFrame(rows, columns=list(CORPORATE_ACTION_COLUMNS))
        if df.empty:
            return df
        return df[(df["ex_date"] >= start) & (df["ex_date"] <= end)].reset_index(drop=True)

    def _history(self, symbol: str, end: date) -> pd.DataFrame:
        days = sessions(HISTORY_START, end)
        n = len(days)
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        log_returns = rng.normal(0.0003, 0.012, n)
        economic = 100.0 * np.exp(np.cumsum(log_returns))  # total-return-like price path

        day_index = pd.DatetimeIndex(days)
        raw_close = economic.copy()
        for s in self.splits.get(symbol, ()):
            raw_close[day_index >= pd.Timestamp(s.ex_date)] /= s.ratio
        for d in self.dividends.get(symbol, ()):
            raw_close[day_index >= pd.Timestamp(d.ex_date)] -= d.amount

        gap = rng.normal(0.0, 0.003, n)
        raw_open = raw_close / np.exp(log_returns) * np.exp(gap)
        raw_open[0] = raw_close[0]
        wiggle = np.abs(rng.normal(0.0, 0.004, n))
        high = np.maximum(raw_open, raw_close) * (1 + wiggle)
        low = np.minimum(raw_open, raw_close) * (1 - wiggle)
        volume = rng.integers(1_000_000, 5_000_000, n).astype(float)

        return pd.DataFrame(
            {
                "symbol": symbol,
                "session": days,
                "timestamp": [session_midnight_utc(d) for d in days],
                "open": raw_open,
                "high": high,
                "low": low,
                "close": raw_close,
                "volume": volume,
                "trade_count": volume / 100,
                "vwap": (high + low + raw_close) / 3,
            }
        )

    # -- options ------------------------------------------------------------------------------

    def underlying_snapshots(self, symbols: Sequence[str]) -> pd.DataFrame:
        self.calls.append(("underlying_snapshots", tuple(symbols), self.as_of, self.as_of))
        at = datetime.combine(self.as_of, datetime.min.time(), tzinfo=UTC) + timedelta(hours=19)
        rows = []
        for symbol in symbols:
            spot = self._spot(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "price": spot,
                    "bid": spot - 0.01,
                    "ask": spot + 0.01,
                    "quote_at": pd.Timestamp(at),
                    "trade_at": pd.Timestamp(at),
                }
            )
        return pd.DataFrame(rows, columns=list(UNDERLYING_COLUMNS))

    def option_chain(self, underlying: str, *, expiration_lte: date) -> pd.DataFrame:
        self.calls.append(("option_chain", (underlying,), self.as_of, expiration_lte))
        if underlying in self.failing_underlyings:
            raise ConnectionError(f"synthetic failure for {underlying}")
        spot = self._spot(underlying)
        at = pd.Timestamp(datetime.combine(self.as_of, datetime.min.time(), tzinfo=UTC))
        rows = []
        for expiration, strike, right in self._grid(underlying, expiration_lte):
            t = max((expiration - self.as_of).days, 1) / 365
            vol = 0.20 + 0.10 * abs(math.log(strike / spot))  # a mild smile
            price, delta = _black_scholes(spot, strike, t, vol, right)
            half_spread = max(0.01, 0.02 * price)
            rows.append(
                {
                    "contract": format_occ(underlying, expiration, right, strike),
                    "bid": round(max(price - half_spread, 0.0), 2),
                    "ask": round(price + half_spread, 2),
                    "bid_size": 10.0,
                    "ask_size": 12.0,
                    "quote_at": at + pd.Timedelta(hours=19),
                    "last_price": round(price, 2),
                    "last_size": 1.0,
                    "trade_at": at + pd.Timedelta(hours=18),
                    "implied_volatility": vol,
                    "delta": delta,
                    "gamma": None,
                    "theta": None,
                    "vega": None,
                    "rho": None,
                }
            )
        return pd.DataFrame(rows, columns=list(OPTION_QUOTE_COLUMNS))

    def option_contracts(self, underlying: str, *, expiration_lte: date) -> pd.DataFrame:
        self.calls.append(("option_contracts", (underlying,), self.as_of, expiration_lte))
        prior = self.as_of - timedelta(days=1)
        rows = [
            {
                "contract": format_occ(underlying, expiration, right, strike),
                "style": "american",
                "open_interest": 1000.0,
                "open_interest_date": prior,
                "close_price": None,
                "close_price_date": None,
            }
            for expiration, strike, right in self._grid(underlying, expiration_lte)
        ]
        return pd.DataFrame(rows, columns=list(OPTION_CONTRACT_COLUMNS))

    def _spot(self, symbol: str) -> float:
        close = self._history(symbol, self.as_of)["close"].iloc[-1]
        return float(round(close, 2))

    def _grid(self, underlying: str, expiration_lte: date) -> list[tuple[date, float, Right]]:
        """Weekly Friday expirations for 8 weeks plus quarterly ones, strikes ±20% of spot."""
        spot = self._spot(underlying)
        step = 1.0 if spot < 200 else 5.0
        strikes = [step * k for k in range(int(spot * 0.8 / step), int(spot * 1.2 / step) + 1)]
        first_friday = self.as_of + timedelta(days=(4 - self.as_of.weekday()) % 7)
        expirations = {first_friday + timedelta(weeks=w) for w in range(8)}
        expirations |= {first_friday + timedelta(weeks=13 * q) for q in range(1, 9)}
        return [
            (expiration, strike, right)
            for expiration in sorted(e for e in expirations if e <= expiration_lte)
            for strike in strikes
            for right in ("C", "P")
        ]


def _black_scholes(
    spot: float, strike: float, t: float, vol: float, right: Right
) -> tuple[float, float]:
    """European price and delta, zero rates. Good enough for synthetic quotes."""
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)

    def n(x: float) -> float:
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    if right == "C":
        return spot * n(d1) - strike * n(d2), n(d1)
    return strike * n(-d2) - spot * n(-d1), n(d1) - 1
