"""Strategy 7: pairs trading on cointegration (Engle-Granger; Gatev, Goetzmann and Rouwenhorst).

Spec: docs/strategies/07-pairs.md. At each month end, same-sector pairs whose log prices were
cointegrated over the last year (an augmented Dickey-Fuller test on the residual of one regressed
on the other) and whose spread mean-reverted at a useful speed are kept; through the following
month, a spread two standard deviations from its mean is traded back towards it.

The engine version (``Pairs``) buys the cheap leg only: the risk service blocks short sales, so
it can't hold the hedge. ``long_short_returns`` is the real thing, long the cheap leg and short
the hedge-ratio-weighted rich one, as a vectorized research backtest.

The formation is a pure function of the prices up to the latest month end, so the strategy stays
stateless between bars; it is cached per month end so a backtest computes it once a month.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import numpy as np
import pandas as pd

from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_trading.strategy import Context, Strategy

EG_CRITICAL_5PCT = -3.34  # Engle-Granger, two variables with a constant (MacKinnon)
COST_BPS = 5.0
_FORMED: dict[tuple[object, ...], list[Pair]] = {}


@dataclass(frozen=True)
class Pair:
    a: str
    b: str
    beta: float  # log A = alpha + beta * log B + spread
    alpha: float
    mean: float
    sd: float
    t_stat: float
    half_life: float

    def z(self, log_a: float, log_b: float) -> float:
        return (log_a - self.alpha - self.beta * log_b - self.mean) / self.sd


def adf_t(series: np.ndarray, lags: int = 1) -> float:
    """The augmented Dickey-Fuller t-statistic of ``series`` (no constant: for regression
    residuals, which have mean zero): the regression of its change on its previous level and
    ``lags`` previous changes; very negative means it reverts to its mean."""
    x = np.asarray(series, dtype=float)
    dx = np.diff(x)
    n = len(dx) - lags
    if n < 20:
        return math.nan
    y = dx[lags:]
    cols = [x[lags:-1]] + [dx[lags - k : -k] for k in range(1, lags + 1)]
    design = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    dof = n - design.shape[1]
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.inv(design.T @ design)
    se = math.sqrt(cov[0, 0])
    return float(coef[0] / se) if se > 0 else math.nan


def half_life(series: np.ndarray) -> float:
    """Sessions for a deviation to halve, from the AR(1) fit of the series' changes on its
    level; infinite when it doesn't revert."""
    x = np.asarray(series, dtype=float)
    lag, dx = x[:-1] - x[:-1].mean(), np.diff(x)
    gamma = float(lag @ dx / (lag @ lag)) if lag @ lag > 0 else 0.0
    return -math.log(2) / math.log(1 + gamma) if -1 < gamma < 0 else math.inf


def form_pairs(
    log_prices: pd.DataFrame,
    group: Callable[[str], str | None],
    *,
    max_pairs: int = 10,
    critical: float = EG_CRITICAL_5PCT,
    half_life_range: tuple[float, float] = (5.0, 60.0),
) -> list[Pair]:
    """Cointegrated same-group pairs in ``log_prices`` (complete columns only), most significant
    first, each symbol in one pair at most."""
    full = log_prices.dropna(axis=1)
    by_group: dict[str, list[str]] = {}
    for s in full.columns:
        g = group(str(s))
        if g:
            by_group.setdefault(g, []).append(str(s))
    found: list[Pair] = []
    for members in by_group.values():
        for a, b in itertools.combinations(sorted(members), 2):
            ya, xb = full[a].to_numpy(), full[b].to_numpy()
            design = np.column_stack([np.ones_like(xb), xb])
            (alpha, beta), *_ = np.linalg.lstsq(design, ya, rcond=None)
            if beta <= 0:
                continue  # moving opposite ways isn't a pair
            spread = ya - alpha - beta * xb
            t = adf_t(spread)
            hl = half_life(spread)
            sd = float(spread.std(ddof=1))
            ok = t == t and t < critical and half_life_range[0] <= hl <= half_life_range[1]
            if ok and sd > 0:
                found.append(Pair(a, b, float(beta), float(alpha), float(spread.mean()), sd,
                                  float(t), float(hl)))  # fmt: skip
    found.sort(key=lambda p: p.t_stat)
    chosen: list[Pair] = []
    used: set[str] = set()
    for p in found:
        if p.a in used or p.b in used:
            continue
        chosen.append(p)
        used |= {p.a, p.b}
        if len(chosen) >= max_pairs:
            break
    return chosen


def formation_date(index: pd.DatetimeIndex, today_is_month_end: bool) -> pd.Timestamp | None:
    """The latest month end in ``index`` (today's session when it is one)."""
    if today_is_month_end:
        return pd.Timestamp(index[-1])
    months = index.to_period("M")
    ends = index[:-1][months[1:] != months[:-1]]
    return pd.Timestamp(ends[-1]) if len(ends) else None


