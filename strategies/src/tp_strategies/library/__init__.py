"""Strategies that run on the ``tp_trading`` contract (backtest and paper trading).

``build("ma-timing", {"assets": "SPY"})`` turns a name and string parameters (from the command
line or a config file) into a strategy object, converting each value to its field's type.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from typing import Any

from tp_strategies.library.dual_momentum import DualMomentum
from tp_strategies.library.leveraged_momentum import LeveragedMomentum
from tp_strategies.library.ma_timing import MaTiming
from tp_strategies.library.rsi2 import Rsi2Reversion
from tp_strategies.library.ts_momentum import TimeSeriesMomentum
from tp_strategies.library.xs_momentum import CrossSectionalMomentum
from tp_trading.strategy import Strategy

STRATEGIES: tuple[type[Strategy], ...] = (
    MaTiming,
    TimeSeriesMomentum,
    CrossSectionalMomentum,
    DualMomentum,
    Rsi2Reversion,
    LeveragedMomentum,
)
REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in STRATEGIES}


def build(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(REGISTRY)}") from None
    hints = typing.get_type_hints(cls)
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in (params or {}).items():
        field = key.replace("-", "_")
        if field not in fields:
            raise ValueError(f"{name} has no parameter {key!r}; parameters: {sorted(fields)}")
        kwargs[field] = _coerce(value, hints[field], key)
    return cls(**kwargs)


def parameters(cls: type[Strategy]) -> dict[str, Any]:
    """Parameter names and defaults."""
    return {f.name: f.default for f in dataclasses.fields(cls)}


def _coerce(value: Any, hint: Any, key: str) -> Any:
    if not isinstance(value, str):
        return tuple(value) if isinstance(value, list) else value
    origin = typing.get_origin(hint)
    if origin is tuple:
        return tuple(v.strip().upper() for v in value.split(",") if v.strip())
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if value.lower() in ("", "none"):
            return None
        return _coerce(value, args[0], key)
    if hint is bool:
        if value.lower() in ("1", "true", "yes", "on"):
            return True
        if value.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key}: expected true/false, got {value!r}")
    if hint in (int, float):
        try:
            return hint(value)
        except ValueError:
            raise ValueError(f"{key}: expected {hint.__name__}, got {value!r}") from None
    return value
