"""Strategy validation and the market regime.

``GET /api/strategies/validation``: every library strategy, whether it paper-trades, and its
latest validation report (the plan's stage-1 gate). ``POST`` runs validations in the background
(a few seconds to a couple of minutes per strategy). ``GET /api/market/regime``: today's trend,
volatility and breadth labels, and the last year of them.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tp_api import queries
from tp_api.deps import Ctx, require_dashboard_client, require_token
from tp_core.bars import close_matrix, load_bars
from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.regime import DIMENSIONS, current, market_regimes
from tp_core.storage import Lake
from tp_strategies.backtest import risk_gate
from tp_strategies.library import REGISTRY
from tp_strategies.validation import ValidationConfig, latest_reports, paper_params, validate
from tp_trading.engine import EngineConfig

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_token)])
REGIME_CACHE_SECONDS = 600.0


def now_utc() -> datetime:
    return datetime.now(UTC)


def validation_config(settings: Settings) -> ValidationConfig:
    path = settings.validation_file
    return ValidationConfig.load(path) if path.exists() else ValidationConfig()


class ValidationJob:
    """Validate strategies one after another in the background; one job at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False}

    def start(self, names: list[str]) -> bool:
        with self._lock:
            if self.state.get("running"):
                return False
            self.state = {
                "running": True,
                "names": names,
                "done": [],
                "current": None,
                "started_at": now_utc().isoformat(),
            }
        threading.Thread(target=self._run, args=(names,), name="validation", daemon=True).start()
        return True

    def _run(self, names: list[str]) -> None:
        s = self.settings
        lake = Lake(s.data_root)
        classifier = Classifier.load(s.classifications_file)
        config = validation_config(s)
        params = paper_params(s.paper_file)
        errors = []
        for name in names:
            with self._lock:
                self.state["current"] = name
            try:
                validate(
                    lake,
                    name,
                    params=params.get(name, {}),
                    engine=EngineConfig(),
                    config=config,
                    risk=(lambda: risk_gate(s.risk_file, classifier))
                    if s.risk_file.exists()
                    else (lambda: None),
                    sectors=classifier.sectors,
                    now=now_utc(),
                )
            except Exception as exc:  # reported on the page; the others still run
                log.exception("validating %s failed", name)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
            with self._lock:
                self.state["done"] = [*self.state["done"], name]
        with self._lock:
            self.state |= {
                "running": False,
                "current": None,
                "ok": not errors,
                "message": "; ".join(errors) or f"validated {len(names)}",
                "finished_at": now_utc().isoformat(),
            }


class ValidateRequest(BaseModel):
    names: list[str] | None = Field(default=None, description="Strategies; all when omitted")


@router.get("/api/strategies/validation")
def validation(c: Ctx, request: Request) -> dict[str, Any]:
    reports = latest_reports(c.lake)
    on_paper = paper_params(c.settings.paper_file)
    config = validation_config(c.settings)
    job: ValidationJob = request.app.state.validation_job
    strategies = [
        {
            "name": name,
            "title": cls.title,
            "spec": cls.spec,
            "on_paper": name in on_paper,
            "report": reports.get(name),
        }
        for name, cls in sorted(REGISTRY.items())
    ]
    out: dict[str, Any] = queries.clean(
        {
            "strategies": strategies,
            "job": dict(job.state),
            "periods": {"train_end": config.train_end, "validation_end": config.validation_end},
            "gate": config.gate.__dict__,
        }
    )
    return out


@router.post(
    "/api/strategies/validation", dependencies=[Depends(require_dashboard_client)], status_code=202
)
def start_validation(request: Request, body: ValidateRequest) -> dict[str, Any]:
    names = body.names or sorted(REGISTRY)
    unknown = sorted(set(names) - set(REGISTRY))
    if unknown:
        raise HTTPException(404, f"unknown strategies: {unknown}")
    job: ValidationJob = request.app.state.validation_job
    if not job.start(names):
        raise HTTPException(409, "a validation is already running")
    return dict(job.state)


def regime_view(ctx: queries.Context) -> dict[str, Any]:
    bars = load_bars(ctx.lake)
    if bars.empty or "SPY" not in set(bars["symbol"]):
        return {"current": None, "history": []}
    regimes = market_regimes(close_matrix(bars))
    recent = regimes.dropna(subset=["trend"]).tail(252)
    history = [
        {"date": pd.Timestamp(str(idx)).date().isoformat(), **{k: row[k] for k in DIMENSIONS}}
        for idx, row in recent.iterrows()
    ]
    out: dict[str, Any] = queries.clean({"current": current(regimes), "history": history})
    return out


@router.get("/api/market/regime")
def regime(c: Ctx, request: Request) -> dict[str, Any]:
    cache: dict[str, Any] = request.app.state.regime_cache
    if cache.get("value") is not None and time.monotonic() - cache["at"] < REGIME_CACHE_SECONDS:
        value: dict[str, Any] = cache["value"]
        return value
    value = regime_view(c)
    cache |= {"value": value, "at": time.monotonic()}
    return value