@dataclass(frozen=True)
class Pairs(Strategy):
    name: ClassVar[str] = "pairs"
    title: ClassVar[str] = "Pairs trading on cointegration (long-only engine version)"
    spec: ClassVar[str] = "docs/strategies/07-pairs.md"

    universe: tuple[str, ...] = LARGE_CAPS
    formation: int = 252
    max_pairs: int = 10  # also the sleeve's slots: 1 / max_pairs per open pair
    entry: float = 2.0
    exit: float = 0.5
    stop: float = 4.0
    max_hold: int = 60  # sessions; also capped at 3 half-lives

    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.universe))

    def pairs_on(self, ctx: Context, closes: pd.DataFrame) -> list[Pair]:
        when = formation_date(pd.DatetimeIndex(closes.index), ctx.is_last_session_of_month())
        if when is None:
            return []
        key = (self.formation, self.max_pairs, tuple(closes.columns), when)
        if key not in _FORMED:
            window = pd.DataFrame(np.log(closes.loc[:when].tail(self.formation)))
            if len(window) < self.formation:
                _FORMED[key] = []
            else:
                _FORMED[key] = form_pairs(window, ctx.sector, max_pairs=self.max_pairs)
        return _FORMED[key]

    def on_bar(self, ctx: Context) -> None:
        closes = ctx.history("close", lookback=self.formation + 45)
        if len(closes) < self.formation:
            return
        pairs = self.pairs_on(ctx, closes)
        last = np.log(closes.iloc[-1])
        weight = 1.0 / self.max_pairs
        held = {s: p for s, p in ctx.positions.items() if p.quantity > 0}
        targets: dict[str, float] = {}
        reasons: dict[str, str] = {}
        for p in pairs:
            if not (math.isfinite(last[p.a]) and math.isfinite(last[p.b])):
                continue
            z = p.z(float(last[p.a]), float(last[p.b]))
            leg = p.a if p.a in held else p.b if p.b in held else None
            if leg is not None:
                days = _sessions_since(closes.index, held[leg].opened)
                limit = min(self.max_hold, math.ceil(3 * p.half_life))
                reverted = z >= -self.exit if leg == p.a else z <= self.exit
                if reverted or abs(z) >= self.stop or days >= limit:
                    why = "reverted" if reverted else "broke" if abs(z) >= self.stop else "time"
                    reasons[leg] = f"{p.a}/{p.b} spread z {z:+.2f}: exit ({why})"
                    continue
                targets[leg] = weight
                reasons[leg] = f"{p.a}/{p.b} spread z {z:+.2f}: hold"
            elif z <= -self.entry:
                targets[p.a] = weight
                reasons[p.a] = f"{p.a} cheap against {p.b} (spread z {z:+.2f}): buy {p.a}"
            elif z >= self.entry:
                targets[p.b] = weight
                reasons[p.b] = f"{p.b} cheap against {p.a} (spread z {z:+.2f}): buy {p.b}"
        for s in held:
            if s not in targets and s not in reasons:
                reasons[s] = f"{s} is no longer in a cointegrated pair: exit"
        if targets or held:
            ctx.order_target_weights(targets, reason=reasons, min_change=0.02)


def _sessions_since(index: pd.Index, opened: date) -> int:
    return int((pd.DatetimeIndex(index) > pd.Timestamp(opened)).sum())


# -- research: the long/short version -------------------------------------------------------------


def long_short_returns(
    closes: pd.DataFrame,
    group: Callable[[str], str | None],
    *,
    formation: int = 252,
    max_pairs: int = 10,
    entry: float = 2.0,
    exit: float = 0.5,
    stop: float = 4.0,
    cost_bps: float = COST_BPS,
) -> tuple[pd.Series, int]:
    """Daily returns of the long/short pairs book and its number of round trips. Each of up to
    ``max_pairs`` slots holds one pair, long the cheap leg and short the rich one weighted by the
    hedge ratio, with gross exposure equal to the slot's capital; unused slots earn nothing.
    Signals at the close, positions from the next session."""
    closes = closes.sort_index()
    rets = closes.pct_change(fill_method=None).fillna(0.0)
    logs = pd.DataFrame(np.log(closes))
    idx = pd.DatetimeIndex(closes.index)
    months = idx.to_period("M")
    month_ends = [i for i in range(len(idx) - 1) if months[i] != months[i + 1]]
    out = pd.Series(0.0, index=idx)
    trips = 0
    for k, i in enumerate(month_ends):
        if i + 1 < formation:
            continue
        pairs = form_pairs(logs.iloc[i + 1 - formation : i + 1], group, max_pairs=max_pairs)
        end = month_ends[k + 1] if k + 1 < len(month_ends) else len(idx) - 1
        for p in pairs:
            gross = 1.0 + p.beta
            state = 0  # +1: long A short B; -1: short A long B
            for t in range(i, end + 1):
                if t > i and state:
                    pnl = state * (rets[p.a].iloc[t] - p.beta * rets[p.b].iloc[t]) / gross
                    out.iloc[t] += pnl / max_pairs
                if t == end:
                    if state:  # closed at the re-formation
                        out.iloc[t] -= cost_bps / 1e4 / max_pairs
                        trips += 1
                    break
                z = p.z(float(logs[p.a].iloc[t]), float(logs[p.b].iloc[t]))
                new = state
                if state == 0 and abs(z) >= entry:
                    new = 1 if z < 0 else -1
                elif state and (abs(z) <= exit or abs(z) >= stop or z * state > 0):
                    new = 0
                if new != state:  # both legs trade: the slot's gross, once
                    out.iloc[t] -= cost_bps / 1e4 / max_pairs
                    trips += int(state != 0 and new == 0)
                    state = new
    return out, trips
