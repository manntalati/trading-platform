"""Strategy 14: timing by a rule from the grammar search (Phase 4).

Spec: docs/strategies/14-rule-timing.md. ``rule`` is a ``tp_strategies.grammar`` rule such as
``and(ma(200),mom(126))``: at each check (``rebalance``: daily, weekly or monthly) every asset the
rule says yes to is held at an equal share of the sleeve (one ``assets``-th each), the rest is
cash. ``tp-backtest search --families grammar`` breeds such rules; this runs one on the engine
so it can be validated (``tp-backtest validate rule-timing -p 'rule=...'``) and paper traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from tp_core.calendar import next_session
from tp_strategies.grammar import LOOKBACK, REBALANCE, latest, parse, to_text
from tp_strategies.library.ts_momentum import DIVERSIFIED
from tp_trading.strategy import Context, Strategy


def last_session_of_week(day: date) -> bool:
    return next_session(day).isocalendar()[:2] != day.isocalendar()[:2]


@dataclass(frozen=True)
class RuleTiming(Strategy):
    name: ClassVar[str] = "rule-timing"
    title: ClassVar[str] = "Rule-based timing (from the grammar search)"
    spec: ClassVar[str] = "docs/strategies/14-rule-timing.md"

    rule: str = "ma(200)"
    assets: tuple[str, ...] = DIVERSIFIED
    rebalance: str = "monthly"  # daily, weekly or monthly

    def __post_init__(self) -> None:
        parse(self.rule)  # fail at construction on a malformed rule
        if self.rebalance not in REBALANCE:
            raise ValueError(f"rebalance: {', '.join(REBALANCE)}")

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.assets))

    def _check_today(self, ctx: Context) -> bool:
        if self.rebalance == "daily" or ctx.catch_up:
            return True
        if self.rebalance == "weekly":
            return last_session_of_week(ctx.now)
        return ctx.is_last_session_of_month()

    def on_bar(self, ctx: Context) -> None:
        if not self._check_today(ctx):
            return
        closes = ctx.history("close", lookback=LOOKBACK)
        if len(closes) < 2:
            return
        node = parse(self.rule)
        verdict = latest(node, closes)
        share = 1.0 / len(self.assets)
        targets = {s: share for s in self.assets if bool(verdict.get(s, False))}
        rule = to_text(node)
        reasons = {
            s: f"{s}: {rule} {'holds' if s in targets else 'does not hold'}"
            + (f": {share:.0%} of the sleeve" if s in targets else ": out")
            for s in self.assets
        }
        ctx.order_target_weights(targets, reason=reasons)
