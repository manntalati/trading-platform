"""Run a library strategy over the lake's clean bars and record the run.

Every saved run gets a directory under ``reports/backtests/<strategy>/<run id>/`` holding the
summary (statistics, parameters, engine settings, data version, git commit) and the equity curve,
orders and fills, so any result can be traced to the exact code, data and configuration that
produced it.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from tp_core import metrics
from tp_core.bars import load_bars
from tp_core.storage import Lake, new_run_id, write_json, write_parquet
from tp_trading.data import MarketData
from tp_trading.engine import BacktestEngine, BacktestResult, EngineConfig
from tp_trading.risk import RiskGate
from tp_trading.strategy import Strategy


@dataclass(frozen=True)
class BacktestRun:
    result: BacktestResult
    benchmark: pd.Series | None  # daily returns of buy-and-hold on the benchmark symbol
    summary: dict[str, Any]
    run_id: str
    path: Path | None


def run_backtest(
    lake: Lake,
    strategy: Strategy,
    config: EngineConfig,
    *,
    risk: RiskGate | None = None,
    benchmark: str | None = "SPY",
    now: datetime,
    save: bool = True,
) -> BacktestRun:
    symbols = list(dict.fromkeys([*strategy.symbols(), *([benchmark] if benchmark else [])]))
    bars = load_bars(lake, symbols, end=config.end)
    data = MarketData.from_bars(bars)
    result = BacktestEngine(strategy, data, config, risk).run()

    bench = None
    if benchmark:
        closes = data.frame("close")[benchmark].ffill()
        bench = closes.pct_change(fill_method=None).reindex(result.equity.index).fillna(0.0)
        bench.iloc[0] = 0.0
        bench = bench.rename(benchmark)

    run_id = new_run_id(now)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "created_at": now.isoformat(),
        "strategy": strategy.name,
        "title": strategy.title,
        "spec": strategy.spec,
        "params": _jsonable(strategy.params()),
        "config": _jsonable(asdict(config)),
        "risk": type(risk).__name__ if risk is not None else "AllowAll",
        "data": data_version(bars),
        "code": git_version(),
        "stats": result.summary(benchmark=bench),
    }
    if bench is not None:
        sheet = metrics.tear_sheet(bench)
        assert isinstance(sheet, pd.Series)
        summary["benchmark"] = {"symbol": benchmark, **{str(k): v for k, v in sheet.items()}}

    path = None
    if save:
        path = lake.reports_dir("backtests") / strategy.name / run_id
        write_json(_jsonable(summary), path / "summary.json")
        curve = pd.DataFrame({"equity": result.equity, "returns": result.returns})
        if bench is not None:
            curve["benchmark_returns"] = bench
        write_parquet(curve.reset_index(names="session"), path / "equity.parquet")
        write_parquet(result.orders.astype({"session": "string"}), path / "orders.parquet")
        write_parquet(result.fills.astype({"session": "string"}), path / "fills.parquet")
    return BacktestRun(result, bench, summary, run_id, path)


def data_version(bars: pd.DataFrame) -> dict[str, Any]:
    """Enough to tell whether two runs saw the same data."""
    if bars.empty:
        return {"rows": 0}
    return {
        "rows": len(bars),
        "symbols": int(bars["symbol"].nunique()),
        "first_session": str(bars["session"].min()),
        "last_session": str(bars["session"].max()),
        "latest_ingest": str(bars["ingested_at"].max()),
    }


def git_version() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(dirty)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, float) and value != value:
        return None
    return value
