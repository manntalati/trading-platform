"""Strategy search: many simple rule sets tried at once, with the guards that keep the winner
honest.

Searching is how backtests lie: try enough variations and one of them looks great by luck. The
search is allowed because it is counted and checked:

1. **Train, then validation, then test** (the periods in ``config/validation.toml``). Rule sets
   are ranked on train; the ``top`` best go to validation, which picks one; only that one is
   shown the locked test period, and every look at it is counted.
2. **Deflated Sharpe** (``tp_core.significance``) of the pick on train and validation against
   what the best of *every rule set ever tried* by the search would show by luck. The trials
   ledger keeps all of them across runs, so searching again raises the bar.
3. **Probability of backtest overfitting** (PBO; Bailey, Borwein, López de Prado and Zhu 2014):
   the train and validation days are cut into ``blocks``; for every way to split the blocks in
   half, pick the best rule set on one half and see where it ranks on the other. PBO is the share
   of splits where the in-sample winner did worse than the median out of sample. Near 0: what wins
   keeps winning. 0.5 or more: picking the best is no better than picking at random.
4. **Walk-forward of the search itself**: every ``wf_test`` sessions, re-pick the best rule set
   on the last ``wf_train`` and trade it; the stitched record is what searching would have
   earned, and its ratio to the in-sample Sharpe (efficiency) is the haircut.

The families are vectorized versions of library strategies (positions decided at a close and
earning from the next session, costs on turnover), so a search runs in seconds; each pick maps
to an engine strategy and its parameters, and ``tp-backtest validate`` on the engine is still
the gate to paper trading. The vectorized versions skip sector caps, slots and the risk limits:
a screen, not a verdict.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tp_core import metrics
from tp_core.indicators import wilder_rsi
from tp_core.significance import deflated_sharpe
from tp_core.storage import Lake, new_run_id, write_json
from tp_strategies import grammar
from tp_strategies.backtest import _jsonable
from tp_strategies.library.ma_timing import GTAA
from tp_strategies.library.rsi2 import INDEX_ETFS
from tp_strategies.library.ts_momentum import DIVERSIFIED
from tp_strategies.library.xs_momentum import LARGE_CAPS
from tp_strategies.validation import ValidationConfig, record_trials, walk_forward

FAMILIES = ("trend", "momentum", "reversion", "grammar")
DEFAULT_FAMILIES = ("trend", "momentum", "reversion")  # grammar: opt in (it adds many trials)
TRADING_DAYS = 252


@dataclass(frozen=True)
class RuleSet:
    family: str
    params: dict[str, Any]
    engine: str  # the library strategy it stands for
    engine_params: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.engine} " + " ".join(f"{k}={v}" for k, v in self.params.items())

    def key(self) -> dict[str, Any]:
        return {"family": self.family, "engine": self.engine, **self.params}


# -- the families -----------------------------------------------------------------------------


def _month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    days = pd.Series(index, index=index)
    return pd.DatetimeIndex(days.groupby([index.year, index.month]).max().to_numpy())


def _hold_monthly(weights_at_month_end: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Weights decided at month ends, held until the next."""
    return weights_at_month_end.reindex(index).ffill().fillna(0.0)


def ma_timing_weights(closes: pd.DataFrame, months: int) -> pd.DataFrame:
    """Strategy 1: each asset's slice held while its month-end close is above the average of its
    last ``months`` month-end closes."""
    ends = _month_ends(pd.DatetimeIndex(closes.index))
    monthly = closes.loc[ends]
    above = monthly > monthly.rolling(months, min_periods=months).mean()
    weights = above.astype(float) / closes.shape[1]
    return _hold_monthly(weights.where(monthly.notna(), 0.0), pd.DatetimeIndex(closes.index))


