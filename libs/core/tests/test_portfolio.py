from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tp_core import portfolio as pf
from tp_core.schemas import (
    RAW_BROKER_ACCOUNTS,
    RAW_BROKER_ACCOUNTS_SCHEMA,
    RAW_BROKER_ACTIVITIES,
    RAW_BROKER_ACTIVITIES_SCHEMA,
    RAW_BROKER_HOLDINGS,
    RAW_BROKER_HOLDINGS_SCHEMA,
    conform,
)
from tp_core.storage import Lake, write_raw

REPO = Path(__file__).resolve().parents[3]
DAY1 = datetime(2024, 7, 10, 21, 30, tzinfo=UTC)
DAY2 = datetime(2024, 7, 11, 21, 30, tzinfo=UTC)
DAY3 = datetime(2024, 7, 12, 21, 30, tzinfo=UTC)


def _stamp(at: datetime, run: str) -> dict[str, object]:
    return {
        "taken_at": pd.Timestamp(at),
        "snapshot_date": at.date(),
        "source": "test",
        "ingested_at": pd.Timestamp(at),
        "run_id": run,
    }


def store_sync(
    lake: Lake,
    at: datetime,
    accounts: list[dict[str, object]],
    holdings: list[dict[str, object]],
) -> None:
    run = f"{at:%Y%m%d%H%M}-{len(accounts)}"
    stamp = _stamp(at, run)
    acc = conform(pd.DataFrame([{**stamp, **a} for a in accounts]), RAW_BROKER_ACCOUNTS_SCHEMA)
    write_raw(lake, RAW_BROKER_ACCOUNTS, acc, run_id=run, partition={}, schema=None)
    if holdings:
        hold = conform(pd.DataFrame([{**stamp, **h} for h in holdings]), RAW_BROKER_HOLDINGS_SCHEMA)
        write_raw(lake, RAW_BROKER_HOLDINGS, hold, run_id=run, partition={}, schema=None)


def holding(account: str, symbol: str, qty: float, price: float, kind: str = "stock",
            cost: float | None = None) -> dict[str, object]:  # fmt: skip
    return {
        "account_id": account,
        "symbol": symbol,
        "kind": kind,
        "quantity": qty,
        "price": price,
        "market_value": qty * price,
        "cost_basis_per_unit": cost,
    }


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path)


@pytest.fixture
def classifier() -> pf.Classifier:
    return pf.Classifier.load(REPO / "config" / "classifications.toml")


def test_classifier(classifier: pf.Classifier) -> None:
    assert classifier.classify("AAPL", "stock") == pf.Classification(
        "Information Technology", "US Equity"
    )
    assert classifier.classify("FXAIX", "mutualfund").asset_class == "US Equity"
    assert classifier.classify("SPAXX", "mutualfund") == pf.Classification("Cash", "Cash")
    assert classifier.classify("ZZZZ", "etf").sector == "Unclassified fund"
    assert classifier.classify("ZZZZ", "stock").sector == "Unclassified"


def test_latest_snapshot_and_totals(lake: Lake) -> None:
    store_sync(lake, DAY1, [{"account_id": "A", "cash": 100.0}], [holding("A", "AAPL", 10, 100)])
    store_sync(
        lake,
        DAY2,
        [{"account_id": "A", "cash": 50.0}, {"account_id": "B", "cash": 0.0, "total_value": 999}],
        [holding("A", "AAPL", 10, 110), holding("B", "MSFT", 1, 400)],
    )
    snap = pf.latest_snapshot(lake)
    assert sorted(snap.accounts["account_id"]) == ["A", "B"]
    assert len(snap.holdings) == 2
    # A: 10 x 110 + 50 cash; B: broker-reported total wins.
    assert snap.total_value == pytest.approx(1150 + 999)


def test_holdings_table_merges_accounts(lake: Lake, classifier: pf.Classifier) -> None:
    store_sync(
        lake,
        DAY1,
        [{"account_id": "A", "cash": 200.0}, {"account_id": "B", "cash": 0.0}],
        [
            holding("A", "AAPL", 10, 100, cost=80),
            holding("B", "AAPL", 10, 100, cost=60),
            holding("B", "FXAIX", 5, 120, kind="mutualfund"),
        ],
    )
    table = pf.holdings_table(pf.latest_snapshot(lake), classifier)
    aapl = table.set_index("symbol").loc["AAPL"]
    assert aapl["quantity"] == 20
    assert aapl["market_value"] == 2000
    assert aapl["cost_basis_per_unit"] == pytest.approx(70)
    assert aapl["unrealized_pnl"] == pytest.approx(600)
    assert table["weight"].sum() == pytest.approx(1.0)
    assert table.set_index("symbol").loc["CASH", "asset_class"] == "Cash"
    assert pf.exposures(table, "asset_class")["US Equity"] == pytest.approx(2600 / 2800)
    conc = pf.concentration(table)
    assert conc["largest_symbol"] == "AAPL"
    assert conc["largest_weight"] == pytest.approx(2000 / 2800)


def test_value_history_carries_missing_account_forward(lake: Lake) -> None:
    store_sync(
        lake,
        DAY1,
        [{"account_id": "A", "cash": 100.0}, {"account_id": "B", "cash": 50.0}],
        [],
    )
    store_sync(lake, DAY2, [{"account_id": "A", "cash": 110.0}], [])  # B's sync failed
    store_sync(
        lake,
        DAY3,
        [{"account_id": "A", "cash": 120.0}, {"account_id": "B", "cash": 60.0}],
        [],
    )
    history = pf.value_history(lake)
    assert list(history) == [150.0, 160.0, 180.0]


