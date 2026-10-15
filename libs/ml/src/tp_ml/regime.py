"""Market regimes learned from the data: a Gaussian hidden Markov model (Hamilton 1989; Ang and
Timmermann 2012 survey regime models in finance).

The market is assumed to switch between a few hidden states (calm, stressed, in between), each
with its own distribution of daily returns and volatility, and to stay in a state for a while
(the transition matrix). Fitted by expectation-maximisation (Baum-Welch) on two daily
observations of SPY: the log return, and the log of its 5-day range-based volatility.

Two things keep it honest:

- **Filtered, not smoothed.** The probability of each state on a day uses only the days up to
  it (the forward pass). The smoothed probabilities a library would usually report use the
  future too, and every regime "signal" built on them looks better than it can be.
- **Out of sample.** ``evaluate_regimes`` refits once a year on the years before and checks
  what the filtered state said about the *next* month: its volatility and its return.

States are numbered calm to stressed (by their volatility). ``tp_core.regime`` has the
rule-based labels (trend, VIX level, breadth, rates); this is the learned counterpart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from tp_ml.vol import TRADING_DAYS, range_variance

NAMES = {2: ("calm", "stressed"), 3: ("calm", "normal", "stressed")}


@dataclass
class GaussianHMM:
    """A hidden Markov model with Gaussian observations (diagonal covariance)."""

    n_states: int = 2
    n_iter: int = 200
    n_init: int = 5
    tol: float = 1e-5
    seed: int = 0
    # fitted
    start_: np.ndarray | None = None
    trans_: np.ndarray | None = None
    means_: np.ndarray | None = None
    vars_: np.ndarray | None = None
    loglik_: float = -math.inf

    def _emission(self, x: np.ndarray) -> np.ndarray:
        assert self.means_ is not None
        assert self.vars_ is not None
        diff = x[:, None, :] - self.means_[None, :, :]
        log = -0.5 * (np.log(2 * math.pi * self.vars_)[None] + diff**2 / self.vars_[None])
        out: np.ndarray = log.sum(axis=2)
        return out

    def _forward(self, logb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Scaled forward pass: filtered state probabilities and the log scale factors."""
        assert self.start_ is not None
        assert self.trans_ is not None
        n, k = logb.shape
        alpha = np.empty((n, k))
        scale = np.empty(n)
        shift = logb.max(axis=1, keepdims=True)
        b = np.exp(logb - shift)
        a = self.start_ * b[0]
        scale[0] = a.sum()
        alpha[0] = a / scale[0]
        for t in range(1, n):
            a = (alpha[t - 1] @ self.trans_) * b[t]
            scale[t] = a.sum()
            alpha[t] = a / scale[t]
        return alpha, np.log(scale) + shift[:, 0]

    def _backward(self, logb: np.ndarray, scale_log: np.ndarray) -> np.ndarray:
        assert self.trans_ is not None
        n, k = logb.shape
        shift = logb.max(axis=1, keepdims=True)
        b = np.exp(logb - shift)
        scale = np.exp(scale_log - shift[:, 0])
        beta = np.empty((n, k))
        beta[-1] = 1.0
        for t in range(n - 2, -1, -1):
            beta[t] = (self.trans_ @ (b[t + 1] * beta[t + 1])) / scale[t + 1]
        return beta

    def _init(self, x: np.ndarray, rng: np.random.Generator) -> None:
        k, d = self.n_states, x.shape[1]
        # spread the starting means along the volatility observation's quantiles
        order = np.argsort(x[:, -1])
        chunks = np.array_split(order, k)
        self.means_ = np.array([x[c].mean(axis=0) for c in chunks])
        self.means_ += rng.normal(scale=0.1, size=(k, d))
        self.vars_ = np.tile(x.var(axis=0), (k, 1)) * rng.uniform(0.5, 1.5, size=(k, d))
        self.start_ = np.full(k, 1.0 / k)
        stay = 0.95
        self.trans_ = np.full((k, k), (1 - stay) / max(k - 1, 1))
        np.fill_diagonal(self.trans_, stay)

    def fit(self, x: np.ndarray) -> GaussianHMM:
        x = np.asarray(x, dtype=float)
        rng = np.random.default_rng(self.seed)
        best: tuple[float, tuple[np.ndarray, ...]] | None = None
        for _ in range(self.n_init):
            self._init(x, rng)
            previous = -math.inf
            for _ in range(self.n_iter):
                logb = self._emission(x)
                alpha, scale_log = self._forward(logb)
                beta = self._backward(logb, scale_log)
                loglik = float(scale_log.sum())
                gamma = alpha * beta
                gamma /= gamma.sum(axis=1, keepdims=True)
                b = np.exp(logb - logb.max(axis=1, keepdims=True))
                scale = np.exp(scale_log - logb.max(axis=1))
                assert self.trans_ is not None
                xi = (
                    alpha[:-1, :, None]
                    * self.trans_[None]
                    * (b[1:] * beta[1:])[:, None, :]
                    / scale[1:, None, None]
                ).sum(axis=0)
                self.start_ = gamma[0] / gamma[0].sum()
                self.trans_ = xi / xi.sum(axis=1, keepdims=True)
                weight = gamma.sum(axis=0)[:, None]
                self.means_ = (gamma.T @ x) / weight
                self.vars_ = (gamma.T @ x**2) / weight - self.means_**2
                self.vars_ = np.maximum(self.vars_, 1e-4)
                if loglik - previous < self.tol:
                    break
                previous = loglik
            if best is None or previous > best[0]:
                params = (self.start_, self.trans_, self.means_, self.vars_)
                best = (previous, tuple(p.copy() for p in params))  # type: ignore[union-attr]
        assert best is not None
        self.loglik_ = best[0]
        self.start_, self.trans_, self.means_, self.vars_ = best[1]
        self._order_by(column=-1)
        return self

    def _order_by(self, column: int) -> None:
        """Renumber states by the mean of one observation (the volatility one): 0 is calm."""
        assert self.means_ is not None
        assert self.vars_ is not None
        assert self.trans_ is not None
        assert self.start_ is not None
        order = np.argsort(self.means_[:, column])
        self.means_, self.vars_ = self.means_[order], self.vars_[order]
        self.start_ = self.start_[order]
        self.trans_ = self.trans_[np.ix_(order, order)]

    def filter(self, x: np.ndarray) -> np.ndarray:
        """P(state on day t | days up to t): uses nothing after each day."""
        alpha, _ = self._forward(self._emission(np.asarray(x, dtype=float)))
        return alpha


