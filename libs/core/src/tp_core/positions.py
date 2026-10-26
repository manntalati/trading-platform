"""Per-position statistics and plain-language notes: what each holding is doing and, for
options, what holding to expiry is up against.

Pure functions over numbers the caller gathers (prices from the lake, a live quote, the
brokerage snapshot), so they can be tested without a network. Nothing here is advice: the notes
lay out the trade-offs (time decay, the move needed, what the market prices in, what selling now
gets you) so the decision is easier to make, not to make it for you.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from tp_core import american
from tp_core import greeks as bs
from tp_core.calendar import NEW_YORK, is_session, session_close

MOVES = (-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10)
WIDE_SPREAD = 0.10  # bid/ask spread as a share of the mid price
LOOKBACKS = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}
SECONDS_PER_YEAR = bs.YEAR_DAYS * 86400.0


@dataclass(frozen=True)
class OptionQuote:
    """The latest market for one contract, per share. Any field may be missing."""

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    implied_volatility: float | None = None  # the vendor's own figure
    quote_at: datetime | None = None


@dataclass(frozen=True)
class OptionPosition:
    symbol: str
    label: str
    underlying: str
    quantity: float  # contracts; negative when short
    strike: float
    expiration: date
    call: bool
    cost: float | None  # average paid (or received, short) per share
    broker_price: float | None  # the broker's last price per share
    multiplier: float = 100.0


@functools.lru_cache(maxsize=8192)
def expiry_time(expiration: date) -> datetime:
    """Options stop trading at the close of their expiration day (4:00 PM New York)."""
    try:
        if is_session(expiration):
            return session_close(expiration)
    except ValueError:  # beyond the calendar: assume a regular close
        pass
    return datetime.combine(expiration, time(16), tzinfo=NEW_YORK)


def _american_put_iv(
    mark: float, spot: float, strike: float, years: float, rate: float, european: float | None
) -> float | None:
    """A put's implied volatility read with the right to exercise early. Black-Scholes counts
    that right as extra volatility, which overstates it for puts deep in the money; when the
    early-exercise value is more than a cent or 1% of the price, the American model's figure is
    used instead."""
    if european is not None:
        premium = american.early_exercise_premium(
            spot, strike, years, european, call=False, rate=rate, steps=100
        )
        if premium < max(0.01, 0.01 * mark):
            return european
    return american.implied_vol(mark, spot, strike, years, call=False, rate=rate) or european


def years_until(expiration: date, now: datetime) -> float:
    return max((expiry_time(expiration) - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


# -- stocks and ETFs ------------------------------------------------------------------------------


def stock_stats(
    closes: pd.Series, price: float | None = None, benchmark: pd.Series | None = None
) -> dict[str, Any]:
    """Trend, range and volatility from daily (split/dividend-adjusted) closes.

    ``price`` is the latest price if newer than the last close (a live quote); returns are
    measured from it. ``benchmark`` is the S&P 500 (SPY) closes, for beta.
    """
    closes = closes.dropna().astype(float).sort_index()
    if closes.empty:
        return {"price": price}
    last_close = float(closes.iloc[-1])
    now = float(price) if price else last_close
    live = price is not None and abs(now - last_close) > 1e-9
    prev = last_close if live else (float(closes.iloc[-2]) if len(closes) > 1 else math.nan)
    series = pd.concat([closes, pd.Series([now])], ignore_index=True) if live else closes
    year = series.iloc[-253:]
    rets = pd.Series(np.log(series / series.shift(1))).dropna()
    out: dict[str, Any] = {
        "price": now,
        "prev_close": prev,
        "day_change": now - prev,
        "day_change_pct": now / prev - 1 if prev == prev and prev else math.nan,
        "returns": {
            k: (now / float(series.iloc[-1 - n]) - 1) if len(series) > n else None
            for k, n in LOOKBACKS.items()
        },
        "ytd": _ytd(closes, now),
        "high_52w": float(year.max()),
        "low_52w": float(year.min()),
        "from_high_pct": now / float(year.max()) - 1,
        "from_low_pct": now / float(year.min()) - 1,
        "vol_20d": _vol(rets, 20),
        "vol_60d": _vol(rets, 60),
        "max_drawdown_1y": float((year / year.cummax() - 1).min()),
    }
    for n in (20, 50, 200):
        ma = float(series.iloc[-n:].mean()) if len(series) >= n else math.nan
        out[f"ma{n}"] = ma
        out[f"vs_ma{n}"] = now / ma - 1 if ma == ma else math.nan
    if benchmark is not None and len(benchmark.dropna()) > 30:
        b = benchmark.dropna().astype(float).sort_index()
        joined = pd.concat([closes, b], axis=1, join="inner").iloc[-253:]
        r = joined.pct_change(fill_method=None).dropna()
        var = float(r.iloc[:, 1].var(ddof=1)) if len(r) > 2 else 0.0
        out["beta_1y"] = float(r.iloc[:, 0].cov(r.iloc[:, 1]) / var) if var else math.nan
    return out


def _ytd(closes: pd.Series, now: float) -> float | None:
    index = pd.DatetimeIndex(closes.index)
    year = index[-1].year
    before = closes[index.year < year]
    return now / float(before.iloc[-1]) - 1 if len(before) else None


def _vol(log_returns: pd.Series, days: int) -> float:
    window = log_returns.iloc[-days:]
    if len(window) < max(5, days // 2):
        return math.nan
    return float(window.std(ddof=1) * math.sqrt(252))


def stock_notes(
    stats: dict[str, Any],
    *,
    symbol: str,
    weight: float | None,
    pnl: float | None,
    pnl_pct: float | None,
) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    price = stats.get("price")
    gain, gain_pct = _num(pnl), _num(pnl_pct)
    if gain is not None and gain_pct is not None:
        side = "Up" if gain >= 0 else "Down"
        text = f"{side} {_usd(abs(gain))} ({_pct(gain_pct)}) on what you paid."
        notes.append(_note("info", "Position", text))
    share = _num(weight)
    if share is not None and share >= 0.2:
        text = (
            f"{symbol} is {_pct(share, 0)} of the portfolio: a 10% drop in it alone costs the "
            f"whole account {_pct(share * 0.1)}."
        )
        notes.append(_note("warn", "Concentration", text))
    vol = _num(stats.get("vol_20d"))
    if price and vol is not None:
        daily = vol / math.sqrt(252)
        text = (
            f"A typical day moves it about ±{_pct(daily)} (±{_usd(price * daily, cents=True)} a "
            f"share; 20-day volatility {_pct(vol, 0)} a year)."
        )
        notes.append(_note("info", "Volatility", text))
    ma50, ma200 = _num(stats.get("vs_ma50")), _num(stats.get("vs_ma200"))
    if ma50 is not None and ma200 is not None:
        if ma50 > 0 and ma200 > 0:
            trend = "above both its 50- and 200-day averages (uptrend)"
        elif ma50 < 0 and ma200 < 0:
            trend = "below both its 50- and 200-day averages (downtrend)"
        else:
            trend = "between its 50- and 200-day averages (mixed trend)"
        notes.append(_note("info", "Trend", f"Trading {trend}."))
    high = _num(stats.get("from_high_pct"))
    if high is not None and high <= -0.2:
        notes.append(_note("info", "Range", f"{_pct(-high, 0)} below its 52-week high."))
    return notes


# -- options --------------------------------------------------------------------------------------


def option_stats(
    pos: OptionPosition,
    *,
    spot: float | None,
    quote: OptionQuote | None,
    now: datetime,
    rate: float = 0.04,
    realized_vol: float | None = None,
) -> dict[str, Any]:
    """Market value, implied volatility, greeks, the move needed, probabilities and a scenario
    grid for one option position. Per-position figures are in dollars for the whole position
    (``quantity`` x ``multiplier``); per-share figures are as brokers quote them."""
    years = years_until(pos.expiration, now)
    days_left = years * bs.YEAR_DAYS
    size = pos.quantity * pos.multiplier  # shares controlled; negative when short
    mark, mark_source = _mark(quote, pos.broker_price)
    out: dict[str, Any] = {
        "symbol": pos.symbol,
        "label": pos.label,
        "underlying": pos.underlying,
        "quantity": pos.quantity,
        "right": "call" if pos.call else "put",
        "strike": pos.strike,
        "expiration": pos.expiration.isoformat(),
        "expires_at": expiry_time(pos.expiration).isoformat(),
        "days_left": days_left,
        "spot": spot,
        "mark": mark,
        "mark_source": mark_source,
        "bid": quote.bid if quote else None,
        "ask": quote.ask if quote else None,
        "quote_at": quote.quote_at.isoformat() if quote and quote.quote_at else None,
        "cost": pos.cost,
        "realized_vol": realized_vol,
    }
    if mark is not None:
        out["value"] = mark * size
    if mark is not None and pos.cost is not None:
        out["pnl"] = (mark - pos.cost) * size
        out["pnl_pct"] = (mark - pos.cost) / pos.cost if pos.cost else None
    if quote and quote.bid is not None and quote.ask is not None and quote.ask > 0:
        mid = (quote.bid + quote.ask) / 2
        out["spread"] = quote.ask - quote.bid
        out["spread_pct"] = (quote.ask - quote.bid) / mid if mid else None
        # what closing at the far side of the market gets (or costs), now
        out["exit_value"] = (quote.bid if size > 0 else quote.ask) * size
    if spot is None or spot <= 0:
        return out
    intrinsic = bs.intrinsic(spot, pos.strike, pos.call)
    breakeven = (
        (pos.strike + pos.cost if pos.call else pos.strike - pos.cost)
        if pos.cost is not None
        else None
    )
    out |= {
        "intrinsic": intrinsic,
        "extrinsic": max(mark - intrinsic, 0.0) if mark is not None else None,
        "moneyness": (spot / pos.strike - 1) * (1 if pos.call else -1),
        "in_the_money": intrinsic > 0,
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1 if breakeven is not None else None,
    }
    iv = None
    if mark is not None and years > 0:
        iv = bs.implied_vol(mark, spot, pos.strike, years, call=pos.call, rate=rate)
        if not pos.call and mark > intrinsic:
            iv = _american_put_iv(mark, spot, pos.strike, years, rate, iv)
    if iv is None and quote is not None and quote.implied_volatility:
        iv = quote.implied_volatility
    out["iv"] = iv
    if iv is None or years <= 0:
        return out
    g = bs.greeks(spot, pos.strike, years, iv, call=pos.call, rate=rate)
    if not pos.call:
        out["early_exercise_value"] = american.early_exercise_premium(
            spot, pos.strike, years, iv, call=False, rate=rate, steps=100
        )
    above_strike = bs.prob_above(spot, pos.strike, years, iv, rate=rate)
    itm = above_strike if pos.call else 1 - above_strike
    profit = None
    if breakeven is not None:
        above_even = bs.prob_above(spot, breakeven, years, iv, rate=rate)
        profit_long = above_even if pos.call else 1 - above_even
        profit = profit_long if size > 0 else 1 - profit_long
    move = bs.expected_move(spot, years, iv)
    out |= {
        "delta": g.delta,
        "gamma": g.gamma,
        "theta": g.theta,
        "vega": g.vega,
        "delta_shares": g.delta * size,  # behaves like this many shares, for small moves
        "theta_day": g.theta * size,  # dollars a day from time alone
        "vega_point": g.vega * size,  # dollars per 1-point change in implied volatility
        "prob_itm": itm,
        "prob_profit": profit,
        "expected_move": move,
        "expected_move_pct": move / spot,
        "scenarios": scenarios(pos, spot=spot, iv=iv, now=now, rate=rate),
    }
    return out


def _mark(quote: OptionQuote | None, broker_price: float | None) -> tuple[float | None, str]:
    """Mid of a live two-sided market, else the last trade, else the broker's last price."""
    if quote is not None:
        bid, ask = quote.bid, quote.ask
        if bid is not None and ask is not None and ask > 0 and ask >= bid >= 0:
            return (bid + ask) / 2, "mid"
        if quote.last:
            return quote.last, "last trade"
    if broker_price is not None:
        return broker_price, "broker"
    return None, "none"


