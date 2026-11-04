"""Strategy 16: stress-dip calls, 1-3 session swing calls on the index funds.

Spec: docs/strategies/16-stress-dip-calls.md. When QQQ, SPY or IWM is above its 200-day average
and dips (RSI(2) under 10 with the 50-day above the 200-day, or a down day closing in the bottom
fifth of its range), **and the market is stressed** (the previous VIX close above its 10-day
average, or at least 20), buy a call about 21 days out at about 0.70 delta in the last fifteen
minutes of the session; sell it on the first close above the 5-day average, at a 40% loss, or
on the third session.

The rule (``daily``, ``dip``, ``stress``) is shared with the playbook's ``stress_dip`` setup
(``tp_strategies.setups``), which evaluates it at 15:45 with the live price as a provisional
close. The research study (``study``) works on the lake:

- **The underlying**: 1, 2 and 3-session returns after the signal close against every day (the
  control), with and without the stress filter, on the earlier ``TRAIN`` share of the sessions
  and the later rest. The stress filter reads the VIX up to the previous session only.
- **Calls, modeled**: Black-Scholes at an implied volatility that moves with the VIX (each
  fund's 30-day IV as a multiple of the VIX, from the lake's snapshots), so a bounce that calms
  the VIX also cheapens the call. Every expiry and delta in ``DTES`` x ``DELTAS``, the exits
  chosen on the training signals from ``EXIT_GRID`` and judged on the test signals, and the
  same model on every day as the control (the model is generous: read the signal minus it).
- **Calls, real prices**: the daily bars of the contracts the lake has (``option_bars_1d``:
  trades, not quotes), slightly in the money, 10 to 60 days out, followed with the strategy's
  exits; one observation per fund and session.
- **Costs**: 1, 2 and 5% of the premium a round trip.
- **At 15:45**: the trigger worked out again from the 5-minute bars, with the 15:45 price as the
  close and the day's high and low so far, entered and exited at 15:45 prices; does the signal
  the live rule sees still earn?
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd
from scipy import special
from scipy.stats import norm

from tp_core.calendar import NEW_YORK

TIER1 = ("QQQ", "SPY", "IWM")  # tradeable: the tightest call markets in the lake
OPTIONAL = ("DIA",)  # tradeable too, wider spreads
TIER2_FUNDS = ("XLF", "XLK", "SMH", "XLE", "XLV", "GLD")  # ideas and research only
TIER2_STOCKS = ("NVDA", "AAPL", "AMZN", "MSFT", "GOOGL", "META", "AVGO", "AMD", "TSLA", "JPM")
INDEX = (*TIER1, *OPTIONAL)
SKIP = (  # leveraged, bond and cash funds: not what the rule is about
    "BIL",
    "AGG",
    "IEF",
    "TLT",
    "TQQQ",
    "UPRO",
    "SOXL",
    "TNA",
    "FAS",
    "TMF",
    "HYG",
    "DBC",
)

RSI_BELOW = 10.0
IBS_BELOW = 0.2
VIX_HIGH = 20.0  # the previous VIX close at least this is stress on its own
VIX_DAYS = 10  # ... or above its average over this many closes
TIER2_VIX = 25.0  # single stocks only when the previous VIX close is at least this
EARNINGS_SESSIONS = 5  # single stocks: no results within this many sessions
TRAIN = 0.6
HORIZONS = (1, 2, 3)
DTES = (7, 14, 21, 30, 45)
DELTAS = (0.5, 0.6, 0.7, 0.8)
COSTS = (0.01, 0.02, 0.05)  # of the premium, a round trip
MIN_TRADES = 30  # a choice of exits on fewer training trades keeps the spec's
EXIT_GRID: dict[str, tuple[Any, ...]] = {
    "hold": (1, 2, 3),
    "stop": (None, -0.25, -0.4),
    "target": (None, 0.2, 0.3, 0.5),
    "ma5": (False, True),
}
REAL_MONEYNESS = (0.93, 1.0)  # strike over spot: slightly in the money
REAL_DTE = (10, 60)
REAL_MIN_VOLUME = 10


@dataclass(frozen=True)
class Exits:
    """When to sell: the session count, a stop on the premium, an optional profit target, and
    the stock-side exit (a close above the 5-day average)."""

    hold: int = 3
    stop: float | None = -0.4
    target: float | None = None
    ma5: bool = True

    def describe(self) -> str:
        parts = [f"{self.hold} session{'s' * (self.hold > 1)}"]
        if self.stop is not None:
            parts.append(f"stop {self.stop:.0%}")
        if self.target is not None:
            parts.append(f"target +{self.target:.0%}")
        if self.ma5:
            parts.append("out on a close above the 5-day average")
        return ", ".join(parts)


SPEC_EXITS = Exits()


# -- the rule -------------------------------------------------------------------------------------


def daily(bars: pd.DataFrame) -> pd.DataFrame:
    """What the rule reads, per session. ``bars``: daily ``open``, ``high``, ``low``, ``close``
    (adjusted), indexed by session; ``raw_close`` (unadjusted) is carried along when given.
    ``gain2`` and ``loss2`` are RSI(2)'s smoothed moves, kept so a provisional close can update
    it (``provisional``)."""
    close = bars["close"].astype(float)
    out = pd.DataFrame(index=bars.index)
    out["close"] = close
    out["high"] = bars["high"].astype(float)
    out["low"] = bars["low"].astype(float)
    if "raw_close" in bars:
        out["raw_close"] = bars["raw_close"].astype(float)
    for n in (5, 50, 200):
        out[f"ma{n}"] = close.rolling(n).mean()
    move = close.diff()
    out["gain2"] = move.clip(lower=0.0).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    out["loss2"] = (-move.clip(upper=0.0)).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    out["rsi2"] = _rsi(out["gain2"], out["loss2"])
    out["ibs"] = (close - out["low"]) / (out["high"] - out["low"]).replace(0.0, np.nan)
    out["ret1"] = close.pct_change(fill_method=None)
    logs = pd.Series(np.log(close.to_numpy()), index=close.index).diff()
    out["vol"] = np.sqrt((logs**2).ewm(alpha=0.06, adjust=False).mean())  # daily, as of t
    return out


def _rsi(gain: pd.Series, loss: pd.Series) -> pd.Series:
    """RSI from the smoothed moves, as ``tp_core.indicators.wilder_rsi`` works it out."""
    out: pd.Series = (100.0 - 100.0 / (1.0 + gain / loss)).where(loss > 0, 100.0)
    return out.where(gain.notna())


def dip(ind: pd.DataFrame) -> pd.Series:
    """Above the 200-day and either RSI(2) under 10 with the 50-day above the 200-day, or a down
    day closing in the bottom fifth of its range (IBS under 0.2)."""
    c = ind["close"]
    above = c > ind["ma200"]
    pullback = (ind["rsi2"] < RSI_BELOW) & (ind["ma50"] > ind["ma200"])
    weak_close = (ind["ret1"] < 0) & (ind["ibs"] < IBS_BELOW)
    out: pd.Series = (above & (pullback | weak_close)).fillna(False).astype(bool)
    return out


def stress(vix: pd.Series, sessions: Sequence[Any] | pd.Index) -> pd.DataFrame:
    """Per session: the previous VIX close (``vix_prev``, the last one dated before the
    session), its average over ``VIX_DAYS`` closes ending then (``vix_avg``), and ``stressed``:
    above that average or at least ``VIX_HIGH``. Nothing from the session itself, so it's known
    at 15:45."""
    v = vix.dropna().astype(float).sort_index()
    v.index = pd.DatetimeIndex(v.index)
    avg = v.rolling(VIX_DAYS).mean()
    days = pd.DatetimeIndex(pd.to_datetime(list(sessions)))
    at = np.searchsorted(v.index.to_numpy(), days.to_numpy(), side="left") - 1
    ok = at >= 0
    prev = np.where(ok, v.to_numpy()[np.maximum(at, 0)], np.nan)
    mean = np.where(ok, avg.to_numpy()[np.maximum(at, 0)], np.nan)
    when = np.where(ok, v.index.to_numpy()[np.maximum(at, 0)], np.datetime64("NaT", "ns"))
    with np.errstate(invalid="ignore"):
        stressed = (prev > mean) | (prev >= VIX_HIGH)
    return pd.DataFrame(
        {"vix_prev": prev, "vix_avg": mean, "vix_date": when, "stressed": stressed & ok},
        index=days,
    )


def provisional(
    ind: pd.DataFrame, price: Any, high: Any, low: Any
) -> dict[str, np.ndarray[Any, np.dtype[np.float64]]]:
    """The rule's inputs on each session of ``ind`` (``daily``) had it closed at ``price`` with
    the day's ``high`` and ``low`` so far (arrays aligned with ``ind``): what the live rule
    sees at 15:45. The same numbers ``daily`` gives with that session's bar replaced."""
    p = np.asarray(price, dtype=float)
    c = ind["close"].to_numpy(dtype=float)
    prev = ind["close"].shift(1).to_numpy(dtype=float)
    out: dict[str, np.ndarray[Any, np.dtype[np.float64]]] = {"close": p}
    for n in (5, 50, 200):
        out[f"ma{n}"] = ind[f"ma{n}"].to_numpy(dtype=float) + (p - c) / n
    move = p - prev
    gain = 0.5 * ind["gain2"].shift(1).to_numpy(dtype=float) + 0.5 * np.maximum(move, 0.0)
    loss = 0.5 * ind["loss2"].shift(1).to_numpy(dtype=float) + 0.5 * np.maximum(-move, 0.0)
    with np.errstate(all="ignore"):
        rsi = np.where(loss > 0, 100.0 - 100.0 / (1.0 + gain / loss), 100.0)
        rng = np.asarray(high, dtype=float) - np.asarray(low, dtype=float)
        out["rsi2"] = np.where(np.isfinite(gain), rsi, np.nan)
        out["ibs"] = np.where(rng > 0, (p - np.asarray(low, dtype=float)) / rng, np.nan)
        out["ret1"] = p / prev - 1
    return out


