"""Backtesting the playbook's setups and exits, and the calibration the live ideas read
(``tp-recs calibrate``).

1. **Events**: every session each bar setup (``tp_strategies.setups``) fired on each name with
   daily bars, over all their history. A signal repeating within the setup's ``valid_for``
   sessions is the same idea, counted once.
2. **The stock after each**: its log return over the setup's horizon, against SPY, and how often
   it went the setup's way. The setup's **drift**, what the live model assumes, is the mean
   shrunk toward the average of all names over the same horizon (``SHRINK`` pseudo-events), so a
   setup that fired 30 times can't claim a big edge.
3. **The option trade**: the setup's contract (a call or put of delta ~0.55 with the horizon plus
   five weeks of life; for leaders a 0.80-delta LEAPS 400 days out) bought at the close the signal
   fired, and marked each session until an exit. Priced by Black-Scholes at the stock's
   volatility forecast (an EWMA of squared returns) times ``OptionModel.iv_ratio`` (implied
   usually runs above realized), paying half the spread each way and $0.65 a contract. Where the
   lake has the real contract's daily bars (``tp-data options history``: Alpaca keeps them from
   February 2024), those closes are used instead, with the same costs, and the report compares
   the two.
4. **Exits**: every combination in ``GRID`` (profit target, stop, time stop, the stock-side rule)
   is applied to every trade, along with the setup's days-to-expiry limit. The combination that
   would have grown an account fastest betting ``BET`` of it on each trade (the mean of
   ``log(1 + BET * return)``: the mean return less a penalty for how spread out the returns are,
   so a lottery ticket that wins big once in a while doesn't beat steady gains) on the earlier
   ``TRAIN`` share of the events (by date) is chosen, and its result on the later events, which
   it never saw, is what's reported: with 72 combinations tried, the in-sample best always
   flatters.

5. **The share trade** (the upward setups, which are stock ideas too): bought at the signal's
   close, a stop and an optional target some multiples of the stock's ATR away, checked each
   session against the open (a gap through the level fills at the open), the low and the high
   (the stop first when both are touched the same day), then the stock-side rule and the time
   stop on the close; ``STOCK_COST`` of the price paid each way. The combination in
   ``STOCK_GRID`` chosen is the one that would have grown an account fastest risking ``RISK``
   of it to the stop on each trade, on its return over what the average stock made in the same
   number of sessions (so holding longer doesn't win just by collecting the market's drift);
   chosen on the earlier events and reported on the later ones, as for options.

What this can't see: the modeled trades assume implied volatility moves with realized, so they
can't show an IV crush or spike (the real-bar trades can); and the names are today's liquid
ones, so the backtest misses companies that shrank or vanished (survivorship).
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from scipy import special

from tp_core import greeks as bs
from tp_core import option_history
from tp_core.bars import load_bars
from tp_core.storage import Lake, write_json
from tp_strategies.playbook import calibration_path, earnings_by_symbol
from tp_strategies.setups import (
    BAR_SETUPS,
    SETUPS,
    Setup,
    exit_signal,
    indicators,
    momentum_ranks,
    triggers,
)

log = logging.getLogger(__name__)

SHRINK = 100  # pseudo-events of the all-names average in each setup's drift
TRAIN = 0.6  # the earlier share of events (by date) the exits are chosen on
MIN_TRADES = 30  # fewer trades than this and an exit choice means little: keep the defaults
BET = 0.1  # the share of the account each trade risks, for judging exits by growth
GRID: dict[str, tuple[Any, ...]] = {
    "take_profit": (0.25, 0.5, 1.0, None),
    "stop_loss": (-0.3, -0.5, None),
    "time_stop": (0.5, 1.0, 2.0),  # times the horizon
    "underlying": (True, False),  # use the setup's stock-side exit
}
REAL_SINCE = date(2024, 2, 1)  # Alpaca's daily option bars start about here
STOCK_GRID: dict[str, tuple[Any, ...]] = {
    "stop_atr": (1.5, 2.5, 4.0, 6.0),
    "target_atr": (2.0, 4.0, 8.0, None),
    "time_stop": (0.5, 1.0, 2.0),  # times the horizon
    "underlying": (True, False),
}
STOCK_COST = 0.0005  # of the price, each way: half the spread of a liquid stock
RISK = 0.02  # the share of the account a stock trade risks to its stop, for judging exits


@dataclass(frozen=True)
class OptionModel:
    iv_ratio: float = 1.15  # implied over the realized-volatility forecast
    half_spread: float = 0.02  # of the mid, paid each way
    fee: float = 0.65 / 100  # per share, each way
    ewma: float = 0.94  # RiskMetrics decay for the volatility forecast
    rate: float = 0.04

    def describe(self) -> str:
        return (
            f"Black-Scholes at {self.iv_ratio:.2f}x an EWMA ({self.ewma}) volatility forecast, "
            f"{self.half_spread:.0%} of the mid paid each way, $0.65 a contract"
        )


@dataclass(frozen=True)
class Contract:
    """The contract a setup's trade uses: days to expiry at entry and the target delta."""

    days: int
    delta: float  # negative for puts