def ts_momentum_weights(closes: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Strategy 2, equally weighted: each asset held while its ``lookback``-day return is
    positive, rebalanced at month ends."""
    ends = _month_ends(pd.DatetimeIndex(closes.index))
    trending = (closes / closes.shift(lookback) - 1).loc[ends] > 0
    return _hold_monthly(trending.astype(float) / closes.shape[1], pd.DatetimeIndex(closes.index))


def xs_momentum_weights(closes: pd.DataFrame, lookback: int, skip: int, top: int) -> pd.DataFrame:
    """Strategy 3: the ``top`` names by return from ``lookback`` to ``skip`` sessions ago, at
    equal weight, rebalanced at month ends."""
    ends = _month_ends(pd.DatetimeIndex(closes.index))
    score = (closes.shift(skip) / closes.shift(lookback) - 1).loc[ends]
    ranks = score.rank(axis=1, ascending=False, method="first")
    picks = (ranks <= top) & score.notna()
    return _hold_monthly(picks.astype(float) / top, pd.DatetimeIndex(closes.index))


def rsi2_weights(
    closes: pd.DataFrame, entry: float, trend: int, exit_ma: int, max_hold: int = 10,
    slots: int = 10,
) -> pd.DataFrame:  # fmt: skip
    """Strategy 5: buy a close with RSI(2) under ``entry`` above the ``trend``-day average; sell
    after a close above the ``exit_ma``-day average or ``max_hold`` sessions. Each position is
    1/``slots`` of the book (scaled down when more than ``slots`` are open)."""
    values = closes.to_numpy(dtype=float)
    rsi = wilder_rsi(closes, 2).to_numpy()
    trend_ma = closes.rolling(trend, min_periods=trend).mean().to_numpy()
    exit_avg = closes.rolling(exit_ma, min_periods=exit_ma).mean().to_numpy()
    n, k = values.shape
    held = np.zeros(k, dtype=bool)
    age = np.zeros(k, dtype=int)
    out = np.zeros((n, k))
    for t in range(n):
        age[held] += 1
        leave = held & ((values[t] > exit_avg[t]) | (age >= max_hold))
        held &= ~leave
        enter = ~held & (rsi[t] < entry) & (values[t] > trend_ma[t])
        held |= enter
        age[enter] = 0
        count = held.sum()
        if count:
            out[t, held] = 1.0 / max(slots, count)
    return pd.DataFrame(out, index=closes.index, columns=closes.columns)


def rule_sets(families: tuple[str, ...] = FAMILIES) -> list[RuleSet]:
    out = []
    if "trend" in families:
        out += [RuleSet("trend", {"months": m}, "ma-timing", {"months": m})
                for m in (4, 6, 8, 10, 12)]  # fmt: skip
        out += [RuleSet("trend", {"lookback": lb}, "ts-momentum", {"lookback": lb})
                for lb in (63, 126, 189, 252)]  # fmt: skip
    if "momentum" in families:
        for lb, skip, top in itertools.product((63, 126, 189, 252), (0, 21), (5, 10)):
            params = {"lookback": lb, "skip": skip, "top": top}
            out.append(RuleSet("momentum", params, "xs-momentum",
                               {"lookback": lb, "skip": skip, "top_n": top}))  # fmt: skip
    if "reversion" in families:
        for entry, trend, exit_ma in itertools.product((5, 10, 15, 20), (100, 200), (5, 10)):
            params = {"entry": entry, "trend": trend, "exit_ma": exit_ma}
            out.append(RuleSet("reversion", params, "rsi2", dict(params)))
    return out


def universe(rule: RuleSet) -> tuple[str, ...]:
    if rule.engine == "rule-timing":
        return DIVERSIFIED
    if rule.engine == "ma-timing":
        return GTAA
    if rule.engine == "ts-momentum":
        return DIVERSIFIED
    if rule.engine == "xs-momentum":
        return LARGE_CAPS
    return INDEX_ETFS + LARGE_CAPS


def weights_for(rule: RuleSet, closes: pd.DataFrame) -> pd.DataFrame:
    p = rule.params
    if rule.engine == "rule-timing":
        return grammar.rule_weights(grammar.parse(p["rule"]), closes, p["rebalance"])
    if rule.engine == "ma-timing":
        return ma_timing_weights(closes, p["months"])
    if rule.engine == "ts-momentum":
        return ts_momentum_weights(closes, p["lookback"])
    if rule.engine == "xs-momentum":
        return xs_momentum_weights(closes, p["lookback"], p["skip"], p["top"])
    return rsi2_weights(closes, p["entry"], p["trend"], p["exit_ma"])


def daily_returns(weights: pd.DataFrame, closes: pd.DataFrame, cost_bps: float) -> pd.Series:
    """Weights decided at a close earn the next session's returns; ``cost_bps`` per unit of
    turnover."""
    asset = closes.pct_change(fill_method=None).fillna(0.0)
    held = weights.shift(1).fillna(0.0)
    # the first day's change is the whole starting position (diff leaves it missing)
    turnover = weights.diff().fillna(weights).abs().sum(axis=1)
    out: pd.Series = (held * asset).sum(axis=1) - turnover.shift(1).fillna(0.0) * cost_bps / 1e4
    return out


# -- the guards -------------------------------------------------------------------------------


def _sharpe(r: pd.Series) -> float:
    value = metrics.sharpe_ratio(r)
    return float(value) if value == value else math.nan


def pbo(returns: pd.DataFrame, blocks: int = 16) -> dict[str, Any]:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation:
    ``returns`` is days x rule sets. For every half of the ``blocks``, the best Sharpe in it is
    ranked against the others on the other half."""
    blocks -= blocks % 2
    values = returns.to_numpy(dtype=float)
    parts = np.array_split(np.arange(len(values)), blocks)
    sums = np.array([values[p].sum(axis=0) for p in parts])
    squares = np.array([(values[p] ** 2).sum(axis=0) for p in parts])
    counts = np.array([len(p) for p in parts], dtype=float)[:, None]
    n_rules = values.shape[1]
    logits = []
    for chosen in itertools.combinations(range(blocks), blocks // 2):
        inside = np.zeros(blocks, dtype=bool)
        inside[list(chosen)] = True
        is_sr = _block_sharpe(sums[inside], squares[inside], counts[inside])
        os_sr = _block_sharpe(sums[~inside], squares[~inside], counts[~inside])
        best = int(np.nanargmax(is_sr))
        rank = float((os_sr < os_sr[best]).sum() + 1) / (n_rules + 1)  # 1 = best out of sample
        logits.append(math.log(rank / (1 - rank)))
    lam = np.array(logits)
    return {
        "splits": len(lam),
        "blocks": blocks,
        "pbo": float((lam <= 0).mean()),
        "median_logit": float(np.median(lam)),
    }


def _block_sharpe(sums: np.ndarray, squares: np.ndarray, counts: np.ndarray) -> np.ndarray:
    n = counts.sum()
    mean = sums.sum(axis=0) / n
    var = squares.sum(axis=0) / n - mean**2
    with np.errstate(invalid="ignore", divide="ignore"):
        out: np.ndarray = np.where(var > 0, mean / np.sqrt(var), np.nan)
    return out


@dataclass(frozen=True)
class SearchConfig:
    top: int = 5  # rule sets that go from train to validation
    cost_bps: float = 5.0
    blocks: int = 16  # PBO
    families: tuple[str, ...] = DEFAULT_FAMILIES
    # the grammar family's genetic search (tp_strategies.grammar)
    population: int = 60
    generations: int = 15
    keep: int = 20  # of its last generation, the best that join the fixed rule sets
    seed: int = 0


def search(
    closes: pd.DataFrame,
    cfg: SearchConfig,
    periods: ValidationConfig,
    *,
    trial_sharpes: Callable[[list[dict[str, Any]]], list[float]] | None = None,
) -> dict[str, Any]:
    """Run every rule set, pick one by the protocol above, and measure how much of its
    performance survives. ``trial_sharpes``: records this run's rule sets and returns the
    Sharpe of every one ever tried (the trials ledger); None counts only this run's."""
    rules = [r for r in rule_sets(cfg.families) if set(universe(r)) & set(closes.columns)]
    evolution = _evolve(closes, cfg, periods) if "grammar" in cfg.families else None
    if evolution is not None:
        rules += [_grammar_rule(c) for c, _ in evolution.population[: cfg.keep]]
    if not rules:
        raise ValueError("no bars for any family's universe: tp-data bars backfill")
    returns = {}
    for rule in rules:
        cols = [s for s in universe(rule) if s in closes.columns]
        px = closes[cols]
        returns[rule.label] = daily_returns(weights_for(rule, px), px, cfg.cost_bps)
    frame = pd.DataFrame(returns)
    # from the first day every rule set could act (the longest warm-up), so all are compared
    # on the same days
    warm = int(max(_first_active(frame[c]) for c in frame.columns))
    frame = frame.iloc[warm:]
    index = pd.DatetimeIndex(frame.index)
    train = index <= pd.Timestamp(periods.train_end)
    valid = (index > pd.Timestamp(periods.train_end)) & (
        index <= pd.Timestamp(periods.validation_end)
    )
    test = index > pd.Timestamp(periods.validation_end)
    if train.sum() < 252 or valid.sum() < 60:
        raise ValueError("too little history before the validation period ends")

    table = pd.DataFrame(
        {
            "family": [r.family for r in rules],
            "train": [_sharpe(frame.loc[train, r.label]) for r in rules],
            "validation": [_sharpe(frame.loc[valid, r.label]) for r in rules],
        },
        index=[r.label for r in rules],
    ).sort_values("train", ascending=False)
    finalists = table.head(cfg.top)
    chosen_label = str(finalists["validation"].idxmax())
    chosen = next(r for r in rules if r.label == chosen_label)

    seen = frame.loc[train | valid]
    rows: list[dict[str, Any]] = [
        {"params": r.key(), "sharpe": _sharpe(seen[r.label])} for r in rules
    ]
    if evolution is not None:  # every rule the genetic search scored is a trial too
        kept = {r.label for r in rules}
        for key, r in evolution.evaluated.items():
            rule = _grammar_rule(evolution.candidates[key])
            if rule.label not in kept:
                rows.append({"params": rule.key(), "sharpe": _sharpe(r.reindex(seen.index))})
    all_trials = trial_sharpes(rows) if trial_sharpes else [float(row["sharpe"]) for row in rows]
    deflated = deflated_sharpe(seen[chosen_label], all_trials)
    variants = [(r.key(), frame[r.label]) for r in rules]
    # the grammar's rules were bred on the training period: walk forward only after it
    start = int(train.sum()) if evolution is not None else periods.wf_train
    wf = walk_forward(variants, start, periods.wf_train, periods.wf_test)
    out_of_sample = frame.loc[test, chosen_label]
    return {
        "config": {"top": cfg.top, "cost_bps": cfg.cost_bps, "families": list(cfg.families)},
        "first": str(index[0].date()),
        "last": str(index[-1].date()),
        "periods": {
            "train_end": str(periods.train_end),
            "validation_end": str(periods.validation_end),
        },
        "rule_sets": len(rules),
        "grammar": (
            {
                "scored": len(evolution.evaluated),
                "generations": evolution.generations,
                "population": cfg.population,
                "kept": min(cfg.keep, len(evolution.population)),
            }
            if evolution is not None
            else None
        ),
        "table": table,
        "finalists": list(finalists.index),
        "chosen": chosen,
        "test": _period(out_of_sample) if len(out_of_sample) > 20 else None,
        "deflated": deflated.to_dict(),
        "pbo": pbo(seen, cfg.blocks),
        "walk_forward": {k: v for k, v in wf.items() if k != "windows"},
        "wf_train": periods.wf_train,
        "wf_test": periods.wf_test,
        "_returns": frame,
    }


def _grammar_rule(c: grammar.Candidate) -> RuleSet:
    params = {"rule": c.text, "rebalance": c.rebalance}
    return RuleSet("grammar", params, "rule-timing", dict(params))


def _evolve(
    closes: pd.DataFrame, cfg: SearchConfig, periods: ValidationConfig
) -> grammar.Evolution | None:
    """Breed grammar rules on the training period (DIVERSIFIED's assets)."""
    cols = [s for s in DIVERSIFIED if s in closes.columns]
    if not cols:
        return None
    px = closes[cols]
    index = pd.DatetimeIndex(px.index)
    train = pd.Series(index <= pd.Timestamp(periods.train_end), index=index)
    return grammar.evolve(
        px, train, lambda w: daily_returns(w, px, cfg.cost_bps),
        population=cfg.population, generations=cfg.generations, seed=cfg.seed,
    )  # fmt: skip


def _first_active(r: pd.Series) -> int:
    nonzero = np.flatnonzero(r.to_numpy() != 0)
    return int(nonzero[0]) if len(nonzero) else 0


def _period(r: pd.Series) -> dict[str, Any]:
    return {
        "sessions": len(r),
        "sharpe": _sharpe(r),
        "cagr": float(metrics.cagr(r)),
        "max_drawdown": float(metrics.max_drawdown(r)),
    }


def verdict(report: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Worth validating on the engine? Every guard must hold."""
    d, p, wf, test = report["deflated"], report["pbo"], report["walk_forward"], report["test"]
    checks = [
        (d["probability"] == d["probability"] and d["probability"] >= 0.95,
         f"Deflated Sharpe probability {d['probability']:.0%} (needs 95%)"),
        (p["pbo"] < 0.5, f"probability of overfitting {p['pbo']:.0%} (needs under 50%)"),
        ((wf.get("sharpe") or 0) > 0,
         f"walk-forward Sharpe {_fmt(wf.get('sharpe'))} (needs above 0)"),
        (test is not None and test["sharpe"] > 0,
         f"locked test Sharpe {_fmt(test['sharpe'] if test else None)} (needs above 0)"),
    ]  # fmt: skip
    return all(ok for ok, _ in checks), [("✓ " if ok else "✗ ") + text for ok, text in checks]


def _fmt(value: Any) -> str:
    return "—" if value is None or value != value else f"{value:.2f}"


def engine_command(rule: RuleSet) -> str:
    params = " ".join(
        f"-p '{k}={v}'" if any(ch in str(v) for ch in "(),") else f"-p {k}={v}"
        for k, v in rule.engine_params.items()
    )
    return f"tp-backtest validate {rule.engine} {params}".strip()


def report_text(report: Mapping[str, Any], looks: int | None = None) -> str:
    table: pd.DataFrame = report["table"]
    chosen: RuleSet = report["chosen"]
    lines = [
        f"Strategy search: {report['rule_sets']} rule sets in "
        f"{', '.join(report['config']['families'])}, {report['first']} to {report['last']} "
        f"(train to {report['periods']['train_end']}, validation to "
        f"{report['periods']['validation_end']}, test after). "
        f"Costs {report['config']['cost_bps']:g} bps per trade.",
        "",
        f"Best {len(report['finalists'])} on train, then validation (Sharpe):",
    ]
    g = report.get("grammar")
    if g:
        lines[1:1] = [
            f"The grammar's genetic search scored {g['scored']} rules over {g['generations']} "
            f"generations on the training period alone; its best {g['kept']} joined the fixed rule "
            f"sets above, and all {g['scored']} count as trials for the Deflated Sharpe.",
        ]
    for label in report["finalists"]:
        row = table.loc[label]
        mark = "→" if label == chosen.label else " "
        lines.append(f" {mark} {label:<44}{_fmt(row['train']):>7}{_fmt(row['validation']):>7}")
    worst = table.tail(3)
    lines.append(
        "   (worst on train: "
        + ", ".join(f"{i} {_fmt(r['train'])}" for i, r in worst.iterrows())
        + ")"
    )
    d, p, wf = report["deflated"], report["pbo"], report["walk_forward"]
    test = report["test"]
    look = f" (look #{looks})" if looks else ""
    lines += [
        "",
        f"Chosen: {chosen.label}",
        f"  Locked test{look}: "
        + (
            f"Sharpe {_fmt(test['sharpe'])}, CAGR {test['cagr']:.1%}, max drawdown "
            f"{test['max_drawdown']:.0%} over {test['sessions']} sessions"
            if test
            else "no test period yet"
        ),
        f"  Deflated Sharpe: {_fmt(d['sharpe'])} on train and validation vs {_fmt(d['threshold'])} "
        f"the best of {d['trials']} rule sets ever tried would show by luck: "
        f"{d['probability']:.0%} probability it is real",
        f"  Probability of backtest overfitting: {p['pbo']:.0%} (the best on one half of the "
        f"{p['blocks']} blocks fell below the median on the other in {p['pbo']:.0%} of "
        f"{p['splits']:,} splits)",
        f"  Walk-forward of the search (re-picked every {report['wf_test']} sessions on the last "
        f"{report['wf_train']}): "
        f"Sharpe {_fmt(wf.get('sharpe'))} out of sample, efficiency {_fmt(wf.get('efficiency'))} "
        "of in-sample",
    ]
    ok, checks = verdict(report)
    lines += ["", "Worth validating on the engine?" + (" Yes." if ok else " Not yet.")]
    lines += [f"  {c}" for c in checks]
    lines.append(f"  On the engine: {engine_command(chosen)}")
    return "\n".join(lines)


# -- the record: every rule set tried, every look at the test -----------------------------------


def search_dir(lake: Lake) -> Path:
    return lake.reports_dir("search")


def record_test_look(lake: Lake, rule: RuleSet, now: datetime) -> int:
    """Log a look at the locked test period for ``rule``; how many there have been."""
    path = search_dir(lake) / "test_looks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    key = json.dumps(rule.key(), sort_keys=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": now.isoformat(), "rule": rule.key()}) + "\n")
    looks = [json.loads(line) for line in path.read_text().splitlines()]
    return sum(json.dumps(entry["rule"], sort_keys=True) == key for entry in looks)


def run_search(
    lake: Lake,
    closes: pd.DataFrame,
    cfg: SearchConfig,
    periods: ValidationConfig,
    *,
    now: datetime,
    record: bool = True,
) -> tuple[dict[str, Any], int | None]:
    """``search`` with its trials added to the search's ledger (so the Deflated Sharpe counts
    every rule set ever tried) and the look at the test period counted; also saves the report.
    Returns the report and the look number."""
    ledger = search_dir(lake) / "trials.jsonl"

    def trials(rows: list[dict[str, Any]]) -> list[float]:
        return record_trials(lake, "search", rows, now, path=ledger)

    report = search(closes, cfg, periods, trial_sharpes=trials if record else None)
    looks = record_test_look(lake, report["chosen"], now) if record else None
    if record:
        summary = {k: v for k, v in report.items() if not k.startswith("_") and k != "table"}
        summary["chosen"] = report["chosen"].key()
        summary["table"] = report["table"].reset_index(names="rule_set").to_dict("records")
        write_json(_jsonable(summary), search_dir(lake) / f"{new_run_id(now)}.json")
    return report, looks