# -- the underlying -------------------------------------------------------------------------------


def events(
    frames: Mapping[str, pd.DataFrame],
    vix: pd.Series,
    earnings: Mapping[str, Sequence[Any]] | None = None,
) -> pd.DataFrame:
    """One row per (symbol, session) with 200 sessions behind it: the rule's inputs, ``dip``,
    the stress columns, the forward returns ``fwd1``..``fwd3`` (close to close) and the same in
    units of the name's daily volatility at the signal less its average day (``z1``..``z3``),
    and ``earnings_near`` (results within ``EARNINGS_SESSIONS`` sessions either way)."""
    rows = []
    for symbol, ind in frames.items():
        if len(ind) < 210:
            continue
        f = ind.copy()
        f["dip"] = dip(f)
        s = stress(vix, f.index)
        f[["vix_prev", "vix_avg", "stressed"]] = s[["vix_prev", "vix_avg", "stressed"]].to_numpy()
        f["stressed"] = f["stressed"].astype(bool)
        mean = float(f["ret1"].mean())
        for k in HORIZONS:
            f[f"fwd{k}"] = f["close"].shift(-k) / f["close"] - 1
            f[f"z{k}"] = (f[f"fwd{k}"] - k * mean) / (f["vol"] * math.sqrt(k))
        f["earnings_near"] = _near(f.index, (earnings or {}).get(symbol))
        f["symbol"] = symbol
        f = f[f["ma200"].notna()]
        rows.append(f.rename_axis("session").reset_index())
    if not rows:
        raise LookupError("no symbol has 210 sessions of daily bars")
    return pd.concat(rows, ignore_index=True)