def contract_for(setup: Setup) -> Contract:
    if setup.structure == "leaps_call":
        return Contract(400, 0.80)
    days = math.ceil(setup.horizon * 365 / 252) + setup.exits.min_dte + 14
    return Contract(days, -0.55 if setup.structure == "long_put" else 0.55)


# -- inputs ---------------------------------------------------------------------------------------


def bar_frames(lake: Lake, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Every symbol's whole history of adjusted daily ``open``, ``high``, ``low``, ``close``."""
    bars = load_bars(lake, list(dict.fromkeys(symbols)))
    out = {}
    for symbol, g in bars.groupby("symbol"):
        frame = g[["adj_open", "adj_high", "adj_low", "adj_close"]].astype(float)
        frame.columns = ["open", "high", "low", "close"]
        frame.index = pd.DatetimeIndex(g["session"])
        out[str(symbol)] = frame.sort_index()
    return out


def ewma_vol(close: pd.Series, decay: float) -> pd.Series:
    """Annualized volatility forecast from an EWMA of squared daily log returns."""
    r = pd.Series(np.log(close.to_numpy(dtype=float)), index=close.index).diff()
    var = (r * r).ewm(alpha=1 - decay, adjust=False, min_periods=20).mean()
    out: pd.Series = np.sqrt(var * 252)
    return out


# -- events and what the stock did ----------------------------------------------------------------


def find_events(
    frames: Mapping[str, pd.DataFrame],
    *,
    benchmark: pd.Series | None,
    earnings: Mapping[str, pd.DataFrame],
    setups: Sequence[str] = BAR_SETUPS,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """One row per setup signal (``symbol``, ``setup``, ``session``, ``i`` its position in the
    symbol's bars, ``strength``), and each symbol's indicators."""
    names = [s for s in frames if s != "SPY"]
    closes = pd.DataFrame({s: frames[s]["close"] for s in names})
    ranks = momentum_ranks(closes) if not closes.empty else pd.DataFrame()
    inds: dict[str, pd.DataFrame] = {}
    rows = []
    for symbol in names:
        frame = frames[symbol]
        if len(frame) < 260:
            continue
        ind = indicators(frame, benchmark, ranks.get(symbol))
        inds[symbol] = ind
        fired_by = triggers(ind, earnings.get(symbol))
        for name in setups:
            fired, strength = fired_by[name]
            gap = SETUPS[name].valid_for
            last = -10_000
            for i in np.flatnonzero(fired.to_numpy()):
                if i - last <= gap:
                    continue
                last = int(i)
                rows.append((symbol, name, ind.index[int(i)], int(i), float(strength.iloc[i])))
    events = pd.DataFrame(rows, columns=["symbol", "setup", "session", "i", "strength"])
    return events.sort_values("session").reset_index(drop=True), inds


def stock_outcomes(
    events: pd.DataFrame, inds: Mapping[str, pd.DataFrame], benchmark: pd.Series | None
) -> pd.DataFrame:
    """Each event's log return over its setup's horizon (NaN if the bars end first) and the
    same for SPY."""
    out = events.copy()
    rets, spy = [], []
    for _, e in events.iterrows():
        close = inds[e["symbol"]]["close"]
        h = SETUPS[e["setup"]].horizon
        j = e["i"] + h
        rets.append(math.log(close.iloc[j] / close.iloc[e["i"]]) if j < len(close) else math.nan)
        if benchmark is not None and j < len(close):
            b = benchmark.reindex(close.index)
            start, end = b.iloc[e["i"]], b.iloc[j]
            spy.append(math.log(end / start) if start > 0 and end > 0 else math.nan)
        else:
            spy.append(math.nan)
    out["ret"] = rets
    out["spy"] = spy
    return out


def baseline(frames: Mapping[str, pd.DataFrame], horizon: int) -> float:
    """The average log return over ``horizon`` sessions across all names and dates."""
    values = []
    for symbol, f in frames.items():
        if symbol == "SPY":
            continue
        c = np.log(f["close"].to_numpy(dtype=float))
        if len(c) > horizon:
            values.append(c[horizon:] - c[:-horizon])
    return float(np.nanmean(np.concatenate(values))) if values else 0.0


# -- the option trades ----------------------------------------------------------------------------


def strike_for(spot: Any, vol: Any, years: float, delta: float, rate: float) -> Any:
    """The strike with Black-Scholes ``delta`` (negative: a put)."""
    d1 = special.ndtri(delta if delta > 0 else 1 + delta)
    root = np.sqrt(years)
    return spot * np.exp(-(d1 * vol * root) + (rate + vol * vol / 2) * years)


@dataclass
class Paths:
    """Every trade of one setup marked each session after entry: ``ret`` (events x sessions,
    NaN where there is no mark), whether the stock-side rule said to exit, and whether the
    days-to-expiry limit had come."""

    ret: np.ndarray[Any, np.dtype[np.float64]]
    stock_exit: np.ndarray[Any, np.dtype[np.bool_]]
    expiry_limit: np.ndarray[Any, np.dtype[np.bool_]]
    real: np.ndarray[Any, np.dtype[np.bool_]]  # per event: marked with real option bars


class Series:
    """Each symbol's closes, volatility forecast and stock-side exits, worked out once."""

    def __init__(self, inds: Mapping[str, pd.DataFrame], model: OptionModel) -> None:
        self.inds = inds
        self.model = model
        self._vol: dict[str, np.ndarray[Any, np.dtype[np.float64]]] = {}
        self._exit: dict[tuple[str, str | None], np.ndarray[Any, np.dtype[np.bool_]]] = {}

    def vol(self, symbol: str) -> np.ndarray[Any, np.dtype[np.float64]]:
        if symbol not in self._vol:
            self._vol[symbol] = ewma_vol(self.inds[symbol]["close"], self.model.ewma).to_numpy()
        return self._vol[symbol]

    def exits(self, symbol: str, rule: str | None) -> np.ndarray[Any, np.dtype[np.bool_]]:
        if (symbol, rule) not in self._exit:
            signal = exit_signal(self.inds[symbol], rule).to_numpy(dtype=bool)
            self._exit[(symbol, rule)] = signal
        return self._exit[(symbol, rule)]


def modeled_paths(
    events: pd.DataFrame,
    series: Series,
    setup: Setup,
    days: int,
    real: Mapping[int, pd.Series] | None = None,
) -> Paths:
    """The option trade of each event over ``days`` sessions. ``real``: event index -> the real
    contract's closes (indexed by session) to use instead of the model."""
    model = series.model
    contract = contract_for(setup)
    n = len(events)
    ret = np.full((n, days + 1), np.nan)
    stock = np.zeros((n, days + 1), dtype=bool)
    limit = np.zeros((n, days + 1), dtype=bool)
    used_real = np.zeros(n, dtype=bool)
    call = contract.delta > 0
    for row, (_, e) in enumerate(events.iterrows()):  # events are numbered 0..n-1
        ind = series.inds[e["symbol"]]
        i = int(e["i"])
        close = ind["close"].to_numpy(dtype=float)
        vol = series.vol(e["symbol"])
        end = min(i + days, len(close) - 1)
        span = np.arange(i, end + 1)
        if not np.isfinite(vol[i]):
            continue
        t_left = (contract.days - (span - i) * 365 / 252) / 365  # years left at each session
        rule = series.exits(e["symbol"], setup.exits.underlying)[span]
        stock[row, : len(span)] = rule
        limit[row, : len(span)] = t_left * 365 <= setup.exits.min_dte
        marks = real.get(row) if real else None
        if marks is not None:
            on = marks.reindex(ind.index[span]).ffill(limit=5).to_numpy(dtype=float)
            if not np.isfinite(on[0]) or on[0] <= 0:
                marks = None
            else:
                values = on
                used_real[row] = True
        if marks is None:
            iv0 = vol[i] * model.iv_ratio
            years0 = contract.days / 365
            strike = float(strike_for(close[i], iv0, years0, contract.delta, model.rate))
            iv = np.where(np.isfinite(vol[span]), vol[span], vol[i]) * model.iv_ratio
            values = bs.price_array(
                close[span], strike, np.maximum(t_left, 0.0), iv, call, model.rate
            )
        cost = values[0] * (1 + model.half_spread) + model.fee
        exit_value = values * (1 - model.half_spread) - model.fee
        ret[row, : len(span)] = exit_value / cost - 1
    ret[:, 0] = np.nan  # no exit on the entry bar
    return Paths(ret, stock, limit, used_real)


def apply_exits(
    paths: Paths,
    *,
    take_profit: float | None,
    stop_loss: float | None,
    time_stop: int,
    underlying: bool,
) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.int64]]]:
    """Each trade's return and sessions held under one set of exits: the first session after
    entry with a mark where the target, the stop, the stock-side rule (if ``underlying``), the
    time stop or the days-to-expiry limit says to close; else the last mark."""
    ret = paths.ret
    n, width = ret.shape
    marked = np.isfinite(ret)
    hit = np.zeros_like(marked)
    if take_profit is not None:
        hit |= ret >= take_profit
    if stop_loss is not None:
        hit |= ret <= stop_loss
    if underlying:
        hit |= paths.stock_exit
    hit |= paths.expiry_limit
    sessions = np.arange(width)
    hit |= sessions[None, :] >= time_stop
    hit &= marked
    first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
    last = np.where(marked.any(axis=1), width - 1 - marked[:, ::-1].argmax(axis=1), -1)
    exit_at = np.where(first >= 0, first, last)
    out = np.where(exit_at >= 0, ret[np.arange(n), np.maximum(exit_at, 0)], np.nan)
    return out, exit_at


