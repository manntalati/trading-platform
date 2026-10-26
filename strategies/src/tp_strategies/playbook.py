"""The options playbook: which setups are live today, the contract each is best expressed with,
and how to get in and out.

For each name with daily bars and a recent option chain:

1. **Setups** (``tp_strategies.setups``): the bar rules that fired in the last few sessions
   (pullback, breakout, trend leader, earnings drift, bounce in a downtrend, breakdown), and two
   judged on the options themselves: expensive options (sell premium) and cheap ones (buy a
   straddle).
2. **The contract**: every liquid candidate the setup could use (long calls or puts across
   expiries and deltas, debit spreads, LEAPS, put credit spreads, iron condors, straddles) is
   valued at the setup's horizon under our forecast (``tp_strategies.option_edge``): the
   setup's drift and the stock's forecast volatility against what the options price in. The
   best risk-adjusted one leads, the best of another structure is the alternative.
3. **Entry**: a limit at the middle of the bid and ask, the most worth paying (the modeled
   value at the horizon), and the last session the signal counts.
4. **Exit**: profit target and stop on the premium, a time stop, a stock-side exit rule, and
   the days to expiry to be out by. Defaults per setup, replaced by the backtest's
   (``tp-recs calibrate``) when it has run.

Lists rotate because setups fire and expire; the score is the modeled edge, so a strong stock
with overpriced options ranks below a decent one with cheap options. At most two names per
sector per list.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from tp_core.calendar import NEW_YORK, sessions
from tp_core.portfolio import Classifier
from tp_core.reference import LakeReference
from tp_core.storage import Lake
from tp_core.volatility import chain_spot, iv_history
from tp_strategies import option_edge as edge
from tp_strategies.option_edge import Leg, Outlook, Surface
from tp_strategies.options_research import (
    DISCLAIMER,
    MAX_SPREAD,
    Rules,
    analyze,
    bars_from_lake,
    contract_view,
    eligible,
    price_chain,
    rv_forecast,
)
from tp_strategies.setups import (
    SETUPS,
    Active,
    Setup,
    active_setups,
    indicators,
    leader_watch,
    momentum_ranks,
    rule_level,
    with_exits,
)

log = logging.getLogger(__name__)

CALIBRATION = "playbook"  # reports/playbook/calibration.json
LISTS = ("swing", "leaps", "bearish", "income", "volatility")
RICH_RATIO = 1.25
CHEAP_RATIO = 0.9
MIN_OPEN_INTEREST = 50
WATCH_SIZE = 8


# -- calibration ----------------------------------------------------------------------------------


def calibration_path(lake: Lake) -> Any:
    return lake.reports_dir(CALIBRATION) / "calibration.json"


def load_calibration(lake: Lake) -> dict[str, Any] | None:
    """The backtest's per-setup drift, exits and track record (``tp-recs calibrate``)."""
    path = calibration_path(lake)
    if not path.exists():
        return None
    out: dict[str, Any] = json.loads(path.read_text())
    return out


def calibrated_setups(calibration: Mapping[str, Any] | None) -> dict[str, Setup]:
    """``SETUPS`` with the calibrated exits where the backtest chose them."""
    found = (calibration or {}).get("setups") or {}
    return {k: with_exits(s, (found.get(k) or {}).get("exits")) for k, s in SETUPS.items()}


def setup_drift(setup: Setup, calibration: Mapping[str, Any] | None) -> tuple[float, str]:
    """The expected log return of the stock over the setup's horizon, and where it comes from."""
    found = ((calibration or {}).get("setups") or {}).get(setup.name) or {}
    drift = found.get("drift")
    if drift is not None and math.isfinite(float(drift)):
        return float(drift), "backtest"
    return edge.default_drift(setup.horizon), "market"


# -- inputs for one name --------------------------------------------------------------------------


@dataclass
class Name:
    """Everything the playbook needs about one name."""

    symbol: str
    ind: pd.DataFrame  # tp_strategies.setups.indicators
    analysis: dict[str, Any]  # tp_strategies.options_research.analyze
    priced: pd.DataFrame | None  # the chain, priced (options_research.price_chain)
    rate: float
    now: datetime
    forecast: float | None  # next month's volatility forecast
    year_vol: float | None  # the last year's realized volatility
    iv_level: float | None  # the usual 30-day implied volatility (a year's median), when known
    earnings: pd.DataFrame | None = None
    next_earnings: date | None = None

    @property
    def spot(self) -> float:
        return float(self.analysis["spot"])

    @property
    def today(self) -> date:
        return self.now.astimezone(NEW_YORK).date()

    def outlook(self, setup: Setup, calibration: Mapping[str, Any] | None, horizon: int) -> Outlook:
        drift, _ = setup_drift(setup, calibration)
        vol = edge.horizon_vol(
            self.forecast, _f(self.analysis["stock"].get("vol_20d")), self.year_vol, horizon
        )
        return Outlook(
            spot=self.spot,
            horizon=horizon,
            drift=drift * horizon / setup.horizon,
            vol=vol or 0.3,
            rate=self.rate,
            iv_level=self.iv_level,
        )


