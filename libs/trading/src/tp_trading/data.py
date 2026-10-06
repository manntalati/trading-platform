"""Daily OHLCV as wide matrices (sessions x symbols), sliced point-in-time for strategies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Literal

import pandas as pd

Field = Literal["open", "high", "low", "close", "volume"]
FIELDS: tuple[Field, ...] = ("open", "high", "low", "close", "volume")
ADV_SESSIONS = 20


class MarketData:
    """Bars for a set of symbols on a shared session index.

    A missing bar (not yet listed, halted, data gap) is NaN. Valuation uses the last known close
    (``marks``); strategies see the raw NaNs through ``history`` and decide for themselves.
    """

    def __init__(self, frames: Mapping[Field, pd.DataFrame]) -> None:
        close = frames["close"]
        index = pd.DatetimeIndex(close.index)
        if not index.is_unique or not index.is_monotonic_increasing:
            raise ValueError("bars need a unique, increasing session index")
        self._frames: dict[Field, pd.DataFrame] = {}
        for name in FIELDS:
            frame = frames[name].reindex(index=close.index, columns=close.columns)
            frame.index = index
            self._frames[name] = frame.astype(float)
        self.index = index
        self.sessions: list[date] = [ts.date() for ts in index]
        self.symbols: list[str] = [str(c) for c in close.columns]
        self._marks = self._frames["close"].ffill()
        self._adv = self._frames["volume"].rolling(ADV_SESSIONS, min_periods=1).mean()
        stamps = pd.DataFrame(dict.fromkeys(close.columns, index), index=index)
        self._last_bar = stamps.where(self._frames["close"].notna()).ffill()

    @classmethod
    def from_bars(cls, bars: pd.DataFrame, *, adjusted: bool = True) -> MarketData:
        """From long-format clean bars (``tp_core.bars.load_bars``).

        ``adjusted=True`` uses split- and dividend-adjusted prices (total return; right for
        backtests). Paper trading sizes orders with raw prices instead.
        """
        prefix = "adj_" if adjusted else ""
        frames: dict[Field, pd.DataFrame] = {}
        for name in FIELDS:
            wide = bars.pivot(index="session", columns="symbol", values=f"{prefix}{name}")
            wide.index = pd.DatetimeIndex(wide.index, name="session")
            wide.columns.name = None
            frames[name] = wide.sort_index()
        return cls(frames)

    @classmethod
    def from_closes(cls, closes: pd.DataFrame, volume: float = 1_000_000.0) -> MarketData:
        """Close-only data (open = high = low = close): for tests and vectorised comparisons."""
        vol = pd.DataFrame(volume, index=closes.index, columns=closes.columns).where(closes.notna())
        return cls({"open": closes, "high": closes, "low": closes, "close": closes, "volume": vol})

    def __len__(self) -> int:
        return len(self.index)

    def frame(self, name: Field) -> pd.DataFrame:
        return self._frames[name]

    def window(
        self, name: Field, t: int, columns: Sequence[str], lookback: int | None = None
    ) -> pd.DataFrame:
        """Rows up to and including session ``t`` (never later): the point-in-time view."""
        lo = 0 if lookback is None else max(0, t + 1 - lookback)
        return self._frames[name].iloc[lo : t + 1].reindex(columns=list(columns))

    def value(self, name: Field, t: int, symbol: str) -> float:
        if symbol not in self._frames[name].columns:
            return float("nan")
        return float(self._frames[name].iloc[t][symbol])

    def marks(self, t: int) -> dict[str, float]:
        """Last known close per symbol as of session ``t`` (for valuation)."""
        row = self._marks.iloc[t]
        return {str(s): float(v) for s, v in row.items() if v == v}

    def average_volume(self, t: int) -> dict[str, float]:
        row = self._adv.iloc[t]
        return {str(s): float(v) for s, v in row.items() if v == v}

    def last_bar(self, t: int) -> dict[str, date]:
        """Session of each symbol's most recent bar as of ``t`` (stale-data checks)."""
        row = self._last_bar.iloc[t]
        return {str(s): pd.Timestamp(v).date() for s, v in row.items() if not pd.isna(v)}

    def position_of(self, day: date) -> int:
        """Index of the last session on or before ``day``."""
        pos = int(self.index.searchsorted(pd.Timestamp(day), side="right")) - 1
        if pos < 0:
            raise KeyError(f"no session on or before {day}")
        return pos
