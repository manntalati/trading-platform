"""Volatility forecasts: how much a stock will move over the next ``horizon`` sessions.

What an option seller is paid for is implied volatility minus the volatility that then happens;
the second number is a forecast, not the trailing realized volatility. Five forecasters, each a
point-in-time estimate of the annualized volatility of daily close-to-close returns over the
next ``horizon`` sessions:

- **naive**: the last ``horizon`` sessions' realized volatility (what the options research used).
- **EWMA**: RiskMetrics' exponentially weighted variance (lambda 0.94).
- **GARCH(1,1)** (Bollerslev 1986): variance reverts to a long-run level at a fitted speed,
  pushed up by each day's squared return. Fitted by maximum likelihood.
- **HAR-RV** (Corsi 2009): tomorrow's variance from today's, the last week's and the last
  month's, a regression on logs. Daily variance from the open, high, low and close (Garman-Klass
  plus the overnight gap), which is less noisy than one squared return.
- **GBM**: gradient boosting on the HAR terms plus recent returns and, when the lake has it, the
  VIX.

``evaluate_vol`` scores them walk-forward: refitted every ``refit`` sessions on data whose
target had been realized, forecasting every ``step`` sessions after ``min_train`` sessions.
Losses are QLIKE (Patton 2011, robust to the noise in realized variance; lower is better), the
error in volatility points, the bias, and the Mincer-Zarnowitz R^2 (how much of the variation in
realized variance the forecast explains). For SPY the VIX is scored as a forecast too: it usually
runs above what happens, and that gap is the volatility risk premium.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter
from sklearn.ensemble import HistGradientBoostingRegressor

TRADING_DAYS = 252
MODELS = ("naive", "ewma", "garch", "har", "gbm")
EWMA_LAMBDA = 0.94


@dataclass(frozen=True)
class VolConfig:
    horizon: int = 21  # sessions ahead
    min_train: int = 756  # sessions before the first forecast (3 years)
    refit: int = 63  # refit every quarter
    step: int = 5  # forecast weekly


# -- daily variance ---------------------------------------------------------------------------


def log_returns(close: pd.Series) -> pd.Series:
    out: pd.Series = np.log(close).diff()
    return out


def range_variance(bars: pd.DataFrame) -> pd.Series:
    """Daily variance from OHLC: the overnight gap squared plus Garman-Klass (1980) for the
    session. ``bars`` has ``open``, ``high``, ``low``, ``close``."""
    o, h, lo, c = (np.log(bars[k]) for k in ("open", "high", "low", "close"))
    overnight = (o - c.shift(1)) ** 2
    session = 0.5 * (h - lo) ** 2 - (2 * math.log(2) - 1) * (c - o) ** 2
    out: pd.Series = (overnight.fillna(0.0) + session.clip(lower=0.0)).rename("rv")
    return out


def realized_vol(returns: pd.Series, horizon: int) -> pd.Series:
    """Annualized volatility of the next ``horizon`` daily returns, aligned at the day before
    them: what a forecast made at that close is scored against."""
    squared = (returns**2).rolling(horizon, min_periods=horizon).mean().shift(-horizon)
    out: pd.Series = np.sqrt(squared * TRADING_DAYS)
    return out


# -- GARCH(1,1) -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Garch:
    omega: float  # in squared percent per day
    alpha: float
    beta: float
    mean: float  # daily mean return, percent

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    @property
    def long_run_vol(self) -> float:
        """Annualized volatility the variance reverts to."""
        level = self.omega / max(1e-9, 1 - self.persistence)
        return math.sqrt(level * TRADING_DAYS) / 100

    def variance_path(self, returns: np.ndarray) -> np.ndarray:
        """Conditional variance of each day given the days before it, then of the next day (so
        ``len(returns) + 1`` values), in squared percent."""
        r = returns * 100 - self.mean
        start = float(np.var(r)) if len(r) > 1 else self.omega
        u = self.omega + self.alpha * r**2
        path = lfilter([1.0], [1.0, -self.beta], u, zi=[self.beta * start])[0]
        return np.concatenate([[start], path])

    def forecast(self, next_variance: float, horizon: int) -> float:
        """Annualized volatility over the next ``horizon`` days from the next day's variance."""
        p = self.persistence
        level = self.omega / max(1e-9, 1 - p)
        steps = np.arange(horizon)
        mean_var = float(np.mean(level + p**steps * (next_variance - level)))
        return math.sqrt(max(mean_var, 0.0) * TRADING_DAYS) / 100