def year_vol(closes: pd.Series) -> float | None:
    logs = pd.Series(np.log(closes.to_numpy(dtype=float)), index=closes.index)
    r = logs.diff().dropna().iloc[-252:]
    return float(r.std() * math.sqrt(252)) if len(r) >= 120 else None


def iv_level(history: pd.Series | None) -> float | None:
    """The median 30-day implied volatility of the last year, with 60+ days of it."""
    if history is None:
        return None
    h = history.dropna().iloc[-252:]
    return float(h.median()) if len(h) >= 60 else None


# -- candidates -----------------------------------------------------------------------------------


def _leg(row: pd.Series, qty: int) -> Leg:
    return Leg(
        contract=str(row["contract"]),
        strike=float(row["strike"]),
        years=float(row["years"]),
        call=row["right"] == "C",
        qty=qty,
        bid=float(row["bid"]),
        ask=float(row["ask"]),
    )


def _liquid(c: pd.DataFrame) -> pd.DataFrame:
    oi = c["open_interest"] if "open_interest" in c else pd.Series(np.nan, index=c.index)
    out: pd.DataFrame = c[(c["spread_pct"] <= MAX_SPREAD) & ~(oi < MIN_OPEN_INTEREST)]
    return out


def _window(
    priced: pd.DataFrame, right: str, dte: tuple[float, float], delta: tuple[float, float]
) -> pd.DataFrame:
    c = priced[
        (priced["right"] == right)
        & priced["dte"].between(*dte)
        & priced["delta"].abs().between(*delta)
    ]
    return _liquid(c)


def _expiries(c: pd.DataFrame, target: float, keep: int = 3) -> pd.DataFrame:
    """The ``keep`` expiries nearest ``target`` days out."""
    if c.empty:
        return c
    days = sorted(set(c["dte"]), key=lambda d: abs(d - target))[:keep]
    out: pd.DataFrame = c[c["dte"].isin(days)]
    return out


def _nearest(c: pd.DataFrame, distance: pd.Series) -> pd.Series:
    """The row of a non-empty ``c`` with the smallest ``distance``."""
    row: pd.Series = c.loc[distance.idxmin()]  # type: ignore[call-overload]
    return row


def _liquidity_penalty(legs: Sequence[Leg]) -> float:
    return sum(max(leg.spread_pct - 0.05, 0.0) for leg in legs) * 2


@dataclass
class Candidate:
    structure: str
    legs: list[tuple[pd.Series, int]]
    model: dict[str, Any]
    score: float
    width: float | None = None


def _candidate(
    structure: str,
    rows: list[tuple[pd.Series, int]],
    outlook: Outlook,
    surface: Surface,
    *,
    capital: float | None = None,
    width: float | None = None,
) -> Candidate:
    legs = [_leg(r, q) for r, q in rows]
    model = edge.evaluate(legs, outlook, surface, capital=capital)
    return Candidate(structure, rows, model, model["sharpe"] - _liquidity_penalty(legs), width)


