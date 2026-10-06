"""Helpers for tests and notebooks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from tp_trading.strategy import Context, Strategy


def _nothing(ctx: Context) -> None:
    return None


@dataclass(frozen=True)
class Scripted(Strategy):
    """A strategy whose ``on_bar`` is any function of the context."""

    name: ClassVar[str] = "scripted"
    title: ClassVar[str] = "Scripted (tests)"

    universe: tuple[str, ...] = ()
    act: Callable[[Context], None] = _nothing

    def symbols(self) -> list[str]:
        return list(self.universe)

    def on_bar(self, ctx: Context) -> None:
        self.act(ctx)