def fit_garch(returns: np.ndarray) -> Garch:
    """Maximum likelihood with normal innovations; returns in decimal (log) units."""
    r = returns[np.isfinite(returns)] * 100
    mean = float(np.mean(r))
    x = r - mean
    var = float(np.var(x))

    def nll(params: np.ndarray) -> float:
        omega, alpha, beta = params
        if alpha + beta >= 0.999:
            return 1e10
        u = omega + alpha * x[:-1] ** 2
        s2 = lfilter([1.0], [1.0, -beta], u, zi=[beta * var])[0]
        s2 = np.concatenate([[var], s2])
        if not np.all(s2 > 0):
            return 1e10
        return float(0.5 * np.sum(np.log(s2) + x**2 / s2))

    best = None
    for alpha, beta in ((0.05, 0.9), (0.1, 0.85), (0.03, 0.95)):
        start = np.array([var * (1 - alpha - beta), alpha, beta])
        fit = minimize(
            nll, start, method="L-BFGS-B", bounds=[(1e-8, None), (0.0, 0.999), (0.0, 0.999)]
        )
        if best is None or fit.fun < best.fun:
            best = fit
    assert best is not None
    omega, alpha, beta = (float(v) for v in best.x)
    return Garch(omega=omega, alpha=alpha, beta=beta, mean=mean)


# -- HAR and GBM ------------------------------------------------------------------------------


def har_terms(rv: pd.Series) -> pd.DataFrame:
    """Logs of the daily, weekly (5-day) and monthly (22-day) average daily variance."""
    floor = rv[rv > 0].min() if (rv > 0).any() else 1e-10
    v = rv.clip(lower=floor)
    return pd.DataFrame(
        {
            "rv_d": np.log(v),
            "rv_w": np.log(v.rolling(5, min_periods=5).mean()),
            "rv_m": np.log(v.rolling(22, min_periods=22).mean()),
        }
    )


@dataclass(frozen=True)
class Linear:
    coef: np.ndarray
    resid_var: float

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(np.column_stack([np.ones(len(x)), x]) @ self.coef)


def fit_linear(x: np.ndarray, y: np.ndarray) -> Linear:
    design = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    return Linear(coef=coef, resid_var=float(np.var(resid)))


def vol_features(
    bars: pd.DataFrame, returns: pd.Series, vix: pd.Series | None = None
) -> pd.DataFrame:
    """The HAR terms, plus recent returns and the VIX (log) for the gradient boosting."""
    out = har_terms(range_variance(bars))
    out["ret_5"] = returns.rolling(5).sum()
    out["ret_21"] = returns.rolling(21).sum()
    if vix is not None:
        v = vix.reindex(bars.index).ffill(limit=5)
        out["vix"] = np.log(v)
    return out


def _target(returns: pd.Series, horizon: int) -> pd.Series:
    """Log of the next ``horizon`` days' mean squared return (what the regressions predict)."""
    mean_sq = (returns**2).rolling(horizon, min_periods=horizon).mean().shift(-horizon)
    out: pd.Series = np.log(mean_sq.clip(lower=1e-12))
    return out


def _annual(log_mean_sq: np.ndarray) -> np.ndarray:
    return np.asarray(np.sqrt(np.exp(log_mean_sq) * TRADING_DAYS))


# -- walk-forward evaluation ------------------------------------------------------------------