def long_candidates(
    name: Name, setup: Setup, outlook: Outlook, surface: Surface, *, call: bool
) -> list[Candidate]:
    """Single long options and debit spreads for a directional setup."""
    assert name.priced is not None
    right = "C" if call else "P"
    word = "call" if call else "put"
    if setup.structure == "leaps_call":
        singles = _window(name.priced, right, (270, 10_000), (0.70, 0.85))
        singles = _expiries(singles, 450)
        label = "LEAPS call"
    else:
        lo = setup.horizon * 365 / 252 + setup.exits.min_dte
        singles = _window(name.priced, right, (lo, max(lo + 45, 120)), (0.40, 0.75))
        singles = _expiries(singles, lo + 20)
        label = word
    out = [_candidate(label, [(r, 1)], outlook, surface) for _, r in singles.iterrows()]
    out.sort(key=lambda c: -c.score)
    spreads = []
    for long in out[:6]:
        row = long.legs[0][0]
        same = name.priced[(name.priced["right"] == right) & (name.priced["dte"] == row["dte"])]
        beyond = same["strike"] > row["strike"] if call else same["strike"] < row["strike"]
        wings = _liquid(same[beyond])
        for target in (0.30, 0.20):
            if wings.empty:
                break
            short = _nearest(wings, (wings["delta"].abs() - target).abs())
            debit = float(row["ask"]) - float(short["bid"])
            width = abs(float(short["strike"]) - float(row["strike"]))
            if not 0 < debit < width:
                continue
            spreads.append(
                _candidate(
                    f"{label} spread" if label != word else f"{word} debit spread",
                    [(row, 1), (short, -1)],
                    outlook,
                    surface,
                    width=width,
                )
            )
    return sorted(out + spreads, key=lambda c: -c.score)


def credit_candidates(
    name: Name, setup: Setup, calibration: Mapping[str, Any] | None, surface: Surface
) -> list[Candidate]:
    """Put credit spreads, cash-secured puts and (with no trend) iron condors 25 to 50 days out,
    each valued at the session it should be closed by (21 days to expiry)."""
    assert name.priced is not None
    puts = _window(name.priced, "P", (25, 50), (0.10, 0.40))
    calls = _window(name.priced, "C", (25, 50), (0.10, 0.40))
    out: list[Candidate] = []
    trend = name.analysis["setup"]["direction"]
    for days, g in _expiries(puts, 35, keep=2).groupby("dte"):
        horizon = max(int((int(g["dte"].iloc[0]) - setup.exits.min_dte) * 252 / 365), 3)
        outlook = name.outlook(setup, calibration, horizon)
        shorts = g[g["delta"].abs().between(0.15, 0.35)]
        for _, short in shorts.iterrows():
            wings = g[g["strike"] < short["strike"]].sort_values("strike", ascending=False)
            for _, long in wings.head(3).iterrows():
                credit = float(short["bid"]) - float(long["ask"])
                width = float(short["strike"]) - float(long["strike"])
                if not 0 < credit < width:
                    continue
                out.append(
                    _candidate(
                        "put credit spread",
                        [(short, -1), (long, 1)],
                        outlook,
                        surface,
                        capital=width - credit,
                        width=width,
                    )
                )
            if trend == "bullish":
                cash = float(short["strike"]) - float(short["bid"])
                out.append(
                    _candidate("cash-secured put", [(short, -1)], outlook, surface, capital=cash)
                )
        if trend != "neutral":
            continue
        side = calls[calls["dte"] == days]
        call_shorts = side[side["delta"].abs().between(0.15, 0.25)]
        put_shorts = shorts[shorts["delta"].abs().between(0.15, 0.25)]
        if call_shorts.empty or put_shorts.empty:
            continue
        ps = _nearest(put_shorts, (put_shorts["delta"].abs() - 0.2).abs())
        cs = _nearest(call_shorts, (call_shorts["delta"].abs() - 0.2).abs())
        pw = g[g["strike"] < ps["strike"]].sort_values("strike", ascending=False).head(2)
        cw = side[side["strike"] > cs["strike"]].sort_values("strike").head(2)
        if pw.empty or cw.empty:
            continue
        pl, cl = pw.iloc[-1], cw.iloc[-1]
        credit = float(ps["bid"]) + float(cs["bid"]) - float(pl["ask"]) - float(cl["ask"])
        width = max(
            float(ps["strike"]) - float(pl["strike"]), float(cl["strike"]) - float(cs["strike"])
        )
        if 0 < credit < width:
            out.append(
                _candidate(
                    "iron condor",
                    [(ps, -1), (pl, 1), (cs, -1), (cl, 1)],
                    outlook,
                    surface,
                    capital=width - credit,
                    width=width,
                )
            )
    return sorted(out, key=lambda c: -c.score)


def straddle_candidates(
    name: Name, setup: Setup, outlook: Outlook, surface: Surface
) -> list[Candidate]:
    assert name.priced is not None
    lo = setup.horizon * 365 / 252 + setup.exits.min_dte
    out = []
    calls = _window(name.priced, "C", (lo, 120), (0.35, 0.65))
    puts = _window(name.priced, "P", (lo, 120), (0.35, 0.65))
    for days, g in _expiries(calls, lo + 20).groupby("dte"):
        call = _nearest(g, (g["strike"] - name.spot).abs())
        same = puts[(puts["dte"] == days) & (puts["strike"] == call["strike"])]
        if same.empty:
            continue
        out.append(_candidate("straddle", [(call, 1), (same.iloc[0], 1)], outlook, surface))
    return sorted(out, key=lambda c: -c.score)


