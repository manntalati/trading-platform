"""The limit values, loaded from ``config/risk.toml``."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Limits:
    risk_per_trade: float = 0.01
    max_position: float = 0.10
    max_sector: float = 0.30
    max_gross_exposure: float = 1.00
    daily_loss_limit: float = 0.02
    max_price_deviation: float = 0.05
    max_adv_fraction: float = 0.02
    require_current_bar: bool = True
    strategy_drawdown: float = 0.10
    strategy_drawdown_overrides: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, float) and not 0 < value <= 1.0:
                raise ValueError(f"risk limit {f.name} must be in (0, 1], got {value}")
        for name, value in self.strategy_drawdown_overrides.items():
            if not 0 < value <= 1.0:
                raise ValueError(f"drawdown override for {name} must be in (0, 1], got {value}")

    def drawdown_limit(self, strategy: str) -> float:
        return self.strategy_drawdown_overrides.get(strategy, self.strategy_drawdown)

    @classmethod
    def load(cls, path: Path) -> Limits:
        """Read the TOML file; an unknown key is an error (a typo must not silently fall back
        to a default limit)."""
        with path.open("rb") as f:
            raw = tomllib.load(f)
        unknown_sections = set(raw) - {"pre_trade", "strategy_drawdown"}
        if unknown_sections:
            raise ValueError(f"{path}: unknown section(s) {sorted(unknown_sections)}")
        pre_trade: dict[str, Any] = dict(raw.get("pre_trade", {}))
        allowed = {f.name for f in fields(cls)} - {
            "strategy_drawdown",
            "strategy_drawdown_overrides",
        }
        unknown = set(pre_trade) - allowed
        if unknown:
            raise ValueError(f"{path}: unknown [pre_trade] key(s) {sorted(unknown)}")
        drawdown = dict(raw.get("strategy_drawdown", {}))
        overrides = {str(k): float(v) for k, v in dict(drawdown.pop("overrides", {})).items()}
        if set(drawdown) - {"default"}:
            raise ValueError(f"{path}: unknown [strategy_drawdown] key(s) {sorted(drawdown)}")
        kwargs: dict[str, Any] = {
            k: v if isinstance(v, bool) else float(v) for k, v in pre_trade.items()
        }
        if "default" in drawdown:
            kwargs["strategy_drawdown"] = float(drawdown["default"])
        return cls(**kwargs, strategy_drawdown_overrides=overrides)

    def describe(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)} | {
            "strategy_drawdown_overrides": dict(self.strategy_drawdown_overrides)
        }
