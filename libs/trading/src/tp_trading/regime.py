"""Switching a strategy off in a bad market regime.

A regime gate says, for each session, whether the strategy should be out of the market ("risk
off"). The ``Context`` enforces it the same way in backtests and paper trading: while risk is
off, the strategy's buys are dropped and what it holds is sold at the next fill; on the first
session risk is back on, the strategy takes the positions its rules call for at once (as a new
sleeve does), instead of waiting for its next scheduled rebalance.

The gate here is a series worked out beforehand, one value per session, each from data up to
that session's close (``tp_strategies.regime_gate`` builds them: the learned volatility regime,
SPY's 200-day trend, or the VIX). A session missing from the series takes the last value before
it; before the first, risk is on.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from typing import Protocol

import pandas as pd


class RegimeGate(Protocol):
    name: str

    def off(self, day: date) -> bool:
        """Risk off on ``day`` (decided at its close)."""
        ...

    def turned_on(self, day: date) -> bool:
        """Risk was off at the previous decision and is on at ``day``."""
        ...


class SeriesGate:
    """A ``RegimeGate`` from a boolean series indexed by session (True: risk off)."""

    def __init__(self, name: str, off: pd.Series, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        s = off.astype(bool)
        s.index = [pd.Timestamp(d).date() for d in s.index]
        s = s[~s.index.duplicated(keep="last")].sort_index()
        self._days: list[date] = list(s.index)
        self._values: list[bool] = [bool(v) for v in s.to_numpy()]

    def _position(self, day: date) -> int:
        return bisect_right(self._days, day) - 1

    def off(self, day: date) -> bool:
        i = self._position(day)
        return self._values[i] if i >= 0 else False

    def turned_on(self, day: date) -> bool:
        i = self._position(day)
        if i < 1 or self._days[i] != day:
            return False
        return self._values[i - 1] and not self._values[i]

    def share_off(self, start: date | None = None, end: date | None = None) -> float:
        """The share of sessions in ``[start, end]`` with risk off."""
        picked = [v for d, v in zip(self._days, self._values, strict=True)
                  if (start is None or d >= start) and (end is None or d <= end)]  # fmt: skip
        return sum(picked) / len(picked) if picked else 0.0

    def __repr__(self) -> str:
        return f"SeriesGate({self.name!r}{', ' + self.detail if self.detail else ''})"