def _stats(values: np.ndarray[Any, np.dtype[np.float64]], held: Any = None) -> dict[str, Any]:
    v = values[np.isfinite(values)]
    if not len(v):
        return {"trades": 0}
    out = {
        "trades": len(v),
        "mean": float(v.mean()),
        "median": float(np.median(v)),
        "win_rate": float((v > 0).mean()),
        "q10": float(np.quantile(v, 0.1)),
        "growth": float(np.log1p(BET * np.maximum(v, -1.0)).mean()),  # per trade, betting BET
    }
    if held is not None:
        h = np.asarray(held)[np.isfinite(values)]
        out["avg_sessions"] = float(h.mean())
    return out


def choose_exits(
    paths: Paths, setup: Setup, train: np.ndarray[Any, np.dtype[np.bool_]]
) -> dict[str, Any]:
    """Try every combination in ``GRID``; pick the fastest growth on the training events and
    report it (and the setup's default exits) on the rest."""
    tried = []
    for tp, sl, ts, rule in itertools.product(*GRID.values()):
        if rule and setup.exits.underlying is None:
            continue
        stop = max(round(ts * setup.horizon), 1)
        result, held = apply_exits(
            paths, take_profit=tp, stop_loss=sl, time_stop=stop, underlying=rule
        )
        fit = _stats(result[train])
        tried.append(((tp, sl, stop, rule), result, held, fit))
    usable = [t for t in tried if t[3].get("trades", 0) >= MIN_TRADES]
    x = setup.exits
    default_result, default_held = apply_exits(
        paths,
        take_profit=x.take_profit,
        stop_loss=x.stop_loss,
        time_stop=x.time_stop,
        underlying=x.underlying is not None,
    )
    test = ~train
    out: dict[str, Any] = {
        "combinations": len(tried),
        "default": _stats(default_result[test], default_held[test]),
        "default_train": _stats(default_result[train], default_held[train]),
    }
    if not usable:
        out["exits"] = None
        out["test"] = out["default"]
        return out
    (tp, sl, stop, rule), result, held, fit = max(usable, key=lambda t: t[3]["growth"])
    out["exits"] = {
        "take_profit": tp,
        "stop_loss": sl,
        "time_stop": stop,
        "underlying": x.underlying if rule else None,
    }
    out["train"] = fit | {"avg_sessions": float(np.mean(held[train][np.isfinite(result[train])]))}
    out["test"] = _stats(result[test], held[test])
    out["all"] = _stats(result, held)
    return out


