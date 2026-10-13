"""Options research: which stocks set up for a swing or a LEAPS position, which contract fits,
and what to do with the options you already hold.

Fixed, documented rules over the daily bars and the daily option chains in the lake, with every
number that drove an idea attached, so you can check the reasoning. Research prompts, not
advice: nothing here places an order.

Stock setups (from daily bars):

- **Uptrend**: price above its 50-day average, which is above the 200-day.
- **Pullback**: an uptrend trading back near its 20-day average (a calmer entry).
- **Extended**: more than 8% above the 20-day average or up 15%+ in a month (wait, or size down).
- **Downtrend**: price below the 50-day, which is below the 200-day (for puts).
- **Relative strength**: 3-month return against the S&P 500 (SPY).

Contracts (from the latest chain snapshot, priced at the ask to buy and the bid to sell):

- **Swing** (weeks to a couple of months): 30-100 days out, nearest 60; delta about 0.55 (at or
  slightly in the money, so the stock doesn't need a big move just to break even).
- **Debit spread** instead when options are expensive (IV rank 60%+ or implied 1.3x the stock's
  realized volatility): buy the swing contract, sell the 0.30-delta one in the same expiry.
  Cheaper, less hurt by a volatility drop, profit capped.
- **LEAPS** (stock replacement, 9+ months out): delta about 0.80; among those, the expiry and
  strike with the least time value paid per year.
- **Covered call** on 100+ shares: 21-50 days out, delta about 0.25.

The IV rank needs ``MIN_RANK_DAYS`` of daily snapshots; until then the IV-to-realized ratio
stands in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import pandas as pd

from tp_core import greeks as bs
from tp_core.bars import load_bars, missing_symbols
from tp_core.calendar import NEW_YORK
from tp_core.pnl import option_label
from tp_core.portfolio import Classifier
from tp_core.positions import OptionPosition, scenarios, stock_stats, years_until
from tp_core.storage import Lake
from tp_core.volatility import atm_term, chain_spot, event_expiry, iv_at, iv_history, iv_rank

Right = Literal["C", "P"]
DISCLAIMER = (
    "Rules-based research from price history and option chains, with the numbers behind each "
    "idea. Not investment advice: options can lose their whole value, quickly. You decide."
)
# Asset classes the research leaves out: no directional options ideas on cash, bonds or the
# leveraged daily-reset funds.
EXCLUDED_CLASSES = frozenset(
    {
        "Cash",
        "US Bonds",
        "US Treasuries",
        "Corporate Credit",
        "Leveraged Equity",
        "Leveraged Treasuries",
    }
)
CHAIN_MAX_AGE_DAYS = 5
RICH_IV_RANK = 0.6
RICH_IV_RATIO = 1.3
MAX_SPREAD = 0.15  # bid/ask spread as a share of the mid


@dataclass(frozen=True)
class Rules:
    swing_dte: tuple[int, int, int] = (30, 100, 60)  # min, max, target days to expiry
    swing_delta: tuple[float, float, float] = (0.45, 0.70, 0.55)
    short_delta: float = 0.30  # the sold leg of a debit spread
    leaps_min_dte: int = 270
    leaps_delta: tuple[float, float, float] = (0.65, 0.90, 0.80)
    covered_dte: tuple[int, int, int] = (21, 50, 35)
    covered_delta: tuple[float, float, float] = (0.15, 0.35, 0.25)
    roll_swing_dte: tuple[int, int, int] = (40, 100, 60)
    leaps_roll_below_dte: int = 180  # LEAPS decay picks up under ~6 months
    swing_decide_dte: int = 21
    take_profit: float = 0.5  # +50%
    cut_loss: float = -0.5
    low_odds: float = 0.2
    ideas_per_list: int = 6


# -- pricing contracts ----------------------------------------------------------------------------


def priced(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    *,
    right: Right,
    dte: tuple[int, int],
    strikes: tuple[float, float] = (0.5, 1.5),
) -> pd.DataFrame:
    """Two-sided quotes of one side of the chain within an expiry and strike window, with mid,
    spread, implied volatility (solved from the mid) and greeks per share."""
    c = chain[
        (chain["right"] == right)
        & chain["dte"].between(*dte)
        & chain["strike"].between(spot * strikes[0], spot * strikes[1])
        & (chain["bid"] > 0)
        & (chain["ask"] >= chain["bid"])
    ].copy()
    if c.empty:
        return c
    c["mid"] = (c["bid"] + c["ask"]) / 2
    c["spread_pct"] = (c["ask"] - c["bid"]) / c["mid"]
    rows = []
    for _, r in c.iterrows():
        exp = (
            r["expiration"]
            if isinstance(r["expiration"], date)
            else pd.Timestamp(r["expiration"]).date()
        )
        years = years_until(exp, now)
        call = right == "C"
        iv = bs.implied_vol(float(r["mid"]), spot, float(r["strike"]), years, call=call, rate=rate)
        if iv is None and pd.notna(r["implied_volatility"]) and r["implied_volatility"] > 0:
            iv = float(r["implied_volatility"])
        g = bs.greeks(spot, float(r["strike"]), years, iv, call=call, rate=rate) if iv else None
        rows.append(
            {
                "years": years,
                "iv": iv,
                "delta": g.delta if g else math.nan,
                "theta": g.theta if g else math.nan,
                "vega": g.vega if g else math.nan,
            }
        )
    extra = pd.DataFrame(rows, index=c.index)
    # the vendor's greeks are replaced by ones consistent with the mid and this spot
    vendor = c.drop(columns=["delta", "gamma", "theta", "vega", "rho"], errors="ignore")
    return pd.concat([vendor, extra], axis=1).dropna(subset=["delta"])


def contract_view(
    row: pd.Series, spot: float, rate: float, *, side: Literal["buy", "sell"] = "buy"
) -> dict[str, Any]:
    """What one contract costs and needs, per share and per contract (x100)."""
    call = row["right"] == "C"
    strike = float(row["strike"])
    price = float(row["ask"] if side == "buy" else row["bid"])
    years, iv = float(row["years"]), row["iv"]
    intrinsic = bs.intrinsic(spot, strike, call)
    breakeven = strike + price if call else strike - price
    above = bs.prob_above(spot, breakeven, years, iv, rate=rate) if iv else None
    itm = bs.prob_above(spot, strike, years, iv, rate=rate) if iv else None
    p_profit = (above if call else 1 - above) if above is not None else None
    if side == "sell" and p_profit is not None:
        p_profit = 1 - p_profit
    exp = row["expiration"]
    return {
        "contract": str(row["contract"]),
        "label": option_label(str(row["contract"])) or str(row["contract"]),
        "right": "call" if call else "put",
        "strike": strike,
        "expiration": exp.isoformat() if isinstance(exp, date) else str(exp),
        "dte": int(row["dte"]),
        "bid": float(row["bid"]),
        "ask": float(row["ask"]),
        "mid": float(row["mid"]),
        "spread_pct": float(row["spread_pct"]),
        "open_interest": _num(row.get("open_interest")),
        "price": price,
        "cost": price * 100,
        "iv": iv,
        "delta": float(row["delta"]),
        "theta_day": float(row["theta"]) * 100,
        "vega": float(row["vega"]) * 100,
        "intrinsic": intrinsic,
        "extrinsic": max(float(row["mid"]) - intrinsic, 0.0),
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "prob_itm": (itm if call else 1 - itm) if itm is not None else None,
        "prob_profit": p_profit,
        "expected_move_pct": iv * math.sqrt(years) if iv else None,
        "leverage": abs(float(row["delta"])) * spot / float(row["mid"]) if row["mid"] else None,
    }


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _closest(c: pd.DataFrame, score: pd.Series) -> pd.Series | None:
    if c.empty:
        return None
    best: pd.Series = c.loc[score.idxmin()]  # type: ignore[call-overload]
    return best


# -- picking contracts ----------------------------------------------------------------------------


def swing_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, right: Right, rules: Rules
) -> pd.Series | None:
    lo, hi, target = rules.swing_dte
    dlo, dhi, dtarget = rules.swing_delta
    c = priced(chain, spot, now, rate, right=right, dte=(lo, hi), strikes=(0.7, 1.3))
    if c.empty:
        return None
    c = c[c["delta"].abs().between(dlo, dhi) & (c["spread_pct"] <= MAX_SPREAD)]
    thin = (c["open_interest"].fillna(1e9) < 100).astype(float)
    score = (
        (c["dte"] - target).abs() / 30
        + (c["delta"].abs() - dtarget).abs() / 0.1
        + c["spread_pct"] * 10
        + thin
    )
    return _closest(c, score)


def spread_pick(
    chain: pd.DataFrame,
    spot: float,
    now: datetime,
    rate: float,
    long: pd.Series,
    rules: Rules,
) -> dict[str, Any] | None:
    """A debit spread on ``long``: sell the ~0.30-delta contract of the same expiry, further out
    of the money."""
    right: Right = "C" if long["right"] == "C" else "P"
    dte = int(long["dte"])
    c = priced(chain, spot, now, rate, right=right, dte=(dte, dte), strikes=(0.5, 1.6))
    beyond = c["strike"] > long["strike"] if right == "C" else c["strike"] < long["strike"]
    c = c[beyond & (c["spread_pct"] <= MAX_SPREAD)]
    short = _closest(c, (c["delta"].abs() - rules.short_delta).abs())
    if short is None:
        return None
    debit = float(long["ask"]) - float(short["bid"])
    width = abs(float(short["strike"]) - float(long["strike"]))
    if debit <= 0 or debit >= width:
        return None
    breakeven = float(long["strike"]) + debit if right == "C" else float(long["strike"]) - debit
    years, iv = float(long["years"]), long["iv"]
    above = bs.prob_above(spot, breakeven, years, iv, rate=rate) if iv else None
    return {
        "long": contract_view(long, spot, rate),
        "short": contract_view(short, spot, rate, side="sell"),
        "debit": debit,
        "cost": debit * 100,
        "max_gain": (width - debit) * 100,
        "reward_risk": (width - debit) / debit,
        "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "prob_profit": (above if right == "C" else 1 - above) if above is not None else None,
        "theta_day": (float(long["theta"]) - float(short["theta"])) * 100,
    }


def leaps_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, right: Right, rules: Rules
) -> pd.Series | None:
    """The ~0.80-delta contract 9+ months out that pays the least time value per year."""
    dlo, dhi, dtarget = rules.leaps_delta
    c = priced(
        chain, spot, now, rate, right=right, dte=(rules.leaps_min_dte, 10_000), strikes=(0.3, 1.7)
    )
    if c.empty:
        return None
    c = c[c["delta"].abs().between(dlo, dhi) & (c["spread_pct"] <= MAX_SPREAD)]
    if c.empty:
        return None
    intrinsic = [bs.intrinsic(spot, k, right == "C") for k in c["strike"]]
    carry = (c["mid"] - intrinsic).clip(lower=0) / spot / c["years"]
    score = carry + (c["delta"].abs() - dtarget).abs() * 0.2 + c["spread_pct"] * 0.5
    return _closest(c, score)


def leaps_view(row: pd.Series, spot: float, rate: float) -> dict[str, Any]:
    """A LEAPS as a stand-in for 100 shares: what it costs against the shares, the capital it
    frees, and the time value paid per year (the price of the leverage)."""
    v = contract_view(row, spot, rate)
    years = float(row["years"])
    v |= {
        "shares_equivalent": abs(v["delta"]) * 100,
        "stock_cost": spot * 100,
        "capital_freed": spot * 100 - v["cost"],
        "carry_per_year_pct": v["extrinsic"] / spot / years if years else None,
        # what the stock has to return a year for the contract to break even at expiry
        "breakeven_cagr": (
            (v["breakeven"] / spot) ** (1 / years) - 1 if years and v["right"] == "call" else None
        ),
    }
    return v


def covered_call_pick(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float, shares: float, rules: Rules
) -> dict[str, Any] | None:
    contracts = int(shares // 100)
    if contracts < 1:
        return None
    lo, hi, target = rules.covered_dte
    dlo, dhi, dtarget = rules.covered_delta
    c = priced(chain, spot, now, rate, right="C", dte=(lo, hi), strikes=(1.0, 1.4))
    c = c[c["delta"].between(dlo, dhi) & (c["bid"] >= 0.05) & (c["strike"] > spot)]
    row = _closest(c, (c["dte"] - target).abs() / 15 + (c["delta"] - dtarget).abs() / 0.05)
    if row is None:
        return None
    v = contract_view(row, spot, rate, side="sell")
    yield_ = float(row["bid"]) / spot
    return v | {
        "contracts": contracts,
        "premium": float(row["bid"]) * 100 * contracts,
        "yield_pct": yield_,
        "annualized_pct": yield_ * 365 / max(int(row["dte"]), 1),
        "upside_to_strike_pct": float(row["strike"]) / spot - 1,
        "prob_assigned": v["prob_itm"],
    }


# -- vol context ----------------------------------------------------------------------------------


def vol_context(
    chain: pd.DataFrame | None, hv20: float | None, history: pd.Series | None
) -> dict[str, Any]:
    """Implied volatility 30 days out, its rank in its own history, against realized
    volatility, and any event the term structure shows."""
    if chain is None or chain.empty:
        return {
            "iv30": None,
            "hv20": hv20,
            "iv_hv": None,
            "rank": None,
            "percentile": None,
            "history_days": 0,
            "event": None,
            "rich": False,
            "as_of": None,
        }
    term = atm_term(chain)
    iv30 = iv_at(term)
    rank = iv_rank(history if history is not None else pd.Series(dtype=float), iv30)
    ratio = iv30 / hv20 if iv30 and hv20 else None
    rich = bool(
        (rank["rank"] is not None and rank["rank"] >= RICH_IV_RANK)
        or (ratio is not None and ratio >= RICH_IV_RATIO)
    )
    return {
        "iv30": iv30,
        "hv20": hv20,
        "iv_hv": ratio,
        "rank": rank["rank"],
        "percentile": rank["percentile"],
        "history_days": rank["days"],
        "iv_low": rank["low"],
        "iv_high": rank["high"],
        "event": event_expiry(term),
        "rich": rich,
        "as_of": chain["snapshot_at"].max().isoformat(),
    }


# -- one symbol -----------------------------------------------------------------------------------


def stock_setup(stats: Mapping[str, Any], rs_3m: float | None) -> dict[str, Any]:
    """Trend, pullback and strength facts from ``stock_stats``."""
    v20, v50, v200 = (_num(stats.get(k)) for k in ("vs_ma20", "vs_ma50", "vs_ma200"))
    ma50, ma200 = _num(stats.get("ma50")), _num(stats.get("ma200"))
    month = _num((stats.get("returns") or {}).get("1M"))
    up = (
        v50 is not None and v200 is not None and v50 > 0 and v200 > 0 and (ma50 or 0) > (ma200 or 0)
    )
    down = (
        v50 is not None and v200 is not None and v50 < 0 and v200 < 0 and (ma50 or 0) < (ma200 or 0)
    )
    tags = []
    if up:
        tags.append("uptrend")
        if v20 is not None and -0.04 <= v20 <= 0.01:
            tags.append("pullback")
    if down:
        tags.append("downtrend")
    if (v20 is not None and v20 > 0.08) or (month is not None and month > 0.15):
        tags.append("extended")
    high = _num(stats.get("from_high_pct"))
    if high is not None and high > -0.03:
        tags.append("near 52-week high")
    if rs_3m is not None:
        if rs_3m > 0.05:
            tags.append("beating the S&P 500")
        elif rs_3m < -0.05:
            tags.append("lagging the S&P 500")
    direction = "bullish" if up else "bearish" if down else "neutral"
    return {"direction": direction, "tags": tags, "rs_3m": rs_3m}


def analyze(
    symbol: str,
    closes: pd.Series,
    *,
    chain: pd.DataFrame | None,
    benchmark: pd.Series | None,
    iv_hist: pd.Series | None,
    now: datetime,
    rate: float,
    rules: Rules | None = None,
) -> dict[str, Any]:
    """Everything the research knows about one symbol: the stock, its options' pricing, and the
    contract each approach would use."""
    rules = rules or Rules()
    stats = stock_stats(closes, None, benchmark)
    spot_now = chain_spot(chain) if chain is not None and not chain.empty else None
    spot = spot_now or _num(stats.get("price"))
    rs = _relative_strength(closes, benchmark)
    setup = stock_setup(stats, rs)
    vol = vol_context(chain, _num(stats.get("vol_20d")), iv_hist)
    out: dict[str, Any] = {
        "symbol": symbol,
        "spot": spot,
        "stock": {
            k: stats.get(k)
            for k in (
                "price",
                "day_change_pct",
                "returns",
                "ytd",
                "high_52w",
                "low_52w",
                "from_high_pct",
                "vs_ma20",
                "vs_ma50",
                "vs_ma200",
                "vol_20d",
                "beta_1y",
                "max_drawdown_1y",
            )
        },
        "setup": setup,
        "vol": vol,
        "picks": {},
    }
    if chain is None or chain.empty or not spot:
        return out
    picks: dict[str, Any] = {}
    for right, name in (("C", "call"), ("P", "put")):
        row = swing_pick(chain, spot, now, rate, right, rules)  # type: ignore[arg-type]
        if row is not None:
            view = contract_view(row, spot, rate)
            view["scenarios"] = _scenarios(row, spot, now, rate)
            picks[f"swing_{name}"] = view
            spread = spread_pick(chain, spot, now, rate, row, rules)
            if spread is not None:
                picks[f"{name}_spread"] = spread
    leaps = leaps_pick(chain, spot, now, rate, "C", rules)
    if leaps is not None:
        picks["leaps_call"] = leaps_view(leaps, spot, rate)
    out["picks"] = picks
    return out


def _relative_strength(
    closes: pd.Series, benchmark: pd.Series | None, days: int = 63
) -> float | None:
    if benchmark is None or len(closes) <= days or len(benchmark) <= days:
        return None
    mine = float(closes.iloc[-1] / closes.iloc[-1 - days] - 1)
    spy = float(benchmark.iloc[-1] / benchmark.iloc[-1 - days] - 1)
    return mine - spy


def _scenarios(row: pd.Series, spot: float, now: datetime, rate: float) -> dict[str, Any] | None:
    if not row["iv"]:
        return None
    exp = (
        row["expiration"]
        if isinstance(row["expiration"], date)
        else pd.Timestamp(row["expiration"]).date()
    )
    pos = OptionPosition(
        symbol=str(row["contract"]),
        label="",
        underlying="",
        quantity=1.0,
        strike=float(row["strike"]),
        expiration=exp,
        call=row["right"] == "C",
        cost=float(row["ask"]),
        broker_price=None,
    )
    return scenarios(pos, spot=spot, iv=float(row["iv"]), now=now, rate=rate)


# -- ranking ideas --------------------------------------------------------------------------------


def rank_ideas(
    analyses: list[dict[str, Any]], held: Mapping[str, str], rules: Rules | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Swing calls, LEAPS and bearish puts, best first, each with why and what to watch.

    Scores are percentile ranks within the scanned names (so they compare names with each
    other, not with an absolute bar): swing favours 6-month momentum, strength against the
    S&P 500 and a pullback entry; LEAPS favours a long, steady uptrend with shallow drawdowns;
    bearish mirrors swing for downtrends.
    """
    rules = rules or Rules()
    df = pd.DataFrame(
        [
            {
                "i": i,
                "r6": _num((a["stock"].get("returns") or {}).get("6M")),
                "r12": _num((a["stock"].get("returns") or {}).get("1Y")),
                "rs": _num(a["setup"].get("rs_3m")),
                "v200": _num(a["stock"].get("vs_ma200")),
                "dd": _num(a["stock"].get("max_drawdown_1y")),
            }
            for i, a in enumerate(analyses)
        ]
    )
    if df.empty:
        return {"swing": [], "leaps": [], "bearish": []}
    ranks = df.drop(columns="i").rank(pct=True).to_dict("index")
    out: dict[str, list[dict[str, Any]]] = {"swing": [], "leaps": [], "bearish": []}
    for idx, a in enumerate(analyses):
        tags = a["setup"]["tags"]
        p = {str(k): float(v) for k, v in ranks[idx].items()}
        if a["setup"]["direction"] == "bullish":
            swing = _mean(p["r6"], p["rs"], p["v200"]) + 0.1 * ("pullback" in tags)
            swing -= 0.15 * ("extended" in tags)
            out["swing"].append(_idea(a, "swing", swing, held))
            leaps = _mean(p["r12"], p["v200"], p["dd"])  # dd: less negative ranks higher
            out["leaps"].append(_idea(a, "leaps", leaps, held))
        elif a["setup"]["direction"] == "bearish":
            bear = _mean(1 - p["r6"], 1 - p["rs"], 1 - p["v200"])
            out["bearish"].append(_idea(a, "bearish", bear, held))
    for k in out:
        out[k] = sorted(out[k], key=lambda x: -x["score"])[: rules.ideas_per_list]
    return out


