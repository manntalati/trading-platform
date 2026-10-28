"""Entry setups for options trades: when a stock is worth an options position, and when to get
out. Each setup is a fixed rule on daily bars with a direction, a horizon (how long the move it
bets on usually takes) and an exit plan, so the ideas list rotates as setups fire and expire
instead of showing the same strongest names every day.

- ``pullback``: an uptrend (above the 200-day, the 50-day above the 200-day) closes with RSI(2)
  under 10. Bets on the dip reverting within days (Connors); out on a close above the 5-day
  average.
- ``breakout``: a new 50-day closing high above the 200-day, within 5 sessions of the tightest
  Bollinger bandwidth of 6 months (a squeeze). Bets on volatility expanding the breakout's way;
  out on a close below the 10-day average.
- ``leader``: a leader (12-1 month momentum 10%+, in the top 40% of the names, within 15% of its
  52-week high, in an uptrend) comes back to its 50-day average. Bets on the trend going on for
  months, with LEAPS; out on a close below the 200-day average.
- ``earnings_up`` / ``earnings_down``: results reach the data with a surprise (SUE) of +-1.5 or
  more, the stock having moved the same way over the 10 sessions to then. Bets on post-earnings
  drift (Bernard and Thomas); out on a close back through the 50-day average.
- ``rally_fade``: a downtrend closes with RSI(2) over 90. Bets on the bounce failing; out on a
  close below the 5-day average.
- ``breakdown``: a new 50-day closing low below the 200-day, the 50-day under the 200-day. Bets
  on the decline going on; out on a close above the 10-day average.

Premium selling and cheap volatility are judged on the option chain, not the bars
(``tp_strategies.playbook``). The upward setups also make stock ideas: shares, with a stop and an
optional target some multiples of the stock's average true range away, a time stop and the
setup's stock-side exit (``StockExits``). The default exits are rules of thumb; ``tp-recs
calibrate`` replaces them, for options and shares, with the ones that held up best in the
backtest (``tp_strategies.playbook_backtest``).
Everything here only looks back: a setup on session t uses closes up to t.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np
import pandas as pd

from tp_core.indicators import wilder_rsi

ListName = Literal["swing", "leaps", "bearish", "income", "volatility"]
Structure = Literal["long_call", "long_put", "leaps_call", "credit", "straddle"]


@dataclass(frozen=True)
class Exits:
    """When to close: a share of the premium won or lost (credits: of the premium received), a
    number of sessions, days to expiry, and a rule on the stock (``UNDERLYING_EXITS``)."""

    take_profit: float | None  # +0.5: up 50% on the premium paid; credits: 50% of it kept
    stop_loss: float | None  # -0.5: half the premium lost; credits: -2.0 is twice it lost
    time_stop: int  # sessions after entry
    min_dte: int  # close (or roll) when this few days are left
    underlying: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "time_stop": self.time_stop,
            "min_dte": self.min_dte,
            "underlying": self.underlying,
        }


@dataclass(frozen=True)
class StockExits:
    """When to sell shares bought on a setup: a stop and an optional target, each a multiple of
    the stock's average true range (14 sessions) at entry away from the entry price; a number
    of sessions; and the setup's stock-side rule (``UNDERLYING_EXITS``)."""

    stop_atr: float
    target_atr: float | None
    time_stop: int  # sessions after entry
    underlying: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_atr": self.stop_atr,
            "target_atr": self.target_atr,
            "time_stop": self.time_stop,
            "underlying": self.underlying,
        }


@dataclass(frozen=True)
class Setup:
    name: str
    title: str
    list: ListName
    direction: int  # +1 up, -1 down, 0 either
    horizon: int  # sessions the move usually takes: the contract is valued at this point
    valid_for: int  # sessions after the signal an entry still counts
    structure: Structure
    exits: Exits
    rule: str  # the trigger in words
    stock: StockExits | None = None  # shares too (the upward setups)