# -- the share trades -----------------------------------------------------------------------------


@dataclass
class StockPaths:
    """Every share trade of one setup, each session after entry (column 0 is the entry): the
    open, high, low and close over the entry close (NaN where the bars end), whether the
    stock-side rule said to exit at the close, and each trade's ATR over its entry close."""

    open: np.ndarray[Any, np.dtype[np.float64]]
    high: np.ndarray[Any, np.dtype[np.float64]]
    low: np.ndarray[Any, np.dtype[np.float64]]
    close: np.ndarray[Any, np.dtype[np.float64]]
    stock_exit: np.ndarray[Any, np.dtype[np.bool_]]
    atr: np.ndarray[Any, np.dtype[np.float64]]


def stock_paths(
    events: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    series: Series,
    rule: str | None,
    days: int,
) -> StockPaths:
    """The share trade of each event over ``days`` sessions."""
    n = len(events)
    shape = (n, days + 1)
    out = StockPaths(
        open=np.full(shape, np.nan),
        high=np.full(shape, np.nan),
        low=np.full(shape, np.nan),
        close=np.full(shape, np.nan),
        stock_exit=np.zeros(shape, dtype=bool),
        atr=np.full(n, np.nan),
    )
    for row, (_, e) in enumerate(events.iterrows()):
        symbol, i = str(e["symbol"]), int(e["i"])
        ind = series.inds[symbol]
        bars = frames[symbol].reindex(ind.index)
        end = min(i + days, len(ind) - 1)
        base = float(ind["close"].iloc[i])
        atr = float(ind["atr14"].iloc[i])
        if not (base > 0 and math.isfinite(atr)):
            continue
        width = end - i + 1
        for name, target in (
            ("open", out.open),
            ("high", out.high),
            ("low", out.low),
            ("close", out.close),
        ):
            target[row, :width] = bars[name].to_numpy(dtype=float)[i : end + 1] / base
        out.stock_exit[row, :width] = series.exits(symbol, rule)[i : end + 1]
        out.atr[row] = atr / base
    return out


