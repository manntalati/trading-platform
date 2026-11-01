"""Strategy 15: the first five-minute candle against the 12-period EMA.

Spec: docs/strategies/15-open-ema.md. At 9:35 New York time the session's first 5-minute candle
has closed. If it closed above the 12-period exponential moving average of 5-minute closes, buy;
below it, sell short. The EMA runs on across sessions (as a chart draws it), so at 9:35 it is
mostly yesterday's last hour: the rule asks whether the open held above or below where the stock
spent the end of the previous day.

A vectorized research backtest on stored 5-minute bars (``tp_core.intraday``). Every
(symbol, session) becomes a row of a matrix whose columns are the session's bars, so a whole
universe and a grid of exits run in seconds:

- **Entry**: the open of the bar after the candle (9:35 for a 5-minute candle), never the
  candle's own close, plus ``cost_bps`` each way (half the spread and slippage).
- **Exits**, the first that comes: a stop (the candle's other extreme, a share of the daily
  ATR, or none), an optional target some multiples of the risk away, an optional exit on a
  5-minute close back across the EMA, and the last bar's close (flat by the close). A bar that
  opens past a level fills at its open; a bar that touches the stop and the target counts as
  stopped (the worse case). A stop fills ``stop_slip_bps`` worse than its level: it becomes a
  market order once touched, and prices jump past it (filling exactly at the level would credit
  every stopped trade with the overshoot, which the tests measure on random walks).
- **The morning's filters** (``in_play``) read only what was known at 9:35: the candle's volume
  against its own average over the 14 sessions before (relative volume), the gap from the
  previous close, the 14-day ATR and average volume, and the previous close.

``study`` runs the rule and its controls (always long; long or short by the candle's colour),
chooses the exits on the earlier ``TRAIN`` share of the sessions and reports them on the rest,
checks other EMA lengths and candle lengths, and compares ways to pick the tickers: one index
fund, every name, the names that did best before (do they keep doing best?), and the names "in
play" each morning by relative volume (Zarattini, Barbon and Aziz's finding for opening-range
trades), by gap and by side. Shorts assume shares can be borrowed at no cost.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from tp_core.calendar import NEW_YORK

SPANS = (9, 12, 20, 26)  # EMA lengths worked out for every session (the rule uses 12)
LOOKBACK = 14  # sessions behind the ATR, the average volume and the relative volume
TRAIN = 0.6  # the earlier share of the sessions the exits are chosen on
MIN_TRADES = 30  # a choice on fewer trades than this keeps the defaults
GRID: dict[str, tuple[Any, ...]] = {
    "stop": ("candle", "atr", "none"),
    "target_r": (1.0, 2.0, None),
    "ema_exit": (False, True),
}
IN_PLAY = {"min_price": 5.0, "min_volume": 1_000_000.0, "min_atr": 0.5}


@dataclass(frozen=True)
class Config:
    ema: int = 12  # periods of 5-minute closes (one of SPANS)
    candle: int = 5  # minutes in the opening candle: 5 is the 9:30 bar, 15 the first three
    minutes: int = 5  # bar length
    side: str = "both"  # "both", "long" or "short"
    stop: str = "candle"  # "candle" (its low for a long), "atr" (stop_atr of the ATR), "none"
    stop_atr: float = 0.10  # Zarattini et al.'s stop: a tenth of the 14-day ATR
    target_r: float | None = None  # take profit this many times the risk to the stop
    ema_exit: bool = False  # also out on a close back across the EMA
    cost_bps: float = 1.0  # each way
    stop_slip_bps: float = 2.0  # a stop is a market order once touched: it fills a little worse

    def __post_init__(self) -> None:
        if self.ema not in SPANS:
            raise ValueError(f"ema is one of {SPANS}")
        if self.candle % self.minutes or self.candle < self.minutes:
            raise ValueError("the candle is a whole number of bars")
        if self.side not in ("both", "long", "short") or self.stop not in ("candle", "atr", "none"):
            raise ValueError("side is both, long or short; stop is candle, atr or none")
        if self.target_r is not None and self.stop == "none":
            raise ValueError("a target in multiples of the risk needs a stop")

    def describe(self) -> str:
        stop = {"candle": "the candle's other end", "atr": f"{self.stop_atr:g} ATR",
                "none": "no stop"}[self.stop]  # fmt: skip
        target = f", target {self.target_r:g}R" if self.target_r else ""
        ema = ", out on a close back across the EMA" if self.ema_exit else ""
        return (
            f"{self.candle}-minute candle vs EMA({self.ema}), {self.side}; stop at {stop}"
            f"{target}{ema}; flat by the close; {self.cost_bps:g} bp each way"
        )


@dataclass
class Sessions:
    """Each (symbol, session) with its bars as a row (columns: the bar's place in the session),
    the EMA of every span at each bar, and the morning's features (``keys``)."""

    keys: pd.DataFrame  # symbol, session, prev_close, atr, avg_volume, candle_volume, rvol, gap
    open: np.ndarray[Any, np.dtype[np.float64]]
    high: np.ndarray[Any, np.dtype[np.float64]]
    low: np.ndarray[Any, np.dtype[np.float64]]
    close: np.ndarray[Any, np.dtype[np.float64]]
    volume: np.ndarray[Any, np.dtype[np.float64]]
    ema: dict[int, np.ndarray[Any, np.dtype[np.float64]]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.keys)

    def take(self, rows: np.ndarray[Any, np.dtype[Any]]) -> Sessions:
        """The sessions at ``rows`` (a boolean mask or positions)."""
        return Sessions(
            keys=self.keys[rows].reset_index(drop=True),
            open=self.open[rows],
            high=self.high[rows],
            low=self.low[rows],
            close=self.close[rows],
            volume=self.volume[rows],
            ema={k: v[rows] for k, v in self.ema.items()},
        )


def sessions_from_bars(bars: pd.DataFrame, minutes: int = 5, warmup: int = 2) -> Sessions:
    """``bars``: ``tp_core.intraday.load_intraday`` (regular hours). Each symbol's first
    ``warmup`` sessions only warm the EMAs up and are left out."""
    if bars.empty:
        raise LookupError("no intraday bars: tp-data bars intraday")
    df = bars.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    local = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NEW_YORK)
    col = ((local.dt.hour * 60 + local.dt.minute - 9 * 60 - 30) // minutes).to_numpy()
    width = int(390 // minutes)
    df = df[(col >= 0) & (col < width)]
    col = col[(col >= 0) & (col < width)]
    close = df["close"].astype(float)
    by = close.groupby(df["symbol"])
    emas = {span: by.transform(_ema, span=span) for span in SPANS}
    keys = df[["symbol", "session"]].drop_duplicates().reset_index(drop=True)
    row = df.groupby(["symbol", "session"], sort=False).ngroup().to_numpy()
    n = len(keys)

    def matrix(values: Any) -> np.ndarray[Any, np.dtype[np.float64]]:
        out = np.full((n, width), np.nan)
        out[row, col] = np.asarray(values, dtype=float)
        return out

    s = Sessions(
        keys=keys,
        open=matrix(df["open"]),
        high=matrix(df["high"]),
        low=matrix(df["low"]),
        close=matrix(close),
        volume=matrix(df["volume"]),
        ema={span: matrix(e) for span, e in emas.items()},
    )
    _features(s)
    keep = s.keys.groupby("symbol").cumcount().to_numpy() >= warmup
    return s.take(keep)


def _ema(closes: pd.Series, span: int) -> pd.Series:
    """The exponential moving average a chart draws (no bias adjustment), on across sessions."""
    out: pd.Series = closes.ewm(span=span, adjust=False).mean()
    return out


def _features(s: Sessions, candle_bars: int = 1) -> None:
    """The morning's view of each session, from the sessions before it (same symbol); the
    relative volume is the first bar's against its own average."""
    with np.errstate(all="ignore"):
        high = np.nanmax(s.high, axis=1)
        low = np.nanmin(s.low, axis=1)
    last = _last_valid(s.close)
    close = s.close[np.arange(len(s)), np.maximum(last, 0)]
    k = s.keys
    prev = pd.Series(close).groupby(k["symbol"]).shift(1)
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))
    k["prev_close"] = prev.to_numpy()
    k["atr"] = _trailing(pd.Series(tr), k["symbol"])
    k["avg_volume"] = _trailing(pd.Series(np.nansum(s.volume, axis=1)), k["symbol"])
    candle = pd.Series(np.nansum(s.volume[:, :candle_bars], axis=1))
    k["candle_volume"] = candle.to_numpy()
    k["rvol"] = candle.to_numpy() / _trailing(candle, k["symbol"])
    k["gap"] = s.open[:, 0] / prev.to_numpy() - 1