SETUPS: dict[str, Setup] = {
    s.name: s
    for s in (
        Setup(
            "pullback",
            "Pullback in an uptrend",
            "swing",
            1,
            10,
            2,
            "long_call",
            Exits(0.5, -0.5, 10, 21, "above_ma5"),
            "RSI(2) under 10 in an uptrend (above the 200-day, the 50-day above the 200-day)",
            StockExits(2.5, None, 10, "above_ma5"),
        ),
        Setup(
            "breakout",
            "Breakout from a squeeze",
            "swing",
            1,
            20,
            3,
            "long_call",
            Exits(1.0, -0.5, 20, 21, "below_ma10"),
            "a new 50-day closing high above the 200-day, within 5 sessions of the tightest "
            "Bollinger bandwidth of the last 6 months",
            StockExits(2.5, None, 40, "below_ma10"),
        ),
        Setup(
            "leader",
            "Trend leader at its 50-day",
            "leaps",
            1,
            126,
            10,
            "leaps_call",
            Exits(None, -0.5, 189, 180, "below_ma200"),
            "12-1 month momentum of 10%+ (and in the top 40% of the names), within 15% of the "
            "52-week high and in an uptrend, back near its 50-day average",
            StockExits(4.0, None, 189, "below_ma200"),
        ),
        Setup(
            "earnings_up",
            "Earnings beat, drifting up",
            "swing",
            1,
            40,
            5,
            "long_call",
            Exits(1.0, -0.5, 40, 21, "below_ma50"),
            "results with a surprise (SUE) of +1.5 or more, the stock up over the 10 sessions "
            "to their filing",
            StockExits(3.0, None, 60, "below_ma50"),
        ),
        Setup(
            "earnings_down",
            "Earnings miss, drifting down",
            "bearish",
            -1,
            40,
            5,
            "long_put",
            Exits(1.0, -0.5, 40, 21, "above_ma50"),
            "results with a surprise (SUE) of -1.5 or less, the stock down over the 10 sessions "
            "to their filing",
        ),
        Setup(
            "rally_fade",
            "Bounce in a downtrend",
            "bearish",
            -1,
            10,
            2,
            "long_put",
            Exits(0.5, -0.5, 10, 21, "below_ma5"),
            "RSI(2) over 90 in a downtrend (below the 200-day, the 50-day below the 200-day)",
        ),
        Setup(
            "breakdown",
            "Breakdown to a new low",
            "bearish",
            -1,
            20,
            3,
            "long_put",
            Exits(1.0, -0.5, 20, 21, "above_ma10"),
            "a new 50-day closing low below the 200-day, the 50-day below the 200-day",
        ),
        Setup(
            "rich_premium",
            "Expensive options: sell premium",
            "income",
            0,
            15,
            5,
            "credit",
            Exits(0.5, -2.0, 25, 21, None),
            "30-day implied volatility at least 1.25x the forecast, IV rank 50%+ when known, no "
            "event priced before expiry, not in a downtrend",
        ),
        Setup(
            "cheap_vol",
            "Cheap options: buy volatility",
            "volatility",
            0,
            15,
            5,
            "straddle",
            Exits(0.3, -0.35, 15, 21, None),
            "30-day implied volatility at most 0.9x the forecast, the term structure not inverted "
            "and no event priced in",
        ),
    )
}
STOCK_SETUPS = ("pullback", "breakout", "leader", "earnings_up")  # the ones with ``stock``
BAR_SETUPS = (
    "pullback",
    "breakout",
    "leader",
    "earnings_up",
    "earnings_down",
    "rally_fade",
    "breakdown",
)

UNDERLYING_EXITS: dict[str, tuple[str, str, int]] = {
    # rule: (indicator, words, +1 exit when the close is above it / -1 below it)
    "above_ma5": ("ma5", "a close above the 5-day average", 1),
    "below_ma5": ("ma5", "a close below the 5-day average", -1),
    "below_ma10": ("ma10", "a close below the 10-day average", -1),
    "above_ma10": ("ma10", "a close above the 10-day average", 1),
    "below_ma50": ("ma50", "a close below the 50-day average", -1),
    "above_ma50": ("ma50", "a close above the 50-day average", 1),
    "below_ma200": ("ma200", "a close below the 200-day average", -1),
}
SUE_LEVEL = 1.5
LEADER_MOMENTUM = 0.10
LEADER_RANK = 0.6


def with_exits(
    setup: Setup, exits: Mapping[str, Any] | None, stock: Mapping[str, Any] | None = None
) -> Setup:
    """``setup`` with exit rules from a calibration (keys of ``Exits``, and of ``StockExits``
    for shares), else as it is."""
    if exits:
        known = {k: exits[k] for k in Exits.__dataclass_fields__ if k in exits}
        setup = replace(setup, exits=replace(setup.exits, **known))
    if stock and setup.stock is not None:
        known = {k: stock[k] for k in StockExits.__dataclass_fields__ if k in stock}
        setup = replace(setup, stock=replace(setup.stock, **known))
    return setup