# -- describing a position ------------------------------------------------------------------------


def position_view(c: Candidate, name: Name) -> dict[str, Any]:
    """The legs priced as ``contract_view`` does, the position's greeks, and the model."""
    spot, rate = name.spot, name.rate
    legs = [contract_view(r, spot, rate, side="buy" if q > 0 else "sell") for r, q in c.legs]
    for leg, (row, _) in zip(legs, c.legs, strict=True):
        leg["gamma"] = float(row["gamma"]) * 100
    entry = c.model["entry"]
    mid = sum(q * float(r["mid"]) for r, q in c.legs)
    theta = sum(q * float(r["theta"]) for r, q in c.legs)
    gamma = sum(q * float(r["gamma"]) for r, q in c.legs)
    credit = entry < 0
    view: dict[str, Any] = {
        "kind": c.structure,
        "structure": c.structure,
        "legs": legs,
        "expiration": legs[0]["expiration"],
        "dte": legs[0]["dte"],
        "debit": entry if not credit else None,
        "credit": -entry if credit else None,
        "cost": entry * 100,
        "capital": c.model["capital"] * 100,
        "width": c.width,
        "max_loss": c.model["capital"] * 100 if credit or c.width else entry * 100,
        "max_gain": (
            (c.width - entry) * 100
            if c.width and not credit
            else (-entry * 100 if credit else None)
        ),
        "greeks": {
            "delta": sum(q * float(r["delta"]) for r, q in c.legs) * 100,  # shares' worth
            "gamma": gamma * 100,
            "theta_day": theta * 100,
            "vega": sum(q * float(r["vega"]) for r, q in c.legs) * 100,
            "theta_pct": theta / abs(entry) if entry else None,  # of the premium, a day
        },
        "breakeven_move": edge.breakeven_move(theta, gamma, spot),
        "forecast_move": (name.forecast or c.model["vol"]) / math.sqrt(edge.TRADING_DAYS),
        "model": c.model,
        "mid": mid,
    }
    if len(c.legs) == 1:
        row = c.legs[0][0]
        view["fair_value"] = edge.fair_value(_leg(row, 1), spot, c.model["vol"], rate)
    return view


def entry_plan(view: dict[str, Any], valid_until: date | None) -> dict[str, Any]:
    """Work the order from the middle of the bid and ask; pay no more (or take no less) than the
    position is modeled to be worth at the horizon."""
    mid = view["mid"]
    expected = view["model"]["expected_exit"]
    credit = view["credit"] is not None
    plan = {
        "limit": round(abs(mid), 2),
        "side": "credit" if credit else "debit",
        "worst": round(-expected, 2) if credit else round(expected, 2),
        "valid_until": valid_until.isoformat() if valid_until else None,
    }
    plan["worth_it"] = plan["limit"] >= plan["worst"] if credit else plan["limit"] <= plan["worst"]
    return plan


def exit_plan(setup: Setup, view: dict[str, Any], name: Name, fired: date | None) -> dict[str, Any]:
    """The setup's exits as prices and dates for this position."""
    x = setup.exits
    entry = abs(view["model"]["entry"])
    credit = view["credit"] is not None
    plan: dict[str, Any] = {"rules": x.to_dict()}
    if x.take_profit is not None:
        plan["take_profit"] = round(
            entry * (1 - x.take_profit) if credit else entry * (1 + x.take_profit), 2
        )
    if x.stop_loss is not None:
        plan["stop"] = round(entry * (1 - x.stop_loss) if credit else entry * (1 + x.stop_loss), 2)
    sell_by = session_after(name.ind.index, fired or name.today, x.time_stop)
    plan["sell_by"] = sell_by.isoformat() if sell_by else None
    expiry = date.fromisoformat(view["expiration"])
    plan["out_by"] = (expiry - timedelta(days=x.min_dte)).isoformat()
    if plan["sell_by"] and plan["sell_by"] > plan["out_by"]:
        plan["sell_by"] = plan["out_by"]
    plan["stock"] = rule_level(name.ind, x.underlying)
    return plan


