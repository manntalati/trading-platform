"""Paper-trading endpoints: status, proposals, and the decisions the dashboard can make.

Reads are open to the dashboard like every other endpoint. Writes (approve, reject, kill) also
require the dashboard's client header and a same-origin request (``require_dashboard_client``),
so a web page you happen to visit can't approve trades on your behalf.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from tp_api.deps import require_dashboard_client, require_token
from tp_api.queries import clean
from tp_core.config import Settings
from tp_paper import jobs
from tp_paper.jobs import Paper, PaperError
from tp_paper.runtime import open_paper
from tp_paper.store import PaperStore

router = APIRouter(prefix="/api/paper", dependencies=[Depends(require_token)])
Write = [Depends(require_dashboard_client)]


class Approve(BaseModel):
    quantity: int | None = Field(default=None, ge=1, description="Approve fewer shares")
    note: str = Field(default="", max_length=500)


class Reject(BaseModel):
    note: str = Field(default="", max_length=500)


class ApproveAll(BaseModel):
    strategy: str | None = None


class Kill(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _paper(request: Request) -> Paper:
    settings = _settings(request)
    try:
        return open_paper(settings, settings.paper_broker, allow_missing_keys=True)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"paper trading not configured: {exc}") from exc


def _store(request: Request) -> PaperStore:
    return PaperStore.under(_settings(request).data_root)


@router.get("")
def status(request: Request) -> dict[str, Any]:
    report: dict[str, Any] = clean(jobs.status(_paper(request), datetime.now(UTC)))
    report["gate"] = {"days": jobs.GATE_DAYS, "trades": jobs.GATE_TRADES}
    return report


@router.get("/proposals")
def proposals(
    request: Request,
    scope: Annotated[Literal["pending", "queued", "recent"], Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """pending: waiting for you; queued: approved, going out at the next open; recent: all."""
    store = _store(request)
    status = {"pending": "pending", "queued": "approved", "recent": None}[scope]
    rows = store.proposals(status=status, limit=limit)
    return [clean(p.to_dict()) for p in rows]


@router.get("/history")
def history(request: Request) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for day in _store(request).sleeve_days():
        out.setdefault(day.strategy, []).append({"session": day.session, "equity": day.equity})
    return out


@router.get("/events")
def events(
    request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 30
) -> list[dict[str, Any]]:
    return _store(request).events(limit)


@router.post("/proposals/{proposal_id}/approve", dependencies=Write)
def approve(request: Request, proposal_id: str, body: Approve) -> dict[str, Any]:
    return _decide(request, proposal_id, approve=True, quantity=body.quantity, note=body.note)


@router.post("/proposals/{proposal_id}/reject", dependencies=Write)
def reject(request: Request, proposal_id: str, body: Reject) -> dict[str, Any]:
    return _decide(request, proposal_id, approve=False, quantity=None, note=body.note)


@router.post("/approve-all", dependencies=Write)
def approve_all(request: Request, body: ApproveAll) -> dict[str, Any]:
    store = _store(request)
    approved = []
    for p in store.proposals(status="pending", strategy=body.strategy):
        try:
            approved.append(jobs.decide(store, p.id, approve=True, by="dashboard").id)
        except PaperError:
            continue  # decided elsewhere in the meantime
    return {"approved": approved}


@router.post("/kill", dependencies=Write)
def kill(request: Request, body: Kill) -> dict[str, Any]:
    try:
        canceled = jobs.kill(_paper(request), body.reason, datetime.now(UTC))
    except PaperError as exc:  # the switch is engaged even when canceling fails
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"kill_switch": True, "canceled": canceled}


def _decide(
    request: Request, proposal_id: str, *, approve: bool, quantity: int | None, note: str
) -> dict[str, Any]:
    store = _store(request)
    try:
        decided = jobs.decide(
            store,
            proposal_id,
            approve=approve,
            by="dashboard",
            quantity=float(quantity) if quantity is not None else None,
            note=note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except PaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    out: dict[str, Any] = clean(decided.to_dict())
    return out