def _mean(*values: float) -> float:
    vals = [float(v) for v in values if v == v]
    return sum(vals) / len(vals) if vals else 0.0


def _idea(a: dict[str, Any], kind: str, score: float, held: Mapping[str, str]) -> dict[str, Any]:
    picks, vol, stock, setup = a["picks"], a["vol"], a["stock"], a["setup"]
    why: list[str] = []
    risks: list[str] = []
    r = stock.get("returns") or {}
    if kind in ("swing", "leaps"):
        why.append(_trend_line(stock, up=True))
    else:
        why.append(_trend_line(stock, up=False))
    rs = _num(setup.get("rs_3m"))
    if rs is not None and a["symbol"] != "SPY":
        side = "ahead of" if rs >= 0 else "behind"
        why.append(
            f"3-month return {_pct(r.get('3M'), signed=True)}, {abs(rs) * 100:.1f} points {side} "
            "the S&P 500."
        )
    if "pullback" in setup["tags"]:
        why.append(
            "Pulled back to its 20-day average "
            f"({_pct(stock.get('vs_ma20'), signed=True)}): a calmer entry."
        )
    if "extended" in setup["tags"]:
        risks.append(
            f"Extended: {_pct(stock.get('vs_ma20'), signed=True)} above its 20-day average; "
            "chasing here risks a pullback."
        )
    if kind == "leaps":
        dd = stock.get("max_drawdown_1y")
        why.append(f"1-year return {_pct(r.get('1Y'))}; worst drop over the year {_pct(dd)}.")
    pick: dict[str, Any] | None
    alternative: dict[str, Any] | None = None
    if kind == "swing":
        pick, alternative = picks.get("swing_call"), picks.get("call_spread")
        if vol.get("rich") and alternative is not None:
            pick, alternative = alternative, pick
    elif kind == "leaps":
        pick = picks.get("leaps_call")
    else:
        pick, alternative = picks.get("swing_put"), picks.get("put_spread")
        if vol.get("rich") and alternative is not None:
            pick, alternative = alternative, pick
    why += _vol_lines(vol, kind)
    event = vol.get("event")
    if event and kind != "leaps":
        risks.append(
            f"The option market is pricing an event (often earnings) before {event['by']}: implied "
            "volatility usually drops right after, which costs option buyers even when the stock "
            "moves their way."
        )
    if pick is None:
        risks.append("No option chain for it yet (or no liquid contract fits the rules).")
    if held.get(a["symbol"]):
        risks.append(f"Already held: {held[a['symbol']]}.")
    return {
        "symbol": a["symbol"],
        "kind": kind,
        "score": score,
        "spot": a["spot"],
        "setup": setup,
        "stock": stock,
        "vol": vol,
        "pick": pick,
        "alternative": alternative,
        "why": why,
        "risks": risks,
        "held": held.get(a["symbol"]),
    }


