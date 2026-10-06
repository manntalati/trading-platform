"""Risk state that must outlive a process: kill switch, disabled strategies, equity peaks.

Backtests use ``MemoryRiskState``. Paper trading uses ``FileRiskState``, a small JSON file under
the data root that ``tp-risk`` and ``tp-paper`` read and write (atomically).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from tp_core.storage import write_json


@dataclass(frozen=True)
class Switch:
    reason: str
    at: str  # ISO timestamp (or session) when it was set


class RiskState(Protocol):
    def kill_switch(self) -> Switch | None: ...

    def set_kill_switch(self, reason: str | None) -> None: ...

    def disabled(self, strategy: str) -> Switch | None: ...

    def set_disabled(self, strategy: str, reason: str | None) -> None: ...

    def peak(self, strategy: str) -> float | None: ...

    def set_peak(self, strategy: str, equity: float | None) -> None: ...


class MemoryRiskState:
    def __init__(self) -> None:
        self._kill: Switch | None = None
        self._disabled: dict[str, Switch] = {}
        self._peaks: dict[str, float] = {}

    def kill_switch(self) -> Switch | None:
        return self._kill

    def set_kill_switch(self, reason: str | None) -> None:
        self._kill = Switch(reason, _now()) if reason else None

    def disabled(self, strategy: str) -> Switch | None:
        return self._disabled.get(strategy)

    def set_disabled(self, strategy: str, reason: str | None) -> None:
        if reason:
            self._disabled[strategy] = Switch(reason, _now())
        else:
            self._disabled.pop(strategy, None)

    def peak(self, strategy: str) -> float | None:
        return self._peaks.get(strategy)

    def set_peak(self, strategy: str, equity: float | None) -> None:
        if equity is None:
            self._peaks.pop(strategy, None)
        else:
            self._peaks[strategy] = equity


class FileRiskState:
    """JSON-backed state; every change is written immediately."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def under(cls, data_root: Path) -> FileRiskState:
        return cls(data_root / "state" / "risk.json")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"kill_switch": None, "strategies": {}}
        data: dict[str, Any] = json.loads(self.path.read_text())
        data.setdefault("kill_switch", None)
        data.setdefault("strategies", {})
        return data

    def _strategy(self, data: dict[str, Any], strategy: str) -> dict[str, Any]:
        entry: dict[str, Any] = data["strategies"].setdefault(
            strategy, {"disabled": None, "peak": None}
        )
        return entry

    def kill_switch(self) -> Switch | None:
        raw = self._read()["kill_switch"]
        return Switch(**raw) if raw else None

    def set_kill_switch(self, reason: str | None) -> None:
        data = self._read()
        data["kill_switch"] = {"reason": reason, "at": _now()} if reason else None
        write_json(data, self.path)

    def disabled(self, strategy: str) -> Switch | None:
        raw = self._read()["strategies"].get(strategy, {}).get("disabled")
        return Switch(**raw) if raw else None

    def set_disabled(self, strategy: str, reason: str | None) -> None:
        data = self._read()
        self._strategy(data, strategy)["disabled"] = (
            {"reason": reason, "at": _now()} if reason else None
        )
        write_json(data, self.path)

    def peak(self, strategy: str) -> float | None:
        value = self._read()["strategies"].get(strategy, {}).get("peak")
        return float(value) if value is not None else None

    def set_peak(self, strategy: str, equity: float | None) -> None:
        data = self._read()
        self._strategy(data, strategy)["peak"] = equity
        write_json(data, self.path)

    def snapshot(self) -> dict[str, Any]:
        return self._read()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