def _near(index: pd.Index, dates: Sequence[Any] | None) -> np.ndarray[Any, np.dtype[np.bool_]]:
    out = np.zeros(len(index), dtype=bool)
    if dates is None or not len(dates):
        return out
    pos = np.searchsorted(
        pd.DatetimeIndex(index).to_numpy(), pd.DatetimeIndex(list(dates)).to_numpy()
    )
    for p in pos:
        out[max(p - EARNINGS_SESSIONS, 0) : p + EARNINGS_SESSIONS + 1] = True
    return out


def cut_of(ev: pd.DataFrame) -> pd.Timestamp:
    """The first test session: the later 1 - ``TRAIN`` of the sessions are the test."""
    days = sorted(set(ev["session"]))
    return pd.Timestamp(days[int(len(days) * TRAIN)])


def underlying_stats(g: pd.DataFrame, control: pd.DataFrame | None = None) -> dict[str, Any]:
    """Count, mean forward returns, the share up after 3 sessions, the mean vol-scaled move, and
    the mean 3-session return over the control's."""
    g = g[g["fwd3"].notna()]
    if g.empty:
        return {"n": 0}
    out: dict[str, Any] = {"n": len(g)}
    for k in HORIZONS:
        out[f"mean{k}"] = float(g[f"fwd{k}"].mean())
    out["hit3"] = float((g["fwd3"] > 0).mean())
    out["z3"] = float(g["z3"].mean())
    if control is not None and len(control):
        out["excess3"] = out["mean3"] - float(control["fwd3"].dropna().mean())
    return out


def _signals(ev: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "dip, stressed": ev["dip"] & ev["stressed"],
        "dip, any VIX": ev["dip"],
        "dip, not stressed": ev["dip"] & ~ev["stressed"],
        "every day (control)": pd.Series(True, index=ev.index),
    }


