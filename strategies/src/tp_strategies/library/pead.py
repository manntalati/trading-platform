"""Strategy 9: post-earnings-announcement drift (Ball and Brown; Bernard and Thomas).

Spec: docs/strategies/09-pead.md. Each session, results that reached the data that day (a 10-Q
or 10-K, ``ctx.reference("earnings")``) with a standardized surprise (SUE) of ``threshold`` or
more are bought, the largest first, into at most ``slots`` equal positions; each is held for
``hold`` sessions, or sold earlier if the company's next results come in with a negative SUE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import pandas as pd

from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_trading.strategy import Context, Strategy


@dataclass(frozen=True)
class EarningsDrift(Strategy):
    name: ClassVar[str] = "pead"
    title: ClassVar[str] = "Post-earnings-announcement drift (SUE)"
    spec: ClassVar[str] = "docs/strategies/09-pead.md"

    universe: tuple[str, ...] = LARGE_CAPS
    threshold: float = 2.0  # SUE at or above this is a buy
    hold: int = 60  # sessions
    slots: int = 10  # 1/slots each; keep it at or under the 10% position cap

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def on_bar(self, ctx: Context) -> None:
        events = ctx.reference("earnings")
        events = events[events["symbol"].isin(self.universe)]
        today = events[events["session"] == ctx.now]
        index = ctx.history("close", lookback=self.hold + 5).index
        held = {s: p for s, p in ctx.positions.items() if p.quantity > 0}
        sells: dict[str, str] = {}
        for symbol, position in held.items():
            days = _sessions_since(index, position.opened)
            news = today[today["symbol"] == symbol]
            if len(news) and _num(news["sue"].iloc[-1]) is not None and news["sue"].iloc[-1] < 0:
                sells[symbol] = f"{symbol} new results, SUE {news['sue'].iloc[-1]:+.1f}: exit"
            elif days >= self.hold:
                sells[symbol] = f"{symbol} held {days} sessions: the drift window is over"
        cash = ctx.cash
        for symbol, why in sells.items():
            quantity = ctx.quantity(symbol)
            cash += quantity * ctx.price(symbol)
            ctx.order(symbol, -quantity, reason=why, target_weight=0.0)
        free = self.slots - (len(held) - len(sells))
        candidates = today[(today["sue"] >= self.threshold) & ~today["symbol"].isin(list(held))]
        slot = ctx.equity / self.slots
        for _, row in candidates.sort_values("sue", ascending=False).head(max(free, 0)).iterrows():
            symbol = str(row["symbol"])
            price = ctx.price(symbol)
            if not (math.isfinite(price) and price > 0):
                continue
            budget = min(slot, cash)
            quantity = budget / price if ctx.fractional else math.floor(budget / price)
            if quantity <= 0:
                break
            cash -= quantity * price
            why = (
                f"{symbol} quarter to {row['end']}: EPS {row['eps']:.2f}, SUE {row['sue']:+.1f} "
                f"(filed {row['known']}): buy the drift"
            )
            ctx.order(symbol, quantity, reason=why, target_weight=1.0 / self.slots)


def _num(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _sessions_since(index: pd.Index, opened: date) -> int:
    return int((pd.DatetimeIndex(index) > pd.Timestamp(opened)).sum())