def observations(bars: pd.DataFrame) -> pd.DataFrame:
    """The model's daily inputs: log return, and log 5-day range-based volatility."""
    close = bars["close"]
    ret = np.log(close).diff()
    vol5 = np.sqrt(range_variance(bars).rolling(5, min_periods=5).mean() * TRADING_DAYS)
    out = pd.DataFrame({"ret": ret, "log_vol": np.log(vol5.clip(lower=1e-4))}, index=bars.index)
    return out.dropna()


@dataclass(frozen=True)
class RegimeModel:
    hmm: GaussianHMM
    center: np.ndarray
    spread: np.ndarray
    names: tuple[str, ...]

    def probabilities(self, obs: pd.DataFrame) -> pd.DataFrame:
        x = (obs.to_numpy() - self.center) / self.spread
        return pd.DataFrame(self.hmm.filter(x), index=obs.index, columns=list(self.names))


def fit_regimes(obs: pd.DataFrame, n_states: int = 2, seed: int = 0) -> RegimeModel:
    center, spread = obs.mean().to_numpy(), obs.std().to_numpy()
    hmm = GaussianHMM(n_states=n_states, seed=seed).fit((obs.to_numpy() - center) / spread)
    return RegimeModel(hmm=hmm, center=center, spread=spread, names=NAMES[n_states])


def describe(model: RegimeModel, obs: pd.DataFrame) -> dict[str, Any]:
    """Each state's returns and volatility, how often and how long it lasts (from the filtered
    states), the transition matrix, and today's probabilities."""
    probs = model.probabilities(obs)
    state = probs.to_numpy().argmax(axis=1)
    trans = model.hmm.trans_
    assert trans is not None
    rows = []
    for k, name in enumerate(model.names):
        days = obs["ret"][state == k]
        rows.append(
            {
                "state": name,
                "share": float((state == k).mean()),
                "ann_return": float(days.mean() * TRADING_DAYS) if len(days) else None,
                "ann_vol": float(days.std() * math.sqrt(TRADING_DAYS)) if len(days) > 1 else None,
                "expected_days": float(1 / max(1e-9, 1 - trans[k, k])),
            }
        )
    return {
        "states": rows,
        "transitions": [[float(v) for v in row] for row in trans],
        "now": {name: float(p) for name, p in probs.iloc[-1].items()},
        "as_of": str(obs.index[-1].date()),
    }


