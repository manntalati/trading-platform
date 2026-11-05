"""Recorded trades counted until the brokerage sync shows them (synthetic XYZ, ABC accounts)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from tp_core.fills import (
    FillState,
    assess,
    describe,
    instrument_symbol,
    load_fills,
    new_fill,
    overlay,
    pending_cash,
    save_fills,
    settles,
    with_fills,
)
from tp_core.occ import format_occ
from tp_core.portfolio import PortfolioSnapshot, account_cash
from tp_core.storage import Lake

MON = datetime(2026, 11, 2, 15, 30, tzinfo=UTC)  # Monday 10:30 New York


def snap(at: datetime, xyz: float, power: float | None, cash: float = 1000.0) -> PortfolioSnapshot:
    """One account holding ``xyz`` shares of XYZ (at $50) and 10 of ABC, synced at ``at``."""
    stamp = pd.Timestamp(at)
    accounts = pd.DataFrame([{
        "source": "snaptrade", "account_id": "a1", "account_name": "Individual",
        "account_number_masked": "…1234", "cash": cash, "total_value": cash + 50 * xyz + 200,
        "buying_power": power, "taken_at": stamp,
    }])  # fmt: skip
    rows = [("XYZ", xyz, 50.0), ("ABC", 10.0, 20.0)]
    holdings = pd.DataFrame([
        {"source": "snaptrade", "account_id": "a1", "taken_at": stamp, "symbol": s,
         "underlying": s, "kind": "stock", "quantity": q, "price": p, "market_value": q * p}
        for s, q, p in rows if q
    ])  # fmt: skip
    return PortfolioSnapshot(accounts, holdings)


def sell_xyz(base: PortfolioSnapshot, qty: float = 40.0, at: datetime = MON) -> dict[str, object]:
    return new_fill([], base, source="snaptrade", account_id="a1",
                    legs=[{"symbol": "xyz", "quantity": -qty, "price": 52.0}], at=at)  # fmt: skip


def only(states: list[FillState]) -> FillState:
    (s,) = states
    return s


def test_a_sale_counts_until_the_sync_shows_it() -> None:
    before = snap(MON - timedelta(hours=1), xyz=40, power=1000.0)
    fill = sell_xyz(before)
    assert fill["cash"] == pytest.approx(40 * 52.0)  # received
    assert fill["baseline"] == {"XYZ": 40.0}
    assert fill["settles"] == "2026-11-03"  # T+1

    s = only(assess([fill], before, MON + timedelta(minutes=5)))
    assert (s.status, s.counts_holding, s.counts_cash) == ("pending", True, True)
    seen = overlay(before, [s])
    assert "XYZ" not in set(seen.holdings["symbol"])  # all of it sold
    assert float(seen.accounts["buying_power"].iloc[0]) == pytest.approx(1000 + 2080)
    assert pending_cash([s], "snaptrade", "a1") == (pytest.approx(2080.0), 1)
    # the value doesn't move: the stock became cash
    cash = account_cash(seen.accounts, seen.holdings)
    assert float(cash["total_value"].iloc[0]) == pytest.approx(1000 + 50 * 40 + 200)

    after = snap(MON + timedelta(hours=1), xyz=0, power=3080.0, cash=3080.0)
    done = only(assess([fill], after, MON + timedelta(hours=1)))
    assert done.status == "synced"  # the shares are gone and the buying power moved
    assert overlay(after, [done]).accounts.equals(after.accounts)


def test_proceeds_count_until_they_settle_if_the_broker_leaves_them_out() -> None:
    before = snap(MON - timedelta(hours=1), xyz=40, power=1000.0)
    fill = sell_xyz(before)
    shown = snap(MON + timedelta(hours=1), xyz=0, power=1000.0)  # the sale, not its money
    s = only(assess([fill], shown, MON + timedelta(hours=1)))
    assert (s.status, s.counts_holding, s.counts_cash) == ("settling", False, True)
    assert float(overlay(shown, [s]).accounts["buying_power"].iloc[0]) == pytest.approx(3080.0)
    settled = snap(datetime(2026, 11, 3, 14, 0, tzinfo=UTC), xyz=0, power=3080.0)
    assert only(assess([fill], settled, datetime(2026, 11, 3, 14, 0, tzinfo=UTC))).status == (
        "synced"
    )


def test_a_purchase_adds_the_holding_and_spends_the_cash() -> None:
    before = snap(MON - timedelta(hours=1), xyz=40, power=5000.0)
    call = format_occ("ABC", date(2026, 12, 18), "C", 25.0)
    fills = [
        new_fill([], before, source="snaptrade", account_id="a1",
                 legs=[{"symbol": call, "quantity": 2, "price": 1.5}], at=MON),
        new_fill([], before, source="snaptrade", account_id="a1",
                 legs=[{"symbol": "XYZ", "quantity": 10, "price": 51.0}], at=MON),
    ]  # fmt: skip
    fills[1]["id"] = 2
    states = assess(fills, before, MON)
    seen = overlay(before, states)
    added = seen.holdings.set_index("symbol")
    assert added.loc[call, "kind"] == "option"
    assert added.loc[call, "underlying"] == "ABC"
    assert added.loc[call, "market_value"] == pytest.approx(2 * 1.5 * 100)
    assert added.loc["XYZ", "quantity"] == 50
    assert float(seen.accounts["buying_power"].iloc[0]) == pytest.approx(5000 - 300 - 510)


def test_trades_in_one_stock_are_matched_oldest_first() -> None:
    before = snap(MON - timedelta(hours=1), xyz=100, power=1000.0)
    first = sell_xyz(before, 40)
    second = sell_xyz(before, 30, at=MON + timedelta(minutes=10))
    second["id"] = 2
    partly = snap(MON + timedelta(hours=1), xyz=60, power=1000 + 2080)  # only the first shows
    states = assess([first, second], partly, MON + timedelta(hours=1))
    assert [s.status for s in states] == ["synced", "pending"]
    assert float(overlay(partly, states).holdings.set_index("symbol").loc["XYZ", "quantity"]) == 30


def test_a_fill_the_sync_never_shows_goes_stale() -> None:
    before = snap(MON - timedelta(hours=1), xyz=40, power=1000.0)
    fill = sell_xyz(before)
    later = MON + timedelta(days=9)
    s = only(assess([fill], before, later))
    assert s.stale
    assert (s.status, s.counts_cash, s.counts_holding) == ("stale", False, False)
    assert overlay(before, [s]).accounts.equals(before.accounts)


def test_settlement_skips_the_weekend() -> None:
    friday = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
    assert settles(friday) == date(2026, 11, 9)


def test_stored_under_the_data_root(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    base = snap(MON, xyz=40, power=1000.0)
    assert load_fills(lake) == []
    save_fills(lake, [sell_xyz(base)])
    assert (tmp_path / "journal" / "fills.json").exists()
    seen, states = with_fills(lake, base, MON)
    assert len(states) == 1
    assert "XYZ" not in set(seen.holdings["symbol"])
    with pytest.raises(ValueError, match="a leg with a quantity"):
        new_fill([], base, source="snaptrade", account_id="a1",
                 legs=[{"symbol": "XYZ", "quantity": 0, "price": 1}], at=MON)  # fmt: skip


def test_a_call_or_put_by_its_expiration_and_strike() -> None:
    call = instrument_symbol("xyz", "call", date(2026, 12, 18), 25)
    assert call == "XYZ261218C00025000"
    assert instrument_symbol("XYZ261218C00025000") == call  # an OCC symbol stays one
    assert instrument_symbol(" xyz ") == "XYZ"
    assert instrument_symbol("abc", "put", date(2026, 12, 31), 7.5) == "ABC261231P00007500"
    with pytest.raises(ValueError, match="needs its expiration and strike"):
        instrument_symbol("XYZ", "call")
    assert describe(call) == {"kind": "call", "label": "XYZ Dec 18 '26 $25 Call",
                              "unit": "contracts", "underlying": "XYZ"}  # fmt: skip
    assert describe("XYZ") == {"kind": "stock", "label": "XYZ shares", "unit": "shares",
                               "underlying": "XYZ"}  # fmt: skip