# -- indicators -----------------------------------------------------------------------------------


def indicators(
    bars: pd.DataFrame, benchmark: pd.Series | None = None, momentum_rank: pd.Series | None = None
) -> pd.DataFrame:
    """Per session: the averages, RSI(2), ATR, the Bollinger squeeze, 50-day extremes and
    momentum the setups read. ``bars``: daily ``open``, ``high``, ``low``, ``close``
    (adjusted), indexed by session; ``momentum_rank``: this name's percentile among the names
    by 12-1 month momentum, per session (``momentum_ranks``)."""
    close = bars["close"].astype(float)
    out = pd.DataFrame(index=bars.index)
    out["close"] = close
    for n in (5, 10, 20, 50, 200):
        out[f"ma{n}"] = close.rolling(n).mean()
    out["rsi2"] = wilder_rsi(close.to_frame(), 2).iloc[:, 0]
    prev = close.shift(1)
    high = bars["high"].astype(float) if "high" in bars else close
    low = bars["low"].astype(float) if "low" in bars else close
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(
        axis=1
    )
    out["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    bandwidth = 4 * close.rolling(20).std() / out["ma20"]
    out["squeeze"] = bandwidth.rolling(126).rank(pct=True)
    out["high50"] = close.shift(1).rolling(50).max()
    out["low50"] = close.shift(1).rolling(50).min()
    out["mom12_1"] = close.shift(21) / close.shift(252) - 1
    out["from_high"] = close / close.rolling(252, min_periods=126).max() - 1
    out["ret10"] = close / close.shift(10) - 1
    if benchmark is not None:
        bench = benchmark.reindex(close.index).astype(float)
        out["rs63"] = close.pct_change(63, fill_method=None) - bench.pct_change(
            63, fill_method=None
        )
    else:
        out["rs63"] = np.nan
    out["mom_rank"] = (
        momentum_rank.reindex(close.index).astype(float) if momentum_rank is not None else np.nan
    )
    return out


def momentum_ranks(closes: pd.DataFrame) -> pd.DataFrame:
    """Each name's percentile by 12-1 month momentum among the names, per session."""
    momentum = closes.shift(21) / closes.shift(252) - 1
    ranks: pd.DataFrame = momentum.rank(axis=1, pct=True)
    return ranks


# -- the triggers ---------------------------------------------------------------------------------


def triggers(
    ind: pd.DataFrame, earnings: pd.DataFrame | None = None
) -> dict[str, tuple[pd.Series, pd.Series]]:
    """Per bar setup: (fired, strength) per session. ``fired`` is True on the session the rule
    fires; ``strength`` (0 to 1) says how clearly. ``earnings``: this company's results
    (``session``, ``sue``) as they reached the data (``tp_core.reference``)."""
    c = ind["close"]
    up = (c > ind["ma200"]) & (ind["ma50"] > ind["ma200"])
    down = (c < ind["ma200"]) & (ind["ma50"] < ind["ma200"])
    above200 = ((c / ind["ma200"] - 1) / 0.2).clip(0, 1)
    below200 = ((1 - c / ind["ma200"]) / 0.2).clip(0, 1)
    out: dict[str, tuple[pd.Series, pd.Series]] = {}

    rsi = ind["rsi2"]
    out["pullback"] = (up & (rsi < 10), (0.5 * (10 - rsi) / 10 + 0.5 * above200).clip(0, 1))
    out["rally_fade"] = (down & (rsi > 90), (0.5 * (rsi - 90) / 10 + 0.5 * below200).clip(0, 1))

    squeezed = ind["squeeze"].rolling(5, min_periods=1).min()
    breakout = (c > ind["high50"]) & (c > ind["ma200"]) & (squeezed <= 0.2)
    out["breakout"] = (breakout, (1 - squeezed / 0.2).clip(0, 1) * 0.5 + 0.5 * above200)
    breakdown = (c < ind["low50"]) & down
    out["breakdown"] = (breakdown, 0.5 + 0.5 * below200)

    rank_ok = ind["mom_rank"].isna() | (ind["mom_rank"] >= LEADER_RANK)
    leader = up & (ind["mom12_1"] >= LEADER_MOMENTUM) & rank_ok & (ind["from_high"] > -0.15)
    near50 = (c / ind["ma50"] - 1).between(-0.01, 0.03)
    touch = leader & near50 & ~near50.shift(1, fill_value=False).astype(bool)
    strength = (ind["mom12_1"] / 0.5).clip(0, 1) * 0.5 + (1 + ind["from_high"] / 0.15).clip(
        0, 1
    ) * 0.5
    out["leader"] = (touch, strength)

    no = pd.Series(False, index=ind.index)
    out["earnings_up"] = (no, no.astype(float))
    out["earnings_down"] = (no, no.astype(float))
    if earnings is not None and len(earnings):
        days = pd.DatetimeIndex(pd.to_datetime(earnings["session"]))
        events = pd.Series(pd.to_numeric(earnings["sue"], errors="coerce").to_numpy(), index=days)
        events = events[events.notna() & ~events.index.duplicated(keep="last")]
        sue = events.reindex(ind.index)
        moved = ind["ret10"]
        out["earnings_up"] = (
            (sue >= SUE_LEVEL) & (moved > 0),
            (sue.abs() / 4).clip(0, 1).fillna(0.0),
        )
        out["earnings_down"] = (
            (sue <= -SUE_LEVEL) & (moved < 0),
            (sue.abs() / 4).clip(0, 1).fillna(0.0),
        )
    return {k: (fired.fillna(False).astype(bool), s.fillna(0.0)) for k, (fired, s) in out.items()}


def leader_watch(ind: pd.DataFrame) -> bool:
    """A leader on the last session that hasn't pulled back to its 50-day yet: one to wait on."""
    last = ind.iloc[-1]
    if not all(pd.notna(last[k]) for k in ("ma50", "ma200", "mom12_1", "from_high")):
        return False
    rank_ok = pd.isna(last["mom_rank"]) or last["mom_rank"] >= LEADER_RANK
    return bool(
        last["close"] > last["ma200"]
        and last["ma50"] > last["ma200"]
        and last["mom12_1"] >= LEADER_MOMENTUM
        and rank_ok
        and last["from_high"] > -0.15
        and last["close"] / last["ma50"] - 1 > 0.03
    )


def exit_signal(ind: pd.DataFrame, rule: str | None) -> pd.Series:
    """True on each session the stock-side exit rule says to close."""
    if rule is None:
        return pd.Series(False, index=ind.index)
    column, _, side = UNDERLYING_EXITS[rule]
    level = ind[column]
    hit = ind["close"] > level if side > 0 else ind["close"] < level
    return hit.fillna(False).astype(bool)


@dataclass(frozen=True)
class Active:
    """A setup that fired recently enough to enter on the last session."""

    setup: Setup
    fired: pd.Timestamp
    age: int  # sessions since it fired (0: today)
    strength: float


def active_setups(
    ind: pd.DataFrame,
    earnings: pd.DataFrame | None = None,
    setups: Mapping[str, Setup] | None = None,
) -> list[Active]:
    """The bar setups whose signal is still good on the last session: fired within
    ``valid_for`` sessions and the stock-side exit hasn't come since (an entry after it would
    be late)."""
    setups = setups or SETUPS
    out = []
    fired_by = triggers(ind, earnings)
    for name in BAR_SETUPS:
        setup = setups[name]
        fired, strength = fired_by[name]
        recent = fired.iloc[-(setup.valid_for + 1) :]
        if not recent.any():
            continue
        when = recent[recent].index[-1]
        age = len(fired.loc[when:]) - 1
        exits = exit_signal(ind, setup.exits.underlying).loc[when:].iloc[1:]
        if exits.any():
            continue
        out.append(Active(setup, pd.Timestamp(when), age, float(strength.loc[when])))
    return out


def rule_level(ind: pd.DataFrame, rule: str | None) -> dict[str, Any] | None:
    """The stock-side exit in words with today's level ("a close below the 10-day average",
    $123.45)."""
    if rule is None:
        return None
    column, words, side = UNDERLYING_EXITS[rule]
    level = ind[column].iloc[-1]
    return {
        "rule": rule,
        "text": words,
        "level": float(level) if pd.notna(level) else None,
        "side": "above" if side > 0 else "below",
    }
