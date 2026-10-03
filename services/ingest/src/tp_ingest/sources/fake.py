"""Deterministic synthetic market data.

Used by the test suite and by ``tp-data ... --source fake`` to exercise the whole pipeline
without API keys. Prices are a seeded random walk per symbol on real XNYS sessions; splits and
dividends can be injected so the adjustment logic sees realistic discontinuities, and individual
bars can be corrupted to exercise validation.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from tp_core.calendar import session_midnight_utc, sessions
from tp_ingest.sources.base import BAR_COLUMNS, CORPORATE_ACTION_COLUMNS

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
    name: str = "fake"
    feed: str = "synthetic"
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
