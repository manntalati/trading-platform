"""Options research: which stocks set up for a swing or a LEAPS position, which contract fits,
and what to do with the options you already hold.

Fixed, documented rules over the daily bars and the daily option chains in the lake, with every
number that drove an idea attached, so you can check the reasoning. Research prompts, not
advice: nothing here places an order.

Stock setups (from daily bars):

- **Uptrend**: price above its 50-day average, which is above the 200-day.
- **Pullback**: an uptrend trading back near its 20-day average (a calmer entry).
- **Extended**: more than 8% above the 20-day average or up 15%+ in a month (wait, or size down).
- **Downtrend**: price below the 50-day, which is below the 200-day (for puts).
- **Relative strength**: 3-month return against the S&P 500 (SPY).

Contracts (from the latest chain snapshot, priced at the ask to buy and the bid to sell):

- **Swing** (weeks to a couple of months): 30-100 days out, nearest 60; delta about 0.55 (at or
  slightly in the money, so the stock doesn't need a big move just to break even).
- **Debit spread** instead when options are expensive (IV rank 60%+, or implied 1.3x the
  volatility forecast for the next month, ``tp_ml.vol``'s HAR-RV; 1.3x the trailing realized
  when there's too little history to forecast): buy the swing contract, sell the 0.30-delta one
  in the same expiry.
  Cheaper, less hurt by a volatility drop, profit capped.
- **LEAPS** (stock replacement, 9+ months out): delta about 0.80; among those, the expiry and
  strike with the least time value paid per year.
- **Covered call** on 100+ shares: 21-50 days out, delta about 0.25.

The IV rank needs ``MIN_RANK_DAYS`` of daily snapshots; until then the IV-to-forecast ratio
stands in.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from tp_core import american
from tp_core import greeks as bs
from tp_core.bars import load_bars, missing_symbols
from tp_core.calendar import NEW_YORK
from tp_core.pnl import option_label
from tp_core.portfolio import Classifier
from tp_core.positions import OptionPosition, scenarios, stock_stats, years_until
from tp_core.smile import ExpiryFit, fit_expiry, term_shape
from tp_core.storage import Lake
from tp_core.volatility import atm_term, chain_spot, event_expiry, iv_at, iv_rank
from tp_ml.vol import har_forecast

Right = Literal["C", "P"]
log = logging.getLogger(__name__)

DISCLAIMER = (
    "Rules-based research from price history and option chains, with the numbers behind each "
    "idea. Not investment advice: options can lose their whole value, quickly. You decide."
)
# Asset classes the research leaves out: no directional options ideas on cash, bonds or the
# leveraged daily-reset funds.
EXCLUDED_CLASSES = frozenset(
    {
        "Cash",
        "US Bonds",
        "US Treasuries",
        "Corporate Credit",
        "Leveraged Equity",
        "Leveraged Treasuries",
    }
)
CHAIN_MAX_AGE_DAYS = 5
RICH_IV_RANK = 0.6
RICH_IV_RATIO = 1.3
FORECAST_DAYS = 21  # the volatility forecast's horizon: about the 30-day implied's
MAX_SPREAD = 0.15  # bid/ask spread as a share of the mid


@dataclass(frozen=True)
class Rules:
    swing_dte: tuple[int, int, int] = (30, 100, 60)  # min, max, target days to expiry
    swing_delta: tuple[float, float, float] = (0.45, 0.70, 0.55)
    short_delta: float = 0.30  # the sold leg of a debit spread
    leaps_min_dte: int = 270
    leaps_delta: tuple[float, float, float] = (0.65, 0.90, 0.80)
    covered_dte: tuple[int, int, int] = (21, 50, 35)
    covered_delta: tuple[float, float, float] = (0.15, 0.35, 0.25)
    roll_swing_dte: tuple[int, int, int] = (40, 100, 60)
    leaps_roll_below_dte: int = 180  # LEAPS decay picks up under ~6 months
    swing_decide_dte: int = 21
    # selling premium: 25-50 days out, where time decay is fastest relative to the risk
    income_dte: tuple[int, int, int] = (25, 50, 35)
    csp_delta: tuple[float, float, float] = (0.15, 0.35, 0.25)
    credit_short_delta: float = 0.30
    credit_long_delta: float = 0.15
    condor_short_delta: float = 0.20
    condor_long_delta: float = 0.10
    calendar_front_dte: tuple[int, int, int] = (20, 50, 30)
    calendar_back_dte: tuple[int, int, int] = (50, 120, 75)
    take_profit: float = 0.5  # +50%
    cut_loss: float = -0.5
    low_odds: float = 0.2
    ideas_per_list: int = 6
    per_group: int = 2  # names from one sector (or one fund class) per list


# -- pricing contracts ----------------------------------------------------------------------------


PRICED_COLUMNS = ("mid", "spread_pct", "years", "iv", "delta", "gamma", "theta", "vega")
QUOTE_COLUMNS = ("contract", "expiration", "right", "strike", "dte", "bid", "ask",
                 "open_interest", "implied_volatility", "underlying_price")  # fmt: skip
PRICED_STRIKES = (0.3, 2.0)  # as a share of spot: every window the picks and rolls look in


def price_chain(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    strikes: tuple[float, float] = PRICED_STRIKES,
) -> pd.DataFrame:
    """Every two-sided quote of ``chain`` (both sides, strikes within ``strikes`` of spot) with
    mid, spread, implied volatility solved from the mid and greeks per share, in one pass over
    the whole chain. The picks filter this; pricing it once per symbol is what keeps a scan of
    many chains fast. Implied volatility is Black-Scholes, with the American model for a deep
    in-the-money put Black-Scholes can't fit and the vendor's figure as the last resort."""
    keep = [k for k in QUOTE_COLUMNS if k in chain.columns]
    c = chain.loc[
        chain["strike"].between(spot * strikes[0], spot * strikes[1])
        & (chain["bid"] > 0)
        & (chain["ask"] >= chain["bid"]),
        keep,
    ]
    if c.empty:
        return c.assign(**{k: pd.Series(dtype=float) for k in PRICED_COLUMNS})
    # plain object strings: the picks filter this frame hundreds of times
    c = c.astype({k: object for k in ("contract", "right") if k in c.columns})
    c["mid"] = (c["bid"] + c["ask"]) / 2
    c["spread_pct"] = (c["ask"] - c["bid"]) / c["mid"]
    expirations = [e if isinstance(e, date) else pd.Timestamp(e).date() for e in c["expiration"]]
    by_expiry = {e: years_until(e, now) for e in set(expirations)}
    years = np.array([by_expiry[e] for e in expirations])
    call = (c["right"] == "C").to_numpy()
    mid = c["mid"].to_numpy(dtype=float)
    strike = c["strike"].to_numpy(dtype=float)
    iv = bs.implied_vol_array(mid, spot, strike, years, call, rate)
    intrinsic = np.where(call, np.maximum(spot - strike, 0.0), np.maximum(strike - spot, 0.0))
    for i in np.flatnonzero(np.isnan(iv) & ~call & (mid > intrinsic)):
        iv[i] = american.implied_vol(mid[i], spot, strike[i], years[i], call=False, rate=rate) or (
            math.nan
        )
    vendor = pd.to_numeric(c["implied_volatility"], errors="coerce").to_numpy(dtype=float)
    iv = np.where(np.isnan(iv) & (vendor > 0), vendor, iv)
    g = bs.greeks_array(spot, strike, years, iv, call, rate)
    c["years"], c["iv"] = years, iv
    for name in ("delta", "gamma", "theta", "vega"):
        c[name] = g[name]
    return c.dropna(subset=["delta"])


def priced(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    *,
    right: Right,
    dte: tuple[int, int],
    strikes: tuple[float, float] = (0.5, 1.5),
) -> pd.DataFrame:
    """Two-sided quotes of one side of the chain within an expiry and strike window, with mid,
    spread, implied volatility (solved from the mid) and greeks per share. ``chain`` is a raw
    snapshot or one ``price_chain`` already priced (then this only filters). Always has those
    columns, even when nothing in the window qualifies (an empty frame)."""
    if not _is_priced(chain):
        chain = price_chain(chain, spot, now, rate, strikes)
    out: pd.DataFrame = chain[
        (chain["right"] == right)
        & chain["dte"].between(*dte)
        & chain["strike"].between(spot * strikes[0], spot * strikes[1])
    ]
    return out


def _is_priced(chain: pd.DataFrame) -> bool:
    return all(k in chain.columns for k in PRICED_COLUMNS)


def contract_view(
    row: pd.Series, spot: float, rate: float, *, side: Literal["buy", "sell"] = "buy"
) -> dict[str, Any]:
    """What one contract costs and needs, per share and per contract (x100)."""
    call = row["right"] == "C"
    strike = float(row["strike"])
    price = float(row["ask"] if side == "buy" else row["bid"])
    years, iv = float(row["years"]), row["iv"]
    intrinsic = bs.intrinsic(spot, strike, call)
    breakeven = strike + price if call else strike - price
    above = bs.prob_above(spot, breakeven, years, iv, rate=rate) if iv else None
    itm = bs.prob_above(spot, strike, years, iv, rate=rate) if iv else None
    p_profit = (above if call else 1 - above) if above is not None else None
    if side == "sell" and p_profit is not None:
        p_profit = 1 - p_profit
    exp = row["expiration"]
    return {
        "contract": str(row["contract"]),
        "label": option_label(str(row["contract"])) or str(row["contract"]),
        "side": side,
        "right": "call" if call else "put",
        "strike": strike,
        "expiration": exp.isoformat() if isinstance(exp, date) else str(exp),
        "dte": int(row["dte"]),
        "bid": float(row["bid"]),
        "ask": float(row["ask"]),
        "mid": float(row["mid"]),
        "spread_pct": float(row["spread_pct"]),
        "open_interest": _num(row.get("open_interest")),
        "price": price,
        "cost": price * 100,
        "iv": iv,
        "delta": float(row["delta"]),
        "theta_day": float(row["theta"]) * 100,
        "vega": float(row["vega"]) * 100,
        "intrinsic": intrinsic,
        "extrinsic": max(float(row["mid"]) - intrinsic, 0.0),
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "prob_itm": (itm if call else 1 - itm) if itm is not None else None,
        "prob_profit": p_profit,
        "expected_move_pct": iv * math.sqrt(years) if iv else None,
        "leverage": abs(float(row["delta"])) * spot / float(row["mid"]) if row["mid"] else None,
    }


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _closest(c: pd.DataFrame, score: pd.Series) -> pd.Series | None:
    if c.empty:
        return None
    best: pd.Series = c.loc[score.idxmin()]  # type: ignore[call-overload]
    return best


# -- picking contracts ----------------------------------------------------------------------------


def swing_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, right: Right, rules: Rules
) -> pd.Series | None:
    lo, hi, target = rules.swing_dte
    dlo, dhi, dtarget = rules.swing_delta
    c = priced(chain, spot, now, rate, right=right, dte=(lo, hi), strikes=(0.7, 1.3))
    if c.empty:
        return None
    c = c[c["delta"].abs().between(dlo, dhi) & (c["spread_pct"] <= MAX_SPREAD)]
    thin = (c["open_interest"].fillna(1e9) < 100).astype(float)
    score = (
        (c["dte"] - target).abs() / 30
        + (c["delta"].abs() - dtarget).abs() / 0.1
        + c["spread_pct"] * 10
        + thin
    )
    return _closest(c, score)


def spread_pick(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    long: pd.Series,
    rules: Rules,
) -> dict[str, Any] | None:
    """A debit spread on ``long``: sell the ~0.30-delta contract of the same expiry, further out
    of the money."""
    right: Right = "C" if long["right"] == "C" else "P"
    dte = int(long["dte"])
    c = priced(chain, spot, now, rate, right=right, dte=(dte, dte), strikes=(0.5, 1.6))
    beyond = c["strike"] > long["strike"] if right == "C" else c["strike"] < long["strike"]
    c = c[beyond & (c["spread_pct"] <= MAX_SPREAD)]
    short = _closest(c, (c["delta"].abs() - rules.short_delta).abs())
    if short is None:
        return None
    debit = float(long["ask"]) - float(short["bid"])
    width = abs(float(short["strike"]) - float(long["strike"]))
    if debit <= 0 or debit >= width:
        return None
    breakeven = float(long["strike"]) + debit if right == "C" else float(long["strike"]) - debit
    years, iv = float(long["years"]), long["iv"]
    above = bs.prob_above(spot, breakeven, years, iv, rate=rate) if iv else None
    return {
        "long": contract_view(long, spot, rate),
        "short": contract_view(short, spot, rate, side="sell"),
        "debit": debit,
        "cost": debit * 100,
        "max_gain": (width - debit) * 100,
        "reward_risk": (width - debit) / debit,
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "prob_profit": (above if right == "C" else 1 - above) if above is not None else None,
        "theta_day": (float(long["theta"]) - float(short["theta"])) * 100,
    }


def leaps_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, right: Right, rules: Rules
) -> pd.Series | None:
    """The ~0.80-delta contract 9+ months out that pays the least time value per year."""
    dlo, dhi, dtarget = rules.leaps_delta
    c = priced(
        chain, spot, now, rate, right=right, dte=(rules.leaps_min_dte, 10_000), strikes=(0.3, 1.7)
    )
    if c.empty:
        return None
    c = c[c["delta"].abs().between(dlo, dhi) & (c["spread_pct"] <= MAX_SPREAD)]
    if c.empty:
        return None
    intrinsic = [bs.intrinsic(spot, k, right == "C") for k in c["strike"]]
    carry = (c["mid"] - intrinsic).clip(lower=0) / spot / c["years"]
    score = carry + (c["delta"].abs() - dtarget).abs() * 0.2 + c["spread_pct"] * 0.5
    return _closest(c, score)


def leaps_view(row: pd.Series, spot: float, rate: float) -> dict[str, Any]:
    """A LEAPS as a stand-in for 100 shares: what it costs against the shares, the capital it
    frees, and the time value paid per year (the price of the leverage)."""
    v = contract_view(row, spot, rate)
    years = float(row["years"])
    v |= {
        "shares_equivalent": abs(v["delta"]) * 100,
        "stock_cost": spot * 100,
        "capital_freed": spot * 100 - v["cost"],
        "carry_per_year_pct": v["extrinsic"] / spot / years if years else None,
        # what the stock has to return a year for the contract to break even at expiry
        "breakeven_cagr": (
            (v["breakeven"] / spot) ** (1 / years) - 1 if years and v["right"] == "call" else None
        ),
    }
    return v


def covered_call_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, shares: float, rules: Rules
) -> dict[str, Any] | None:
    contracts = int(shares // 100)
    if contracts < 1:
        return None
    lo, hi, target = rules.covered_dte
    dlo, dhi, dtarget = rules.covered_delta
    c = priced(chain, spot, now, rate, right="C", dte=(lo, hi), strikes=(1.0, 1.4))
    c = c[c["delta"].between(dlo, dhi) & (c["bid"] >= 0.05) & (c["strike"] > spot)]
    row = _closest(c, (c["dte"] - target).abs() / 15 + (c["delta"] - dtarget).abs() / 0.05)
    if row is None:
        return None
    v = contract_view(row, spot, rate, side="sell")
    yield_ = float(row["bid"]) / spot
    return v | {
        "contracts": contracts,
        "premium": float(row["bid"]) * 100 * contracts,
        "yield_pct": yield_,
        "annualized_pct": yield_ * 365 / max(int(row["dte"]), 1),
        "upside_to_strike_pct": float(row["strike"]) / spot - 1,
        "prob_assigned": v["prob_itm"],
    }


# -- selling premium and time spreads -------------------------------------------------------------


def _income_side(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    right: Right,
    rules: Rules,
    dte: tuple[int, int] | None = None,
) -> pd.DataFrame:
    """Out-of-the-money contracts of one side in the premium-selling window, liquid enough."""
    lo, hi, _ = rules.income_dte
    c = priced(chain, spot, now, rate, right=right, dte=dte or (lo, hi), strikes=(0.5, 1.5))
    otm = c["strike"] < spot if right == "P" else c["strike"] > spot
    return c[otm & (c["bid"] >= 0.05) & (c["spread_pct"] <= MAX_SPREAD)]


def _nearest_expiry(c: pd.DataFrame, target: int) -> pd.DataFrame:
    if c.empty:
        return c
    gap = (c["dte"] - target).abs()
    out: pd.DataFrame = c[gap == gap.min()]
    return out


def cash_secured_put_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, rules: Rules
) -> dict[str, Any] | None:
    """Sell a ~0.25-delta put 25-50 days out with the cash to buy the shares set aside: paid to
    wait for a lower entry price, or to keep the premium."""
    _, _, target = rules.income_dte
    dlo, dhi, dtarget = rules.csp_delta
    c = _nearest_expiry(_income_side(chain, spot, now, rate, "P", rules), target)
    c = c[c["delta"].abs().between(dlo, dhi)]
    row = _closest(c, (c["delta"].abs() - dtarget).abs())
    if row is None:
        return None
    v = contract_view(row, spot, rate, side="sell")
    strike, bid = float(row["strike"]), float(row["bid"])
    return v | {
        "premium": bid * 100,
        "cash_required": strike * 100,
        "yield_pct": bid / strike,
        "annualized_pct": bid / strike * 365 / max(int(row["dte"]), 1),
        "net_entry": strike - bid,
        "net_entry_discount_pct": (strike - bid) / spot - 1,
        "prob_assigned": v["prob_itm"],
    }


def credit_spread_pick(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    right: Right,
    rules: Rules,
    *,
    short_delta: float | None = None,
    long_delta: float | None = None,
    dte: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    """Sell a vertical: the ~0.30-delta contract, bought back with the ~0.15-delta one further
    out of the money, same expiry. A put spread is bullish (keeps the credit above the short
    strike), a call spread bearish. The loss is capped at the width minus the credit."""
    _, _, target = rules.income_dte
    sd = short_delta if short_delta is not None else rules.credit_short_delta
    ld = long_delta if long_delta is not None else rules.credit_long_delta
    c = _nearest_expiry(_income_side(chain, spot, now, rate, right, rules, dte), target)
    short = _closest(c, (c["delta"].abs() - sd).abs())
    if short is None:
        return None
    beyond = c["strike"] < short["strike"] if right == "P" else c["strike"] > short["strike"]
    wings = c[beyond]
    long = _closest(wings, (wings["delta"].abs() - ld).abs())
    if long is None:
        return None
    credit = float(short["bid"]) - float(long["ask"])
    width = abs(float(short["strike"]) - float(long["strike"]))
    if credit <= 0 or credit >= width:
        return None
    k = float(short["strike"])
    breakeven = k - credit if right == "P" else k + credit
    years, iv = float(short["years"]), short["iv"]
    above = bs.prob_above(spot, breakeven, years, iv, rate=rate) if iv else None
    return {
        "kind": "put credit spread" if right == "P" else "call credit spread",
        "short": contract_view(short, spot, rate, side="sell"),
        "long": contract_view(long, spot, rate),
        "credit": credit,
        "premium": credit * 100,
        "width": width,
        "max_loss": (width - credit) * 100,
        "return_on_risk": credit / (width - credit),
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "prob_profit": (above if right == "P" else 1 - above) if above is not None else None,
        "theta_day": (float(long["theta"]) - float(short["theta"])) * 100,
        "dte": int(short["dte"]),
    }


def iron_condor_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, rules: Rules
) -> dict[str, Any] | None:
    """A put credit spread and a call credit spread on the same expiry: paid if the stock stays
    between the two short strikes. For a range-bound stock with expensive options."""
    puts = credit_spread_pick(chain, spot, now, rate, "P", rules,
                              short_delta=rules.condor_short_delta,
                              long_delta=rules.condor_long_delta)  # fmt: skip
    if puts is None:
        return None
    days = puts["dte"]
    calls = credit_spread_pick(chain, spot, now, rate, "C", rules,
                               short_delta=rules.condor_short_delta,
                               long_delta=rules.condor_long_delta, dte=(days, days))  # fmt: skip
    if calls is None:
        return None
    credit = puts["credit"] + calls["credit"]
    width = max(puts["width"], calls["width"])
    if credit >= width:
        return None
    lower = puts["short"]["strike"] - credit
    upper = calls["short"]["strike"] + credit
    years = years_until(date.fromisoformat(puts["short"]["expiration"]), now)
    p_lower = bs.prob_above(spot, lower, years, puts["short"]["iv"], rate=rate)
    p_upper = bs.prob_above(spot, upper, years, calls["short"]["iv"], rate=rate)
    return {
        "kind": "iron condor",
        "put_spread": puts,
        "call_spread": calls,
        "credit": credit,
        "premium": credit * 100,
        "max_loss": (width - credit) * 100,
        "return_on_risk": credit / (width - credit),
        "lower_breakeven": lower,
        "upper_breakeven": upper,
        "range_pct": (lower / spot - 1, upper / spot - 1),
        "prob_profit": max(p_lower - p_upper, 0.0),
        "theta_day": puts["theta_day"] + calls["theta_day"],
        "dte": days,
    }


def calendar_pick(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    rules: Rules,
    right: Right = "C",
) -> dict[str, Any] | None:
    """Sell the ~30-day at-the-money option and buy the same strike ~75 days out: paid by the
    front month's faster decay while the stock sits near the strike. It also gains if implied
    volatility rises, and most when the front month's is the richer one."""
    flo, fhi, ftarget = rules.calendar_front_dte
    blo, bhi, btarget = rules.calendar_back_dte
    front = priced(chain, spot, now, rate, right=right, dte=(flo, fhi), strikes=(0.85, 1.15))
    back = priced(chain, spot, now, rate, right=right, dte=(blo, bhi), strikes=(0.85, 1.15))
    front = _nearest_expiry(front[front["spread_pct"] <= MAX_SPREAD], ftarget)
    back = _nearest_expiry(back[back["spread_pct"] <= MAX_SPREAD], btarget)
    common = sorted(set(front["strike"]) & set(back["strike"]), key=lambda k: abs(k - spot))
    if not common:
        return None
    k = common[0]
    f = front[front["strike"] == k].iloc[0]
    b = back[back["strike"] == k].iloc[0]
    debit = float(b["ask"]) - float(f["bid"])
    if debit <= 0 or not f["iv"] or not b["iv"]:
        return None
    left = float(b["years"]) - float(f["years"])
    call = right == "C"

    def at_front_expiry(level: float) -> float:
        return bs.price(level, k, left, float(b["iv"]), call=call, rate=rate) - bs.intrinsic(
            level, k, call
        )

    levels = np.linspace(spot * 0.7, spot * 1.3, 241)
    pnl = np.array([at_front_expiry(x) - debit for x in levels])
    winning = levels[pnl > 0]
    lo, hi = (float(winning.min()), float(winning.max())) if len(winning) else (None, None)
    years_f, iv_f = float(f["years"]), float(f["iv"])
    prob = (
        bs.prob_above(spot, lo, years_f, iv_f, rate=rate)
        - bs.prob_above(spot, hi, years_f, iv_f, rate=rate)
        if lo is not None and hi is not None
        else 0.0
    )
    return {
        "kind": "calendar",
        "front": contract_view(f, spot, rate, side="sell"),
        "back": contract_view(b, spot, rate),
        "strike": k,
        "debit": debit,
        "cost": debit * 100,
        "max_profit_est": (at_front_expiry(k) - debit) * 100,
        "profit_range": (lo, hi),
        "prob_profit": max(prob, 0.0),
        "theta_day": (float(b["theta"]) - float(f["theta"])) * 100,
        "vega": (float(b["vega"]) - float(f["vega"])) * 100,
        "front_iv": iv_f,
        "back_iv": float(b["iv"]),
    }