def evaluate_regimes(
    bars: pd.DataFrame, n_states: int = 2, min_years: int = 4, horizon: int = 21
) -> dict[str, Any]:
    """Out of sample: refit each January on the years before, filter through that year, and
    score what the most likely state on each day said about the next ``horizon`` sessions'
    volatility and return. Also: SPY held only while the model says calm (a day late)."""
    obs = observations(bars)
    when = pd.DatetimeIndex(obs.index)
    ret = np.log(bars["close"]).diff().reindex(obs.index)
    future_vol = (ret**2).rolling(horizon).mean().shift(-horizon) ** 0.5 * math.sqrt(TRADING_DAYS)
    future_ret = ret.rolling(horizon).sum().shift(-horizon)
    years = sorted(set(when.year))
    pieces = []
    for year in years[min_years:]:
        past = obs[when.year < year]
        now = obs[when.year == year]
        if len(past) < 250 * min_years or now.empty:
            continue
        model = fit_regimes(past, n_states)
        # filter through the past into this year, so the year starts from the right state
        probs = model.probabilities(pd.concat([past.iloc[-250:], now])).loc[now.index]
        pieces.append(probs)
    if not pieces:
        return {"days": 0}
    probs = pd.concat(pieces)
    names = list(probs.columns)
    state = pd.Series(np.array(names)[probs.to_numpy().argmax(axis=1)], index=probs.index)
    by_state = []
    for name in names:
        on = state == name
        by_state.append(
            {
                "state": name,
                "share": float(on.mean()),
                "next_vol": _mean(future_vol[on.reindex(future_vol.index, fill_value=False)]),
                "next_return": _mean(future_ret[on.reindex(future_ret.index, fill_value=False)]),
            }
        )
    calm = probs[names[0]].shift(1).reindex(ret.index)  # decided at yesterday's close
    timed = (ret * (calm > 0.5)).loc[probs.index].dropna()
    held = ret.loc[probs.index].dropna()
    return {
        "days": len(probs),
        "first": str(probs.index.min().date()),
        "last": str(probs.index.max().date()),
        "by_state": by_state,
        "timed": _perf(timed),
        "buy_and_hold": _perf(held),
        "time_in_market": float((calm.loc[probs.index] > 0.5).mean()),
        "_probabilities": probs,
    }


def _mean(values: pd.Series) -> float | None:
    v = values.dropna()
    return float(v.mean()) if len(v) else None


def _perf(daily_log: pd.Series) -> dict[str, float | None]:
    if len(daily_log) < 20:
        return {"cagr": None, "vol": None, "sharpe": None, "max_drawdown": None}
    simple = np.expm1(daily_log)
    wealth = (1 + simple).cumprod()
    years = len(daily_log) / TRADING_DAYS
    vol = float(simple.std() * math.sqrt(TRADING_DAYS))
    return {
        "cagr": float(wealth.iloc[-1] ** (1 / years) - 1),
        "vol": vol,
        "sharpe": float(simple.mean() * TRADING_DAYS / vol) if vol > 0 else None,
        "max_drawdown": float((wealth / wealth.cummax() - 1).min()),
    }


def report_text(desc: dict[str, Any], ev: dict[str, Any]) -> str:
    lines = [f"Regimes of SPY, fitted on all history (as of {desc['as_of']}):", ""]
    lines.append(f"{'':10}{'share':>7}{'return/yr':>11}{'vol/yr':>9}{'lasts':>10}")
    for s in desc["states"]:
        ret = "—" if s["ann_return"] is None else f"{s['ann_return']:+.1%}"
        vol = "—" if s["ann_vol"] is None else f"{s['ann_vol']:.1%}"
        lines.append(
            f"{s['state']:10}{s['share']:>7.0%}{ret:>11}{vol:>9}{s['expected_days']:>7.0f} days"
        )
    now = ", ".join(f"{k} {v:.0%}" for k, v in desc["now"].items())
    lines += ["", f"Today: {now}"]
    if ev.get("days"):
        lines += [
            "",
            f"Out of sample ({ev['first']} to {ev['last']}, refit each January on the years "
            "before): what the state said about the next month",
        ]
        for s in ev["by_state"]:
            vol = "—" if s["next_vol"] is None else f"{s['next_vol']:.1%}"
            ret = "—" if s["next_return"] is None else f"{s['next_return']:+.2%}"
            lines.append(
                f"  {s['state']:10} {s['share']:>4.0%} of days: next-month volatility {vol}, "
                f"return {ret}"
            )
        t, b = ev["timed"], ev["buy_and_hold"]
        if t["sharpe"] is not None and b["sharpe"] is not None:
            lines.append(
                f"  SPY only while calm (a day late, {ev['time_in_market']:.0%} of the time): "
                f"Sharpe {t['sharpe']:.2f}, max drawdown {t['max_drawdown']:.0%}; buy and hold: "
                f"Sharpe {b['sharpe']:.2f}, max drawdown {b['max_drawdown']:.0%}"
            )
    return "\n".join(lines)
