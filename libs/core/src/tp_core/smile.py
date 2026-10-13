"""The volatility smile: SVI fits per expiration, arbitrage checks, skew and the term structure.

**SVI** (Gatheral's "stochastic volatility inspired" raw form) describes one expiration's total
implied variance ``w = iv^2 * years`` as a function of log-moneyness ``k = ln(strike / forward)``:

    w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))

five numbers: the level ``a``, the wings' steepness ``b``, their asymmetry ``rho`` (negative for
stocks: puts dearer than calls), the shift ``m`` and the curvature at the bottom ``sigma``. The
fit is least squares on the out-of-the-money quotes (puts below the forward, calls above): for a
fixed ``m`` and ``sigma`` the rest is linear, so a grid over those two, refined twice, finds the
best fit without an optimizer and without local minima to get stuck in.

**Arbitrage.** A smile that isn't arbitrage-free can't be real prices:

- *Butterfly*: the risk-neutral density implied by one expiration's smile must be non-negative.
  Gatheral's ``g(k) >= 0`` checks it on the fitted curve; quote-level checks look for call prices
  rising with the strike or not convex in it, at prices you could trade (bid and ask).
- *Calendar*: total variance can't fall with time at the same moneyness; a later expiration with
  less total variance than an earlier one is an arbitrage (or stale quotes).

**Skew.** The 25-delta risk reversal is the implied volatility of the 25-delta call minus the
25-delta put (negative: downside protection costs more, the norm for stocks); the 25-delta
butterfly is their average minus the at-the-money volatility (how curved the smile is). Deltas
are forward deltas on the fitted smile.

**Term structure.** The 90-day over the 30-day at-the-money volatility: above 1 (contango) is the
calm norm, below 1 (backwardation) means the market expects the near term to be rough.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from tp_core import greeks as bs
from tp_core.positions import years_until

MIN_POINTS = 5
MAX_SPREAD = 0.5  # of the mid; wider quotes say little about the price
MIN_MID = 0.05
CONTANGO, BACKWARDATION = 1.03, 0.97


@dataclass(frozen=True)
class SVI:
    a: float
    b: float
    rho: float
    m: float
    sigma: float

    def w(self, k: Any) -> Any:
        """Total implied variance at log-moneyness ``k`` (scalar or array)."""
        x = np.asarray(k, dtype=float) - self.m
        return self.a + self.b * (self.rho * x + np.sqrt(x * x + self.sigma**2))

    def iv(self, k: Any, years: float) -> Any:
        return np.sqrt(np.maximum(self.w(k), 0.0) / years)

    def g(self, k: Any) -> Any:
        """Gatheral's density function: negative somewhere means a butterfly arbitrage."""
        x = np.asarray(k, dtype=float) - self.m
        root = np.sqrt(x * x + self.sigma**2)
        w = self.w(k)
        w1 = self.b * (self.rho + x / root)
        w2 = self.b * self.sigma**2 / root**3
        kk = np.asarray(k, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            return (1 - kk * w1 / (2 * w)) ** 2 - w1**2 / 4 * (1 / w + 0.25) + w2 / 2


def fit_svi(k: Any, w: Any, weights: Any | None = None) -> tuple[SVI, float] | None:
    """Least-squares SVI through ``(k, w)``; returns the fit and its root-mean-square error in
    total variance, or None with fewer than ``MIN_POINTS`` points or no admissible fit."""
    k = np.asarray(k, dtype=float)
    w = np.asarray(w, dtype=float)
    ok = np.isfinite(k) & np.isfinite(w) & (w > 0)
    k, w = k[ok], w[ok]
    if len(k) < MIN_POINTS:
        return None
    wt = np.ones_like(k) if weights is None else np.asarray(weights, dtype=float)[ok]
    span = max(float(k.max() - k.min()), 0.05)
    ms = np.linspace(float(k.min()) - 0.25 * span, float(k.max()) + 0.25 * span, 31)
    sigmas = np.geomspace(0.005, 2.0, 31)
    best: tuple[float, SVI] | None = None
    for _ in range(3):  # coarse grid, then two finer ones around the best
        found = _grid(k, w, wt, ms, sigmas)
        if found is not None and (best is None or found[0] < best[0]):
            best = found
        if best is None:
            return None
        s = best[1]
        dm = (ms[1] - ms[0]) * 2
        ms = np.linspace(s.m - dm, s.m + dm, 21)
        ratio = sigmas[1] / sigmas[0]
        sigmas = np.geomspace(max(s.sigma / ratio**2, 1e-4), s.sigma * ratio**2, 21)
    assert best is not None
    rmse = math.sqrt(best[0] / float(wt.sum()))
    return best[1], rmse


def _grid(
    k: np.ndarray, w: np.ndarray, wt: np.ndarray, ms: np.ndarray, sigmas: np.ndarray
) -> tuple[float, SVI] | None:
    """For every (m, sigma) pair, the linear least squares in (a, b*rho, b); the best admissible
    one (b >= 0, |rho| < 1, variance never negative)."""
    mm, ss = np.meshgrid(ms, sigmas, indexing="ij")
    mm, ss = mm.ravel(), ss.ravel()
    x = k[None, :] - mm[:, None]
    root = np.sqrt(x * x + ss[:, None] ** 2)
    design = np.stack([np.ones_like(x), x, root], axis=2)  # (combos, points, 3)
    dw = design * wt[None, :, None]
    lhs = np.einsum("cpi,cpj->cij", dw, design) + np.eye(3) * 1e-12
    rhs = np.einsum("cpi,p->ci", dw, w)
    try:
        coef = np.linalg.solve(lhs, rhs[..., None])[..., 0]
    except np.linalg.LinAlgError:
        return None
    a, c, b = coef[:, 0], coef[:, 1], coef[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = np.where(b > 0, c / b, np.nan)
    floor = a + b * ss * np.sqrt(np.clip(1 - rho**2, 0, None))
    valid = (b > 0) & (np.abs(rho) < 0.999) & (floor >= 0)
    if not valid.any():
        return None
    resid = np.einsum("cpi,ci->cp", design, coef) - w[None, :]
    sse = np.where(valid, (resid**2 * wt[None, :]).sum(axis=1), np.inf)
    i = int(np.argmin(sse))
    return float(sse[i]), SVI(float(a[i]), float(b[i]), float(rho[i]), float(mm[i]), float(ss[i]))


# -- from a chain ---------------------------------------------------------------------------------


def smile_points(
    chain: pd.DataFrame, spot: float, expiration: date, now: datetime, rate: float
) -> pd.DataFrame:
    """One expiration's out-of-the-money quotes with implied volatility solved from the mid:
    columns ``strike, right, mid, iv, k, w``."""
    years = years_until(expiration, now)
    cols = ["strike", "right", "mid", "iv", "k", "w"]
    if years <= 0 or not spot:
        return pd.DataFrame(columns=cols)
    g = chain[
        (chain["expiration"] == expiration) & (chain["bid"] > 0) & (chain["ask"] >= chain["bid"])
    ]
    forward = spot * math.exp(rate * years)
    rows = []
    for _, r in g.iterrows():
        strike, right = float(r["strike"]), str(r["right"])
        if (right == "C") != (strike >= forward):
            continue  # in the money: the out-of-the-money side has the information
        mid = (float(r["bid"]) + float(r["ask"])) / 2
        if mid < MIN_MID or (float(r["ask"]) - float(r["bid"])) / mid > MAX_SPREAD:
            continue
        iv = bs.implied_vol(mid, spot, strike, years, call=right == "C", rate=rate)
        if iv is None:
            continue
        k = math.log(strike / forward)
        rows.append({"strike": strike, "right": right, "mid": mid, "iv": iv, "k": k,
                     "w": iv * iv * years})  # fmt: skip
    return pd.DataFrame(rows, columns=cols).sort_values("strike").reset_index(drop=True)


@dataclass(frozen=True)
class ExpiryFit:
    expiration: date
    dte: int
    years: float
    points: int
    svi: SVI
    rmse_iv: float  # vol points (0.01 = 1 point) between the fit and the quotes
    atm_iv: float
    skew_25d: float | None  # 25-delta call IV minus 25-delta put IV
    butterfly_25d: float | None
    butterfly_arbitrage: bool
    k_min: float  # the fitted range of log-moneyness
    k_max: float
    calendar_arbitrage: bool = False  # set by fit_chain against the expiration before

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expiration"] = self.expiration.isoformat()
        return d


def fit_expiry(
    chain: pd.DataFrame, spot: float, expiration: date, now: datetime, rate: float
) -> tuple[ExpiryFit, pd.DataFrame] | None:
    pts = smile_points(chain, spot, expiration, now, rate)
    if len(pts) < MIN_POINTS:
        return None
    years = years_until(expiration, now)
    fitted = fit_svi(pts["k"], pts["w"])
    if fitted is None:
        return None
    svi, _ = fitted
    model_iv = svi.iv(pts["k"].to_numpy(), years)
    rmse_iv = float(np.sqrt(np.mean((model_iv - pts["iv"].to_numpy()) ** 2)))
    lo, hi = float(pts["k"].min()), float(pts["k"].max())
    grid = np.linspace(lo, hi, 101)
    c25, p25 = delta_strike(svi, years, 0.25), delta_strike(svi, years, -0.25)
    atm = float(svi.iv(0.0, years))
    skew = fly = None
    if c25 is not None and p25 is not None:
        iv_c, iv_p = float(svi.iv(c25, years)), float(svi.iv(p25, years))
        skew, fly = iv_c - iv_p, (iv_c + iv_p) / 2 - atm
    exp_day = expiration if isinstance(expiration, date) else pd.Timestamp(expiration).date()
    fit = ExpiryFit(
        expiration=exp_day,
        dte=round(years * bs.YEAR_DAYS),
        years=years,
        points=len(pts),
        svi=svi,
        rmse_iv=rmse_iv,
        atm_iv=atm,
        skew_25d=skew,
        butterfly_25d=fly,
        butterfly_arbitrage=bool(np.nanmin(svi.g(grid)) < -1e-6),
        k_min=lo,
        k_max=hi,
    )
    return fit, pts


def delta_strike(svi: SVI, years: float, delta: float) -> float | None:
    """The log-moneyness where the forward delta on the fitted smile equals ``delta``: 0.25 for
    the 25-delta call, -0.25 for the 25-delta put."""
    call = delta > 0
    target = delta if call else 1 + delta  # put delta = N(d1) - 1

    def nd1(k: float) -> float:
        w = float(svi.w(k))
        if w <= 0:
            return math.nan
        return bs.norm_cdf((-k + w / 2) / math.sqrt(w))

    lo, hi = (0.0, 3.0) if call else (-3.0, 0.0)
    f_lo, f_hi = nd1(lo) - target, nd1(hi) - target
    if not (math.isfinite(f_lo) and math.isfinite(f_hi)) or f_lo * f_hi > 0:
        return None
    for _ in range(60):  # N(d1) falls as k rises
        mid = 0.5 * (lo + hi)
        f_mid = nd1(mid) - target
        if not math.isfinite(f_mid):
            return None
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def fit_chain(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    *,
    min_dte: int = 5,
    max_dte: int = 400,
) -> list[ExpiryFit]:
    """SVI for every expiration from ``min_dte`` to ``max_dte`` days that has enough quotes, with
    each one's calendar arbitrage against the one before."""
    fits: list[ExpiryFit] = []
    for expiration in sorted(chain["expiration"].unique()):
        exp = expiration if isinstance(expiration, date) else pd.Timestamp(expiration).date()
        days = years_until(exp, now) * bs.YEAR_DAYS
        if not min_dte <= days <= max_dte:
            continue
        out = fit_expiry(chain, spot, exp, now, rate)
        if out is not None:
            fits.append(out[0])
    return calendar_check(fits)


def calendar_check(fits: list[ExpiryFit], grid: Any | None = None) -> list[ExpiryFit]:
    """Flag each expiration whose total variance falls below the previous one's anywhere on the
    moneyness grid (default -0.3 to 0.3)."""
    k = np.linspace(-0.3, 0.3, 61) if grid is None else np.asarray(grid, dtype=float)
    out: list[ExpiryFit] = []
    for i, f in enumerate(fits):
        if i == 0:
            out.append(f)
            continue
        earlier = fits[i - 1].svi.w(k)
        later = f.svi.w(k)
        bad = bool(np.any(later < earlier - 1e-5))
        out.append(ExpiryFit(**{**f.__dict__, "calendar_arbitrage": bad}))
    return out


def quote_arbitrage(chain: pd.DataFrame, expiration: date) -> dict[str, int]:
    """Tradable static arbitrage in one expiration's call quotes: a lower strike you can buy
    (ask) for less than a higher strike sells for (bid) (``vertical``), and a butterfly
    (buy the wings at the ask, sell two bodies at the bid) that pays you to put on
    (``butterfly``, equally spaced strikes only). Usually stale or bad quotes, not free money."""
    g = chain[
        (chain["expiration"] == expiration)
        & (chain["right"] == "C")
        & (chain["bid"] >= 0)
        & (chain["ask"] > 0)
    ].sort_values("strike")
    strikes, bid, ask = (g[c].to_numpy(dtype=float) for c in ("strike", "bid", "ask"))
    vertical = int(np.sum(ask[:-1] < bid[1:] - 1e-9)) if len(g) > 1 else 0
    butterfly = 0
    for i in range(1, len(g) - 1):
        even = abs((strikes[i] - strikes[i - 1]) - (strikes[i + 1] - strikes[i])) < 1e-9
        if even and ask[i - 1] - 2 * bid[i] + ask[i + 1] < -1e-9:
            butterfly += 1
    return {"vertical": vertical, "butterfly": butterfly}


def term_shape(iv30: float | None, iv90: float | None) -> dict[str, Any]:
    """The 90-day over the 30-day implied volatility and what it means."""
    if not iv30 or not iv90:
        return {"ratio": None, "shape": None}
    ratio = iv90 / iv30
    shape = "contango" if ratio > CONTANGO else "backwardation" if ratio < BACKWARDATION else "flat"
    return {"ratio": ratio, "shape": shape}


def nearest_fit(fits: list[ExpiryFit], days: float = 30.0) -> ExpiryFit | None:
    return min(fits, key=lambda f: abs(f.dte - days)) if fits else None


def smile_curve(fit: ExpiryFit, spot: float, rate: float, n: int = 41) -> list[dict[str, float]]:
    """The fitted smile as (strike, iv) points across the quotes it was fitted to, for drawing."""
    forward = spot * math.exp(rate * fit.years)
    k = np.linspace(fit.k_min, fit.k_max, n)
    ivs = fit.svi.iv(k, fit.years)
    return [
        {"strike": float(forward * math.exp(x)), "iv": float(v)}
        for x, v in zip(k, ivs, strict=True)
        if math.isfinite(float(v))
    ]