def _trend_line(stock: Mapping[str, Any], *, up: bool) -> str:
    where = "above" if up else "below"
    return (
        f"{'Uptrend' if up else 'Downtrend'}: {_pct(stock.get('vs_ma50'), signed=True)} vs "
        f"its 50-day and {_pct(stock.get('vs_ma200'), signed=True)} vs its 200-day average "
        f"(price {where} both)."
    )


def _vol_lines(vol: Mapping[str, Any], kind: str) -> list[str]:
    iv, hv = vol.get("iv30"), vol.get("hv20")
    if iv is None:
        return []
    lines = []
    rank = vol.get("rank")
    rank_text = (
        f"IV rank {_pct(rank, 0)} of its last {vol['history_days']} days"
        if rank is not None
        else f"IV rank from 20 days of history ({vol.get('history_days', 0)} so far)"
    )
    ratio = (
        f", {vol['iv_hv']:.1f}x the stock's 20-day realized {_pct(hv, 0)}"
        if vol.get("iv_hv")
        else ""
    )
    lines.append(f"Options: 30-day implied volatility {_pct(iv, 0)}{ratio}; {rank_text}.")
    if vol.get("rich") and kind != "leaps":
        lines.append(
            "Options are expensive by those measures, so the debit spread leads: it pays less "
            "for volatility."
        )
    elif vol.get("rich"):
        lines.append(
            "Options are expensive by those measures; a LEAPS pays for it over its whole life, "
            "so consider waiting for calmer pricing."
        )
    return lines


