"""``RiskManager``: the plan's pre-trade checks as a ``RiskGate``.

Every intent gets every applicable check, passed or failed, so the record shows why an order
went through as well as why one did not. Intents in a batch are reviewed in order and each
approval counts against the next (sells come first in a rebalance, so they free room for buys).

What "risk-reducing" means here: a sell of shares the book holds. Those orders skip the limits
that exist to stop new risk (daily loss, exposure, concentration, stale data, disabled strategy)
but still respect the kill switch, the no-short rule, price sanity and liquidity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tp_core.occ import parse_occ
from tp_core.portfolio import Classifier
from tp_risk.limits import Limits
from tp_risk.state import MemoryRiskState, RiskState, Switch
from tp_trading.events import CheckResult, OrderIntent, OrderType, RiskDecision, Side
from tp_trading.portfolio import EPSILON
from tp_trading.risk import Book

TOLERANCE = 1e-9


@dataclass
class _Working:
    """The book as it would be after the intents approved so far in this batch."""

    positions: dict[str, float]
    cash: float

    def apply(self, intent: OrderIntent) -> None:
        self.positions[intent.symbol] = (
            self.positions.get(intent.symbol, 0.0) + intent.signed_quantity
        )
        self.cash -= intent.signed_quantity * intent.reference_price


class RiskManager:
    def __init__(
        self,
        limits: Limits,
        classifier: Classifier,
        state: RiskState | None = None,
        *,
        enforce_drawdown: bool = True,
    ) -> None:
        self.limits = limits
        self.classifier = classifier
        self.state: RiskState = state if state is not None else MemoryRiskState()
        self.enforce_drawdown = enforce_drawdown
        self.events: list[dict[str, Any]] = []  # drawdown breaches, in order
        self._breached: set[str] = set()

    # -- RiskGate ---------------------------------------------------------------------------------

    def review(self, intents: Sequence[OrderIntent], book: Book) -> list[RiskDecision]:
        work = _Working(dict(book.positions), book.cash)
        kill = self.state.kill_switch()
        disabled: dict[str, Switch | None] = {}
        decisions = []
        for intent in intents:
            if intent.strategy not in disabled:
                disabled[intent.strategy] = self.state.disabled(intent.strategy)
            checks = self._checks(intent, book, work, kill, disabled[intent.strategy])
            decision = RiskDecision(intent, tuple(checks))
            if decision.approved:
                work.apply(intent)
            decisions.append(decision)
        return decisions

    def end_of_day(self, session: date, strategy: str, equity: float) -> None:
        """Track the strategy's peak; a drawdown past its limit is recorded and, when enforced,
        disables the strategy until someone re-enables it."""
        peak = self.state.peak(strategy)
        if peak is None or equity > peak:
            self.state.set_peak(strategy, equity)
            peak = equity
        drawdown = equity / peak - 1.0 if peak > 0 else 0.0
        limit = self.limits.drawdown_limit(strategy)
        if drawdown >= -limit:
            self._breached.discard(strategy)
            return
        if strategy in self._breached:
            return
        self._breached.add(strategy)
        reason = (
            f"drawdown {drawdown:.1%} from peak {peak:,.2f} breached the {limit:.0%} limit "
            f"on {session}"
        )
        self.events.append(
            {"session": session.isoformat(), "strategy": strategy, "drawdown": drawdown,
             "limit": limit, "enforced": self.enforce_drawdown}
        )  # fmt: skip
        if self.enforce_drawdown and self.state.disabled(strategy) is None:
            self.state.set_disabled(strategy, reason)

    # -- checks -----------------------------------------------------------------------------------

    def _checks(
        self,
        intent: OrderIntent,
        book: Book,
        work: _Working,
        kill: Switch | None,
        disabled: Switch | None,
    ) -> list[CheckResult]:
        lim = self.limits
        symbol = intent.symbol
        held = work.positions.get(symbol, 0.0)
        reducing = intent.side is Side.SELL and held > 0 and intent.quantity <= held + EPSILON
        checks = [
            CheckResult(
                "kill_switch",
                kill is None,
                f"engaged {kill.at}: {kill.reason}" if kill else "off",
            )
        ]
        if _is_option(symbol):
            checks.append(
                CheckResult(
                    "instrument",
                    False,
                    "options orders are not supported yet (defined-risk structures with a 5% "
                    "premium cap come with the options module)",
                )
            )
        if intent.side is Side.SELL and not reducing:
            checks.append(
                CheckResult(
                    "no_short",
                    False,
                    f"sell {intent.quantity:g} but the book holds {max(held, 0.0):g} "
                    "(no short selling)",
                )
            )
        if not reducing:
            checks.append(
                CheckResult(
                    "strategy_enabled",
                    disabled is None,
                    f"disabled {disabled.at}: {disabled.reason}" if disabled else "enabled",
                )
            )
            checks.extend(self._new_risk_checks(intent, book, work))
        if intent.order_type is OrderType.LIMIT and intent.limit_price is not None:
            deviation = abs(intent.limit_price / intent.reference_price - 1.0)
            checks.append(
                CheckResult(
                    "price_sanity",
                    deviation <= lim.max_price_deviation + TOLERANCE,
                    f"limit {intent.limit_price:.2f} is {deviation:.1%} from the last close "
                    f"{intent.reference_price:.2f} (limit {lim.max_price_deviation:.0%})",
                )
            )
        adv = book.average_volume.get(symbol)
        if adv is None or adv <= 0:
            checks.append(CheckResult("liquidity", False, f"no volume history for {symbol}"))
        else:
            share = intent.quantity / adv
            checks.append(
                CheckResult(
                    "liquidity",
                    share <= lim.max_adv_fraction + TOLERANCE,
                    f"{intent.quantity:g} shares is {share:.2%} of 20-day average volume "
                    f"(limit {lim.max_adv_fraction:.0%})",
                )
            )
        return checks

    def _new_risk_checks(
        self, intent: OrderIntent, book: Book, work: _Working
    ) -> list[CheckResult]:
        lim = self.limits
        symbol = intent.symbol
        equity = book.equity
        if equity <= 0:
            return [CheckResult("equity", False, "the book has no equity")]
        checks: list[CheckResult] = []

        change = book.equity / book.previous_equity - 1.0 if book.previous_equity > 0 else 0.0
        checks.append(
            CheckResult(
                "daily_loss",
                change > -lim.daily_loss_limit,
                f"equity {change:+.2%} since the prior close (limit -{lim.daily_loss_limit:.0%})",
            )
        )
        if lim.require_current_bar:
            last = book.last_bar.get(symbol)
            checks.append(
                CheckResult(
                    "fresh_data",
                    last == book.session,
                    f"newest {symbol} bar is {last or 'missing'}, signal session {book.session}",
                )
            )

        prices = dict(book.prices)
        prices.setdefault(symbol, intent.reference_price)
        after = dict(work.positions)
        after[symbol] = after.get(symbol, 0.0) + intent.signed_quantity
        value = {s: q * prices.get(s, 0.0) for s, q in after.items()}
        gross = sum(abs(v) for v in value.values()) / equity
        checks.append(
            CheckResult(
                "gross_exposure",
                gross <= lim.max_gross_exposure + TOLERANCE,
                f"{gross:.1%} of equity after this order (limit {lim.max_gross_exposure:.0%})",
            )
        )

        fund = self.classifier.funds.get(symbol)
        if fund is None:
            weight = value[symbol] / equity
            checks.append(
                CheckResult(
                    "max_position",
                    weight <= lim.max_position + TOLERANCE,
                    f"{symbol} {weight:.1%} of equity after this order "
                    f"(limit {lim.max_position:.0%})",
                )
            )
            sector = self._sector(symbol)
            in_sector = sum(
                v
                for s, v in value.items()
                if s not in self.classifier.funds and self._sector(s) == sector
            )
            share = in_sector / equity
            checks.append(
                CheckResult(
                    "max_sector",
                    share <= lim.max_sector + TOLERANCE,
                    f"{sector} {share:.1%} of equity after this order (limit {lim.max_sector:.0%})",
                )
            )

        if intent.stop_price is not None:
            per_share = intent.reference_price - intent.stop_price
            if per_share <= 0:
                checks.append(
                    CheckResult(
                        "risk_per_trade",
                        False,
                        f"stop {intent.stop_price:.2f} is not below the price "
                        f"{intent.reference_price:.2f}",
                    )
                )
            else:
                at_risk = per_share * intent.quantity / equity
                checks.append(
                    CheckResult(
                        "risk_per_trade",
                        at_risk <= lim.risk_per_trade + TOLERANCE,
                        f"{at_risk:.2%} of equity to the stop (limit {lim.risk_per_trade:.0%})",
                    )
                )
        return checks

    def _sector(self, symbol: str) -> str:
        """GICS sector; an unclassified name is its own bucket so it never pools with others."""
        return self.classifier.sectors.get(symbol, f"Unclassified ({symbol})")


def _is_option(symbol: str) -> bool:
    try:
        parse_occ(symbol)
    except ValueError:
        return False
    return True