def _trailing(values: pd.Series, symbol: pd.Series) -> np.ndarray[Any, np.dtype[np.float64]]:
    """The mean of the ``LOOKBACK`` sessions before each one (NaN until there are enough)."""
    out = values.groupby(symbol.to_numpy()).transform(
        lambda v: v.shift(1).rolling(LOOKBACK, min_periods=LOOKBACK).mean()
    )
    return out.to_numpy(dtype=float)


def _last_valid(m: np.ndarray[Any, np.dtype[np.float64]]) -> np.ndarray[Any, np.dtype[np.int64]]:
    ok = np.isfinite(m)
    width = m.shape[1]
    return np.where(ok.any(axis=1), width - 1 - ok[:, ::-1].argmax(axis=1), -1)


# -- the trades -----------------------------------------------------------------------------------


def candle(s: Sessions, cfg: Config) -> dict[str, np.ndarray[Any, np.dtype[np.float64]]]:
    """Each session's opening candle and the EMA at its close."""
    k = cfg.candle // cfg.minutes
    with np.errstate(all="ignore"):
        return {
            "open": s.open[:, 0],
            "high": np.nanmax(s.high[:, :k], axis=1),
            "low": np.nanmin(s.low[:, :k], axis=1),
            "close": s.close[:, k - 1],
            "ema": s.ema[cfg.ema][:, k - 1],
            "volume": np.nansum(s.volume[:, :k], axis=1),
        }


