"""Trading costs for simulated fills.

Defaults model a commission-free US broker (Alpaca) trading liquid ETFs and large caps:

- **Slippage**: a fixed number of basis points against you on every fill, standing in for half
  the bid/ask spread plus a little market impact (the plan's starting model). Daily bars carry no
  spread, so this is a flat assumption; widen it for less liquid names and re-run.
- **Regulatory fees on sales**: the SEC Section 31 fee (a rate per dollar sold) and FINRA's
  Trading Activity Fee (per share sold, capped per trade). Both rates are reset periodically; the
  defaults are recent published values, so check sec.gov and finra.org before relying on them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tp_trading.events import Side


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 5.0
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    sec_fee_rate: float = 27.80 / 1_000_000  # $ per $ of sale proceeds
    taf_per_share: float = 0.000166  # $ per share sold
    taf_max: float = 8.30  # $ per trade

    def fill_price(self, side: Side, price: float) -> float:
        """The price you actually get: worse than ``price`` by the slippage."""
        return price * (1 + side.sign * self.slippage_bps / 10_000)

    def fees(self, side: Side, quantity: float, price: float) -> float:
        commission = 0.0
        if self.commission_per_share or self.commission_min:
            commission = max(self.commission_min, self.commission_per_share * quantity)
        if side is Side.BUY:
            return commission
        sec = self.sec_fee_rate * quantity * price
        taf = min(self.taf_max, self.taf_per_share * math.ceil(quantity))
        return commission + sec + taf

    @classmethod
    def free(cls) -> CostModel:
        """No costs at all: for checking accounting against a frictionless reference."""
        return cls(slippage_bps=0.0, sec_fee_rate=0.0, taf_per_share=0.0, taf_max=0.0)