def underlying_study(ev: pd.DataFrame, groups: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Each group of names (and, for single stocks, only when the previous VIX close is at least
    ``TIER2_VIX`` with no results near) by signal, on the training and test sessions."""
    cut = cut_of(ev)
    out: dict[str, Any] = {}
    for name, symbols in groups.items():
        mine = ev[ev["symbol"].isin(symbols)]
        if mine.empty:
            continue
        table: dict[str, Any] = {}
        variants = {name: mine}
        if set(symbols) <= set(TIER2_STOCKS):
            calm = mine[(mine["vix_prev"] >= TIER2_VIX) & ~mine["earnings_near"]]
            variants[f"{name}, VIX(t-1) >= {TIER2_VIX:g}, no results near"] = calm
        for label, frame in variants.items():
            table[label] = {}
            for part, rows in (
                ("train", frame["session"] < cut),
                ("test", frame["session"] >= cut),
            ):
                part_df = frame[rows]
                control = (
                    mine[mine["session"] < cut] if part == "train" else mine[mine["session"] >= cut]
                )
                table[label][part] = {
                    sig: underlying_stats(part_df[m[rows]], control)
                    for sig, m in _signals(frame).items()
                }
        out[name] = table
    return out


# -- calls, modeled -------------------------------------------------------------------------------


@dataclass
class Paths:
    """Each signal's next ``HOLD`` sessions: closes (``spot``, entry first), whether each close
    is above its 5-day average (``above5``), the VIX (``vix``), calendar days since entry
    (``days``), the fund's IV over the VIX (``ratio``), the rate and the session."""

    keys: pd.DataFrame
    spot: np.ndarray[Any, np.dtype[np.float64]]
    above5: np.ndarray[Any, np.dtype[np.bool_]]
    vix: np.ndarray[Any, np.dtype[np.float64]]
    days: np.ndarray[Any, np.dtype[np.float64]]
    ratio: np.ndarray[Any, np.dtype[np.float64]]
    rate: np.ndarray[Any, np.dtype[np.float64]]

    def __len__(self) -> int:
        return len(self.keys)

    def take(self, rows: Any) -> Paths:
        return Paths(
            self.keys[rows].reset_index(drop=True),
            self.spot[rows],
            self.above5[rows],
            self.vix[rows],
            self.days[rows],
            self.ratio[rows],
            self.rate[rows],
        )


HOLD = max(EXIT_GRID["hold"])


def paths(
    keys: pd.DataFrame,
    prices: Mapping[str, pd.Series],
    above5: Mapping[str, pd.Series],
    vix: pd.Series,
    ratios: Mapping[str, float],
    rate: pd.Series | float,
) -> Paths:
    """``keys``: rows with ``symbol`` and ``session``; ``prices``: each symbol's price per
    session (the close, or the 15:45 price), ``above5``: whether it was above the 5-day average
    then. Signals without ``HOLD`` sessions after them are dropped."""
    v = vix.dropna().astype(float).sort_index()
    v.index = pd.DatetimeIndex(v.index)
    out_keys, spot, up, vx, days, ratio, rates = [], [], [], [], [], [], []
    for symbol, g in keys.groupby("symbol", sort=False):
        s = prices.get(str(symbol))
        if s is None:
            continue
        index = pd.DatetimeIndex(s.index)
        where = {d: i for i, d in enumerate(index)}
        values, above = s.to_numpy(dtype=float), above5[str(symbol)].reindex(index).to_numpy()
        for _, row in g.iterrows():
            i = where.get(pd.Timestamp(row["session"]))
            if i is None or i + HOLD >= len(index):
                continue
            span = index[i : i + HOLD + 1]
            out_keys.append(row)
            spot.append(values[i : i + HOLD + 1])
            up.append(above[i : i + HOLD + 1].astype(bool))
            vx.append(v.reindex(span, method="ffill").to_numpy() / 100)
            days.append(np.asarray((span - span[0]).days, dtype=float))
            ratio.append(float(ratios.get(str(symbol), 1.0)))
            r = rate if isinstance(rate, float) else float(rate.asof(span[0]))  # type: ignore[arg-type]
            rates.append(r if math.isfinite(r) else 0.04)
    if not out_keys:
        empty = np.zeros((0, HOLD + 1))
        return Paths(
            pd.DataFrame(columns=keys.columns),
            empty,
            empty.astype(bool),
            empty,
            empty,
            np.zeros(0),
            np.zeros(0),
        )
    return Paths(
        pd.DataFrame(out_keys).reset_index(drop=True),
        np.vstack(spot),
        np.vstack(up),
        np.vstack(vx),
        np.vstack(days),
        np.asarray(ratio),
        np.asarray(rates),
    )


def strike_for_delta(spot: Any, years: float, vol: Any, rate: Any, delta: float) -> Any:
    """The call strike with Black-Scholes delta ``delta``."""
    d1 = norm.ppf(delta)
    return spot * np.exp(-(d1 * vol * math.sqrt(years) - (rate + 0.5 * vol * vol) * years))


def simulate(
    p: Paths, dte: int, delta: float, exits: Exits, cost: float = 0.0
) -> tuple[np.ndarray[Any, np.dtype[np.float64]], np.ndarray[Any, np.dtype[np.float64]]]:
    """The call's return on the premium for each path (after ``cost``, a share of the premium a
    round trip) and the sessions it was held. Bought at the entry price at ``delta``, ``dte``
    days out; marked at each later price, at the IV the VIX then implies. Exits are checked on
    each mark in order: the stop, the target, the stock-side exit, the session count."""
    n = len(p)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    iv = p.vix * p.ratio[:, None]
    years = dte / 365.0
    strike = strike_for_delta(p.spot[:, 0], years, iv[:, 0], p.rate, delta)
    entry = _price(p.spot[:, 0], strike, years, iv[:, 0], p.rate)
    ret = np.full(n, np.nan)
    held = np.zeros(n)
    done = np.zeros(n, dtype=bool)
    for j in range(1, exits.hold + 1):
        t = np.maximum(dte - p.days[:, j], 0.0) / 365.0
        r = _price(p.spot[:, j], strike, t, iv[:, j], p.rate) / entry - 1
        hit = np.zeros(n, dtype=bool)
        if exits.stop is not None:
            hit |= ~done & (r <= exits.stop)
        if exits.target is not None:
            hit |= ~done & (r >= exits.target)
        if exits.ma5:
            hit |= ~done & p.above5[:, j]
        if j == exits.hold:
            hit |= ~done
        ret[hit], held[hit] = r[hit], j
        done |= hit
    return ret - cost, held


def _price(spot: Any, strike: Any, years: Any, vol: Any, rate: Any) -> Any:
    """Black-Scholes call value, element by element with a rate per row."""
    s, k, t, v, r = (
        np.asarray(x, dtype=float) for x in np.broadcast_arrays(spot, strike, years, vol, rate)
    )
    live = (t > 0) & (v > 0)
    tt, vv = np.where(live, t, 1.0), np.where(live, v, 1.0)
    sd = vv * np.sqrt(tt)
    d1 = (np.log(s / k) + (r + 0.5 * vv * vv) * tt) / sd
    call = s * special.ndtr(d1) - k * np.exp(-r * tt) * special.ndtr(d1 - sd)
    return np.where(live, call, np.maximum(s - k, 0.0))


def trade_stats(ret: np.ndarray[Any, np.dtype[np.float64]]) -> dict[str, Any]:
    """Count, mean, median, the share that won, the worst tenth."""
    x = ret[np.isfinite(ret)]
    if len(x) == 0:
        return {"n": 0}
    return {
        "n": len(x),
        "mean": float(x.mean()),
        "median": float(np.median(x)),
        "win": float((x > 0).mean()),
        "q10": float(np.quantile(x, 0.1)),
    }


def _grid() -> list[Exits]:
    out = []
    for hold, stop, target, ma5 in itertools.product(*EXIT_GRID.values()):
        out.append(Exits(hold=hold, stop=stop, target=target, ma5=ma5))
    return out


def choose_exits(
    p: Paths, train: np.ndarray[Any, np.dtype[np.bool_]], dte: int, delta: float
) -> Exits:
    """The exits in ``EXIT_GRID`` with the best mean on the training signals (with at least
    ``MIN_TRADES``), else the spec's."""
    early = p.take(train)
    if len(early) < MIN_TRADES:
        return SPEC_EXITS
    best, best_mean = SPEC_EXITS, -math.inf
    for exits in _grid():
        st = trade_stats(simulate(early, dte, delta, exits)[0])
        if st["n"] >= MIN_TRADES and st["mean"] > best_mean:
            best, best_mean = exits, st["mean"]
    return best


def modeled_study(
    p: Paths, control: Paths, cut: pd.Timestamp, cost: float = 0.02
) -> dict[str, Any]:
    """Every expiry and delta: the exits chosen on the training signals, judged on the test, and
    the spec's exits, after ``cost``; the control (every day, spec's exits) for the model's own
    bias; and profit targets added to the spec's exits."""
    train = (pd.to_datetime(p.keys["session"]) < cut).to_numpy()
    ctrl_train = (pd.to_datetime(control.keys["session"]) < cut).to_numpy()
    grid: dict[str, Any] = {}
    for dte, delta in itertools.product(DTES, DELTAS):
        chosen = choose_exits(p, train, dte, delta)
        ret, held = simulate(p, dte, delta, chosen, cost)
        spec, _ = simulate(p, dte, delta, SPEC_EXITS, cost)
        ctrl, _ = simulate(control, dte, delta, SPEC_EXITS, cost)
        grid[f"{dte}d, delta {delta:.1f}"] = {
            "dte": dte,
            "delta": delta,
            "chosen": chosen.describe(),
            "train": trade_stats(ret[train]),
            "test": trade_stats(ret[~train])
            | {"held": float(held[~train].mean()) if (~train).any() else None},
            "spec_train": trade_stats(spec[train]),
            "spec_test": trade_stats(spec[~train]),
            "control_test": trade_stats(ctrl[~ctrl_train]),
        }
    targets = {}
    for target in (None, 0.2, 0.3, 0.5):
        exits = replace(SPEC_EXITS, target=target)
        r, _ = simulate(p, 21, 0.7, exits, cost)
        label = "no target (spec)" if target is None else f"target +{target:.0%}"
        targets[label] = {"train": trade_stats(r[train]), "test": trade_stats(r[~train])}
    return {"cost": cost, "grid": grid, "targets": targets}


def as_traded(
    keys: pd.DataFrame,
    ret: np.ndarray[Any, np.dtype[np.float64]],
    held: np.ndarray[Any, np.dtype[np.float64]],
    *,
    max_open: int = 2,
) -> pd.DataFrame:
    """The signals the account would have taken: one new entry a day (the lowest RSI(2)), one
    position per fund, at most ``max_open`` open."""
    df = keys[["symbol", "session", "rsi2"]].copy()
    df["ret"], df["held"] = ret, held
    df = df[np.isfinite(df["ret"])].sort_values(["session", "rsi2"])
    days = sorted(set(df["session"]))
    order = {d: i for i, d in enumerate(sorted(set(keys["session"])))}
    open_until: dict[str, int] = {}
    taken = []
    for day in days:
        i = order[day]
        open_until = {s: e for s, e in open_until.items() if e > i}
        for _, row in df[df["session"] == day].iterrows():
            if row["symbol"] in open_until or len(open_until) >= max_open:
                continue
            open_until[str(row["symbol"])] = i + int(row["held"])
            taken.append(row)
            break  # one new entry a day
    return pd.DataFrame(taken, columns=df.columns)


# -- calls, real prices ---------------------------------------------------------------------------


def real_prices(
    option_bars: pd.DataFrame,
    contracts: pd.DataFrame,
    ev: pd.DataFrame,
    exits: Exits = SPEC_EXITS,
) -> pd.DataFrame:
    """Calls bought at a daily bar's close (strike ``REAL_MONEYNESS`` of the underlying's close,
    ``REAL_DTE`` days out, ``REAL_MIN_VOLUME`` contracts traded) and sold by ``exits`` at later
    closes, on every session the lake has a bar for; one row per underlying and session (the
    median over its contracts) with the signal columns of ``ev``. A contract with no trade on
    the session it would be sold is left out."""
    calls = contracts[contracts["right"] == "C"].drop_duplicates("contract")
    bars = option_bars.merge(
        calls[["contract", "underlying", "expiration", "strike"]], on="contract"
    )
    if bars.empty:
        return pd.DataFrame()
    bars = bars.assign(
        session=pd.to_datetime(bars["session"]), expiration=pd.to_datetime(bars["expiration"])
    )
    under = ev[["symbol", "session", "raw_close", "close", "ma5"]].copy()
    under["session"] = pd.to_datetime(under["session"])
    under["above5"] = under["close"] > under["ma5"]
    nxt = {}
    for symbol, g in under.sort_values("session").groupby("symbol"):
        s = g["session"].to_list()
        for i, d in enumerate(s):
            nxt[(symbol, d)] = s[i + 1 : i + 1 + exits.hold]
    above = dict(
        zip(zip(under["symbol"], under["session"], strict=True), under["above5"], strict=True)
    )
    spot = dict(
        zip(zip(under["symbol"], under["session"], strict=True), under["raw_close"], strict=True)
    )
    closes = dict(
        zip(zip(bars["contract"], bars["session"], strict=True), bars["close"], strict=True)
    )
    bars["spot"] = [
        spot.get(k, np.nan) for k in zip(bars["underlying"], bars["session"], strict=True)
    ]
    bars["m"] = bars["strike"] / bars["spot"]
    bars["dte"] = (bars["expiration"] - bars["session"]).dt.days
    ok = bars[
        bars["m"].between(*REAL_MONEYNESS)
        & bars["dte"].between(*REAL_DTE)
        & (bars["volume"] >= REAL_MIN_VOLUME)
        & (bars["close"] > 0)
    ]
    rows = []
    for contract, symbol, day, entry, dte in zip(
        ok["contract"], ok["underlying"], ok["session"], ok["close"], ok["dte"], strict=True
    ):
        ahead = nxt.get((symbol, day)) or []
        if len(ahead) < exits.hold:
            continue
        result, held = np.nan, 0
        for j, d in enumerate(ahead, start=1):
            mark = closes.get((contract, d))
            if mark is None:
                break
            r = mark / entry - 1
            if (
                (exits.stop is not None and r <= exits.stop)
                or (exits.target is not None and r >= exits.target)
                or (exits.ma5 and above.get((symbol, d), False))
                or j == exits.hold
            ):
                result, held = r, j
                break
        if np.isfinite(result):
            rows.append((symbol, day, result, held, dte))
    trades = pd.DataFrame(rows, columns=["symbol", "session", "ret", "held", "dte"])
    if trades.empty:
        return trades
    agg = (
        trades.groupby(["symbol", "session"])
        .agg(
            ret=("ret", "median"),
            held=("held", "median"),
            dte=("dte", "median"),
            contracts=("ret", "size"),
        )
        .reset_index()
    )
    flags = ev[["symbol", "session", "dip", "stressed", "rsi2"]].assign(
        session=lambda x: pd.to_datetime(x["session"])
    )
    return agg.merge(flags, on=["symbol", "session"], how="left")


def real_study(
    obs: pd.DataFrame, groups: Mapping[str, Sequence[str]], cut: pd.Timestamp
) -> dict[str, Any]:
    """The real-price observations by group and signal, on every session the bars cover and on
    the test sessions alone, before costs and after each of ``COSTS``."""
    if obs.empty:
        return {}
    out: dict[str, Any] = {
        "start": str(obs["session"].min().date()),
        "end": str(obs["session"].max().date()),
        "groups": {},
    }
    signals = {
        "dip, stressed": obs["dip"].fillna(False) & obs["stressed"].fillna(False),
        "dip, not stressed": obs["dip"].fillna(False) & ~obs["stressed"].fillna(False),
        "every day (control)": pd.Series(True, index=obs.index),
    }
    for name, symbols in groups.items():
        mine = obs["symbol"].isin(symbols)
        if not mine.any():
            continue
        table: dict[str, Any] = {}
        for sig, m in signals.items():
            for part, when in (
                ("all", obs["session"] >= obs["session"].min()),
                ("test", obs["session"] >= cut),
            ):
                ret = obs.loc[mine & m & when, "ret"].to_numpy(dtype=float)
                table.setdefault(sig, {})[part] = {
                    "gross": trade_stats(ret),
                    **{f"{c:.0%}": trade_stats(ret - c) for c in COSTS},
                }
        out["groups"][name] = table
    stressed = obs[signals["dip, stressed"] & obs["symbol"].isin(INDEX)]
    if len(stressed):
        buckets = pd.cut(stressed["dte"], [9, 20, 35, 60])
        out["index_by_dte"] = {
            str(b): trade_stats(stressed.loc[buckets == b, "ret"].to_numpy(dtype=float))
            for b in buckets.cat.categories
        }
        out["index_by_symbol"] = {
            str(s): trade_stats(g["ret"].to_numpy(dtype=float))
            for s, g in stressed.groupby("symbol")
        }
    return out


# -- at 15:45 -------------------------------------------------------------------------------------


def at_1545(intraday: pd.DataFrame, minutes_before: int = 15, bar: int = 5) -> pd.DataFrame:
    """Per (symbol, session) from 5-minute bars (``tp_core.intraday.load_intraday``, raw
    prices): the price ``minutes_before`` the close (the close of the bar ending then) and the
    day's open, high and low up to it. Half days count from their own close. Sessions without
    the bar ending then are left out."""
    df = intraday.copy()
    local = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NEW_YORK)
    df["minute"] = local.dt.hour * 60 + local.dt.minute
    last = df.groupby(["symbol", "session"])["minute"].transform("max")
    cutoff = last + bar - minutes_before  # bars starting before this end by the cutoff
    early = df[df["minute"] < cutoff]
    g = early.sort_values("minute").groupby(["symbol", "session"])
    out = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        price=("close", "last"),
        minute=("minute", "last"),
    ).reset_index()
    want = (last + bar - minutes_before - bar).groupby([df["symbol"], df["session"]]).first()
    out = out[
        out["minute"].to_numpy()
        == want.reindex(pd.MultiIndex.from_frame(out[["symbol", "session"]])).to_numpy()
    ]
    out["session"] = pd.to_datetime(out["session"])
    result: pd.DataFrame = out.drop(columns="minute").reset_index(drop=True)
    return result