def apply_stock_exits(
    paths: StockPaths,
    *,
    stop_atr: float,
    target_atr: float | None,
    time_stop: int,
    underlying: bool,
) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.int64]]]:
    """Each trade's return (costs paid) and sessions held: the first session after entry where
    the open gaps through the stop or the target (filled at the open), the low reaches the stop
    or the high the target (filled there; the stop first when both), or, on the close, the
    stock-side rule (if ``underlying``) or the time stop says to sell; else the last close."""
    n, width = paths.close.shape
    stop = (1 - stop_atr * paths.atr)[:, None]
    target = (1 + target_atr * paths.atr)[:, None] if target_atr is not None else None
    sessions = np.arange(width)[None, :]
    marked = np.isfinite(paths.close)
    price = np.full((n, width), np.nan)  # the fill on each session it would sell
    on_close = sessions >= time_stop
    if underlying:
        on_close = on_close | paths.stock_exit
    price = np.where(on_close & marked, paths.close, price)
    if target is not None:
        price = np.where(paths.high >= target, target, price)
    price = np.where(paths.low <= stop, stop, price)
    if target is not None:
        price = np.where(paths.open >= target, paths.open, price)
    price = np.where(paths.open <= stop, paths.open, price)
    price[:, 0] = np.nan  # no exit on the entry bar
    hit = np.isfinite(price)
    first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
    last = np.where(marked.any(axis=1), width - 1 - marked[:, ::-1].argmax(axis=1), -1)
    exit_at = np.where(first >= 0, first, last)
    rows = np.arange(n)
    fill = np.where(first >= 0, price[rows, np.maximum(exit_at, 0)], paths.close[rows, exit_at])
    ret = fill * (1 - STOCK_COST) / (1 + STOCK_COST) - 1
    ret = np.where((exit_at > 0) & np.isfinite(paths.atr), ret, np.nan)
    return ret, exit_at


def _stock_stats(
    ret: np.ndarray[Any, np.dtype[np.float64]],
    held: np.ndarray[Any, np.dtype[np.int64]],
    risk: np.ndarray[Any, np.dtype[np.float64]],
    daily: float,
) -> dict[str, Any]:
    """``risk``: each trade's distance to its stop over the entry; ``daily``: the average
    stock's mean daily log return, what the same sessions would have made anyway."""
    ok = np.isfinite(ret) & np.isfinite(risk) & (risk > 0)
    if not ok.any():
        return {"trades": 0}
    r, h, k = ret[ok], held[ok].astype(float), risk[ok]
    excess = r - np.expm1(daily * h)
    multiple = np.maximum(excess / k, -40.0)  # in units of the risk to the stop
    return {
        "trades": int(ok.sum()),
        "mean": float(r.mean()),
        "median": float(np.median(r)),
        "win_rate": float((r > 0).mean()),
        "q10": float(np.quantile(r, 0.1)),
        "excess": float(excess.mean()),
        "mean_r": float((r / k).mean()),
        "avg_sessions": float(h.mean()),
        "growth": float(np.log1p(RISK * multiple).mean()),
    }