# -- vol context ----------------------------------------------------------------------------------


def vol_context(
    chain: pd.DataFrame | None,
    hv20: float | None,
    history: pd.Series | None,
    *,
    spot: float | None = None,
    now: datetime | None = None,
    rate: float = 0.0,
    forecast: float | None = None,
) -> dict[str, Any]:
    """Implied volatility 30 days out, its rank in its own history, against realized
    volatility and against the forecast of the next month's (``forecast``), and any event the
    term structure shows; with ``spot`` and ``now``, also the skew of the ~30-day smile (SVI
    fit) and the term structure's slope (``tp_core.smile``).

    Expensive (``rich``) means a high IV rank, or implied at least ``RICH_IV_RATIO`` times the
    forecast volatility (the trailing realized when there is no forecast): what a seller is
    paid for is implied minus the volatility still to come, not the volatility already past."""
    if chain is None or chain.empty:
        return {
            "iv30": None,
            "hv20": hv20,
            "iv_hv": None,
            "rv_forecast": forecast,
            "iv_rv": None,
            "rank": None,
            "percentile": None,
            "history_days": 0,
            "event": None,
            "rich": False,
            "as_of": None,
            "skew_25d": None,
            "butterfly_25d": None,
            "term_ratio": None,
            "term_shape": None,
        }
    term = atm_term(chain)
    iv30 = iv_at(term)
    shape = term_shape(iv30, iv_at(term, 90.0) if len(term) and term["dte"].max() >= 60 else None)
    skew: dict[str, Any] = {"skew_25d": None, "butterfly_25d": None, "smile_arbitrage": None}
    if spot and now is not None:
        fit = _fit_near(chain, spot, now, rate, 30)
        if fit is not None:
            skew = {
                "skew_25d": fit.skew_25d,
                "butterfly_25d": fit.butterfly_25d,
                "smile_arbitrage": fit.butterfly_arbitrage,
            }
    rank = iv_rank(history if history is not None else pd.Series(dtype=float), iv30)
    ratio = iv30 / hv20 if iv30 and hv20 else None
    to_forecast = iv30 / forecast if iv30 and forecast else None
    judged = to_forecast if to_forecast is not None else ratio
    rich = bool(
        (rank["rank"] is not None and rank["rank"] >= RICH_IV_RANK)
        or (judged is not None and judged >= RICH_IV_RATIO)
    )
    return {
        "iv30": iv30,
        "hv20": hv20,
        "iv_hv": ratio,
        "rv_forecast": forecast,
        "iv_rv": to_forecast,
        "rank": rank["rank"],
        "percentile": rank["percentile"],
        "history_days": rank["days"],
        "iv_low": rank["low"],
        "iv_high": rank["high"],
        "event": event_expiry(term),
        "rich": rich,
        "as_of": chain["snapshot_at"].max().isoformat(),
        **skew,
        "term_ratio": shape["ratio"],
        "term_shape": shape["shape"],
    }