def _forecasts_until(
    bars: pd.DataFrame,
    returns: pd.Series,
    features: pd.DataFrame,
    target: pd.Series,
    fit_end: int,
    rows: np.ndarray,
    horizon: int,
) -> dict[str, np.ndarray]:
    """Forecasts at positions ``rows`` from models fitted on what was known at ``fit_end``:
    targets realized by then (their window ended at or before ``fit_end``)."""
    known = slice(0, max(0, fit_end - horizon))
    r = returns.to_numpy()
    out: dict[str, np.ndarray] = {}
    sq = pd.Series(r**2, index=returns.index)
    out["naive"] = np.sqrt(
        sq.rolling(horizon, min_periods=horizon).mean().to_numpy()[rows] * TRADING_DAYS
    )
    ewma = sq.ewm(alpha=1 - EWMA_LAMBDA, adjust=False).mean().to_numpy()
    out["ewma"] = np.sqrt(ewma[rows] * TRADING_DAYS)

    garch = fit_garch(r[1:fit_end])
    path = garch.variance_path(np.nan_to_num(r[1:]))  # path[k] = variance of day k+1 given before
    out["garch"] = np.array([garch.forecast(float(path[i]), horizon) for i in rows])

    har_cols = ["rv_d", "rv_w", "rv_m"]
    train = (
        pd.concat([features, target.rename("y")], axis=1)
        .iloc[known]
        .dropna(subset=[*har_cols, "y"])
    )
    x_now = features.iloc[rows]
    if len(train) > 50:
        har = fit_linear(train[har_cols].to_numpy(), train["y"].to_numpy())
        pred = har.predict(x_now[har_cols].fillna(x_now[har_cols].mean()).to_numpy())
        out["har"] = _annual(pred + har.resid_var / 2)
        gbm = HistGradientBoostingRegressor(
            max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=40, random_state=0
        )
        cols = [c for c in features.columns if train[c].notna().any()]
        gbm.fit(train[cols], train["y"])
        resid = train["y"] - gbm.predict(train[cols])
        out["gbm"] = _annual(gbm.predict(x_now[cols]) + float(np.var(resid)) / 2)
    else:
        out["har"] = out["gbm"] = np.full(len(rows), np.nan)
    return out


def _scores(forecast: pd.Series, realized: pd.Series) -> dict[str, float | None]:
    ok = forecast.notna() & realized.notna() & (forecast > 0) & (realized > 0)
    if ok.sum() < 10:
        return {"n": int(ok.sum())}
    f, a = forecast[ok], realized[ok]
    fv, av = f**2, a**2
    ratio = av / fv
    # Mincer-Zarnowitz R^2: undefined for a forecast that never moves
    mz = float(np.corrcoef(fv, av)[0, 1] ** 2) if fv.std() > 0 and av.std() > 0 else None
    return {
        "n": int(ok.sum()),
        "qlike": float(np.mean(ratio - np.log(ratio) - 1)),
        "rmse": float(np.sqrt(np.mean((f - a) ** 2))),
        "bias": float(np.mean(f - a)),
        "mz_r2": mz,
    }


def evaluate_vol(
    bars: pd.DataFrame,
    cfg: VolConfig,
    vix: pd.Series | None = None,
    implied: pd.Series | None = None,
) -> dict[str, Any]:
    """Walk-forward forecasts of the next ``horizon`` sessions' volatility and their scores.
    ``bars``: one symbol's daily adjusted OHLC (DatetimeIndex). ``vix``: the VIX (percent), a
    feature for the gradient boosting. ``implied``: the symbol's own implied volatility
    (decimal; the VIX / 100 for SPY), scored as one more forecast where it exists."""
    bars = bars.sort_index().dropna(subset=["close"])
    returns = log_returns(bars["close"])
    features = vol_features(bars, returns, vix)
    target = _target(returns, cfg.horizon)
    realized = realized_vol(returns, cfg.horizon)
    n = len(bars)
    rows_all = np.arange(cfg.min_train, n - cfg.horizon, cfg.step)
    if len(rows_all) == 0:
        raise ValueError(f"needs more than {cfg.min_train + cfg.horizon} sessions of bars")
    blocks: dict[str, list[np.ndarray]] = {m: [] for m in MODELS}
    for start in range(cfg.min_train, n - cfg.horizon, cfg.refit):
        rows = rows_all[(rows_all >= start) & (rows_all < start + cfg.refit)]
        if not len(rows):
            continue
        out = _forecasts_until(bars, returns, features, target, start, rows, cfg.horizon)
        for m in MODELS:
            blocks[m].append(out[m])
    index = bars.index[rows_all]
    forecasts = pd.DataFrame({m: np.concatenate(blocks[m]) for m in MODELS}, index=index)
    if implied is not None:
        aligned = implied.reindex(bars.index).ffill(limit=5)
        forecasts["implied"] = aligned.iloc[rows_all].to_numpy(dtype=float)
    forecasts["realized"] = realized.iloc[rows_all].to_numpy()
    scores = {
        m: _scores(forecasts[m], forecasts["realized"])
        for m in forecasts.columns
        if m != "realized"
    }
    return {
        "config": asdict(cfg),
        "first": str(index.min().date()),
        "last": str(index.max().date()),
        "scores": scores,
        "_forecasts": forecasts,
    }


