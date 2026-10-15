"""The plan's backtest validation protocol: does a strategy earn its place in paper trading?

One run of ``validate`` backtests a strategy's parameter variants (``config/validation.toml``)
through the same engine, costs and risk limits as ``tp-backtest run``, then checks the plan's
stage-1 gate:

1. **Locked test set**: positive Sharpe after costs on the most recent period, which nothing
   was tuned on. Every look is counted; looking often turns it into another validation set.
2. **Walk-forward**: every six months, pick the variant with the best Sharpe over the last two
   years and trade it for the next six. The stitched out-of-sample returns must be positive.
3. **Deflated Sharpe**: the Sharpe must beat what the best of all the variants ever tried would
   show by luck (``tp_core.significance``). Every variant run is logged for this.
4. **Stress periods**: in each crash in the data, the drawdown stays under 15% or is no worse
   than buy-and-hold SPY's.
5. **Parameter stability**: nearby parameter sets keep at least half the Sharpe, so the result
   isn't a lucky spike.

Also reported, not gated: returns by market regime (``tp_core.regime``), and a block bootstrap
of the daily returns (the spread of outcomes the same strategy could have produced).
"""

from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core import metrics
from tp_core.bars import close_matrix, load_bars
from tp_core.macro import load_macro
from tp_core.regime import current, market_regimes, performance_by_regime
from tp_core.significance import block_bootstrap, deflated_sharpe
from tp_core.storage import Lake, new_run_id, write_json
from tp_core.versions import git_version
from tp_strategies.backtest import BacktestRun, _jsonable, run_backtest
from tp_strategies.library import build
from tp_trading.engine import EngineConfig
from tp_trading.risk import RiskGate

PERIOD_STATS = ("total_return", "cagr", "ann_volatility", "sharpe", "max_drawdown")


@dataclass(frozen=True)
class Gate:
    min_test_sharpe: float = 0.0
    min_walk_forward_sharpe: float = 0.0
    min_deflated_excess: float = 0.0
    stress_vs_benchmark: float = 1.0
    stress_max_drawdown: float = 0.15  # shallower stress drawdowns pass whatever SPY did
    min_neighbor_sharpe_ratio: float = 0.5