def test_external_flows_and_twr(lake: Lake) -> None:
    rows = [
        ("1", "CONTRIBUTION", date(2024, 7, 11), 100),
        ("2", "WITHDRAWAL", date(2024, 7, 12), -30),
        ("3", "BUY", date(2024, 7, 12), -500),
        ("4", "DIVIDEND", date(2024, 7, 12), 5),
    ]
    acts = pd.DataFrame(rows, columns=["activity_id", "type", "trade_date", "amount"]).assign(
        source="test", ingested_at=pd.Timestamp(DAY3), run_id="r"
    )
    write_raw(
        lake,
        RAW_BROKER_ACTIVITIES,
        conform(acts, RAW_BROKER_ACTIVITIES_SCHEMA),
        run_id="r",
        partition={},
    )
    flows = pf.external_flows(lake)
    assert flows.to_dict() == {pd.Timestamp("2024-07-11"): 100.0, pd.Timestamp("2024-07-12"): -30.0}

    values = pd.Series(
        [1000.0, 1210.0, 1180.0], index=pd.to_datetime(["2024-07-10", "2024-07-11", "2024-07-12"])
    )
    twr = pf.time_weighted_returns(values, flows)
    # Day 2: (1210 - 100) / 1000 - 1 = +11%; day 3: (1180 + 30) / 1210 - 1 = 0%.
    np.testing.assert_allclose(twr, [0.11, 0.0], atol=1e-12)


def test_holdings_backtest_and_benchmarks() -> None:
    idx = pd.bdate_range("2024-01-01", periods=3)
    prices = pd.DataFrame(
        {"AAPL": [100, 110, 99], "SPY": [100, 101, 102], "IEF": [100, 100, 101], "QQQ": [1, 1, 1]},
        index=idx,
    )
    weights = pd.Series({"AAPL": 0.5})  # the other half is unpriced or cash
    bt = pf.holdings_backtest(weights, prices)
    np.testing.assert_allclose(bt, [0.05, -0.05])
    bench = pf.benchmark_returns(prices)
    assert list(bench.columns) == list(pf.BENCHMARKS)
    np.testing.assert_allclose(bench["60/40 (SPY/IEF)"].iloc[1], 0.6 * (102 / 101 - 1) + 0.4 * 0.01)
    sheet = pf.compare(pd.concat([bt, bench], axis=1))
    assert "beta_to_spy" in sheet.index


def test_priced_weights() -> None:
    table = pd.DataFrame({"symbol": ["AAPL", "FXAIX", "CASH"], "weight": [0.5, 0.3, 0.2]})
    assert pf.priced_weights(table, {"AAPL"}).to_dict() == {"AAPL": 0.5}


def test_held_symbols(lake: Lake) -> None:
    store_sync(
        lake,
        DAY1,
        [{"account_id": "A", "cash": 0.0}],
        [
            holding("A", "aapl", 1, 1),
            holding("A", "FXAIX", 1, 1, kind="mutualfund"),
            holding("A", "SPAXX", 1, 1, kind="cash"),
        ],
    )
    assert pf.held_symbols(lake) == ["AAPL"]


def test_empty_lake(lake: Lake, classifier: pf.Classifier) -> None:
    snap = pf.latest_snapshot(lake)
    assert snap.empty
    assert pf.holdings_table(snap, classifier).empty
    assert pf.value_history(lake).empty
    assert pf.external_flows(lake).empty
    assert pf.held_symbols(lake) == []


def test_position_changes_since_an_earlier_day_are_the_trades_not_posted_yet(lake: Lake) -> None:
    acct = [{"account_id": "a", "cash": 1000.0}]
    store_sync(
        lake,
        DAY1,
        acct,
        [holding("a", "AAA", 10, 50, cost=40), holding("a", "BBB", 5, 20, cost=18)],
    )
    # same day, positions not refreshed: no change
    store_sync(
        lake,
        DAY2.replace(hour=14),
        acct,
        [holding("a", "AAA", 10, 50, cost=40), holding("a", "BBB", 5, 20, cost=18)],
    )
    # refreshed after trading: bought 5 more AAA at 52, sold all BBB, opened CCC at 9
    store_sync(lake, DAY2, acct, [holding("a", "AAA", 15, 52, cost=(400 + 5 * 52) / 15),
                                  holding("a", "CCC", 3, 9, cost=9)])  # fmt: skip
    changes = pf.position_changes(lake).set_index("symbol")
    assert changes.loc["AAA", "change"] == pytest.approx(5)
    assert changes.loc["AAA", "price"] == pytest.approx(52)  # from the change in cost
    assert changes.loc["BBB", "change"] == pytest.approx(-5)
    assert np.isnan(changes.loc["BBB", "price"])  # a sale's price isn't in the positions
    assert changes.loc["CCC", "price"] == pytest.approx(9)
    assert (changes["date"] == DAY2.date()).all()
    assert (changes["since"] == pd.Timestamp(DAY1)).all()  # vs the last sync of an earlier day


def test_no_position_changes_without_an_earlier_day(lake: Lake) -> None:
    store_sync(lake, DAY1, [{"account_id": "a", "cash": 1.0}], [holding("a", "AAA", 1, 5)])
    assert pf.position_changes(lake).empty