def _fit_near(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, days: int
) -> ExpiryFit | None:
    """The SVI fit of the expiration nearest ``days`` out (at least a week) that fits."""
    exps = sorted(
        {e for e in set(chain["expiration"]) if 7 <= years_until(e, now) * bs.YEAR_DAYS <= 120},
        key=lambda e: abs(years_until(e, now) * bs.YEAR_DAYS - days),
    )
    for exp in exps[:3]:
        out = fit_expiry(chain, spot, exp, now, rate)
        if out is not None:
            return out[0]
    return None


# -- one symbol -----------------------------------------------------------------------------------


def stock_setup(stats: Mapping[str, Any], rs_3m: float | None) -> dict[str, Any]:
    """Trend, pullback and strength facts from ``stock_stats``."""
    v20, v50, v200 = (_num(stats.get(k)) for k in ("vs_ma20", "vs_ma50", "vs_ma200"))
    ma50, ma200 = _num(stats.get("ma50")), _num(stats.get("ma200"))
    month = _num((stats.get("returns") or {}).get("1M"))
    up = (
        v50 is not None and v200 is not None and v50 > 0 and v200 > 0 and (ma50 or 0) > (ma200 or 0)
    )
    down = (
        v50 is not None and v200 is not None and v50 < 0 and v200 < 0 and (ma50 or 0) < (ma200 or 0)
    )
    tags = []
    if up:
        tags.append("uptrend")
        if v20 is not None and -0.04 <= v20 <= 0.01:
            tags.append("pullback")
    if down:
        tags.append("downtrend")
    if (v20 is not None and v20 > 0.08) or (month is not None and month > 0.15):
        tags.append("extended")
    high = _num(stats.get("from_high_pct"))
    if high is not None and high > -0.03:
        tags.append("near 52-week high")
    if rs_3m is not None:
        if rs_3m > 0.05:
            tags.append("beating the S&P 500")
        elif rs_3m < -0.05:
            tags.append("lagging the S&P 500")
    direction = "bullish" if up else "bearish" if down else "neutral"
    return {"direction": direction, "tags": tags, "rs_3m": rs_3m}


