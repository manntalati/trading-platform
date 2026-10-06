"""Strategy 1 on the trading contract: 10-month moving-average timing (Faber 2007).

Same rule as the vectorised ``tp_strategies.ma_timing`` (spec: docs/strategies/01-ma-timing.md):
on the last session of each month, each asset is held at ``1/N`` of the strategy's capital if
its month-end close is above the average of the last ``months`` month-end closes, else its slice
sits in cash. With ``fill_at="next_close", size_at="fill"`` and no costs the engine reproduces
the vectorised returns exactly (see the parity test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from tp_strategies.ma_timing import month_end_closes
from tp_trading.strategy import Context, Strategy

GTAA = ("SPY", "EFA", "IEF", "VNQ", "DBC")


@dataclass(frozen=True)
class MaTiming(Strategy):
    name: ClassVar[str] = "ma-timing"
    title: ClassVar[str] = "10-month moving-average timing"
    spec: ClassVar[str] = "docs/strategies/01-ma-timing.md"

    assets: tuple[str, ...] = GTAA
    months: int = 10
    min_change: float = 0.0  # skip rebalance trades smaller than this (weight); 0 = always

    def symbols(self) -> list[str]:
        return list(self.assets)

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_rebalance_day():
            return
        month_end = month_end_closes(ctx.history("close"))
        if len(month_end) < self.months:
            return
        window = month_end.iloc[-self.months :]
        sma = window.mean().where(window.notna().all())
        last = month_end.iloc[-1]
        slice_ = 1.0 / len(self.assets)
        targets: dict[str, float] = {}
        reasons: dict[str, str] = {}
        for symbol in self.assets:
            close, average = float(last[symbol]), float(sma[symbol])
            invested = close > average  # False when either is NaN
            targets[symbol] = slice_ if invested else 0.0
            reasons[symbol] = (
                f"{symbol} month-end close {close:.2f} is "
                f"{'above' if invested else 'not above'} its {self.months}-month average "
                f"{average:.2f}: {'invested' if invested else 'in cash'}"
            )
        ctx.order_target_weights(targets, reason=reasons, min_change=self.min_change)