def choose_stock_exits(
    paths: StockPaths, setup: Setup, train: np.ndarray[Any, np.dtype[np.bool_]], daily: float
) -> dict[str, Any]:
    """Try every combination in ``STOCK_GRID``; pick the fastest growth on the training events
    and report it (and the setup's default share exits) on the rest."""
    x = setup.stock
    assert x is not None
    tried = []
    for stop_atr, target_atr, ts, rule in itertools.product(*STOCK_GRID.values()):
        if rule and x.underlying is None:
            continue
        stop = max(round(ts * setup.horizon), 1)
        ret, held = apply_stock_exits(
            paths, stop_atr=stop_atr, target_atr=target_atr, time_stop=stop, underlying=rule
        )
        risk = stop_atr * paths.atr
        tried.append(((stop_atr, target_atr, stop, rule), ret, held, risk))
    test = ~train
    d_ret, d_held = apply_stock_exits(
        paths,
        stop_atr=x.stop_atr,
        target_atr=x.target_atr,
        time_stop=x.time_stop,
        underlying=x.underlying is not None,
    )
    d_risk = x.stop_atr * paths.atr
    out: dict[str, Any] = {
        "combinations": len(tried),
        "default": _stock_stats(d_ret[test], d_held[test], d_risk[test], daily),
    }
    fits = [(t, _stock_stats(t[1][train], t[2][train], t[3][train], daily)) for t in tried]
    usable = [(t, fit) for t, fit in fits if fit.get("trades", 0) >= MIN_TRADES]
    if not usable:
        out["exits"] = None
        out["test"] = out["default"]
        return out
    ((stop_atr, target_atr, stop, rule), ret, held, risk), fit = max(
        usable, key=lambda tf: tf[1]["growth"]
    )
    out["exits"] = {
        "stop_atr": stop_atr,
        "target_atr": target_atr,
        "time_stop": stop,
        "underlying": x.underlying if rule else None,
    }
    out["train"] = fit
    out["test"] = _stock_stats(ret[test], held[test], risk[test], daily)
    return out


# -- real option bars -----------------------------------------------------------------------------


def request_key(underlying: str, right: str, expiration: date, strike: float) -> str:
    return f"{underlying}|{right}|{expiration.isoformat()}|{strike:.2f}"


def request_for(e: pd.Series, series: Series, setup: Setup) -> dict[str, Any] | None:
    """The listed contract an event's trade would have used, as a request for its daily bars:
    the model's target expiry and strike, and the dates to fetch (None before ``REAL_SINCE``)."""
    session = pd.Timestamp(e["session"]).date()
    vol = series.vol(e["symbol"])[int(e["i"])]
    if session < REAL_SINCE or not math.isfinite(vol):
        return None
    model = series.model
    contract = contract_for(setup)
    spot = float(series.inds[e["symbol"]]["close"].iloc[int(e["i"])])
    years = contract.days / 365
    strike = round(
        float(strike_for(spot, vol * model.iv_ratio, years, contract.delta, model.rate)), 2
    )
    expiration = session + timedelta(days=contract.days)
    right = "C" if contract.delta > 0 else "P"
    days = int(max(GRID["time_stop"]) * setup.horizon * 365 / 252) + 7
    return {
        "key": request_key(str(e["symbol"]), right, expiration, strike),
        "underlying": str(e["symbol"]),
        "right": right,
        "expiration": expiration.isoformat(),
        "strike": strike,
        "start": session.isoformat(),
        "end": (session + timedelta(days=days)).isoformat(),
    }


def requests_for(events: pd.DataFrame, series: Series, setup: Setup) -> list[dict[str, Any]]:
    found = (request_for(e, series, setup) for _, e in events.iterrows())
    return [r for r in found if r is not None]