def scenarios(
    pos: OptionPosition, *, spot: float, iv: float, now: datetime, rate: float
) -> dict[str, Any]:
    """Position P&L if the underlying moved ``MOVES`` by a few dates, at today's implied
    volatility (the model's view: real moves come with volatility changes too)."""
    years = years_until(pos.expiration, now)
    expiry = expiry_time(pos.expiration)
    points: list[tuple[str, float]] = [("Now", years)]
    days = int(years * bs.YEAR_DAYS)
    if days >= 2:
        midpoint = now + timedelta(days=days // 2)
        label = _day(midpoint.astimezone(NEW_YORK).date())
        points.append((label, years_until(pos.expiration, midpoint)))
    points.append((f"Expiry {_day(expiry.astimezone(NEW_YORK).date())}", 0.0))
    size = pos.quantity * pos.multiplier
    cost = pos.cost if pos.cost is not None else 0.0
    rows = []
    for m in MOVES:
        level = spot * (1 + m)
        cells = []
        for _, t in points:
            value = bs.price(level, pos.strike, t, iv, call=pos.call, rate=rate)
            cells.append({"value": value * size, "pnl": (value - cost) * size})
        rows.append({"move": m, "price": level, "cells": cells})
    return {"columns": [label for label, _ in points], "rows": rows}


def option_notes(stats: dict[str, Any], pos: OptionPosition) -> list[dict[str, str]]:
    """What holding is up against, in plain words, from ``option_stats``' output."""
    notes: list[dict[str, str]] = []
    long = pos.quantity > 0
    spot, strike = _num(stats.get("spot")), pos.strike
    under = pos.underlying
    days = _num(stats.get("days_left")) or 0.0
    when = _day(pos.expiration)
    shares = int(abs(pos.quantity) * pos.multiplier)
    k = _usd(strike, cents=True)
    if stats.get("mark_source") in ("broker", "none"):
        text = (
            "Using the broker's last price for this contract, which can be a day old; refresh or "
            "check the bid/ask before acting on these figures."
        )
        notes.append(_note("warn", "No live quote", text))
    if spot is None:
        text = f"No current price for {under}, so the greeks and odds can't be worked out."
        notes.append(_note("warn", "No price for the underlying", text))
        return notes
    s = _usd(spot, cents=True)

    itm = bool(stats.get("in_the_money"))
    mark, intrinsic = _num(stats.get("mark")), _num(stats.get("intrinsic"))
    if mark is not None and intrinsic is not None and mark < intrinsic - 0.05:
        text = (
            f"Priced at {_usd(mark, cents=True)} a share, but worth {_usd(intrinsic, cents=True)} "
            f"if exercised now ({under} at {s} against the {k} strike): the price is likely "
            "stale. Check the live bid before deciding anything."
        )
        notes.append(_note("warn", "Price below exercise value", text))
    if days <= 0:
        notes.append(_note("risk", "Expired", f"Past its {when} close."))
        return notes
    if days < 1:
        text = (
            f"Trading stops at 4:00 PM ET. {under} is {s}, {'in' if itm else 'out of'} the "
            f"money (strike {k})."
        )
        notes.append(_note("risk", "Expires today", text))
    if itm and days <= 5:
        cash = _usd(strike * shares)
        if long and pos.call:
            text = (
                f"If {under} is still above {k} at the {when} close, the call is exercised "
                f"automatically: you'd buy {shares} shares for {cash}. Sell the contract before "
                "the close if you don't want the shares or don't have the cash."
            )
        elif long:
            text = (
                f"If {under} is still below {k} at the {when} close, the put is exercised "
                f"automatically: you'd sell {shares} shares at the strike ({cash}), opening a "
                "short position if you don't own them. Sell the contract before the close to "
                "avoid that."
            )
        else:
            text = (
                "Short and in the money: you can be assigned at any time, and almost certainly "
                f"will be if it stays in the money at the {when} close ({shares} shares at {k}). "
                "Buy it back first to avoid that."
            )
        notes.append(_note("risk", "Exercise at expiry", text))
    elif not itm and days <= 7:
        side = "above" if pos.call else "below"
        left = f"{days:.0f} day{'' if round(days) == 1 else 's'} left"
        keep = "" if long else " (you keep the premium)"
        text = f"{left}: unless {under} closes {side} {k} on {when}, it expires worthless{keep}."
        notes.append(_note("warn" if long else "info", "Out of the money", text))

    theta_day, value = _num(stats.get("theta_day")), _num(stats.get("value"))
    if theta_day is not None and value:
        if long:
            at_expiry = bs.intrinsic(spot, strike, pos.call) * pos.quantity * pos.multiplier
            text = (
                f"Losing about {_usd(-theta_day)} a day to time alone ({_pct(-theta_day / value)} "
                f"of its value), faster as expiry nears. If {under} doesn't move, it's worth "
                f"{_usd(at_expiry)} at expiry, {_usd(value - at_expiry)} less than now."
            )
            notes.append(_note("warn" if days <= 14 else "info", "Time decay", text))
        else:
            text = (
                f"Earning about {_usd(theta_day)} a day from time decay while the price stays put."
            )
            notes.append(_note("info", "Time decay", text))

    move, even = _num(stats.get("expected_move_pct")), _num(stats.get("breakeven"))
    need, p_profit = _num(stats.get("breakeven_move_pct")), _num(stats.get("prob_profit"))
    if move is not None and even is not None and need is not None and p_profit is not None:
        e = _usd(even, cents=True)
        if not long:
            what = f"Breakeven at expiry is {e}"
        elif (need > 0) == pos.call:
            direction = "rise" if need > 0 else "fall"
            what = (
                f"Breakeven at expiry is {e}: {under} has to {direction} {_pct(abs(need))} from {s}"
            )
        else:
            what = f"Breakeven at expiry is {e}, already passed ({under} at {s})"
        text = (
            f"{what}. The options market prices a typical move of ±{_pct(move)} by {when}; at "
            f"those odds there's about a {_pct(p_profit, 0)} chance of finishing at a profit and "
            f"{_pct(_num(stats.get('prob_itm')) or 0.0, 0)} of finishing in the money."
        )
        notes.append(_note("info", "What it needs", text))

    exit_value = _num(stats.get("exit_value"))
    if long and value:
        sell = (
            f"Selling now gets about {_usd(exit_value)} at the bid"
            if exit_value is not None
            else f"It's marked at {_usd(value)}"
        )
        if p_profit is None:
            text = f"{sell}; with no implied volatility for that price, there are no odds to weigh."
        else:
            text = (
                f"{sell}. At today's implied volatility the price already reflects the odds above, "
                "so holding and selling have about the same expected value: holding pays off only "
                f"if {under} moves further (or sooner) than the market expects. Holding keeps the "
                "upside, at the cost of the time decay above."
            )
        notes.append(_note("info", "Hold or sell", text))

    pnl, pnl_pct = _num(stats.get("pnl")), _num(stats.get("pnl_pct"))
    if pnl is not None and pnl_pct is not None:
        if pnl < 0:
            text = (
                f"Down {_usd(-pnl)} ({_pct(-pnl_pct, 0)}). What you paid is spent whichever you "
                "choose; the choice is between what it fetches now and the range in the "
                "scenarios table."
            )
            notes.append(_note("info", "The loss so far", text))
        elif long:
            text = (
                f"Up {_usd(pnl)} ({_pct(pnl_pct, 0)}). Selling locks it in; holding leaves it "
                "exposed to time decay and to a move the wrong way."
            )
            notes.append(_note("info", "The gain so far", text))

    iv, rv = _num(stats.get("iv")), _num(stats.get("realized_vol"))
    if iv is not None and rv:
        ratio = iv / rv
        if ratio >= 1.25 or ratio <= 0.8:
            richer = ratio >= 1.25
            why = " (often ahead of earnings or news)" if richer else ""
            text = (
                f"Implied volatility {_pct(iv, 0)} vs {_pct(rv, 0)} the stock has actually moved "
                f"over 20 days: options are pricing {'bigger' if richer else 'smaller'} moves "
                f"than it has been making{why}."
            )
            notes.append(_note("info", "Implied vs actual volatility", text))

    spread, bid, ask = _num(stats.get("spread_pct")), _num(stats.get("bid")), _num(stats.get("ask"))
    if spread is not None and spread >= WIDE_SPREAD and bid is not None and ask is not None:
        assert mark is not None
        give_up = (mark - bid if long else ask - mark) * abs(pos.quantity) * pos.multiplier
        side = "Selling at the bid" if long else "Buying back at the ask"
        text = (
            f"Bid {_usd(bid, cents=True)} / ask {_usd(ask, cents=True)}: the spread is "
            f"{_pct(spread, 0)} of the price. {side} gives up about {_usd(give_up)} versus the "
            "middle; a limit order between the two may fill."
        )
        notes.append(_note("warn", "Wide market", text))
    return notes


# -- formatting -----------------------------------------------------------------------------------


def _note(level: str, title: str, text: str) -> dict[str, str]:
    return {"level": level, "title": title, "text": text}


def _num(value: object) -> float | None:
    """A finite number, or None (missing values arrive as None or NaN)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _day(day: date) -> str:
    return f"{day:%a %b} {day.day}"


def _usd(value: float | None, *, cents: bool = False) -> str:
    if value is None or value != value:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}" if cents else f"{sign}${abs(value):,.0f}"


def _pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"
