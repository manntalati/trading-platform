"""Build a ``Paper`` (store, broker, book, risk) from settings: shared by the CLI and the API."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from tp_core.bars import load_bars
from tp_core.config import MissingCredentialsError, Settings, load_universes
from tp_core.portfolio import Classifier, held_symbols
from tp_core.storage import Lake
from tp_paper.bot import BarsRefresher
from tp_paper.broker import (
    AlpacaPaperBroker,
    FakePaperBroker,
    PaperBroker,
    PriceSource,
    UnavailableBroker,
)
from tp_paper.config import PaperBook
from tp_paper.jobs import Paper
from tp_paper.store import PaperStore
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import FileRiskState


def open_paper(
    settings: Settings,
    broker_kind: str,
    *,
    time_in_force: str = "opg",
    allow_missing_keys: bool = False,
) -> Paper:
    """``allow_missing_keys`` swaps in ``UnavailableBroker`` instead of failing, for read-mostly
    callers like the dashboard."""
    lake = Lake(settings.data_root)
    store = PaperStore.under(settings.data_root)
    broker: PaperBroker
    if broker_kind == "fake":
        broker = FakePaperBroker(
            settings.data_root / "state" / "fake_broker.json", lake_prices(lake)
        )
    else:
        try:
            key, secret = settings.require_alpaca_keys()
        except MissingCredentialsError as exc:
            if not allow_missing_keys:
                raise
            broker = UnavailableBroker(str(exc))
        else:
            broker = AlpacaPaperBroker(key, secret)
    classifier = Classifier.load(settings.classifications_file)
    risk = RiskManager(
        Limits.load(settings.risk_file),
        classifier,
        FileRiskState.under(settings.data_root),
        enforce_drawdown=True,
    )
    return Paper(
        store=store,
        broker=broker,
        book=PaperBook.load(settings.paper_file),
        lake=lake,
        risk=risk,
        classifier=classifier,
        time_in_force="day" if time_in_force == "day" else "opg",
    )


def lake_prices(lake: Lake) -> PriceSource:
    """Raw open and close per (symbol, session) from the lake, for the simulated broker."""
    cache: dict[str, dict[date, tuple[float, float]]] = {}

    def prices(symbol: str, day: date) -> tuple[float, float] | None:
        if symbol not in cache:
            try:
                bars = load_bars(lake, [symbol])
            except KeyError:
                bars = pd.DataFrame(columns=["session", "open", "close"])
            cache[symbol] = {
                s: (float(o), float(c))
                for s, o, c in zip(bars["session"], bars["open"], bars["close"], strict=True)
            }
        return cache[symbol].get(day)

    return prices


def bars_refresher(settings: Settings, source: str, book: PaperBook) -> BarsRefresher:
    """The daily bars job for the bot: the universe, every strategy's symbols and anything
    held, from Alpaca (or the synthetic source)."""

    def refresh(now: datetime) -> str:
        from tp_ingest.jobs.bars import run_daily
        from tp_ingest.sources.base import MarketDataSource

        lake = Lake(settings.data_root)
        symbols = set(load_universes(settings.universes_file).bars)
        symbols |= {s for sleeve in book.sleeves for s in sleeve.strategy.symbols()}
        symbols |= set(held_symbols(lake))
        market: MarketDataSource
        if source == "fake":
            from tp_ingest.sources.fake import FakeSource

            market = FakeSource()
        else:
            from tp_ingest.sources.alpaca import AlpacaSource

            market = AlpacaSource.from_settings(settings)
        result = run_daily(lake, market, sorted(symbols), now=now)
        return f"bars refreshed through {result.end}: {result.report.summary()}"

    return refresh