def _pct(value: Any, digits: int = 1, *, signed: bool = False) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


# -- the whole scan -------------------------------------------------------------------------------


def eligible(symbol: str, classifier: Classifier) -> bool:
    return classifier.funds.get(symbol) not in EXCLUDED_CLASSES


def closes_from_lake(lake: Lake, symbols: list[str], today: date) -> dict[str, pd.Series]:
    present = [s for s in dict.fromkeys(symbols) if s not in missing_symbols(lake, [s])]
    if not present:
        return {}
    bars = load_bars(lake, present, start=today - timedelta(days=420))
    return {
        str(s): pd.Series(
            g["adj_close"].to_numpy(dtype=float), index=pd.DatetimeIndex(g["session"])
        )
        for s, g in bars.groupby("symbol")
    }


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
) -> dict[str, Any]:
    """Analyze ``symbols`` (those with daily bars) and rank the ideas."""
    rules = rules or Rules()
    today = now.astimezone(NEW_YORK).date()
    wanted = [s for s in dict.fromkeys(symbols) if eligible(s, classifier)]
    closes = closes_from_lake(lake, [*wanted, "SPY"], today)
    bench = closes.get("SPY")
    history = iv_history(lake)
    by_symbol = {
        str(u): g.set_index("snapshot_date")["iv30"] for u, g in history.groupby("underlying")
    }
    analyses = []
    for symbol in wanted:
        series = closes.get(symbol)
        if series is None or len(series) < 60:
            continue
        analyses.append(
            analyze(
                symbol,
                series,
                chain=chains.get(symbol),
                benchmark=bench,
                iv_hist=by_symbol.get(symbol),
                now=now,
                rate=rate,
                rules=rules,
            )
        )
    ideas = rank_ideas(analyses, held or {}, rules)
    without = sorted({a["symbol"] for a in analyses if not a["picks"]})
    return {
        "as_of": now.isoformat(),
        "chains_as_of": max((c["snapshot_at"].max() for c in chains.values()), default=None),
        "scanned": len(analyses),
        "with_chains": sum(1 for a in analyses if a["picks"]),
        "without_chains": without,
        "iv_history_days": int(history["snapshot_date"].nunique()) if len(history) else 0,
        **ideas,
        "disclaimer": DISCLAIMER,
    }


