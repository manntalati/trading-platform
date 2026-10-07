"""Brokerage sync from the dashboard: pull the latest Fidelity data (read-only) on demand.

A sync with ``refresh`` first has SnapTrade re-pull the positions from Fidelity, which takes up to
a few minutes and carries a small SnapTrade fee, so it runs in the background: ``POST`` starts
it, ``GET`` reports progress. Only one runs at a time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from tp_api.deps import require_dashboard_client, require_token
from tp_broker.jobs import run_sync
from tp_broker.sources import open_source
from tp_core.config import MissingCredentialsError, Settings
from tp_core.portfolio import read_accounts
from tp_core.storage import Lake

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/broker", dependencies=[Depends(require_token)])


class SyncRequest(BaseModel):
    refresh: bool = False


class BrokerSync:
    """One background sync at a time, and what the last one did."""

    def __init__(self, settings: Settings, on_done: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.on_done = on_done
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False}

    def source_name(self) -> str:
        """The source of the latest synced snapshot (Fidelity through SnapTrade by default)."""
        accounts = read_accounts(Lake(self.settings.data_root))
        if accounts.empty:
            return "snaptrade"
        return str(accounts.sort_values("taken_at")["source"].iloc[-1])

    def start(self, refresh: bool) -> bool:
        with self._lock:
            if self.state.get("running"):
                return False
            name = self.source_name()
            source = open_source(name, self.settings)  # raises without keys: before starting
            self.state = {
                "running": True,
                "refresh": refresh,
                "source": name,
                "started_at": datetime.now(UTC).isoformat(),
            }
        threading.Thread(
            target=self._run, args=(source, refresh), name="broker-sync", daemon=True
        ).start()
        return True

    def _run(self, source: Any, refresh: bool) -> None:
        outcome: dict[str, Any]
        try:
            result = run_sync(
                Lake(self.settings.data_root), source, now=datetime.now(UTC), refresh=refresh
            )
            message = (
                f"{result.accounts} account(s), {result.holdings} holdings, "
                f"{result.activities} transaction(s) fetched"
            )
            if result.refresh:
                message = f"{result.refresh}; {message}"
            outcome = {"ok": True, "message": message}
        except Exception as exc:  # report it on the page; the next sync tries again
            log.exception("brokerage sync failed")
            outcome = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if outcome["ok"] and self.on_done is not None:
            try:
                self.on_done()
            except Exception:
                log.exception("updating the live portfolio after a sync failed")
        with self._lock:
            self.state = (
                self.state
                | outcome
                | {
                    "running": False,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )


def _sync(request: Request) -> BrokerSync:
    syncer: BrokerSync = request.app.state.broker_sync
    return syncer


@router.get("/sync")
def sync_status(request: Request) -> dict[str, Any]:
    return dict(_sync(request).state)


@router.post("/sync", dependencies=[Depends(require_dashboard_client)], status_code=202)
def start_sync(request: Request, body: SyncRequest) -> dict[str, Any]:
    syncer = _sync(request)
    try:
        started = syncer.start(body.refresh)
    except MissingCredentialsError as exc:
        raise HTTPException(status_code=503, detail=f"brokerage not configured: {exc}") from exc
    if not started:
        raise HTTPException(status_code=409, detail="a sync is already running")
    return dict(syncer.state)
