"""The account's live value: moved from the broker's own prices at the sync, and today's P&L from
the last close before today, however long the server has been running (synthetic demo lake)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tp_api.app import QuoteMode, build_hub, update_hub_portfolio
from tp_api.live import LiveHub, Position, Tick
from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.bars import load_bars
from tp_core.config import Settings
from tp_core.portfolio import latest_snapshot
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource

REPO = Path(__file__).resolve().parents[3]
LAST_BARS = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)  # the lake's bars end on Friday 12 July
SYNCED = datetime(2024, 7, 16, 18, 0, tzinfo=UTC)  # the brokerage sync is the next Tuesday's
SYMBOLS = ["SPY", "AAPL", "NVDA", "MSFT"]


def settings_for(root: Path) -> Settings:
    return Settings(
        data_root=root,
        universes_file=REPO / "config" / "universes.toml",
        classifications_file=REPO / "config" / "classifications.toml",
        dashboard_token=SecretStr("t"),
    )


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    lake = Lake(tmp_path)
    run_backfill(lake, FakeSource(), SYMBOLS, now=LAST_BARS, years=1)
    closes = load_bars(lake).groupby("symbol")["close"].last().to_dict()

    def later(symbol: str) -> float | None:  # prices have moved 10% since the last bars
        return closes[symbol] * 1.1 if symbol in closes else None

    run_sync(lake, FakeBroker(price_of=later, as_of=SYNCED.date()), now=SYNCED)
    return tmp_path


def test_the_value_moves_from_the_broker_s_prices_not_from_old_closes(lake_root: Path) -> None:
    hub = build_hub(settings_for(lake_root), QuoteMode.fake)
    assert hub is not None
    total = latest_snapshot(Lake(lake_root)).total_value
    assert hub.portfolio_value()["value"] == pytest.approx(total)  # nothing streamed: the broker's

    aapl = next(p for p in hub.positions if p.symbol == "AAPL")
    assert aapl.prev_close is not None
    assert aapl.mark == pytest.approx(aapl.prev_close * 1.1)  # the sync's price, not the bars'
    hub.latest["AAPL"] = Tick("AAPL", aapl.mark + 1.0, SYNCED)
    live = hub.portfolio_value()
    # up $1 a share since the sync: the account is up exactly that, not the 10% since the bars too
    assert live["value"] == pytest.approx(total + aapl.quantity)
    # today's P&L: each holding from the last close (the rest at their price at the sync)
    moved = sum(p.quantity * (p.mark - (p.prev_close or p.mark)) for p in hub.positions)
    assert live["day_pnl"] == pytest.approx(moved + aapl.quantity)


def test_today_s_reference_closes_follow_the_day(lake_root: Path) -> None:
    settings = settings_for(lake_root)
    hub = build_hub(settings, QuoteMode.fake)
    assert hub is not None
    app = FastAPI()
    app.state.hub, app.state.settings = hub, settings
    bars = load_bars(Lake(lake_root), ["AAPL"]).set_index("session")["close"]

    friday = datetime(2024, 7, 12, 15, 0, tzinfo=UTC)  # during Friday's session
    update_hub_portfolio(app, friday)
    assert hub.closes_day == date(2024, 7, 12)
    assert hub.prev_close["AAPL"] == pytest.approx(bars.iloc[-2])  # Thursday's, not Friday's own
    aapl = next(p for p in hub.positions if p.symbol == "AAPL")
    assert aapl.prev_close == pytest.approx(bars.iloc[-2])

    hub.prev_close["AAPL"] = 1.0
    update_hub_portfolio(app, friday + timedelta(minutes=10))
    assert hub.prev_close["AAPL"] == 1.0  # read a moment ago: not again

    update_hub_portfolio(app, datetime(2024, 7, 15, 14, 0, tzinfo=UTC))  # Monday
    assert hub.closes_day == date(2024, 7, 15)
    assert hub.prev_close["AAPL"] == pytest.approx(bars.iloc[-1])  # Friday's close now


def test_a_holding_without_a_close_still_counts_in_the_value() -> None:
    class Silent:
        name = "fake"
        error: str | None = None

        async def stream(self, symbols: list[str]):  # type: ignore[no-untyped-def]  # pragma: no cover
            yield Tick("XYZ", 1.0, LAST_BARS)

    hub = LiveHub(Silent(), ["XYZ"], prev_close={}, positions=[Position("XYZ", 10, 50.0)],
                  static_value=1000.0)  # fmt: skip
    hub.latest["XYZ"] = Tick("XYZ", 52.0, SYNCED)
    assert hub.portfolio_value() == {"value": 1520.0, "day_pnl": 0.0, "day_pnl_pct": 0.0}