def real_marks(
    events: pd.DataFrame, series: Series, setup: Setup, bars: pd.DataFrame, matches: pd.DataFrame
) -> dict[int, pd.Series]:
    """Event index -> the matched contract's daily closes, for the events that have them."""
    if bars.empty or matches.empty:
        return {}
    found = matches[matches["contract"].notna()]
    contract_of = dict(zip(found["key"], found["contract"], strict=True))
    closes = {
        str(c): pd.Series(
            g["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(pd.to_datetime(g["session"]))
        )
        for c, g in bars.sort_values("session").groupby("contract")
    }
    out: dict[int, pd.Series] = {}
    for row, (_, e) in enumerate(events.iterrows()):
        request = request_for(e, series, setup)
        contract = contract_of.get(request["key"]) if request else None
        if contract is not None and contract in closes:
            out[row] = closes[contract]
    return out


# -- the calibration ------------------------------------------------------------------------------


def calibrate(
    lake: Lake,
    symbols: Sequence[str],
    *,
    now: datetime,
    model: OptionModel | None = None,
    setups: Sequence[str] = BAR_SETUPS,
) -> dict[str, Any]:
    """Backtest each setup on ``symbols`` and work out its drift and exits; store the result
    (``reports/playbook/calibration.json``, read by the live playbook) and the list of real
    contracts worth fetching (``wanted_contracts.json``)."""
    model = model or OptionModel()
    frames = bar_frames(lake, [*symbols, "SPY"])
    if not frames:
        raise LookupError("no daily bars in the lake: run `tp-data bars backfill` first")
    bench = frames["SPY"]["close"] if "SPY" in frames else None
    events, inds = find_events(
        frames, benchmark=bench, earnings=earnings_by_symbol(lake), setups=setups
    )
    events = stock_outcomes(events, inds, bench)
    bars, matches = option_history.load_bars(lake), option_history.load_matches(lake)
    series = Series(inds, model)
    daily = baseline(frames, 1)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "start": min(f.index[0] for f in frames.values()).date().isoformat(),
        "end": max(f.index[-1] for f in frames.values()).date().isoformat(),
        "names": len(inds),
        "model": model.describe(),
        "option_model": asdict(model),
        "setups": {},
    }
    wanted: list[dict[str, Any]] = []
    for name in setups:
        setup = SETUPS[name]
        mine = events[events["setup"] == name].reset_index(drop=True)
        report["setups"][name] = _calibrate_one(mine, series, setup, frames, bars, matches)
        if setup.stock is not None:
            stock = _calibrate_stock(mine, series, setup, frames, daily)
            if stock is not None:
                report["setups"][name]["stock"] = stock
        wanted += requests_for(mine, series, setup)
    path = calibration_path(lake)
    write_json(report, path)
    write_json(report, path.with_name(f"calibration-{now:%Y%m%d}.json"))
    folder, name = option_history.WANTED
    write_json(
        {"generated_at": now.isoformat(), "contracts": wanted}, lake.reports_dir(folder) / name
    )
    return report


def _calibrate_one(
    events: pd.DataFrame,
    series: Series,
    setup: Setup,
    frames: Mapping[str, pd.DataFrame],
    bars: pd.DataFrame,
    matches: pd.DataFrame,
) -> dict[str, Any]:
    done = events[events["ret"].notna()]
    base = baseline(frames, setup.horizon)
    out: dict[str, Any] = {
        "title": setup.title,
        "horizon": setup.horizon,
        "events": len(events),
        "baseline": base,
    }
    if done.empty:
        out["drift"] = None
        return out
    ret = done["ret"].to_numpy()
    n = len(ret)
    direction = setup.direction or 1
    out["drift"] = float((n * ret.mean() + SHRINK * base) / (n + SHRINK))
    out["underlying"] = {
        "mean": float(ret.mean()),
        "median": float(np.median(ret)),
        "hit_rate": float((ret * direction > 0).mean()),
        "excess": float(np.nanmean(ret - done["spy"].to_numpy()))
        if done["spy"].notna().any()
        else None,
    }
    days = int(max(GRID["time_stop"]) * setup.horizon)
    real = real_marks(events, series, setup, bars, matches)
    paths = modeled_paths(events, series, setup, days, real)
    order = np.argsort(pd.to_datetime(events["session"]).to_numpy())
    train = np.zeros(len(events), dtype=bool)
    train[order[: int(len(events) * TRAIN)]] = True
    option = choose_exits(paths, setup, train)
    chosen = option.pop("exits")
    if chosen is not None:
        out["exits"] = chosen
    out["option"] = option.pop("test") | {"detail": option}
    if paths.real.any():
        x = chosen or setup.exits.to_dict()
        result, _ = apply_exits(
            paths,
            take_profit=x["take_profit"],
            stop_loss=x["stop_loss"],
            time_stop=x["time_stop"],
            underlying=x["underlying"] is not None,
        )
        modeled = modeled_paths(events, series, setup, days)
        alone, _ = apply_exits(
            modeled,
            take_profit=x["take_profit"],
            stop_loss=x["stop_loss"],
            time_stop=x["time_stop"],
            underlying=x["underlying"] is not None,
        )
        both = paths.real & np.isfinite(result) & np.isfinite(alone)
        out["real"] = _stats(result[paths.real]) | {
            "model_bias": float(np.mean(alone[both] - result[both])) if both.any() else None
        }
    return out