def sides(s: Sessions, cfg: Config, rule: str = "ema") -> np.ndarray[Any, np.dtype[np.float64]]:
    """+1 long, -1 short, 0 no trade. ``rule``: "ema" (the strategy), "long" (always long, the
    control), "colour" (long a green candle, short a red one)."""
    c = candle(s, cfg)
    if rule == "ema":
        side = np.sign(c["close"] - c["ema"])
    elif rule == "long":
        side = np.ones(len(s))
    elif rule == "colour":
        side = np.sign(c["close"] - c["open"])
    else:
        raise ValueError("rule is ema, long or colour")
    side = np.nan_to_num(side)
    if cfg.side == "long":
        side[side < 0] = 0
    elif cfg.side == "short":
        side[side > 0] = 0
    return side


def simulate(s: Sessions, cfg: Config, rule: str = "ema") -> pd.DataFrame:
    """One row per trade: the session's keys plus ``side``, ``entry``, ``stop``, ``exit``,
    ``reason`` (stop, target, ema, close), ``bars`` held, ``ret`` (after costs) and ``r``
    (the result in multiples of the risk to the stop; NaN without a stop)."""
    k = cfg.candle // cfg.minutes
    side = sides(s, cfg, rule)
    entry = s.open[:, k]
    c = candle(s, cfg)
    ok = (side != 0) & np.isfinite(entry) & (entry > 0) & np.isfinite(c["close"])
    if cfg.stop == "candle":
        stop = np.where(side > 0, c["low"], c["high"])
    elif cfg.stop == "atr":
        stop = entry - side * cfg.stop_atr * s.keys["atr"].to_numpy(dtype=float)
        ok &= np.isfinite(stop)
    else:
        stop = np.full(len(s), np.nan)
    risk = np.abs(entry - stop)
    target = entry + side * cfg.target_r * risk if cfg.target_r else np.full(len(s), np.nan)
    if not ok.any():
        return _trades(s, ok, side, entry, stop, entry, "close", 0, risk, cfg)
    sd = side[:, None]
    fo, fc = sd * s.open, sd * s.close
    fh = np.where(sd > 0, s.high, -s.low)  # the side's best and worst prices in each bar
    fl = np.where(sd > 0, s.low, -s.high)
    sf, tf = (side * stop)[:, None], (side * target)[:, None]
    width = s.close.shape[1]
    valid = np.isfinite(s.close) & (np.arange(width)[None, :] >= k)
    fill = np.full(s.close.shape, np.nan)
    reason = np.zeros(s.close.shape, dtype=np.int8)  # 1 ema, 2 target, 3 stop
    with np.errstate(invalid="ignore"):
        steps: list[tuple[Any, Any, int]] = []  # the weakest first: later ones override
        if cfg.ema_exit:
            steps.append((fc < sd * s.ema[cfg.ema], fc, 1))
        if cfg.target_r:
            steps.append((fh >= tf, np.broadcast_to(tf, fill.shape), 2))
        if cfg.stop != "none":
            steps.append((fl <= sf, np.broadcast_to(sf, fill.shape), 3))
        if cfg.target_r:
            steps.append((fo >= tf, fo, 2))  # opened past the target: filled at the open
        if cfg.stop != "none":
            steps.append((fo <= sf, fo, 3))  # opened past the stop: filled at the open
        for hit, price, code in steps:
            hit = hit & valid
            fill = np.where(hit, price, fill)
            reason = np.where(hit, code, reason)
    rows = np.arange(len(s))
    hit = np.isfinite(fill)
    first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
    last = _last_valid(np.where(valid, s.close, np.nan))
    at = np.where(first >= 0, first, last)
    out_f = np.where(first >= 0, fill[rows, np.maximum(at, 0)], fc[rows, np.maximum(at, 0)])
    why = np.where(first >= 0, reason[rows, np.maximum(at, 0)], 0)
    out_f = np.where(why == 3, out_f - cfg.stop_slip_bps / 1e4 * entry, out_f)  # the slippage
    ok &= at >= k
    exit_price = side * out_f
    words = np.array(["close", "ema", "target", "stop"])[why]
    return _trades(s, ok, side, entry, stop, exit_price, words, at - k + 1, risk, cfg)