@dataclass(frozen=True)
class ValidationConfig:
    train_end: date = date(2023, 6, 30)
    validation_end: date = date(2024, 12, 31)
    stress: dict[str, tuple[date, date]] = field(default_factory=dict)
    wf_train: int = 504
    wf_test: int = 126
    gate: Gate = field(default_factory=Gate)
    variants: dict[str, dict[str, list[Any]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ValidationConfig:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        periods = raw.get("periods", {})
        wf = raw.get("walk_forward", {})
        return cls(
            train_end=_date(periods.get("train_end"), date(2023, 6, 30)),
            validation_end=_date(periods.get("validation_end"), date(2024, 12, 31)),
            stress={k: (_date(v[0]), _date(v[1])) for k, v in raw.get("stress", {}).items()},
            wf_train=int(wf.get("train_sessions", 504)),
            wf_test=int(wf.get("test_sessions", 126)),
            gate=Gate(**raw.get("gate", {})),
            variants={k: dict(v) for k, v in raw.get("variants", {}).items()},
        )


def _date(value: Any, default: date | None = None) -> date:
    if value is None:
        assert default is not None
        return default
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def signature(params: Mapping[str, Any]) -> str:
    return json.dumps(_jsonable(dict(params)), sort_keys=True)


def variant_params(
    name: str, base: Mapping[str, Any], config: ValidationConfig
) -> list[dict[str, Any]]:
    """The base parameters first, then one variant per value in ``config.variants[name]``."""
    out = [dict(base)]
    for key, values in config.variants.get(name, {}).items():
        for value in values:
            candidate = dict(base) | {key: value}
            if signature(build(name, candidate).params()) not in {
                signature(build(name, p).params()) for p in out
            }:
                out.append(candidate)
    return out


# -- the pieces -----------------------------------------------------------------------------------


def active_returns(run: BacktestRun) -> pd.Series:
    """Daily returns from the first session the strategy held anything (before that it's
    warming up, and those zero days would flatter the statistics)."""
    exposure = run.result.exposure
    held = exposure[exposure > 1e-9]
    returns = run.result.returns
    return returns.loc[held.index[0] :] if len(held) else returns.iloc[0:0]


def period_stats(returns: pd.Series, benchmark: pd.Series | None) -> dict[str, Any]:
    if len(returns) < 2:
        return {"sessions": len(returns)}
    sheet = metrics.tear_sheet(returns)
    assert isinstance(sheet, pd.Series)
    out: dict[str, Any] = {
        "start": str(returns.index[0].date()),
        "end": str(returns.index[-1].date()),
        "sessions": len(returns),
        **{k: _num(sheet[k]) for k in PERIOD_STATS},
    }
    if benchmark is not None:
        b = metrics.tear_sheet(benchmark.reindex(returns.index).fillna(0.0))
        assert isinstance(b, pd.Series)
        out["benchmark"] = {k: _num(b[k]) for k in PERIOD_STATS}
    return out


def split_periods(
    returns: pd.Series, benchmark: pd.Series | None, config: ValidationConfig
) -> dict[str, Any]:
    days = pd.DatetimeIndex(returns.index).date
    train = returns[days <= config.train_end]
    validation = returns[(days > config.train_end) & (days <= config.validation_end)]
    test = returns[days > config.validation_end]
    return {
        "train": period_stats(train, benchmark),
        "validation": period_stats(validation, benchmark),
        "test": period_stats(test, benchmark),
    }


def walk_forward(
    variants: list[tuple[dict[str, Any], pd.Series]], start: int, train: int, test: int
) -> dict[str, Any]:
    """Re-pick the variant with the best trailing Sharpe every ``test`` sessions; the stitched
    returns of what was picked are the out-of-sample record."""
    if not variants:
        return {"windows": []}
    index = variants[0][1].index
    oos: list[pd.Series] = []
    windows = []
    in_sample = []
    i = max(start, train)
    while i < len(index):
        scores = [metrics.sharpe_ratio(r.iloc[i - train : i]) for _, r in variants]
        valid = [(s, k) for k, s in enumerate(scores) if s == s]
        best = max(valid)[1] if valid else 0
        chunk = variants[best][1].iloc[i : i + test]
        oos.append(chunk)
        in_sample.append(scores[best] if scores[best] == scores[best] else math.nan)
        windows.append(
            {
                "start": str(index[i].date()),
                "end": str(chunk.index[-1].date()),
                "params": variants[best][0],
                "train_sharpe": _num(scores[best]),
                "test_return": _num(metrics.total_return(chunk)),
            }
        )
        i += test
    if not oos:
        return {"windows": []}
    stitched = pd.concat(oos)
    sharpe = metrics.sharpe_ratio(stitched)
    mean_is = pd.Series(in_sample).mean()
    return {
        "windows": windows,
        "sessions": len(stitched),
        "sharpe": _num(sharpe),
        "cagr": _num(metrics.cagr(stitched)),
        "max_drawdown": _num(metrics.max_drawdown(stitched)),
        # out-of-sample Sharpe as a share of what the picks showed in-sample (1 = no decay)
        "efficiency": _num(sharpe / mean_is) if mean_is and mean_is == mean_is else None,
    }


def stress_tests(
    returns: pd.Series, benchmark: pd.Series | None, config: ValidationConfig
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not len(returns):
        return out
    first, last = returns.index[0].date(), returns.index[-1].date()
    for name, (start, end) in config.stress.items():
        row: dict[str, Any] = {"name": name, "start": start.isoformat(), "end": end.isoformat()}
        if start < first or start > last:
            out.append(row | {"passed": None, "note": "not in the backtest"})
            continue
        days = pd.DatetimeIndex(returns.index).date
        r = returns[(days >= start) & (days <= end)]
        row |= {"return": _num(metrics.total_return(r)), "max_drawdown": _num(_path_dd(r))}
        if benchmark is not None:
            b = benchmark.reindex(r.index).fillna(0.0)
            row |= {
                "benchmark_return": _num(metrics.total_return(b)),
                "benchmark_max_drawdown": _num(_path_dd(b)),
            }
            shallow = row["max_drawdown"] >= -config.gate.stress_max_drawdown
            vs_market = (
                row["max_drawdown"]
                >= row["benchmark_max_drawdown"] * config.gate.stress_vs_benchmark
            )
            row["passed"] = bool(shallow or vs_market)
        else:
            row["passed"] = None
        out.append(row)
    return out


def _path_dd(returns: pd.Series) -> float:
    """Worst peak-to-trough within the period, counting from its start."""
    if not len(returns):
        return 0.0
    wealth = (1 + returns.fillna(0.0)).cumprod()
    return float(min((wealth / wealth.cummax().clip(lower=1.0) - 1.0).min(), 0.0))


def stability(
    base: dict[str, Any], variants: list[tuple[dict[str, Any], BacktestRun]], ratio: float
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for params, run in variants:
        r = active_returns(run)
        rows.append(
            {
                "params": params,
                "sharpe": _num(metrics.sharpe_ratio(r)),
                "cagr": _num(metrics.cagr(r)),
                "max_drawdown": _num(metrics.max_drawdown(r)),
                "base": params == base,
            }
        )
    base_sharpe: float | None = next((r["sharpe"] for r in rows if r["base"]), None)
    others: list[float] = [r["sharpe"] for r in rows if not r["base"] and r["sharpe"] is not None]
    worst = min(others) if others else None
    passed = None
    if base_sharpe is not None and worst is not None:
        passed = base_sharpe > 0 and worst >= ratio * base_sharpe
    return {
        "variants": rows,
        "base_sharpe": base_sharpe,
        "worst_neighbor_sharpe": worst,
        "passed": passed,
    }


# -- trials ledger --------------------------------------------------------------------------------


def trials_path(lake: Lake, strategy: str) -> Path:
    return lake.reports_dir("validation") / strategy / "trials.jsonl"


def record_trials(
    lake: Lake,
    strategy: str,
    rows: list[dict[str, Any]],
    now: datetime,
    path: Path | None = None,
) -> list[float]:
    """Append this run's variants to the strategy's trial log (or ``path``); every distinct
    parameter set ever run (latest Sharpe each) is what the Deflated Sharpe has to beat."""
    path = path or trials_path(lake, strategy)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(
                json.dumps({"at": now.isoformat(), "params": r["params"], "sharpe": r["sharpe"]})
                + "\n"
            )
    latest: dict[str, float] = {}
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("sharpe") is not None:
            latest[signature(entry["params"])] = float(entry["sharpe"])
    return list(latest.values())


# -- the whole protocol ---------------------------------------------------------------------------


def validate(
    lake: Lake,
    name: str,
    *,
    params: Mapping[str, Any] | None = None,
    engine: EngineConfig,
    config: ValidationConfig,
    risk: Callable[[], RiskGate | None] = lambda: None,
    sectors: Mapping[str, str] | None = None,
    now: datetime,
    save: bool = True,
    progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    base_strategy = build(name, params or {})
    base = base_strategy.params()
    runs: list[tuple[dict[str, Any], BacktestRun]] = []
    for p in variant_params(name, base, config):
        strategy = build(name, p)
        progress(f"backtesting {name} {strategy.params()}")
        run = run_backtest(
            lake, strategy, engine, risk=risk(), sectors=sectors, now=now, save=False
        )
        runs.append((strategy.params(), run))
    base_run = runs[0][1]
    bench = base_run.benchmark
    returns = active_returns(base_run)
    if len(returns) < 2:
        raise ValueError(f"{name} never traded in the backtest: not enough history?")

    progress("walk-forward, stress periods, regimes, Monte Carlo")
    start = int(base_run.result.returns.index.searchsorted(returns.index[0]))
    wf = walk_forward(
        [(p, r.result.returns) for p, r in runs], start, config.wf_train, config.wf_test
    )
    stab = stability(base, runs, config.gate.min_neighbor_sharpe_ratio)
    trial_sharpes = (
        record_trials(lake, name, stab["variants"], now)
        if save
        else [v["sharpe"] for v in stab["variants"] if v["sharpe"] is not None]
    )
    deflated = deflated_sharpe(returns, trial_sharpes)
    periods = split_periods(returns, bench, config)
    stress = stress_tests(returns, bench, config)
    closes = close_matrix(load_bars(lake, end=engine.end))
    macro = load_macro(lake, end=engine.end)
    regimes = market_regimes(closes, macro=macro) if "SPY" in closes.columns else pd.DataFrame()
    by_regime = performance_by_regime(returns, regimes, bench) if len(regimes) else {}
    folder = lake.reports_dir("validation") / name
    touches = (
        1
        + sum(
            1
            for f in folder.glob("*.json")
            if json.loads(f.read_text()).get("params_signature") == signature(base)
        )
        if folder.exists()
        else 1
    )

    report: dict[str, Any] = {
        "run_id": new_run_id(now),
        "created_at": now.isoformat(),
        "strategy": name,
        "title": base_strategy.title,
        "spec": base_strategy.spec,
        "params": base,
        "params_signature": signature(base),
        "code": git_version(),
        "engine": _jsonable(asdict(engine)),
        "full": period_stats(returns, bench),
        "periods": periods,
        "walk_forward": wf,
        "deflated": deflated.to_dict(),
        "stress": stress,
        "stability": stab,
        "monte_carlo": block_bootstrap(returns),
        "regimes": by_regime,
        "regime_now": current(regimes) if len(regimes) else {},
        "test_touches": touches,
    }
    report["gate"] = gate(report, config)
    report = _jsonable(_finite(report))
    if save:
        write_json(report, folder / f"{report['run_id']}.json")
    return report


def gate(report: Mapping[str, Any], config: ValidationConfig) -> dict[str, Any]:
    g = config.gate
    test = report["periods"]["test"]
    wf, deflated = report["walk_forward"], report["deflated"]
    stress = [s for s in report["stress"] if s.get("passed") is not None]
    checks = [
        _check(
            "Locked test set",
            None if test.get("sharpe") is None else test["sharpe"] > g.min_test_sharpe,
            f"Sharpe {_fmt(test.get('sharpe'))} after costs over {test.get('sessions', 0)} "
            f"sessions since {config.validation_end}"
            + (
                f" (looked at {report['test_touches']} times)" if report["test_touches"] > 1 else ""
            ),
        ),
        _check(
            "Walk-forward",
            None if wf.get("sharpe") is None else wf["sharpe"] > g.min_walk_forward_sharpe,
            f"out-of-sample Sharpe {_fmt(wf.get('sharpe'))} over {len(wf.get('windows', []))} "
            "re-chosen windows",
        ),
        _check(
            "Deflated Sharpe",
            None if deflated["probability"] is None else deflated["excess"] > g.min_deflated_excess,
            f"Sharpe {_fmt(deflated['sharpe'])} vs {_fmt(deflated['threshold'])} the best of "
            f"{deflated['trials']} variants tried would show by luck "
            f"({_fmt(deflated['probability'], pct=True)} probability it's real)",
        ),
        _check(
            "Stress periods",
            None if not stress else all(s["passed"] for s in stress),
            "; ".join(
                f"{s['name']}: {_fmt(s['max_drawdown'], pct=True)} vs SPY "
                f"{_fmt(s['benchmark_max_drawdown'], pct=True)}"
                for s in stress
            )
            or "none of the stress periods are in the backtest",
        ),
        _check(
            "Parameter stability",
            report["stability"]["passed"],
            f"worst nearby Sharpe {_fmt(report['stability']['worst_neighbor_sharpe'])} vs "
            f"{_fmt(report['stability']['base_sharpe'])} for the chosen parameters",
        ),
    ]
    return {"passed": all(c["passed"] is True for c in checks), "checks": checks}


def _check(name: str, passed: bool | None, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _fmt(value: Any, *, pct: bool = False) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:.1%}" if pct else f"{value:.2f}"


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _finite(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item") and not isinstance(value, str):
        return _finite(value.item())
    return value


def paper_params(path: Path) -> dict[str, dict[str, Any]]:
    """Each paper-traded strategy's parameters: validation checks what actually trades."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return {name: dict(e.get("params", {})) for name, e in raw.get("strategies", {}).items()}


def latest_reports(lake: Lake) -> dict[str, dict[str, Any]]:
    """The newest validation report of each strategy."""
    base = lake.reports_dir("validation")
    out: dict[str, dict[str, Any]] = {}
    if not base.exists():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        files = list(folder.glob("*.json"))
        if files:
            newest = max(files, key=lambda f: f.stat().st_mtime_ns)
            out[folder.name] = json.loads(newest.read_text())
    return out
