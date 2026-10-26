"""Fitting options ideas to the account: buying power from the brokerage sync (the demo broker's
Individual and ROTH accounts, or synthetic ones), sizing, and the options approval level."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tp_broker.fake import FakeBroker
from tp_broker.jobs import run_sync
from tp_core.portfolio import Classifier
from tp_core.schemas import (
    RAW_BROKER_ACCOUNTS,
    RAW_BROKER_ACCOUNTS_SCHEMA,
    RAW_BROKER_HOLDINGS,
    RAW_BROKER_HOLDINGS_SCHEMA,
    conform,
)
from tp_core.storage import Lake, write_raw
from tp_strategies import playbook
from tp_strategies.budget import Budget, budget_from_lake

NOW = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)


def synced(tmp_path: Path) -> Lake:
    lake = Lake(tmp_path)
    run_sync(lake, FakeBroker(price_of=lambda s: 100.0, as_of=date(2026, 10, 9)), now=NOW)
    return lake


def test_the_account_with_the_most_buying_power_or_the_one_named(tmp_path: Path) -> None:
    lake = synced(tmp_path)
    budget = budget_from_lake(lake)
    assert budget is not None
    assert budget.account == "Individual …1234"
    assert budget.buying_power == 3000.0
    assert budget.account_value > budget.buying_power  # the holdings count toward its value
    assert budget.risk_cap == pytest.approx(0.02 * budget.account_value)
    assert budget.paper is False
    roth = budget_from_lake(lake, account="roth")
    assert roth is not None
    assert (roth.account, roth.buying_power) == ("ROTH IRA …5678", 120.0)
    by_digits = budget_from_lake(lake, account="5678")
    assert by_digits is not None
    assert by_digits.account == roth.account
    assert budget_from_lake(lake, account="joint") is None
    assert budget_from_lake(Lake(tmp_path / "empty")) is None  # never synced


def test_without_a_reported_buying_power_cash_and_money_market_count(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    taken = pd.Timestamp(NOW)
    accounts = pd.DataFrame([{"taken_at": taken, "snapshot_date": NOW.date(), "source": "snaptrade",
                              "account_id": "a1", "account_name": "Brokerage", "cash": 500.0,
                              "total_value": 4500.0, "buying_power": None}])  # fmt: skip
    holdings = pd.DataFrame([
        {"taken_at": taken, "snapshot_date": NOW.date(), "source": "snaptrade", "account_id": "a1",
         "symbol": s, "underlying": s, "kind": kind, "quantity": q, "price": 1.0,
         "market_value": v}
        for s, kind, q, v in (("SPAXX", "mutualfund", 1500, 1500.0), ("XYZ", "stock", 25, 2500.0))
    ])  # fmt: skip
    write_raw(lake, RAW_BROKER_ACCOUNTS, conform(accounts, RAW_BROKER_ACCOUNTS_SCHEMA),
              run_id="r1", partition={}, schema=RAW_BROKER_ACCOUNTS_SCHEMA)  # fmt: skip
    write_raw(lake, RAW_BROKER_HOLDINGS, conform(holdings, RAW_BROKER_HOLDINGS_SCHEMA),
              run_id="r1", partition={}, schema=RAW_BROKER_HOLDINGS_SCHEMA)  # fmt: skip
    classifier = Classifier(sectors={}, funds={"SPAXX": "Cash"})
    budget = budget_from_lake(lake, classifier=classifier)
    assert budget is not None
    assert budget.buying_power == pytest.approx(500.0 + 1500.0)  # cash + the money-market fund
    assert budget.account == "Brokerage"


def test_size_by_buying_power_and_by_the_risk_guideline() -> None:
    b = Budget(buying_power=3000.0, account_value=20_000.0, account="A", risk_per_trade=0.02)
    assert b.risk_cap == 400.0
    small = b.size(150.0, 150.0)
    assert (small["contracts"], small["limited_by"]) == (2, "risk")  # 2 x 150 within $400
    assert small["share_of_account"] == pytest.approx(300 / 20_000)
    assert small["buying_power_left"] == 2700.0
    big = b.size(900.0, 900.0)
    assert (big["contracts"], big["over_guideline"]) == (1, True)  # one, flagged
    loose = Budget(buying_power=1000.0, account_value=100_000.0, account="A", risk_per_trade=0.05)
    assert loose.size(300.0, 300.0)["contracts"] == 3  # the buying power runs out first
    assert loose.size(300.0, 300.0)["limited_by"] == "buying power"
    assert b.size(5000.0, 5000.0)["contracts"] == 0


def test_the_approval_level_decides_the_structures() -> None:
    level2 = Budget(1e6, 1e6, "A", 0.02, options_level=2)
    assert level2.allows("call")
    assert level2.allows("cash-secured put")
    assert not level2.allows("call debit spread")
    assert not level2.allows("iron condor")
    assert Budget(1e6, 1e6, "A", 0.02).allows("iron condor")  # unset: everything


def test_ideas_fit_the_budget_or_say_why_not() -> None:
    from strategies.tests.test_playbook import name, path

    n = name(path(320, 0.002, seed=4, dip=0.03), iv=0.07)  # a pullback with cheap options
    setups = playbook.calibrated_setups(None)
    roomy = Budget(50_000.0, 100_000.0, "Individual …1234", 0.02)
    (idea,) = [i for i in playbook.ideas_for(n, None, setups, roomy) if i["play"]["name"] ==
               "pullback"]  # fmt: skip
    size = idea["pick"]["size"]
    assert size["contracts"] >= 1
    assert size["max_loss"] <= roomy.risk_cap  # the guideline holds when something fits it
    assert size["capital"] == pytest.approx(size["contracts"] * idea["pick"]["capital"])

    missed: list[dict[str, object]] = []
    broke = Budget(20.0, 1000.0, "Individual …1234", 0.02)
    left = playbook.ideas_for(n, None, setups, broke, missed)
    assert "pullback" not in {i["play"]["name"] for i in left}
    (gone,) = [m for m in missed if m["setup"] == "Pullback in an uptrend"]
    assert gone["reason"] == "buying power"
    assert float(str(gone["needs"])) > 20.0

    singles = Budget(50_000.0, 100_000.0, "A", 0.02, options_level=2)
    for i in playbook.ideas_for(n, None, setups, singles):
        assert "spread" not in i["pick"]["structure"]
        assert i["pick"]["structure"] != "iron condor"