def _trades(
    s: Sessions,
    ok: np.ndarray[Any, np.dtype[np.bool_]],
    side: Any,
    entry: Any,
    stop: Any,
    exit_price: Any,
    reason: Any,
    held: Any,
    risk: Any,
    cfg: Config,
) -> pd.DataFrame:
    n = len(s)
    out = s.keys.copy()
    out["side"] = side
    out["entry"] = entry
    out["stop"] = stop
    out["exit"] = np.broadcast_to(exit_price, n)
    out["reason"] = np.broadcast_to(reason, n)
    out["bars"] = np.broadcast_to(held, n)
    gross = side * (out["exit"].to_numpy(dtype=float) / entry - 1)
    out["ret"] = gross - 2 * cfg.cost_bps / 1e4
    with np.errstate(all="ignore"):
        out["r"] = np.where(risk > 0, side * (out["exit"].to_numpy(dtype=float) - entry) / risk,
                            np.nan)  # fmt: skip
    return out[ok].reset_index(drop=True)


# -- picking the tickers --------------------------------------------------------------------------


def in_play(s: Sessions, top: int | None, **limits: float) -> np.ndarray[Any, np.dtype[np.bool_]]:
    """The sessions that pass the liquidity floor (previous close, average volume, ATR:
    ``IN_PLAY`` unless given) and, with ``top``, are among the ``top`` highest relative volumes
    of their day."""
    floor = IN_PLAY | limits
    k = s.keys
    ok = (
        (k["prev_close"] >= floor["min_price"])
        & (k["avg_volume"] >= floor["min_volume"])
        & (k["atr"] >= floor["min_atr"])
        & k["rvol"].notna()
    ).to_numpy()
    if top is None:
        return ok
    rank = k["rvol"].where(ok).groupby(k["session"]).rank(ascending=False, method="first")
    return ok & (rank <= top).to_numpy()