def late_study(
    late: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    vix: pd.Series,
    ratios: Mapping[str, float],
    rate: pd.Series | float,
    cut: pd.Timestamp,
    *,
    exits: Exits = SPEC_EXITS,
    dte: int = 21,
    delta: float = 0.7,
) -> dict[str, Any]:
    """The rule worked out at 15:45 (``at_1545``) on each fund with 5-minute bars: how often it
    agrees with the close's signal, the underlying's moves from 15:45 to the later 15:45s, and
    the modeled call bought and sold at 15:45 prices with the spec's exits (the exit's 5-day
    average with the 15:45 price as its close), after each of ``COSTS``."""
    prices, above, rows = {}, {}, []
    for symbol, g in late.groupby("symbol"):
        ind = frames.get(str(symbol))
        if ind is None or "raw_close" not in ind:
            continue
        g = g.set_index("session").reindex(ind.index)
        factor = (ind["close"] / ind["raw_close"]).to_numpy()  # to the adjusted prices
        p = g["price"].to_numpy() * factor
        prov = provisional(ind, p, g["high"].to_numpy() * factor, g["low"].to_numpy() * factor)
        frame = pd.DataFrame(prov, index=ind.index)
        frame["ma200_ok"] = frame["ma200"].notna() & ind["ma200"].notna()
        fired = dip(frame) & frame["ma200_ok"]
        s = stress(vix, ind.index)
        have = np.isfinite(p)
        prices[str(symbol)] = pd.Series(p, index=ind.index)
        above[str(symbol)] = pd.Series(p > frame["ma5"].to_numpy(), index=ind.index)
        close_sig = dip(ind) & s["stressed"].to_numpy()
        late_sig = fired & s["stressed"].to_numpy() & have
        rows.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "session": ind.index,
                    "late": late_sig.to_numpy(),
                    "close": close_sig.to_numpy() & have,
                    "have": have,
                    "rsi2": frame["rsi2"].to_numpy(),
                }
            )
        )
    if not rows:
        return {}
    flags = pd.concat(rows, ignore_index=True)
    flags = flags[flags["have"]]
    out: dict[str, Any] = {
        "sessions": int(flags["session"].nunique()),
        "start": str(flags["session"].min().date()),
        "end": str(flags["session"].max().date()),
        "agreement": {
            "both": int((flags["late"] & flags["close"]).sum()),
            "only at 15:45": int((flags["late"] & ~flags["close"]).sum()),
            "only at the close": int((~flags["late"] & flags["close"]).sum()),
        },
    }
    for label, m in (
        ("signal at 15:45", flags["late"]),
        ("signal at the close", flags["close"]),
        ("every day (control)", pd.Series(True, index=flags.index)),
    ):
        keys = flags[m][["symbol", "session", "rsi2"]]
        p = paths(keys, prices, above, vix, ratios, rate)
        train = (pd.to_datetime(p.keys["session"]) < cut).to_numpy()
        moves = (p.spot[:, 1:] / p.spot[:, :1] - 1) if len(p) else np.zeros((0, HOLD))
        ret, held = simulate(p, dte, delta, exits)
        entry = {
            "underlying": {"train": _moves(moves[train]), "test": _moves(moves[~train])},
            "calls": {
                part: {
                    "gross": trade_stats(ret[rows_]),
                    **{f"{c:.0%}": trade_stats(ret[rows_] - c) for c in COSTS},
                }
                for part, rows_ in (("train", train), ("test", ~train))
            },
        }
        if label == "signal at 15:45":
            taken = as_traded(p.keys, ret, held)
            t = taken["session"] >= cut
            entry["as_traded_test"] = {
                f"{c:.0%}": trade_stats(taken.loc[t, "ret"].to_numpy() - c) for c in COSTS
            }
        out[label] = entry
    out["model"] = f"{dte} days, delta {delta:.2f}; {exits.describe()}"
    return out


