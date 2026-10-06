"""Strategy 5: RSI(2) short-term mean reversion (Connors & Alvarez 2008).

Spec: docs/strategies/05-rsi2.md. Every session, after the close:

- **Exit** a holding when it closes above its 5-day average (the bounce came), or after
  ``max_hold`` sessions (the bounce didn't).
- **Enter** names in a long-term uptrend (close above the 200-day average) whose 2-period RSI
  closed below ``entry``: a sharp short-term pullback. The most oversold go first, into at most
  ``slots`` equal positions and at most ``max_per_sector`` names per GICS sector.

Holdings last days, not months, so this is the library's high-turnover strategy.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import pandas as pd

from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_trading.portfolio import Position
from tp_trading.strategy import Context, Strategy

INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")


def wilder_rsi(closes: pd.DataFrame, period: int) -> pd.DataFrame:
    """Relative strength index with Wilder's smoothing (an EMA with alpha = 1 / period).

    100 when a window has no down moves; NaN until ``period`` changes exist.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi.where(avg_loss > 0, 100.0).where(avg_gain.notna())


@dataclass(frozen=True)
class Decision:
    sells: dict[str, str]  # symbol -> reason
    buys: list[tuple[str, str]]  # (symbol, reason), in priority order


def rsi2_decisions(
    closes: pd.DataFrame,
    positions: Mapping[str, Position],
    *,
    period: int,
    entry: float,
    trend: int,
    exit_ma: int,
    slots: int,
    max_hold: int | None,
    max_per_sector: int,
    sector: Callable[[str], str | None],
) -> Decision:
    last = closes.iloc[-1]
    rsi = wilder_rsi(closes, period).iloc[-1]
    trend_ma = closes.iloc[-trend:].mean().where(closes.iloc[-trend:].notna().all())
    exit_avg = closes.iloc[-exit_ma:].mean()

    sells: dict[str, str] = {}
    for symbol, position in positions.items():
        if position.quantity <= 0 or symbol not in closes.columns:
            continue
        held = _sessions_held(closes.index, position.opened)
        close, average = float(last[symbol]), float(exit_avg[symbol])
        if close > average:
            sells[symbol] = (
                f"{symbol} closed {close:.2f} above its {exit_ma}-day average {average:.2f} "
                f"after {held} session(s): take the bounce"
            )
        elif max_hold is not None and held >= max_hold:
            sells[symbol] = f"{symbol} held {held} sessions without a bounce: time stop"

    kept = [s for s, p in positions.items() if p.quantity > 0 and s not in sells]
    free = slots - len(kept)
    per_sector: Counter[str] = Counter(sector(s) or s for s in kept)
    oversold = rsi[(rsi < entry) & (last > trend_ma)].sort_values()
    buys: list[tuple[str, str]] = []
    for key, value in oversold.items():
        name = str(key)
        if len(buys) >= free:
            break
        if name in positions or name in sells:
            continue
        bucket = sector(name) or name
        if per_sector[bucket] >= max_per_sector:
            continue
        per_sector[bucket] += 1
        buys.append(
            (
                name,
                f"{name} RSI({period}) {value:.1f} below {entry:g} while above its {trend}-day "
                f"average ({float(last[name]):.2f} > {float(trend_ma[name]):.2f}): buy the dip",
            )
        )
    return Decision(sells, buys)


def _sessions_held(index: pd.Index, opened: date) -> int:
    """Sessions since the position was opened (the fill session counts as day 0)."""
    return int((pd.DatetimeIndex(index) > pd.Timestamp(opened)).sum())


@dataclass(frozen=True)
class Rsi2Reversion(Strategy):
    name: ClassVar[str] = "rsi2"
    title: ClassVar[str] = "RSI(2) short-term mean reversion"
    spec: ClassVar[str] = "docs/strategies/05-rsi2.md"

    universe: tuple[str, ...] = INDEX_ETFS + LARGE_CAPS
    period: int = 2
    entry: float = 10.0  # RSI below this is oversold
    trend: int = 200  # only buy above the 200-day average
    exit_ma: int = 5  # sell on a close above the 5-day average
    # At most this many positions, 1/slots of the sleeve each. With single stocks in the
    # universe keep 1/slots at or under the 10% position cap, or the risk gate rejects entries.
    slots: int = 10
    max_hold: int | None = 10  # time stop in sessions; None to hold until the bounce
    max_per_sector: int = 3

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def on_bar(self, ctx: Context) -> None:
        closes = ctx.history("close", lookback=self.trend + 1)
        if len(closes) < self.trend:
            return
        decision = rsi2_decisions(
            closes,
            ctx.positions,
            period=self.period,
            entry=self.entry,
            trend=self.trend,
            exit_ma=self.exit_ma,
            slots=self.slots,
            max_hold=self.max_hold,
            max_per_sector=self.max_per_sector,
            sector=ctx.sector,
        )
        cash = ctx.cash
        for symbol, why in decision.sells.items():
            quantity = ctx.quantity(symbol)
            cash += quantity * ctx.price(symbol)
            ctx.order(symbol, -quantity, reason=why, target_weight=0.0)
        slot = ctx.equity / self.slots
        for symbol, why in decision.buys:
            price = ctx.price(symbol)
            budget = min(slot, cash)
            quantity = budget / price if ctx.fractional else math.floor(budget / price)
            if quantity <= 0:
                break
            cash -= quantity * price
            ctx.order(symbol, quantity, reason=why, target_weight=1.0 / self.slots)