def forecast_now(
    bars: pd.DataFrame, horizon: int = 21, vix: pd.Series | None = None
) -> dict[str, Any]:
    """Every model's forecast of the next ``horizon`` sessions' annualized volatility, fitted on
    all of ``bars``, and the GARCH parameters."""
    bars = bars.sort_index().dropna(subset=["close"])
    returns = log_returns(bars["close"])
    features = vol_features(bars, returns, vix)
    target = _target(returns, horizon)
    n = len(bars)
    last = np.array([n - 1])
    out = _forecasts_until(bars, returns, features, target, n, last, horizon)
    garch = fit_garch(returns.to_numpy()[1:])
    return {
        "as_of": str(bars.index[-1].date()),
        "horizon": horizon,
        "forecasts": {m: _clean(float(out[m][0])) for m in MODELS},
        "garch": {
            **{k: round(v, 6) for k, v in asdict(garch).items()},
            "persistence": round(garch.persistence, 4),
            "long_run_vol": round(garch.long_run_vol, 4),
        },
    }


def har_forecast(bars: pd.DataFrame, horizon: int = 21, min_sessions: int = 300) -> float | None:
    """The HAR forecast alone (fast enough to run per symbol in the options research): the next
    ``horizon`` sessions' annualized volatility, or None with too little history."""
    bars = bars.sort_index().dropna(subset=["close"])
    if len(bars) < min_sessions:
        return None
    returns = log_returns(bars["close"])
    terms = har_terms(range_variance(bars))
    data = pd.concat([terms, _target(returns, horizon).rename("y")], axis=1)
    train = data.dropna()
    now = terms.iloc[-1]
    if len(train) < 100 or now.isna().any():
        return None
    model = fit_linear(train[["rv_d", "rv_w", "rv_m"]].to_numpy(), train["y"].to_numpy())
    pred = model.predict(now.to_numpy()[None, :]) + model.resid_var / 2
    return _clean(float(_annual(pred)[0]))


def _clean(value: float) -> float | None:
    return value if math.isfinite(value) else None


LABELS: Mapping[str, str] = {
    "naive": "Trailing realized",
    "ewma": "EWMA (RiskMetrics)",
    "garch": "GARCH(1,1)",
    "har": "HAR-RV",
    "gbm": "Gradient boosting",
    "implied": "Implied volatility",
}


def report_text(report: dict[str, Any], now: dict[str, Any] | None = None) -> str:
    cfg = report["config"]
    lines = [
        f"Forecasts of the next {cfg['horizon']} sessions' volatility, walk-forward "
        f"{report['first']} to {report['last']} (refit every {cfg['refit']} sessions).",
        "",
        f"{'':22}{'QLIKE':>8}{'error':>8}{'bias':>8}{'MZ R2':>8}{'forecasts':>11}",
    ]
    for key, s in report["scores"].items():
        label = LABELS.get(key, key)
        if "qlike" not in s:
            lines.append(f"{label:22}  too few")
            continue
        lines.append(
            f"{label:22}{s['qlike']:>8.3f}{s['rmse']:>8.1%}{s['bias']:>+8.1%}"
            f"{'—' if s['mz_r2'] is None else format(s['mz_r2'], '.2f'):>8}"
            f"{s['n']:>11}"
        )
    lines.append(
        "QLIKE: lower is better. error: RMSE in volatility points. bias: forecast minus realized."
    )
    if now:
        f = now["forecasts"]
        parts = [f"{LABELS[m]} {v:.1%}" for m, v in f.items() if v is not None]
        lines += ["", f"Now ({now['as_of']}): " + ", ".join(parts)]
        g = now["garch"]
        lines.append(
            f"GARCH persistence {g['persistence']:.3f} (half-life "
            f"{_half_life(g['persistence'])}), long-run volatility {g['long_run_vol']:.1%}"
        )
    return "\n".join(lines)


def _half_life(persistence: float) -> str:
    if not 0 < persistence < 1:
        return "—"
    return f"{math.log(0.5) / math.log(persistence):.0f} sessions"


def best_model(
    scores: Mapping[str, Mapping[str, Any]], models: Callable[[str], bool] | None = None
) -> str | None:
    """The model with the lowest QLIKE among ``scores`` (optionally filtered)."""
    ranked = [
        (s["qlike"], m) for m, s in scores.items() if "qlike" in s and (models is None or models(m))
    ]
    return min(ranked)[1] if ranked else None
