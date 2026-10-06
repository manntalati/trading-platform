"""Strategy 4: dual momentum, "global equities momentum" (Antonacci 2014).

Spec: docs/strategies/04-dual-momentum.md. On the last session of each month:

1. **Absolute momentum**: if US stocks' trailing 12-month return beats T-bills, stay in equities;
   otherwise hold bonds.
2. **Relative momentum**: when in equities, hold whichever of US and international stocks has
   the higher 12-month return.

The whole sleeve sits in one ETF at a time, so trades are rare and large.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from tp_strategies.library.signals import pct, trailing_return
from tp_trading.strategy import Context, Strategy


def dual_momentum_pick(
    closes: pd.DataFrame, *, us: str, international: str, bonds: str, cash: str, lookback: int
) -> tuple[str | None, str]:
    """The ETF to hold and why; None when the signals can't be computed yet."""
    momentum = trailing_return(closes, lookback)
    us_ret, intl_ret = float(momentum[us]), float(momentum[international])
    hurdle = float(momentum[cash])
    if us_ret != us_ret or intl_ret != intl_ret:
        return None, "not enough history for 12-month returns"
    hurdle_text = f"T-bills ({cash}) {pct(hurdle)}"
    if hurdle != hurdle:
        hurdle, hurdle_text = 0.0, "zero (no T-bill history)"
    if us_ret <= hurdle:
        return bonds, (
            f"{us} 12-month return {pct(us_ret)} does not beat {hurdle_text}: "
            f"absolute momentum says bonds ({bonds})"
        )
    pick = us if us_ret >= intl_ret else international
    return pick, (
        f"{us} 12-month return {pct(us_ret)} beats {hurdle_text}; {us} {pct(us_ret)} vs "
        f"{international} {pct(intl_ret)}: relative momentum says {pick}"
    )


@dataclass(frozen=True)
class DualMomentum(Strategy):
    name: ClassVar[str] = "dual-momentum"
    title: ClassVar[str] = "Dual momentum (global equities momentum)"
    spec: ClassVar[str] = "docs/strategies/04-dual-momentum.md"

    us: str = "SPY"
    international: str = "EFA"
    bonds: str = "AGG"
    cash: str = "BIL"
    lookback: int = 252
    min_change: float = 0.05  # don't chase small drift: the sleeve is one ETF

    def symbols(self) -> list[str]:
        return list(dict.fromkeys([self.us, self.international, self.bonds, self.cash]))

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_last_session_of_month():
            return
        closes = ctx.history("close", lookback=self.lookback + 1)
        if len(closes) < self.lookback + 1:
            return
        pick, why = dual_momentum_pick(
            closes,
            us=self.us,
            international=self.international,
            bonds=self.bonds,
            cash=self.cash,
            lookback=self.lookback,
        )
        if pick is None:
            return
        reasons = {s: why if s == pick else f"{why}; {s} not held" for s in self.symbols()}
        ctx.order_target_weights({pick: 1.0}, reason=reasons, min_change=self.min_change)