def _moves(moves: np.ndarray[Any, np.dtype[np.float64]]) -> dict[str, Any]:
    """The underlying's mean move 1 to ``HOLD`` sessions on, and the share up at the last."""
    last = moves[:, HOLD - 1] if len(moves) else np.zeros(0)
    ok = np.isfinite(last)
    if not ok.any():
        return {"n": 0}
    out: dict[str, Any] = {"n": int(ok.sum()), "hit3": float((last[ok] > 0).mean())}
    for k in HORIZONS:
        out[f"mean{k}"] = float(np.nanmean(moves[ok, k - 1]))
    return out


# -- the whole study ------------------------------------------------------------------------------


def iv_ratios(iv: pd.DataFrame, vix: pd.Series) -> dict[str, float]:
    """Each underlying's median 30-day ATM implied volatility over the VIX on the same day
    (``tp_core.volatility.iv_history``)."""
    if iv.empty:
        return {}
    v = vix.dropna().astype(float)
    v.index = pd.DatetimeIndex(v.index)
    days = pd.DatetimeIndex(pd.to_datetime(iv["snapshot_date"]))
    level = v.reindex(days, method="ffill").to_numpy() / 100
    ratio = pd.Series(iv["iv30"].to_numpy(dtype=float) / level, index=iv.index)
    ok = ratio.notna() & np.isfinite(ratio) & (ratio > 0)
    return {
        str(k): float(x) for k, x in ratio[ok].groupby(iv.loc[ok, "underlying"]).median().items()
    }