def analyze(
    symbol: str,
    closes: pd.Series,
    *,
    chain: pd.DataFrame | None,
    benchmark: pd.Series | None,
    iv_hist: pd.Series | None,
    now: datetime,
    rate: float,
    rules: Rules | None = None,
    forecast: float | None = None,
    priced: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Everything the research knows about one symbol: the stock, its options' pricing, and the
    contract each approach would use. ``forecast``: the next month's volatility forecast;
    ``priced``: the chain already priced (``price_chain``), to save pricing it twice."""
    rules = rules or Rules()
    stats = stock_stats(closes, None, benchmark)
    spot_now = chain_spot(chain) if chain is not None and not chain.empty else None
    spot = spot_now or _num(stats.get("price"))
    rs = _relative_strength(closes, benchmark)
    setup = stock_setup(stats, rs)
    vol = vol_context(
        chain, _num(stats.get("vol_20d")), iv_hist, spot=spot, now=now, rate=rate,
        forecast=forecast,
    )  # fmt: skip
    out: dict[str, Any] = {
        "symbol": symbol,
        "spot": spot,
        "stock": {
            k: stats.get(k)
            for k in (
                "price",
                "day_change_pct",
                "returns",
                "ytd",
                "high_52w",
                "low_52w",
                "from_high_pct",
                "vs_ma20",
                "vs_ma50",
                "vs_ma200",
                "vol_20d",
                "beta_1y",
                "max_drawdown_1y",
            )
        },
        "setup": setup,
        "vol": vol,
        "picks": {},
    }
    if chain is None or chain.empty or not spot:
        return out
    chain = priced if priced is not None else price_chain(chain, spot, now, rate)
    picks: dict[str, Any] = {}
    for right, name in (("C", "call"), ("P", "put")):
        row = swing_pick(chain, spot, now, rate, right, rules)  # type: ignore[arg-type]
        if row is not None:
            view = contract_view(row, spot, rate)
            view["scenarios"] = _scenarios(row, spot, now, rate)
            picks[f"swing_{name}"] = view
            spread = spread_pick(chain, spot, now, rate, row, rules)
            if spread is not None:
                picks[f"{name}_spread"] = spread
    leaps = leaps_pick(chain, spot, now, rate, "C", rules)
    if leaps is not None:
        picks["leaps_call"] = leaps_view(leaps, spot, rate)
    income = {
        "cash_secured_put": cash_secured_put_pick(chain, spot, now, rate, rules),
        "put_credit_spread": credit_spread_pick(chain, spot, now, rate, "P", rules),
        "call_credit_spread": credit_spread_pick(chain, spot, now, rate, "C", rules),
        "iron_condor": iron_condor_pick(chain, spot, now, rate, rules),
        "calendar": calendar_pick(chain, spot, now, rate, rules),
    }
    picks |= {k: v for k, v in income.items() if v is not None}
    out["picks"] = picks
    return out


def _relative_strength(
    closes: pd.Series, benchmark: pd.Series | None, days: int = 63
) -> float | None:
    if benchmark is None or len(closes) <= days or len(benchmark) <= days:
        return None
    mine = float(closes.iloc[-1] / closes.iloc[-1 - days] - 1)
    spy = float(benchmark.iloc[-1] / benchmark.iloc[-1 - days] - 1)
    return mine - spy


def _scenarios(row: pd.Series, spot: float, now: datetime, rate: float) -> dict[str, Any] | None:
    if not row["iv"]:
        return None
    exp = (
        row["expiration"]
        if isinstance(row["expiration"], date)
        else pd.Timestamp(row["expiration"]).date()
    )
    pos = OptionPosition(
        symbol=str(row["contract"]),
        label="",
        underlying="",
        quantity=1.0,
        strike=float(row["strike"]),
        expiration=exp,
        call=row["right"] == "C",
        cost=float(row["ask"]),
        broker_price=None,
    )
    return scenarios(pos, spot=spot, iv=float(row["iv"]), now=now, rate=rate)


# -- the whole scan -------------------------------------------------------------------------------


def eligible(symbol: str, classifier: Classifier) -> bool:
    return classifier.funds.get(symbol) not in EXCLUDED_CLASSES


def bars_from_lake(
    lake: Lake, symbols: list[str], today: date, days: int = 560
) -> dict[str, pd.DataFrame]:
    """Adjusted daily ``open``, ``high``, ``low``, ``close`` per symbol (DatetimeIndex)."""
    present = [s for s in dict.fromkeys(symbols) if s not in missing_symbols(lake, [s])]
    if not present:
        return {}
    bars = load_bars(lake, present, start=today - timedelta(days=days))
    out = {}
    for symbol, g in bars.groupby("symbol"):
        frame = g[["adj_open", "adj_high", "adj_low", "adj_close"]].astype(float)
        frame.columns = ["open", "high", "low", "close"]
        frame.index = pd.DatetimeIndex(g["session"])
        out[str(symbol)] = frame
    return out


def rv_forecast(bars: pd.DataFrame | None) -> float | None:
    """The next month's volatility as HAR-RV forecasts it (``tp_ml.vol``): what implied
    volatility should be compared with, rather than last month's realized."""
    if bars is None:
        return None
    return har_forecast(bars, horizon=FORECAST_DAYS, min_sessions=200)


# -- what to do with what you hold ----------------------------------------------------------------


def holding_actions(
    position: Mapping[str, Any],
    chain: pd.DataFrame | None,
    *,
    now: datetime,
    rate: float,
    rules: Rules | None = None,
) -> list[dict[str, Any]]:
    """Concrete choices for one position (a row of the positions view), priced from the latest
    chain: rolls, taking profits, cutting low-odds losers, covered calls, LEAPS in place of
    shares, protective puts. Each says what it costs or brings in."""
    rules = rules or Rules()
    have_chain = chain is not None and not chain.empty
    if position.get("kind") == "option":  # the same spot each would price with
        spot = _num((position.get("stats") or {}).get("spot"))
        spot = spot or (chain_spot(chain) if chain is not None and have_chain else None)
    else:
        spot = _num(position.get("price"))
    if chain is not None and have_chain and spot:
        chain = price_chain(chain, spot, now, rate)
    if position.get("kind") == "option":
        return _option_actions(position, chain, now, rate, rules)
    return _stock_actions(position, chain, now, rate, rules)


def _action(
    level: str, title: str, text: str, trade: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"level": level, "title": title, "text": text, "trade": trade}


def _usd(value: float | None) -> str:
    if value is None or value != value:
        return "—"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _option_actions(
    p: Mapping[str, Any], chain: pd.DataFrame | None, now: datetime, rate: float, rules: Rules
) -> list[dict[str, Any]]:
    s = p.get("stats") or {}
    qty = float(p.get("quantity") or 0)
    if qty <= 0:
        return []  # short options: the notes cover assignment; no rules here yet
    days = _num(s.get("days_left")) or 0.0
    pnl_pct, mark = _num(s.get("pnl_pct")), _num(s.get("mark"))
    bid, delta = _num(s.get("bid")), _num(s.get("delta"))
    spot = _num(s.get("spot")) or (chain_spot(chain) if chain is not None else None)
    right: Right = "C" if s.get("right") == "call" else "P"
    strike = float(s.get("strike") or 0)
    size = qty * 100
    out: list[dict[str, Any]] = []
    if chain is not None and not chain.empty and bid is None:
        mine = chain[chain["contract"] == p.get("symbol")]
        bid = _num(mine["bid"].iloc[0]) if len(mine) else None
    sell_value = bid * size if bid is not None else (mark * size if mark is not None else None)
    have_chain = chain is not None and not chain.empty and spot

    if days >= rules.leaps_roll_below_dte:
        if pnl_pct is not None and pnl_pct >= 1.0 and delta is not None and abs(delta) >= 0.85:
            up = (
                _roll(
                    chain,
                    spot,
                    now,
                    rate,
                    right,
                    strike,
                    rules,
                    same_expiry=s.get("expiration"),
                    target_delta=0.70,
                )
                if have_chain
                else None
            )
            text = (
                f"Up {pnl_pct:.0%} and deep in the money (delta {delta:.2f}): it now moves "
                "almost like the shares."
            )
            if up is not None and bid is not None:
                credit = (bid - up["ask"]) * size
                text += (
                    f" Rolling up to the {up['label']} (delta {up['delta']:.2f}) would take about "
                    f"{_usd(credit)} off the table and keep most of the exposure."
                )
            out.append(_action("consider", "Lock in some of the gain", text, up))
        return out

    if 90 <= days < rules.leaps_roll_below_dte and delta is not None and abs(delta) >= 0.6:
        longer = (
            _roll(chain, spot, now, rate, right, strike, rules, min_dte=rules.leaps_min_dte)
            if have_chain
            else None
        )
        text = (
            f"{days:.0f} days left: under about six months, a long-dated option's time decay "
            "picks up. "
            "If you're holding for the long run, rolling out keeps the decay low."
        )
        if longer is not None and bid is not None:
            net = _usd((longer["ask"] - bid) * size)
            text += f" Rolling to the {longer['label']} costs about {net} net."
        out.append(_action("info", "Roll out (long-term holding)", text, longer))

    if pnl_pct is not None and pnl_pct >= rules.take_profit and days < 90:
        half = max(int(qty // 2), 1)
        part = (
            f"selling {half} of {int(qty)} returns about {_usd((bid or mark or 0) * half * 100)}"
            if qty > 1
            else (f"selling returns about {_usd(sell_value)}")
        )
        text = (
            f"Up {pnl_pct:.0%}. A common swing rule is to take some off at +50% to +100%: {part}, "
            "and the rest rides with the risk mostly off the table."
        )
        out.append(_action("consider", "Take profits", text))

    extrinsic = _num(s.get("extrinsic"))
    if (
        0 < days <= rules.swing_decide_dte
        and mark
        and extrinsic is not None
        and extrinsic / mark >= 0.3
    ):
        later = _roll(chain, spot, now, rate, right, strike, rules) if have_chain else None
        theta_now = _num(s.get("theta_day"))
        text = (
            f"{days:.0f} days left and {extrinsic / mark:.0%} of the price is time value, which "
            f"decays fastest from here (about {_usd(-(theta_now or 0))} a day now). "
            f"Closing returns about {_usd(sell_value)}."
        )
        if later is not None and bid is not None:
            text += (
                f" Rolling out to the {later['label']} ({later['dte']} days) costs about "
                f"{_usd((later['ask'] - bid) * size)} net and decays about "
                f"{_usd(-later['theta_day'] * qty)} a day."
            )
        out.append(_action("consider", "Close or roll before the decay speeds up", text, later))

    p_profit = _num(s.get("prob_profit"))
    if (
        pnl_pct is not None
        and pnl_pct <= rules.cut_loss
        and p_profit is not None
        and p_profit < rules.low_odds
    ):
        text = (
            f"Down {-pnl_pct:.0%} with about a {p_profit:.0%} chance of finishing at a profit "
            f"(the market's odds). Selling now salvages about {_usd(sell_value)}; holding risks "
            "that too, for the chance of a larger move."
        )
        out.append(_action("attention", "Low odds of recovering", text))
    return out


def _roll(
    chain: pd.DataFrame | None,
    spot: float | None,
    now: datetime,
    rate: float,
    right: Right,
    strike: float,
    rules: Rules,
    *,
    min_dte: int | None = None,
    same_expiry: object = None,
    target_delta: float | None = None,
) -> dict[str, Any] | None:
    """The contract to roll into: later (the swing window, or the longest LEAPS) at the same
    strike, or, with ``same_expiry``, the strike nearest ``target_delta``."""
    if chain is None or chain.empty or not spot:
        return None
    if same_expiry is not None:
        exp = date.fromisoformat(str(same_expiry))
        days = (exp - now.astimezone(NEW_YORK).date()).days
        c = priced(chain, spot, now, rate, right=right, dte=(days, days), strikes=(0.3, 2.0))
        row = _closest(c, (c["delta"].abs() - (target_delta or 0.7)).abs())
    elif min_dte is not None:
        c = priced(chain, spot, now, rate, right=right, dte=(min_dte, 10_000), strikes=(0.3, 2.0))
        if c.empty:
            return None
        c = c[c["dte"] == c["dte"].max()]
        row = _closest(c, (c["strike"] - strike).abs())
    else:
        lo, hi, target = rules.roll_swing_dte
        c = priced(chain, spot, now, rate, right=right, dte=(lo, hi), strikes=(0.3, 2.0))
        row = _closest(
            c, (c["dte"] - target).abs() / 30 + (c["strike"] - strike).abs() / max(spot * 0.02, 0.5)
        )
    return contract_view(row, spot, rate) if row is not None else None


def _stock_actions(
    p: Mapping[str, Any], chain: pd.DataFrame | None, now: datetime, rate: float, rules: Rules
) -> list[dict[str, Any]]:
    shares = float(p.get("quantity") or 0)
    spot = _num(p.get("price"))
    if chain is None or chain.empty or not spot or shares <= 0:
        return []
    out: list[dict[str, Any]] = []
    stats = p.get("stats") or {}
    if shares >= 100:
        cc = covered_call_pick(chain, spot, now, rate, shares, rules)
        if cc is not None:
            text = (
                f"Selling {cc['contracts']} {cc['label']} would bring in about "
                f"{_usd(cc['premium'])} ({cc['yield_pct']:.1%} in {cc['dte']} days, "
                f"{cc['annualized_pct']:.0%} a year), in "
                f"exchange for giving up gains above {cc['strike']:g} "
                f"({cc['upside_to_strike_pct']:+.1%}); about a {cc['prob_assigned'] or 0:.0%} "
                "chance the "
                "shares get called away."
            )
            out.append(_action("info", "Covered call income", text, cc))
        leaps = leaps_pick(chain, spot, now, rate, "C", rules)
        if leaps is not None:
            v = leaps_view(leaps, spot, rate)
            text = (
                f"The {v['label']} (delta {v['delta']:.2f}) costs {_usd(v['cost'])} against "
                f"{_usd(v['stock_cost'])} for 100 shares, freeing {_usd(v['capital_freed'])}. "
                f"The price is about {v['carry_per_year_pct'] or 0:.1%} a year in time value, and "
                "it can expire worthless if the stock falls below the strike."
            )
            out.append(_action("info", "LEAPS in place of 100 shares", text, v))
    weight, pnl_pct = _num(p.get("weight")), _num(p.get("pnl_pct"))
    if weight is not None and weight >= 0.15 and pnl_pct is not None and pnl_pct >= 0.5:
        put = _protective_put(chain, spot, now, rate)
        if put is not None:
            covered = max(int(shares // 100), 1)
            cost = put["cost"] * covered
            text = (
                f"{weight:.0%} of the portfolio and up {pnl_pct:.0%}. {covered} {put['label']} "
                f"would cost about {_usd(cost)} ({cost / (spot * shares):.1%} of the position) to "
                f"cap the loss below {put['strike']:g} until {put['expiration']}."
            )
            out.append(_action("info", "Insure the gain", text, put))
    if _num(stats.get("vs_ma200")) is not None and stats["vs_ma200"] < 0 and (pnl_pct or 0) < 0:
        out.append(
            _action(
                "consider",
                "Below its 200-day average",
                f"Trading {stats['vs_ma200']:+.1%} vs its 200-day average and below what you "
                "paid: the long-term trend is against it.",
            )
        )
    return out


def _protective_put(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float
) -> dict[str, Any] | None:
    c = priced(chain, spot, now, rate, right="P", dte=(45, 120), strikes=(0.7, 1.0))
    c = c[c["delta"].abs().between(0.15, 0.40) & (c["spread_pct"] <= MAX_SPREAD)]
    row = _closest(c, (c["dte"] - 75).abs() / 30 + (c["delta"].abs() - 0.25).abs() / 0.05)
    return contract_view(row, spot, rate) if row is not None else None
