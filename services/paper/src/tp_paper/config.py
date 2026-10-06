"""The paper-trading book from ``config/paper.toml``."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tp_strategies.library import build
from tp_trading.strategy import Strategy

Approval = Literal["manual", "auto"]


@dataclass(frozen=True)
class Sleeve:
    """One strategy's slice of the paper account."""

    strategy: Strategy
    capital: float
    approval: Approval = "manual"
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.strategy.name


@dataclass(frozen=True)
class PaperBook:
    sleeves: tuple[Sleeve, ...]

    @property
    def capital(self) -> float:
        return sum(s.capital for s in self.sleeves)

    def sleeve(self, name: str) -> Sleeve:
        for s in self.sleeves:
            if s.name == name:
                return s
        raise KeyError(f"{name} is not in the paper book")

    @classmethod
    def load(cls, path: Path) -> PaperBook:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        unknown = set(raw) - {"strategies"}
        if unknown:
            raise ValueError(f"{path}: unknown section(s) {sorted(unknown)}")
        sleeves = []
        for name, entry in raw.get("strategies", {}).items():
            extra = set(entry) - {"capital", "approval", "params"}
            if extra:
                raise ValueError(f"{path}: [strategies.{name}] has unknown key(s) {sorted(extra)}")
            capital = float(entry.get("capital", 0))
            if capital <= 0:
                raise ValueError(f"{path}: [strategies.{name}] needs a positive capital")
            approval = entry.get("approval", "manual")
            if approval not in ("manual", "auto"):
                raise ValueError(f"{path}: [strategies.{name}] approval must be manual or auto")
            params = dict(entry.get("params", {}))
            sleeves.append(Sleeve(build(name, params), capital, approval, params))
        return cls(tuple(sleeves))
