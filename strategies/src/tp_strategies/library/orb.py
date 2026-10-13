"""Strategy 11: opening-range breakout on intraday bars (Crabel; Zarattini, Barbon and Aziz).

Spec: docs/strategies/11-orb.md. A vectorized research backtest on stored intraday bars
(``tp_core.intraday``): each day, the high and low of the first ``or_minutes`` are the opening
range; a stop order at the range high buys the first break above it (and, with
``direction="both"``, one at the range low sells the first break below), the stop is the other
side of the range (or its middle), and anything still open is sold at the last bar's close. If
the bar that triggers the entry also reaches the stop, the trade counts as stopped out.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OrbConfig:
    or_minutes: int = 30
    minutes: int = 5  # bar length
    stop: str = "range"  # "range": the other side; "mid": the middle of the range
    direction: str = "long"  # "long" or "both" (shorts: research only)
    cost_bps: float = 2.0  # per side
    slip_bps: float = 1.0  # on the stop entry


def orb_trades(bars: pd.DataFrame, cfg: OrbConfig | None = None) -> pd.DataFrame:
    """One row per trade: ``symbol, session, side, entry, stop, exit, exit_reason, ret, r``
    (``ret`` after costs; ``r``: the result in multiples of the risk to the stop)."""
    cfg = cfg or OrbConfig()
    if cfg.stop not in ("range", "mid") or cfg.direction not in ("long", "both"):
        raise ValueError("stop is range or mid; direction is long or both")
    n_or = cfg.or_minutes // cfg.minutes
    rows = []
    for (symbol, session), g in bars.sort_values("timestamp").groupby(["symbol", "session"]):
        if len(g) < n_or + 2:
            continue
        o, h, lo, c = (g[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        top, bottom = h[:n_or].max(), lo[:n_or].min()
        mid = (top + bottom) / 2
        after = slice(n_or, len(g))
        up = np.flatnonzero(h[after] > top)
        down = np.flatnonzero(lo[after] < bottom) if cfg.direction == "both" else np.array([])
        first_up = up[0] if len(up) else None
        first_down = down[0] if len(down) else None
        if first_up is None and first_down is None:
            continue
        if first_up is not None and first_down is not None and first_up == first_down:
            continue  # one bar broke both ways: no way to tell which came first
        long = first_down is None or (first_up is not None and first_up < first_down)
        trigger = first_up if long else first_down
        assert trigger is not None
        i = n_or + int(trigger)
        side = 1 if long else -1
        level = top if long else bottom
        fill = max(o[i], level) if long else min(o[i], level)
        entry = fill * (1 + side * cfg.slip_bps / 1e4)
        stop = (bottom if long else top) if cfg.stop == "range" else mid
        hit_now = lo[i] <= stop if long else h[i] >= stop
        exit_price, reason = c[-1], "close"
        if hit_now:
            exit_price, reason = stop, "stop"
        else:
            later = (
                np.flatnonzero(lo[i + 1 :] <= stop) if long else np.flatnonzero(h[i + 1 :] >= stop)
            )
            if len(later):
                j = i + 1 + later[0]
                exit_price = min(o[j], stop) if long else max(o[j], stop)  # a gap fills worse
                reason = "stop"
        ret = side * (exit_price / entry - 1) - 2 * cfg.cost_bps / 1e4
        risk = abs(entry - stop)
        rows.append(
            {
                "symbol": symbol,
                "session": session,
                "side": "long" if long else "short",
                "entry": entry,
                "stop": stop,
                "exit": exit_price,
                "exit_reason": reason,
                "ret": ret,
                "r": side * (exit_price - entry) / risk if risk > 0 else np.nan,
            }
        )
    cols = ["symbol", "session", "side", "entry", "stop", "exit", "exit_reason", "ret", "r"]
    return pd.DataFrame(rows, columns=cols)


def daily_returns(trades: pd.DataFrame, bars: pd.DataFrame) -> pd.Series:
    """The book's return per session with equal capital per symbol: the average over the
    symbols, a symbol without a trade that day contributing nothing."""
    symbols = sorted(set(bars["symbol"]))
    days = sorted(set(bars["session"]))
    grid = pd.DataFrame(0.0, index=pd.DatetimeIndex(days), columns=symbols)
    for _, t in trades.iterrows():
        grid.loc[pd.Timestamp(t["session"]), t["symbol"]] += t["ret"]
    return grid.mean(axis=1).rename("orb")


def summary(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"trades": 0}
    wins = trades["ret"] > 0
    return {
        "trades": len(trades),
        "hit_rate": float(wins.mean()),
        "avg_trade": float(trades["ret"].mean()),
        "avg_win": float(trades.loc[wins, "ret"].mean()) if wins.any() else 0.0,
        "avg_loss": float(trades.loc[~wins, "ret"].mean()) if (~wins).any() else 0.0,
        "avg_r": float(trades["r"].mean()),
        "stopped": float((trades["exit_reason"] == "stop").mean()),
    }