# -- how it did -----------------------------------------------------------------------------------


def book(trades: pd.DataFrame, days: Sequence[Any]) -> pd.Series:
    """The account's return each session, the day's capital split equally among its trades
    (no trade: 0)."""
    daily = trades.groupby("session")["ret"].mean() if len(trades) else pd.Series(dtype=float)
    out = pd.Series(0.0, index=pd.Index(sorted(set(days)), name="session"))
    out.loc[daily.index] = daily.to_numpy()
    return out


def stats(trades: pd.DataFrame, days: Sequence[Any]) -> dict[str, Any]:
    """Per trade (count, winners, mean and median in basis points, mean R, how they ended) and
    for the book (``book``: annualized return and Sharpe, maximum drawdown)."""
    if trades.empty:
        return {"trades": 0, "sessions": len(set(days))}
    ret = trades["ret"].to_numpy(dtype=float)
    daily = book(trades, days)
    curve = (1 + daily).cumprod()
    sd = float(daily.std())
    r = trades["r"].dropna()
    return {
        "trades": len(trades),
        "sessions": len(daily),
        "long": int((trades["side"] > 0).sum()),
        "win_rate": float((ret > 0).mean()),
        "mean_bps": float(ret.mean() * 1e4),
        "median_bps": float(np.median(ret) * 1e4),
        "avg_r": float(r.mean()) if len(r) else None,
        "stopped": float((trades["reason"] == "stop").mean()),
        "annual": float(daily.mean() * 252),
        "sharpe": float(daily.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "max_drawdown": float((curve / curve.cummax() - 1).min()),
    }


def _split(s: Sessions) -> tuple[np.ndarray[Any, np.dtype[np.bool_]], Any]:
    days = sorted(set(s.keys["session"]))
    cut = days[int(len(days) * TRAIN)] if len(days) > 1 else days[-1]
    return (s.keys["session"] < cut).to_numpy(), cut


def _parts(s: Sessions, trades: pd.DataFrame, train: Any) -> dict[str, Any]:
    days = s.keys["session"]
    early = trades["session"] < train
    return {
        "all": stats(trades, list(days)),
        "train": stats(trades[early], list(days[days < train])),
        "test": stats(trades[~early], list(days[days >= train])),
    }


def choose_exits(s: Sessions, cfg: Config, train: np.ndarray[Any, np.dtype[np.bool_]]) -> Config:
    """The combination in ``GRID`` with the best Sharpe on the training sessions (with at least
    ``MIN_TRADES`` trades), else ``cfg`` as it is."""
    early = s.take(train)
    days = list(early.keys["session"])
    best, best_sharpe = cfg, -math.inf
    for stop, target, ema_exit in itertools.product(*GRID.values()):
        if target is not None and stop == "none":
            continue
        trial = replace(cfg, stop=stop, target_r=target, ema_exit=ema_exit)
        st = stats(simulate(early, trial), days)
        if st["trades"] >= MIN_TRADES and (st.get("sharpe") or -math.inf) > best_sharpe:
            best, best_sharpe = trial, st["sharpe"]
    return best


def study(
    s: Sessions, cfg: Config | None = None, *, tops: Sequence[int] = (5, 10, 20)
) -> dict[str, Any]:
    """The rule, its controls, the walk-forward exits, other EMA and candle lengths, the cost,
    and ways of picking the tickers, each on the earlier (train) and later (test) sessions."""
    cfg = cfg or Config()
    early, cut = _split(s)
    base = simulate(s, cfg)
    out: dict[str, Any] = {
        "config": cfg.describe(),
        "symbols": int(s.keys["symbol"].nunique()),
        "sessions": int(s.keys["session"].nunique()),
        "start": str(min(s.keys["session"])),
        "end": str(max(s.keys["session"])),
        "test_from": str(cut),
        "rule": _parts(s, base, cut),
        "controls": {
            "always long": _parts(s, simulate(s, cfg, "long"), cut),
            "candle colour": _parts(s, simulate(s, cfg, "colour"), cut),
        },
    }
    chosen = choose_exits(s, cfg, early)
    out["exits"] = {"chosen": chosen.describe(), **_parts(s, simulate(s, chosen), cut)}
    out["lengths"] = {
        f"EMA {span}, {length}-minute candle": _parts(
            s, simulate(s, replace(cfg, ema=span, candle=length)), cut
        )["test"]
        for span in SPANS
        for length in (cfg.minutes, 3 * cfg.minutes)
    }
    out["costs"] = {
        f"{bps:g} bp": _parts(s, simulate(s, replace(cfg, cost_bps=bps)), cut)["test"]
        for bps in (0.0, 1.0, 2.0, 5.0)
    }
    out["sides"] = {
        side: _parts(s, simulate(s, replace(cfg, side=side)), cut) for side in ("long", "short")
    }
    out["tickers"] = pick_tickers(s, cfg, base, cut, tops)
    out["by_year"] = _by_year(s, base)
    return out


def pick_tickers(
    s: Sessions, cfg: Config, base: pd.DataFrame, cut: Any, tops: Sequence[int]
) -> dict[str, Any]:
    """Ways of choosing what to trade, each on the same rule: every name; each name alone; the
    quarter of the names with the best training Sharpe (judged on the test sessions); the
    names in play each morning; relative-volume and gap buckets."""
    days = s.keys["session"]
    test_days = list(days[days >= cut])
    later = base[base["session"] >= cut]
    out: dict[str, Any] = {"every name": stats(later, test_days)}
    per = {}
    for symbol, g in base.groupby("symbol"):
        mine = s.keys["symbol"] == symbol
        per[str(symbol)] = {
            "train": stats(g[g["session"] < cut], list(days[mine & (days < cut)])),
            "test": stats(g[g["session"] >= cut], list(days[mine & (days >= cut)])),
        }
    out["per_symbol"] = per
    ranked = sorted(
        (k for k, v in per.items() if (v["train"].get("trades") or 0) >= 20),
        key=lambda k: -(per[k]["train"].get("sharpe") or -math.inf),
    )
    if len(ranked) >= 4:
        best = set(ranked[: max(1, len(ranked) // 4)])
        out["best quarter on train"] = {
            "symbols": sorted(best),
            "test": stats(later[later["symbol"].isin(best)], test_days),
            "the rest": stats(later[~later["symbol"].isin(best)], test_days),
        }
    flagged = in_play(s, None)
    keys = s.keys.assign(liquid=flagged)
    for top in tops:
        chosen = in_play(s, top)
        picked = keys[chosen][["symbol", "session"]]
        mine = later.merge(picked, on=["symbol", "session"])
        out[f"top {top} by relative volume"] = stats(mine, test_days)
    liquid = later.merge(keys[keys["liquid"]][["symbol", "session"]], on=["symbol", "session"])
    if len(liquid) >= 10:
        buckets = pd.qcut(liquid["rvol"], 5, labels=False, duplicates="drop")
        out["relative volume quintiles"] = {
            f"Q{int(q) + 1} (rvol {liquid['rvol'][buckets == q].min():.2f}-"
            f"{liquid['rvol'][buckets == q].max():.2f})": stats(liquid[buckets == q], test_days)
            for q in sorted(buckets.dropna().unique())
        }
    gap = later["gap"].abs() >= 0.01
    agrees = np.sign(later["gap"]) == later["side"]
    out["gap of 1%+"] = stats(later[gap], test_days)
    out["gap of 1%+, the rule with the gap"] = stats(later[gap & agrees], test_days)
    out["gap of 1%+, the rule against the gap"] = stats(later[gap & ~agrees], test_days)
    return out


def _by_year(s: Sessions, trades: pd.DataFrame) -> dict[str, Any]:
    days = pd.Series(pd.to_datetime(s.keys["session"]))
    year = pd.to_datetime(trades["session"]).dt.year
    return {
        str(y): stats(trades[year == y], list(s.keys["session"][(days.dt.year == y).to_numpy()]))
        for y in sorted(year.unique())
    }
