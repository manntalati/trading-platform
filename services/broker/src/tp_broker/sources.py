"""Open a brokerage source by name (for the CLI and the dashboard API)."""

from __future__ import annotations

from datetime import UTC, datetime

from tp_broker.base import BrokerSource
from tp_core.config import Settings
from tp_core.storage import Lake

NAMES = ("snaptrade", "alpaca", "fake")


def open_source(name: str, settings: Settings) -> BrokerSource:
    """``snaptrade`` (Fidelity), ``alpaca`` or ``fake``. Raises MissingCredentialsError without
    the keys it needs."""
    if name == "fake":
        from tp_broker.fake import FakeBroker

        closes = latest_closes(Lake(settings.data_root))
        return FakeBroker(price_of=closes.get, as_of=datetime.now(UTC).date())
    if name == "alpaca":
        from tp_broker.alpaca import AlpacaAccountSource

        return AlpacaAccountSource.from_settings(settings)
    if name == "snaptrade":
        from tp_broker.snaptrade import SnapTradeSource

        return SnapTradeSource.from_settings(settings)
    raise ValueError(f"unknown broker source {name!r}; known: {', '.join(NAMES)}")


def latest_closes(lake: Lake) -> dict[str, float]:
    from tp_core.bars import load_bars

    bars = load_bars(lake)
    if bars.empty:
        return {}
    last = bars.sort_values("session").groupby("symbol")["close"].last()
    return {str(k): float(v) for k, v in last.items()}