# -- ideas ----------------------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def session_after(index: pd.Index, day: date, count: int) -> date | None:
    """The session ``count`` sessions after ``day``: from the bars while they reach, then the
    exchange calendar."""
    days = [d.date() for d in pd.DatetimeIndex(index) if d.date() >= day]
    if len(days) > count:
        return days[count]
    start = days[-1] if days else day
    ahead = [d for d in sessions(start, start + timedelta(days=count * 2 + 10)) if d > start]
    left = count - (len(days) - 1 if days else 0)
    return ahead[left - 1] if 0 < left <= len(ahead) else (start if left <= 0 else None)


def option_setups(name: Name) -> list[Active]:
    """Premium selling and cheap volatility, judged on today's chain."""
    vol = name.analysis["vol"]
    iv, forecast = _f(vol.get("iv30")), name.forecast or _f(vol.get("hv20"))
    if iv is None or not forecast:
        return []
    ratio = iv / forecast
    day = pd.Timestamp(name.today)
    out = []
    rank = _f(vol.get("rank"))
    event = vol.get("event")
    downtrend = name.analysis["setup"]["direction"] == "bearish"
    if ratio >= RICH_RATIO and (rank is None or rank >= 0.5) and not event and not downtrend:
        out.append(Active(SETUPS["rich_premium"], day, 0, min((ratio - 1) / 1.0, 1.0)))
    inverted = vol.get("term_shape") == "backwardation"
    if ratio <= CHEAP_RATIO and not inverted and not event:
        out.append(Active(SETUPS["cheap_vol"], day, 0, min((1 - ratio) / 0.3, 1.0)))
    return out


def ideas_for(
    name: Name, calibration: Mapping[str, Any] | None, setups: Mapping[str, Setup]
) -> list[dict[str, Any]]:
    """An idea per live setup on this name, with the contract that expresses it best."""
    if name.priced is None or name.priced.empty:
        return []
    surface = Surface(name.priced, name.spot)
    if not surface:
        return []
    live = [
        Active(setups[a.setup.name], a.fired, a.age, a.strength)
        for a in active_setups(name.ind, name.earnings, setups)
    ]
    live += [Active(setups[a.setup.name], a.fired, a.age, a.strength) for a in option_setups(name)]
    out = []
    for act in live:
        setup = act.setup
        if setup.structure == "credit":
            found = credit_candidates(name, setup, calibration, surface)
        else:
            outlook = name.outlook(setup, calibration, setup.horizon)
            if setup.structure == "straddle":
                found = straddle_candidates(name, setup, outlook, surface)
            else:
                found = long_candidates(
                    name, setup, outlook, surface, call=setup.structure != "long_put"
                )
        if not found:
            continue
        if setup.structure == "leaps_call":  # a LEAPS stands in for shares: lead with one
            found = sorted(found, key=lambda c: (_family(c) != "single", -c.score))
        best = found[0]
        other = next((c for c in found[1:] if _family(c) != _family(best)), None)
        out.append(_idea(name, act, best, other, calibration))
    return out


def _family(c: Candidate) -> str:
    return "spread" if len(c.legs) > 1 else "single"