def _calibrate_stock(
    events: pd.DataFrame,
    series: Series,
    setup: Setup,
    frames: Mapping[str, pd.DataFrame],
    daily: float,
) -> dict[str, Any] | None:
    """The share trade's exits and how they did, for an upward setup with events."""
    if events.empty or setup.stock is None:
        return None
    days = int(max(STOCK_GRID["time_stop"]) * setup.horizon)
    paths = stock_paths(events, frames, series, setup.stock.underlying, days)
    order = np.argsort(pd.to_datetime(events["session"]).to_numpy())
    train = np.zeros(len(events), dtype=bool)
    train[order[: int(len(events) * TRAIN)]] = True
    chosen = choose_stock_exits(paths, setup, train, daily)
    exits = chosen.pop("exits")
    out = {"test": chosen.pop("test"), "detail": chosen}
    if exits is not None:
        out["exits"] = exits
    return out


def report_lines(report: Mapping[str, Any]) -> list[str]:
    """The calibration in words, one setup per paragraph."""
    lines = [
        f"{report['names']} names, {report['start']} to {report['end']}; option trades "
        f"{report['model']}.",
        "",
    ]
    for name, s in report["setups"].items():
        lines.append(
            f"{s['title']} ({name}): {s['events']} signals, horizon {s['horizon']} sessions"
        )
        u = s.get("underlying")
        if u:
            lines.append(
                f"  stock: went the setup's way {u['hit_rate']:.0%} of the time, median "
                f"{u['median']:+.1%}; drift used {s['drift']:+.2%} "
                f"(all names: {s['baseline']:+.2%})"
            )
        o = s.get("option") or {}
        if o.get("trades"):
            exits = s.get("exits")
            chosen = (
                f"target {exits['take_profit']}, stop {exits['stop_loss']}, "
                f"{exits['time_stop']} sessions, stock rule {exits['underlying'] or 'off'}"
                if exits
                else "the defaults (too few trades to choose)"
            )
            d = (o.get("detail") or {}).get("default") or {}
            lines.append(
                f"  option trades, later {o['trades']} (out of sample): won {o['win_rate']:.0%}, "
                f"mean {o['mean']:+.0%}, median {o['median']:+.0%} with {chosen}"
                + (f"; the default exits: mean {d['mean']:+.0%}" if d.get("trades") else "")
            )
        st = (s.get("stock") or {}).get("test") or {}
        if st.get("trades"):
            x = (s.get("stock") or {}).get("exits")
            chosen = (
                f"stop {x['stop_atr']} ATR, target "
                f"{x['target_atr'] if x['target_atr'] is not None else 'none'}, "
                f"{x['time_stop']} sessions, stock rule {x['underlying'] or 'off'}"
                if x
                else "the default exits"
            )
            lines.append(
                f"  shares, later {st['trades']} (out of sample): won {st['win_rate']:.0%}, "
                f"mean {st['mean']:+.1%} in {st['avg_sessions']:.0f} sessions "
                f"({st['excess']:+.1%} over the average stock) with {chosen}"
            )
        r = s.get("real")
        if r and r.get("trades"):
            bias = r.get("model_bias")
            lines.append(
                f"  on real option prices ({r['trades']} trades since {REAL_SINCE}): won "
                f"{r['win_rate']:.0%}, mean {r['mean']:+.0%}"
                + (
                    f"; the model was {bias:+.0%} off on the same trades"
                    if bias is not None
                    else ""
                )
            )
        lines.append("")
    return lines
