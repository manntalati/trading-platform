"""Builders for synthetic bars and lakes (used by tests): random walks, never real prices."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tp_core.bars import build_clean_bars
from tp_core.calendar import sessions
from tp_core.schemas import RAW_BARS_SCHEMA, RAW_STOCK_BARS_1D, conform
from tp_core.storage import Lake, write_raw
from tp_core.testing import raw_bars

NOW = datetime(2024, 1, 2, 23, 0, tzinfo=UTC)


def random_bars(
    symbols: list[str], n: int = 600, seed: int = 0, start: str = "2020-01-02"
) -> dict[str, pd.DataFrame]:
    """Wide OHLCV random walks (daily vol ~1.5%) on business days."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(start, periods=n)
    rets = rng.normal(0.0003, 0.015, size=(n, len(symbols)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=index, columns=symbols)
    gap = pd.DataFrame(rng.normal(0, 0.004, size=close.shape), index=index, columns=symbols)
    open_ = close.shift(1).fillna(close) * (1 + gap)
    spread = pd.DataFrame(rng.uniform(0.002, 0.02, size=close.shape), index=index, columns=symbols)
    high = open_.where(open_ >= close, close) * (1 + spread / 2)
    low = open_.where(open_ <= close, close) * (1 - spread / 2)
    volume = pd.DataFrame(
        rng.lognormal(14, 0.3, size=close.shape), index=index, columns=symbols
    ).round()
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def write_lake(
    root: Path,
    symbols: list[str],
    n: int = 420,
    seed: int = 0,
    drifts: list[float] | None = None,
) -> Lake:
    """A lake with clean daily bars: ``n`` sessions of random walks for ``symbols`` ending in
    2023 (``drifts``: each symbol's daily drift, to plant a cross-sectional signal)."""
    lake = Lake(root)
    rng = np.random.default_rng(seed)
    days = sessions(date(2010, 1, 4), date(2023, 12, 29))[-n:]
    frames = []
    for i, symbol in enumerate(symbols):
        drift = drifts[i] if drifts else 0.0003
        closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.015, size=n)))
        prices = [round(float(c), 4) for c in closes]
        # raw_bars covers up to two calendar years from its start: build it in chunks
        for k in range(0, n, 250):
            frames.append(raw_bars(symbol, prices[k : k + 250], start=days[k]))
    write_raw(
        lake,
        RAW_STOCK_BARS_1D,
        conform(pd.concat(frames), RAW_BARS_SCHEMA),
        run_id="r1",
        partition={"ingest_date": "x"},
        schema=RAW_BARS_SCHEMA,
    )
    build_clean_bars(lake, now=NOW)
    return lake


def ohlc_from_returns(
    returns: np.ndarray, sigma: np.ndarray | float, *, seed: int = 0, start: str = "2010-01-04"
) -> pd.DataFrame:
    """Daily OHLC bars (business days) for simulated log ``returns`` whose daily volatility was
    ``sigma``: a small overnight gap, and a high and low that widen with the volatility."""
    rng = np.random.default_rng(seed)
    n = len(returns)
    sig = np.broadcast_to(np.asarray(sigma, dtype=float), (n,))
    gap = rng.normal(0, 0.2, n) * sig
    close = 100 * np.exp(np.cumsum(returns))
    prev = np.concatenate([[100.0], close[:-1]])
    open_ = prev * np.exp(gap)
    reach = np.abs(rng.normal(0, 0.6, (2, n))) * sig
    high = np.maximum(open_, close) * np.exp(reach[0])
    low = np.minimum(open_, close) * np.exp(-reach[1])
    index = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=index)


def simulate_garch(
    n: int, omega: float, alpha: float, beta: float, *, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Daily log returns of a GARCH(1,1) process (parameters in squared percent) and the
    daily volatility (decimal) each day had."""
    rng = np.random.default_rng(seed)
    var = omega / (1 - alpha - beta)
    out, sig = np.empty(n), np.empty(n)
    for t in range(n):
        sig[t] = np.sqrt(var) / 100
        out[t] = sig[t] * rng.standard_normal()
        var = omega + alpha * (out[t] * 100) ** 2 + beta * var
    return out, sig


def simulate_regimes(
    n: int, vols: tuple[float, ...] = (0.006, 0.02), stay: float = 0.98, *, seed: int = 0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Daily log returns from a Markov-switching volatility model: the returns, each day's
    true state and its daily volatility."""
    rng = np.random.default_rng(seed)
    k = len(vols)
    state = np.empty(n, dtype=int)
    state[0] = 0
    for t in range(1, n):
        stays = rng.random() < stay
        state[t] = state[t - 1] if stays else rng.choice([s for s in range(k) if s != state[t - 1]])
    sig = np.asarray(vols)[state]
    return sig * rng.standard_normal(n) + 0.0003, state, sig