def _idea(
    name: Name,
    act: Active,
    best: Candidate,
    other: Candidate | None,
    calibration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    setup = act.setup
    fired = act.fired.date()
    view = position_view(best, name)
    until = session_after(name.ind.index, fired, setup.valid_for)
    view["entry"] = entry_plan(view, until)
    view["exit"] = exit_plan(setup, view, name, fired)
    alternative = None
    if other is not None:
        alternative = position_view(other, name)
        alternative["entry"] = entry_plan(alternative, until)
        alternative["exit"] = exit_plan(setup, alternative, name, fired)
    model = best.model
    drift, source = setup_drift(setup, calibration)
    stats = ((calibration or {}).get("setups") or {}).get(setup.name) or {}
    score = model["sharpe"] + 0.2 * act.strength - 0.02 * act.age
    return {
        "symbol": name.symbol,
        "kind": setup.list,
        "list": setup.list,
        "setup": name.analysis["setup"],
        "play": {
            "name": setup.name,
            "title": setup.title,
            "rule": setup.rule,
            "fired": fired.isoformat(),
            "age": act.age,
            "strength": act.strength,
            "horizon": setup.horizon,
            "drift": drift,
            "drift_source": source,
            "backtest": {k: stats.get(k) for k in ("events", "underlying", "option") if k in stats},
        },
        "score": score,
        "spot": name.spot,
        "stock": name.analysis["stock"],
        "vol": name.analysis["vol"],
        "pick": view,
        "alternative": alternative,
        "why": _why(name, act, view, drift, source, stats),
        "risks": _risks(name, setup, view),
    }


def _pct(v: Any, digits: int = 1, *, signed: bool = False) -> str:
    x = _f(v)
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%" if signed else f"{x * 100:.{digits}f}%"


def _why(
    name: Name,
    act: Active,
    view: Mapping[str, Any],
    drift: float,
    source: str,
    stats: Mapping[str, Any],
) -> list[str]:
    setup, model, ind = act.setup, view["model"], name.ind.iloc[-1]
    when = "today" if act.age == 0 else f"{act.age} session{'s' * (act.age > 1)} ago"
    lines = [f"{setup.title} ({when}): {setup.rule}."]
    if setup.name in ("pullback", "rally_fade"):
        lines.append(
            f"RSI(2) {ind['rsi2']:.0f}; {_pct(ind['close'] / ind['ma200'] - 1, signed=True)} "
            "vs the 200-day average."
        )
    if setup.name == "leader":
        lines.append(
            f"12-1 month momentum {_pct(ind['mom12_1'], 0, signed=True)}, "
            f"{_pct(ind['from_high'], 1, signed=True)} from the 52-week high, "
            f"{_pct(ind['close'] / ind['ma50'] - 1, 1, signed=True)} vs the 50-day."
        )
    vol = name.analysis["vol"]
    iv, rv = _f(vol.get("iv30")), name.forecast
    if iv and rv:
        lines.append(
            f"Options: 30-day implied {_pct(iv, 0)} against a {_pct(rv, 0)} forecast for "
            f"the next month ({iv / rv:.2f}x)."
        )
    basis = (
        "what the stock did after this signal in the backtest"
        if source == "backtest"
        else "an ordinary market drift, with no backtest yet: tp-recs calibrate"
    )
    lines.append(
        f"Modeled over {model['horizon']} sessions with {_pct(model['vol'], 0)} volatility and a "
        f"{_pct(model['drift'], 1, signed=True)} drift ({basis}). Expected "
        f"{_pct(model['expected_return'], 0, signed=True)} on the capital at risk, "
        f"{_pct(model['prob_profit'], 0)} chance of a profit, 1 in 10 worse than "
        f"{_pct(model['q10'], 0, signed=True)}."
    )
    move, daily = view.get("breakeven_move"), view.get("forecast_move")
    if move and daily:
        side = "more" if daily > move else "less"
        lines.append(
            f"Gamma vs theta: it needs about a {_pct(move, 1)} daily move to pay its "
            f"time decay; the stock is forecast to move {_pct(daily, 1)} a day, {side}."
        )
    test = (stats.get("option") or {}) if stats else {}
    under = (stats.get("underlying") or {}) if stats else {}
    if stats.get("events"):
        lines.append(
            f"Backtest: fired {stats['events']:,} times; the stock went the right way over "
            f"{setup.horizon} sessions {_pct(under.get('hit_rate'), 0)} of the time (median "
            f"{_pct(under.get('median'), 1, signed=True)})"
            + (
                f"; the option trade with this exit plan won {_pct(test.get('win_rate'), 0)}, "
                f"median {_pct(test.get('median'), 0, signed=True)}."
                if test.get("trades")
                else "."
            )
        )
    return lines


def _risks(name: Name, setup: Setup, view: Mapping[str, Any]) -> list[str]:
    out = []
    model, entry = view["model"], view["entry"]
    if model["expected_return"] <= 0:
        out.append(
            "At these prices the model expects a loss: the options cost more than the forecast "
            "move pays for. Wait for a better price, use the alternative, or pass."
        )
    if not entry["worth_it"]:
        out.append(
            f"Even the mid is past what it's modeled to be worth ({entry['worst']:.2f}); only "
            "worth it filled better than that."
        )
    vol = name.analysis["vol"]
    event = vol.get("event")
    expiry = date.fromisoformat(view["expiration"])
    if name.next_earnings is not None and name.today <= name.next_earnings <= expiry:
        out.append(
            f"Earnings expected about {name.next_earnings.isoformat()} (an estimate), before "
            "expiry: implied volatility usually falls right after."
        )
    elif event:
        out.append(
            f"The option market prices an event before {event['by']}: implied volatility "
            "usually drops right after, even when the stock moves the right way."
        )
    if "extended" in name.analysis["setup"]["tags"] and setup.direction > 0:
        out.append("Extended above its 20-day average: entries after a run fail more often.")
    if view["credit"] is not None:
        out.append(
            "Selling options caps the gain at the premium; a sharp move against it costs far "
            "more. Size it for the maximum loss."
        )
    return out


# -- the lists ------------------------------------------------------------------------------------


def build_lists(
    names: Sequence[Name],
    *,
    held: Mapping[str, str],
    groups: Mapping[str, str],
    calibration: Mapping[str, Any] | None,
    per_list: int = 6,
    per_group: int = 2,
) -> dict[str, Any]:
    """The playbook's ideas by list, best modeled edge first (at most ``per_group`` per sector),
    and the leaders to watch for an entry."""
    setups = calibrated_setups(calibration)
    found: dict[str, list[dict[str, Any]]] = {k: [] for k in LISTS}
    watch: list[dict[str, Any]] = []
    for name in names:
        for idea in ideas_for(name, calibration, setups):
            if held.get(name.symbol):
                idea["held"] = held[name.symbol]
                idea["risks"].append(f"Already held: {held[name.symbol]}.")
            found[idea["list"]].append(idea)
        if name.priced is not None and not name.priced.empty and leader_watch(name.ind):
            ind = name.ind.iloc[-1]
            watch.append(
                {
                    "symbol": name.symbol,
                    "spot": name.spot,
                    "entry_level": float(ind["ma50"]) * 1.01,
                    "ma50": float(ind["ma50"]),
                    "momentum": float(ind["mom12_1"]),
                    "from_high": float(ind["from_high"]),
                    "above_ma50": float(ind["close"] / ind["ma50"] - 1),
                    "setup": "leader",
                    "text": (
                        f"Trend leader {(ind['close'] / ind['ma50'] - 1) * 100:+.1f}% above its "
                        f"50-day average: a LEAPS entry near {float(ind['ma50']) * 1.01:,.2f}."
                    ),
                }
            )
    out: dict[str, Any] = {}
    for k, ideas in found.items():
        unique: dict[str, dict[str, Any]] = {}  # one idea per name per list: its best setup
        for idea in sorted(ideas, key=lambda i: -i["score"]):
            unique.setdefault(idea["symbol"], idea)
        out[k] = _capped(list(unique.values()), groups, per_group, per_list)
    leaps = {i["symbol"] for i in out["leaps"]}
    watch = [w for w in watch if w["symbol"] not in leaps]
    out["watch"] = sorted(watch, key=lambda w: -w["momentum"])[:WATCH_SIZE]
    return out


def _capped(
    ranked: list[dict[str, Any]], groups: Mapping[str, str], per_group: int, size: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    taken: dict[str, int] = {}
    for idea in ranked:
        group = groups.get(idea["symbol"], idea["symbol"])
        if taken.get(group, 0) >= per_group:
            continue
        taken[group] = taken.get(group, 0) + 1
        idea["group"] = group
        out.append(idea)
        if len(out) == size:
            break
    return out


# -- the whole scan -------------------------------------------------------------------------------


def earnings_by_symbol(lake: Lake) -> dict[str, pd.DataFrame]:
    """Each company's results with their surprise, as they reached the data (none without
    fundamentals: ``tp-data fundamentals``)."""
    try:
        table = LakeReference(lake).earnings_table()
    except LookupError:
        return {}
    return {str(s): g for s, g in table.groupby("symbol")}


def prepare(
    symbol: str,
    bars: pd.DataFrame,
    *,
    chain: pd.DataFrame | None,
    benchmark: pd.Series | None,
    iv_hist: pd.Series | None,
    now: datetime,
    rate: float,
    momentum_rank: pd.Series | None = None,
    earnings: pd.DataFrame | None = None,
    rules: Rules | None = None,
) -> Name:
    """Analyze one name (``options_research.analyze``) and work out what the setups read."""
    closes = bars["close"]
    spot = chain_spot(chain) if chain is not None and not chain.empty else None
    spot = spot or float(closes.iloc[-1])
    have_chain = chain is not None and not chain.empty
    priced = price_chain(chain, spot, now, rate) if chain is not None and have_chain else None
    forecast = rv_forecast(bars)
    analysis = analyze(
        symbol,
        closes,
        chain=chain,
        benchmark=benchmark,
        iv_hist=iv_hist,
        now=now,
        rate=rate,
        rules=rules,
        forecast=forecast,
        priced=priced,
    )
    return Name(
        symbol=symbol,
        ind=indicators(bars, benchmark, momentum_rank),
        analysis=analysis,
        priced=priced,
        rate=rate,
        now=now,
        forecast=forecast,
        year_vol=year_vol(closes),
        iv_level=iv_level(iv_hist),
        earnings=earnings,
    )


def scan(
    lake: Lake,
    symbols: list[str],
    *,
    classifier: Classifier,
    chains: Mapping[str, pd.DataFrame],
    now: datetime,
    rate: float,
    held: Mapping[str, str] | None = None,
    rules: Rules | None = None,
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze ``symbols`` (those with daily bars) and list the playbook's ideas."""
    rules = rules or Rules()
    today = now.astimezone(NEW_YORK).date()
    wanted = [s for s in dict.fromkeys(symbols) if eligible(s, classifier)]
    bars = bars_from_lake(lake, [*wanted, "SPY"], today)
    closes = pd.DataFrame({s: b["close"] for s, b in bars.items() if s in wanted})
    ranks = momentum_ranks(closes) if not closes.empty else pd.DataFrame()
    bench = bars["SPY"]["close"] if "SPY" in bars else None
    history = iv_history(lake)
    by_symbol = {
        str(u): g.set_index("snapshot_date")["iv30"] for u, g in history.groupby("underlying")
    }
    earnings = earnings_by_symbol(lake)
    names: list[Name] = []
    errors: dict[str, str] = {}
    for symbol in wanted:
        frame = bars.get(symbol)
        if frame is None or len(frame) < 60:
            continue
        try:
            names.append(
                prepare(
                    symbol,
                    frame,
                    chain=chains.get(symbol),
                    benchmark=bench,
                    iv_hist=by_symbol.get(symbol),
                    now=now,
                    rate=rate,
                    momentum_rank=ranks.get(symbol),
                    earnings=earnings.get(symbol),
                    rules=rules,
                )
            )
        except Exception as exc:  # one odd chain must not take the whole scan down
            log.exception("options research: %s failed", symbol)
            errors[symbol] = f"{type(exc).__name__}: {exc}"
    groups = {s: classifier.sectors.get(s) or classifier.funds.get(s) or s for s in wanted}
    lists = build_lists(
        names,
        held=held or {},
        groups=groups,
        calibration=calibration,
        per_list=rules.ideas_per_list,
        per_group=rules.per_group,
    )
    with_chain = [n for n in names if n.analysis["picks"]]
    return {
        "as_of": now.isoformat(),
        "chains_as_of": max((c["snapshot_at"].max() for c in chains.values()), default=None),
        "scanned": len(names),
        "with_chains": len(with_chain),
        "without_chains": sorted(n.symbol for n in names if not n.analysis["picks"]),
        "errors": errors,
        "iv_history_days": int(history["snapshot_date"].nunique()) if len(history) else 0,
        "calibration": _calibration_brief(calibration),
        "live_setups": _live_counts(lists),
        **lists,
        "disclaimer": DISCLAIMER,
    }


def _calibration_brief(calibration: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not calibration:
        return None
    return {k: calibration.get(k) for k in ("generated_at", "start", "end", "names", "model")}


def _live_counts(lists: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in LISTS:
        for idea in lists.get(k) or []:
            out[idea["play"]["name"]] = out.get(idea["play"]["name"], 0) + 1
    return out


def for_symbol(name: Name, calibration: Mapping[str, Any] | None) -> dict[str, Any]:
    """The playbook on one name for Look up: the setups live on it with their contracts, how
    close the others are, and whether it's a leader to wait on."""
    setups = calibrated_setups(calibration)
    ideas = ideas_for(name, calibration, setups)
    ind = name.ind.iloc[-1]
    near: list[str] = []
    if pd.notna(ind["rsi2"]):
        near.append(
            f"RSI(2) {ind['rsi2']:.0f} (a pullback entry fires under 10 in an uptrend, a "
            "fade over 90 in a downtrend)"
        )
    if pd.notna(ind["high50"]):
        near.append(
            f"50-day closing high {ind['high50']:,.2f} ({ind['close'] / ind['high50'] - 1:+.1%} "
            "away); the Bollinger squeeze is at the "
            f"{(ind['squeeze'] if pd.notna(ind['squeeze']) else 0.5):.0%} mark of 6 months "
            "(a breakout counts within 5 sessions of under 20%)"
        )
    if pd.notna(ind["ma50"]):
        near.append(f"50-day average {ind['ma50']:,.2f} ({ind['close'] / ind['ma50'] - 1:+.1%})")
    return {
        "ideas": ideas,
        "watch": leader_watch(name.ind),
        "levels": near,
        "calibrated": calibration is not None,
    }