# -- what to do with what you hold ----------------------------------------------------------------


def holding_actions(
    position: Mapping[str, Any],
    chain: pd.DataFrame | None,
    *,
    now: datetime,
    rate: float,
    rules: Rules | None = None,
) -> list[dict[str, Any]]:
    """Concrete choices for one position (a row of the positions view), priced from the latest
    chain: rolls, taking profits, cutting low-odds losers, covered calls, LEAPS in place of
    shares, protective puts. Each says what it costs or brings in."""
    rules = rules or Rules()
    if position.get("kind") == "option":
        return _option_actions(position, chain, now, rate, rules)
    return _stock_actions(position, chain, now, rate, rules)


def _action(
    level: str, title: str, text: str, trade: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"level": level, "title": title, "text": text, "trade": trade}


def _usd(value: float | None) -> str:
    if value is None or value != value:
        return "—"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _option_actions(
    p: Mapping[str, Any], chain: pd.DataFrame | None, now: datetime, rate: float, rules: Rules
) -> list[dict[str, Any]]:
    s = p.get("stats") or {}
    qty = float(p.get("quantity") or 0)
    if qty <= 0:
        return []  # short options: the notes cover assignment; no rules here yet
    days = _num(s.get("days_left")) or 0.0
    pnl_pct, mark = _num(s.get("pnl_pct")), _num(s.get("mark"))
    bid, delta = _num(s.get("bid")), _num(s.get("delta"))
    spot = _num(s.get("spot")) or (chain_spot(chain) if chain is not None else None)
    right: Right = "C" if s.get("right") == "call" else "P"
    strike = float(s.get("strike") or 0)
    size = qty * 100
    out: list[dict[str, Any]] = []
    if chain is not None and not chain.empty and bid is None:
        mine = chain[chain["contract"] == p.get("symbol")]
        bid = _num(mine["bid"].iloc[0]) if len(mine) else None
    sell_value = bid * size if bid is not None else (mark * size if mark is not None else None)
    have_chain = chain is not None and not chain.empty and spot

    if days >= rules.leaps_roll_below_dte:
        if pnl_pct is not None and pnl_pct >= 1.0 and delta is not None and abs(delta) >= 0.85:
            up = (
                _roll(
                    chain,
                    spot,
                    now,
                    rate,
                    right,
                    strike,
                    rules,
                    same_expiry=s.get("expiration"),
                    target_delta=0.70,
                )
                if have_chain
                else None
            )
            text = (
                f"Up {pnl_pct:.0%} and deep in the money (delta {delta:.2f}): it now moves "
                "almost like the shares."
            )
            if up is not None and bid is not None:
                credit = (bid - up["ask"]) * size
                text += (
                    f" Rolling up to the {up['label']} (delta {up['delta']:.2f}) would take about "
                    f"{_usd(credit)} off the table and keep most of the exposure."
                )
            out.append(_action("consider", "Lock in some of the gain", text, up))
        return out

    if 90 <= days < rules.leaps_roll_below_dte and delta is not None and abs(delta) >= 0.6:
        longer = (
            _roll(chain, spot, now, rate, right, strike, rules, min_dte=rules.leaps_min_dte)
            if have_chain
            else None
        )
        text = (
            f"{days:.0f} days left: under about six months, a long-dated option's time decay "
            "picks up. "
            "If you're holding for the long run, rolling out keeps the decay low."
        )
        if longer is not None and bid is not None:
            net = _usd((longer["ask"] - bid) * size)
            text += f" Rolling to the {longer['label']} costs about {net} net."
        out.append(_action("info", "Roll out (long-term holding)", text, longer))

    if pnl_pct is not None and pnl_pct >= rules.take_profit and days < 90:
        half = max(int(qty // 2), 1)
        part = (
            f"selling {half} of {int(qty)} returns about {_usd((bid or mark or 0) * half * 100)}"
            if qty > 1
            else (f"selling returns about {_usd(sell_value)}")
        )
        text = (
            f"Up {pnl_pct:.0%}. A common swing rule is to take some off at +50% to +100%: {part}, "
            "and the rest rides with the risk mostly off the table."
        )
        out.append(_action("consider", "Take profits", text))

    extrinsic = _num(s.get("extrinsic"))
    if (
        0 < days <= rules.swing_decide_dte
        and mark
        and extrinsic is not None
        and extrinsic / mark >= 0.3
    ):
        later = _roll(chain, spot, now, rate, right, strike, rules) if have_chain else None
        theta_now = _num(s.get("theta_day"))
        text = (
            f"{days:.0f} days left and {extrinsic / mark:.0%} of the price is time value, which "
            f"decays fastest from here (about {_usd(-(theta_now or 0))} a day now). "
            f"Closing returns about {_usd(sell_value)}."
        )
        if later is not None and bid is not None:
            text += (
                f" Rolling out to the {later['label']} ({later['dte']} days) costs about "
                f"{_usd((later['ask'] - bid) * size)} net and decays about "
                f"{_usd(-later['theta_day'] * qty)} a day."
            )
        out.append(_action("consider", "Close or roll before the decay speeds up", text, later))

    p_profit = _num(s.get("prob_profit"))
    if (
        pnl_pct is not None
        and pnl_pct <= rules.cut_loss
        and p_profit is not None
        and p_profit < rules.low_odds
    ):
        text = (
            f"Down {-pnl_pct:.0%} with about a {p_profit:.0%} chance of finishing at a profit "
            f"(the market's odds). Selling now salvages about {_usd(sell_value)}; holding risks "
            "that too, for the chance of a larger move."
        )
        out.append(_action("attention", "Low odds of recovering", text))
    return out


def _roll(
    chain: pd.DataFrame | None,
    spot: float | None,
    now: datetime,
    rate: float,
    right: Right,
    strike: float,
    rules: Rules,
    *,
    min_dte: int | None = None,
    same_expiry: object = None,
    target_delta: float | None = None,
) -> dict[str, Any] | None:
    """The contract to roll into: later (the swing window, or the longest LEAPS) at the same
    strike, or, with ``same_expiry``, the strike nearest ``target_delta``."""
    if chain is None or chain.empty or not spot:
        return None
    if same_expiry is not None:
        exp = date.fromisoformat(str(same_expiry))
        days = (exp - now.astimezone(NEW_YORK).date()).days
        c = priced(chain, spot, now, rate, right=right, dte=(days, days), strikes=(0.3, 2.0))
        row = _closest(c, (c["delta"].abs() - (target_delta or 0.7)).abs())
    elif min_dte is not None:
        c = priced(chain, spot, now, rate, right=right, dte=(min_dte, 10_000), strikes=(0.3, 2.0))
        if c.empty:
            return None
        c = c[c["dte"] == c["dte"].max()]
        row = _closest(c, (c["strike"] - strike).abs())
    else:
        lo, hi, target = rules.roll_swing_dte
        c = priced(chain, spot, now, rate, right=right, dte=(lo, hi), strikes=(0.3, 2.0))
        row = _closest(
            c, (c["dte"] - target).abs() / 30 + (c["strike"] - strike).abs() / max(spot * 0.02, 0.5)
        )
    return contract_view(row, spot, rate) if row is not None else None


def _stock_actions(
    p: Mapping[str, Any], chain: pd.DataFrame | None, now: datetime, rate: float, rules: Rules
) -> list[dict[str, Any]]:
    shares = float(p.get("quantity") or 0)
    spot = _num(p.get("price"))
    if chain is None or chain.empty or not spot or shares <= 0:
        return []
    out: list[dict[str, Any]] = []
    stats = p.get("stats") or {}
    if shares >= 100:
        cc = covered_call_pick(chain, spot, now, rate, shares, rules)
        if cc is not None:
            text = (
                f"Selling {cc['contracts']} {cc['label']} would bring in about "
                f"{_usd(cc['premium'])} ({cc['yield_pct']:.1%} in {cc['dte']} days, "
                f"{cc['annualized_pct']:.0%} a year), in "
                f"exchange for giving up gains above {cc['strike']:g} "
                f"({cc['upside_to_strike_pct']:+.1%}); about a {cc['prob_assigned'] or 0:.0%} "
                "chance the "
                "shares get called away."
            )
            out.append(_action("info", "Covered call income", text, cc))
        leaps = leaps_pick(chain, spot, now, rate, "C", rules)
        if leaps is not None:
            v = leaps_view(leaps, spot, rate)
            text = (
                f"The {v['label']} (delta {v['delta']:.2f}) costs {_usd(v['cost'])} against "
                f"{_usd(v['stock_cost'])} for 100 shares, freeing {_usd(v['capital_freed'])}. "
                f"The price is about {v['carry_per_year_pct'] or 0:.1%} a year in time value, and "
                "it can expire worthless if the stock falls below the strike."
            )
            out.append(_action("info", "LEAPS in place of 100 shares", text, v))
    weight, pnl_pct = _num(p.get("weight")), _num(p.get("pnl_pct"))
    if weight is not None and weight >= 0.15 and pnl_pct is not None and pnl_pct >= 0.5:
        put = _protective_put(chain, spot, now, rate)
        if put is not None:
            covered = max(int(shares // 100), 1)
            cost = put["cost"] * covered
            text = (
                f"{weight:.0%} of the portfolio and up {pnl_pct:.0%}. {covered} {put['label']} "
                f"would cost about {_usd(cost)} ({cost / (spot * shares):.1%} of the position) to "
                f"cap the loss below {put['strike']:g} until {put['expiration']}."
            )
            out.append(_action("info", "Insure the gain", text, put))
    if _num(stats.get("vs_ma200")) is not None and stats["vs_ma200"] < 0 and (pnl_pct or 0) < 0:
        out.append(
            _action(
                "consider",
                "Below its 200-day average",
                f"Trading {stats['vs_ma200']:+.1%} vs its 200-day average and below what you "
                "paid: the long-term trend is against it.",
            )
        )
    return out


def _protective_put(
    chain: pd.DataFrame, spot: float, now: datetime, rate: float
) -> dict[str, Any] | None:
    c = priced(chain, spot, now, rate, right="P", dte=(45, 120), strikes=(0.7, 1.0))
    c = c[c["delta"].abs().between(0.15, 0.40) & (c["spread_pct"] <= MAX_SPREAD)]
    row = _closest(c, (c["dte"] - 75).abs() / 30 + (c["delta"].abs() - 0.25).abs() / 0.05)
    return contract_view(row, spot, rate) if row is not None else None
