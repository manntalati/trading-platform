"""Strategy 17: Internal Bar Strength (IBS) mean reversion on index ETFs (Pagonidis 2014).

Spec: docs/strategies/17-ibs.md. After each session's close:

- ``IBS = (close - low) / (high - low)``: where the close sits in the day's range, 0 at the low,
  1 at the high. A bar with ``high == low`` (or a missing bar) gives no signal.
- A symbol whose IBS closed below ``entry`` is held through the next session: bought at the next
  open, sold at the open after that, unless that session's IBS is also below ``entry``, in which
  case it is held one more session. The decision uses only today's bar, so a position is never
  held past one session after a non-signal close.
- With ``tom``, a symbol is also held through the turn-of-month window (Lakonishok & Smidt
  1988): bought at the open of the month's last session and held through the close of the third
  session of the next month, so sold at the open of the fourth. Session dates come from the
  exchange calendar, which is known in advance (no look-ahead).
- The sleeve is split equally among the symbols that want to be held; the rest goes to
  ``cash_symbol`` (T-bills; ``None`` for plain cash).

Stateless: everything comes from today's bar, the calendar and the positions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import pandas as pd

from tp_core.calendar import next_session, sessions
from tp_trading.strategy import Context, Strategy

TOM_FIRST_SESSIONS = 3  # the window's sessions at the start of the month


def ibs(high: float, low: float, close: float) -> float:
    """Internal bar strength, in [0, 1]; NaN when the bar has no range or a value is missing."""
    if not all(math.isfinite(v) for v in (high, low, close)) or high <= low:
        return math.nan
    return min(max((close - low) / (high - low), 0.0), 1.0)


def in_tom_window(day: date) -> bool:
    """True on the month's last session and on the first three sessions of a month."""
    month = sessions(date(day.year, day.month, 1), day)
    if 0 < len(month) <= TOM_FIRST_SESSIONS and month[-1] == day:
        return True
    return next_session(day).month != day.month


@dataclass(frozen=True)
class Signal:
    symbol: str
    ibs: float  # NaN when the bar gives no reading
    low_ibs: bool
    tom: bool

    @property
    def hold(self) -> bool:
        return self.low_ibs or self.tom


def ibs_signals(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    universe: Sequence[str],
    *,
    entry: float,
    tom_next: bool,
) -> list[Signal]:
    """One signal per universe symbol from the bar just closed. ``tom_next``: the next session
    is in the turn-of-month window (and the ``tom`` option is on)."""
    out = []
    for symbol in universe:
        value = ibs(
            float(high.get(symbol, math.nan)),
            float(low.get(symbol, math.nan)),
            float(close.get(symbol, math.nan)),
        )
        has_bar = math.isfinite(float(close.get(symbol, math.nan)))
        out.append(Signal(symbol, value, bool(value < entry), tom_next and has_bar))
    return out


def ibs_targets(
    signals: Sequence[Signal], cash_symbol: str | None, entry: float
) -> tuple[dict[str, float], dict[str, str]]:
    """Equal weights among the symbols to hold, the rest in ``cash_symbol``; with reasons."""
    holding = [s for s in signals if s.hold]
    share = 1.0 / len(holding) if holding else 0.0
    targets: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for s in signals:
        targets[s.symbol] = share if s.hold else 0.0
        reading = "no range (high == low)" if math.isnan(s.ibs) else f"{s.ibs:.2f}"
        if s.low_ibs:
            why = f"IBS {reading} below {entry:g}: hold through the next session"
        elif s.tom:
            why = f"IBS {reading}; next session is in the turn-of-month window: hold"
        else:
            why = f"IBS {reading} not below {entry:g}: out at the next open"
        reasons[s.symbol] = f"{s.symbol} {why}"
    if cash_symbol is not None:
        idle = 1.0 - share * len(holding)
        targets[cash_symbol] = idle if idle > 1e-9 else 0.0
        reasons[cash_symbol] = (
            f"{cash_symbol}: idle cash in T-bills"
            if idle > 1e-9
            else f"{cash_symbol}: fully invested"
        )
    return targets, reasons


@dataclass(frozen=True)
class IbsReversion(Strategy):
    name: ClassVar[str] = "ibs"
    title: ClassVar[str] = "IBS mean reversion (hold one session after a low close)"
    spec: ClassVar[str] = "docs/strategies/17-ibs.md"

    universe: tuple[str, ...] = ("QQQ",)  # ("QQQ", "SPY", "DIA") also supported
    entry: float = 0.2  # hold after a close in the bottom 20% of the day's range
    cash_symbol: str | None = "BIL"  # idle cash; None to leave it as cash
    tom: bool = False  # also hold through the turn-of-month window
    # Skip drift trims smaller than this weight, so a held position isn't topped up every day.
    min_change: float = 0.05

    def symbols(self) -> list[str]:
        cash = [self.cash_symbol] if self.cash_symbol else []
        return list(dict.fromkeys([*self.universe, *cash]))

    def on_bar(self, ctx: Context) -> None:
        universe = list(dict.fromkeys(self.universe))
        bar = {f: ctx.history(f, lookback=1).iloc[-1] for f in ("high", "low", "close")}
        signals = ibs_signals(
            bar["high"], bar["low"], bar["close"], universe,
            entry=self.entry, tom_next=self.tom and in_tom_window(next_session(ctx.now)),
        )  # fmt: skip
        targets, reasons = ibs_targets(signals, self.cash_symbol, self.entry)
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)