def study(
    frames: Mapping[str, pd.DataFrame],
    vix: pd.Series,
    *,
    rate: pd.Series | float = 0.04,
    ratios: Mapping[str, float] | None = None,
    funds: Sequence[str] = (),
    earnings: Mapping[str, Sequence[Any]] | None = None,
    option_bars: pd.DataFrame | None = None,
    contracts: pd.DataFrame | None = None,
    intraday: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """The whole study (see the module's docstring). ``frames``: ``daily`` per symbol (with
    ``raw_close`` for the real-price and 15:45 checks); ``funds``: the symbols that are funds
    (the rest of the non-index names count as single stocks)."""
    ratios = dict(ratios or {})
    ev = events(frames, vix, earnings)
    cut = cut_of(ev)
    names = set(ev["symbol"])
    sector = [s for s in names if s in set(funds) and s not in INDEX and s not in SKIP]
    groups = {
        "index (QQQ, SPY, IWM)": list(TIER1),
        "DIA": list(OPTIONAL),
        "tier 2 funds": list(TIER2_FUNDS),
        "tier 2 mega caps": list(TIER2_STOCKS),
        "every other fund": sector,
        "every single stock": [
            s for s in names if s not in set(funds) and s not in INDEX and s not in SKIP
        ],
    }
    out: dict[str, Any] = {
        "symbols": len(names),
        "start": str(pd.Timestamp(ev["session"].min()).date()),
        "end": str(pd.Timestamp(ev["session"].max()).date()),
        "test_from": str(cut.date()),
        "rule": (
            f"above the 200-day; RSI(2) < {RSI_BELOW:g} with the 50-day above the 200-day, or a "
            f"down day with IBS < {IBS_BELOW:g}; stressed: previous VIX close above its "
            f"{VIX_DAYS}-day average or >= {VIX_HIGH:g}"
        ),
        "ratios": {k: ratios.get(k) for k in INDEX},
        "underlying": underlying_study(ev, groups),
    }
    tier1 = ev[ev["symbol"].isin(TIER1)]
    prices = {s: frames[s]["close"] for s in TIER1 if s in frames}
    above = {s: frames[s]["close"] > frames[s]["ma5"] for s in TIER1 if s in frames}
    signal = tier1[tier1["dip"] & tier1["stressed"]]
    p = paths(signal, prices, above, vix, ratios, rate)
    control = paths(tier1, prices, above, vix, ratios, rate)
    modeled = modeled_study(p, control, cut)
    train = (pd.to_datetime(p.keys["session"]) < cut).to_numpy()
    no_filter = paths(tier1[tier1["dip"]], prices, above, vix, ratios, rate)
    nf_train = (pd.to_datetime(no_filter.keys["session"]) < cut).to_numpy()
    spec_ret, spec_held = simulate(p, 21, 0.7, SPEC_EXITS)
    nf_ret, _ = simulate(no_filter, 21, 0.7, SPEC_EXITS)
    taken = as_traded(p.keys, spec_ret, spec_held)
    later = taken["session"] >= cut
    modeled["spec"] = {
        "model": "21 days, delta 0.70; " + SPEC_EXITS.describe(),
        "costs": {
            f"{c:.0%}": {
                "train": trade_stats(spec_ret[train] - c),
                "test": trade_stats(spec_ret[~train] - c),
            }
            for c in COSTS
        },
        "without the stress filter, 2%": {
            "train": trade_stats(nf_ret[nf_train] - 0.02),
            "test": trade_stats(nf_ret[~nf_train] - 0.02),
        },
        "as traded (1 a day, 2 open), 2%": {
            "train": trade_stats(taken.loc[~later, "ret"].to_numpy() - 0.02),
            "test": trade_stats(taken.loc[later, "ret"].to_numpy() - 0.02),
        },
    }
    out["modeled"] = modeled
    if option_bars is not None and contracts is not None and len(option_bars):
        obs = real_prices(option_bars, contracts, ev)
        out["real"] = real_study(
            obs,
            {
                "index (QQQ, SPY, IWM, DIA)": list(INDEX),
                "every fund": list(INDEX) + sector,
                "single stocks": groups["every single stock"],
            },
            cut,
        )
    if intraday is not None and len(intraday):
        late = at_1545(intraday)
        out["at_1545"] = late_study(
            late, {s: frames[s] for s in INDEX if s in frames}, vix, ratios, rate, cut
        )
    out["by_year"] = _by_year(p, spec_ret)
    return out


def _by_year(p: Paths, ret: np.ndarray[Any, np.dtype[np.float64]]) -> dict[str, Any]:
    if not len(p):
        return {}
    year = pd.to_datetime(p.keys["session"]).dt.year.to_numpy()
    return {str(y): trade_stats(ret[year == y] - 0.02) for y in sorted(set(year))}
