"""The decision journal: the gates, sizing, closing a trade, the review, and ``tp-journal``."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tp_core.storage import Lake
from tp_risk.state import FileRiskState, Switch
from tp_strategies import journal as j
from tp_strategies import journal_cli
from tp_strategies.journal_cli import app

TODAY = date(2026, 11, 2)
AT = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # 10:00 in New York
BUDGET = {"account_value": 10_000.0, "buying_power": 2_000.0, "risk_per_trade": 0.02}
runner = CliRunner()


def spread(**pick: Any) -> dict[str, Any]:
    """A swing call debit spread: one contract pays 1.50 to risk $150."""
    return {
        "symbol": "XYZ",
        "list": "swing",
        "kind": "swing",
        "new": True,
        "play": {"name": "pullback"},
        "risks": [],
        "vol": {"event": None},
        "pick": {
            "kind": "call debit spread",
            "capital": 150.0,
            "max_loss": 150.0,
            "entry": {"limit": 1.5, "side": "debit", "valid_until": "2026-11-04"},
            "exit": {"out_by": "2026-11-20"},
            "size": {"contracts": 1},
            "model": {"expected_return": 0.12},
        }
        | pick,
    }


def shares() -> dict[str, Any]:
    return {
        "symbol": "KO",
        "list": "stocks",
        "kind": "stocks",
        "play": {"name": "trend"},
        "pick": {
            "kind": "shares",
            "price": 60.0,
            "risk_per_share": 3.0,
            "entry": {"limit": 60.0, "side": "debit", "valid_until": "2026-11-13"},
            "size": {"shares": 20},
            "model": {"expected_return": 0.08},
        },
    }


def a_run(*ideas: dict[str, Any], budget: dict[str, Any] | None = None) -> dict[str, Any]:
    research: dict[str, Any] = {"budget": BUDGET if budget is None else budget}
    for idea in ideas:
        research.setdefault(idea["list"], []).append(idea)
    return {"run_id": "20261102T143000Z-abcd1234", "as_of": "2026-11-02T14:30:00+00:00",
            "research": research}  # fmt: skip


def verdicts(checks: list[j.Check]) -> dict[str, str]:
    return {c.gate: c.verdict for c in checks}


def check(
    idea: dict[str, Any],
    *,
    run: dict[str, Any] | None = None,
    units: float | None = None,
    entries: list[dict[str, Any]] | None = None,
    rules: j.Rules | None = None,
    kill: Switch | None = None,
) -> list[j.Check]:
    return j.gates(
        idea,
        run or a_run(idea),
        sizing=j.Sizing.of(idea["pick"], units, None) if idea.get("pick") else None,
        entries=entries or [],
        rules=rules or j.Rules(),
        today=TODAY,
        kill=kill,
    )


def took(n: int, at: datetime, pnl: float | None = None) -> dict[str, Any]:
    closed = {"at": at.isoformat(), "pnl": pnl} if pnl is not None else None
    return {"id": n, "at": at.isoformat(), "decision": "took", "closed": closed}


def test_a_plain_idea_passes_every_gate() -> None:
    checks = check(spread())
    assert j.blocked(checks) == []
    assert verdicts(checks) == {
        "kill switch": "ok", "fresh": "ok", "risk": "ok", "position": "ok",
        "buying power": "ok", "pace": "ok", "losing streak": "ok", "edge": "ok",
    }  # fmt: skip


def test_what_blocks() -> None:
    assert (
        verdicts(check(spread(), kill=Switch("drawdown", "2026-11-01")))["kill switch"] == "block"
    )
    stale = spread(entry={"limit": 1.5, "side": "debit", "valid_until": "2026-11-01"})
    assert verdicts(check(stale))["fresh"] == "block"
    two = verdicts(check(spread(), units=2))  # $300 at risk, 3% of the account
    assert two["risk"] == "block"
    assert two["position"] == "ok"
    big = verdicts(check(spread(), units=10))  # $1,500 tied up, 15%; under the buying power
    assert big["position"] == "block"
    assert big["buying power"] == "ok"
    poor = a_run(spread(), budget=BUDGET | {"buying_power": 100.0})
    assert verdicts(check(spread(), run=poor))["buying power"] == "block"
    credit = spread(entry={"limit": 0.5, "side": "credit", "valid_until": "2026-11-04"})
    assert verdicts(check(credit, run=poor))["buying power"] == "block"  # the collateral
    watch = {"symbol": "XYZ", "list": "swing", "pick": None}
    assert [c.gate for c in check(watch)] == ["kill switch", "contract"]


def test_pace_and_a_losing_streak() -> None:
    rules = j.Rules(max_per_day=2, loss_streak=2)
    today = [took(1, AT - timedelta(hours=1)), took(2, AT - timedelta(minutes=30))]
    assert verdicts(check(spread(), entries=today, rules=rules))["pace"] == "block"
    yesterday = [took(n, AT - timedelta(days=1)) for n in (1, 2)]
    assert verdicts(check(spread(), entries=yesterday, rules=rules))["pace"] == "ok"

    lost = [took(1, AT - timedelta(days=1), -50), took(2, AT - timedelta(hours=2), -80)]
    assert verdicts(check(spread(), entries=lost, rules=rules))["losing streak"] == "block"
    won_last = [*lost, took(3, AT - timedelta(hours=1), 20)]
    assert verdicts(check(spread(), entries=won_last, rules=rules))["losing streak"] == "ok"
    stale_streak = [took(n, AT - timedelta(days=2), -50) for n in (1, 2)]  # a new day: go again
    assert verdicts(check(spread(), entries=stale_streak, rules=rules))["losing streak"] == "ok"


def test_what_warns() -> None:
    idea = spread(model={"expected_return": -0.03})
    idea["vol"] = {"event": {"by": "2026-11-14", "after": "2026-11-07"}}
    idea["risks"] = ["News against it."]
    checks = check(idea)
    assert j.blocked(checks) == []
    assert {g: v for g, v in verdicts(checks).items() if v == "warn"} == {
        "edge": "warn",
        "event": "warn",
        "flags": "warn",
    }
    after_exit = spread()
    after_exit["vol"] = {"event": {"by": "2026-12-18", "after": "2026-11-27"}}  # out by 11-20
    assert "event" not in verdicts(check(after_exit))
    no_account = verdicts(check(spread(), run=a_run(spread(), budget={})))
    assert no_account["risk"] == "warn"


def test_the_risk_gate_says_when_it_cant_tell() -> None:
    unknown = spread(max_loss=None)
    assert verdicts(check(unknown))["risk"] == "warn"
    no_stop = shares()
    no_stop["pick"].pop("risk_per_share")
    assert verdicts(check(no_stop, units=5))["risk"] == "warn"
    budget = {k: v for k, v in BUDGET.items() if k != "risk_per_trade"}
    no_guideline = a_run(spread(), budget=budget)
    assert verdicts(check(spread(), run=no_guideline))["risk"] == "warn"
    short_call = spread(
        kind=None, max_loss=None, short={"contract": "XYZ C", "side": "sell", "right": "C"},
        entry={"limit": 1.0, "side": "credit", "valid_until": "2026-11-04"},
    )  # fmt: skip
    assert verdicts(check(short_call))["risk"] == "block"
    covered = spread(
        kind=None,
        legs=[{"contract": "XYZ C1", "side": "buy", "right": "C"},
              {"contract": "XYZ C2", "side": "sell", "right": "C"}],
    )  # fmt: skip
    assert verdicts(check(covered))["risk"] == "ok"


def test_a_worse_fill_moves_the_risk() -> None:
    idea = spread(max_loss=180.0, capital=180.0)  # $180 at the 1.80 limit: 1.8% of the account
    idea["pick"]["entry"] = {"limit": 1.8, "side": "debit", "valid_until": "2026-11-04"}
    at_limit = j.Sizing.of(idea["pick"], 1, None)
    assert (at_limit.max_loss, at_limit.capital) == (180.0, 180.0)
    filled = j.Sizing.of(idea["pick"], 1, 2.3)  # paid 50 cents more a share
    assert filled.entry == 2.3
    assert filled.max_loss == pytest.approx(230.0)
    assert filled.capital == pytest.approx(230.0)
    gates = j.gates(idea, a_run(idea), sizing=filled, entries=[], rules=j.Rules(), today=TODAY,
                    kill=None)  # fmt: skip
    assert verdicts(gates)["risk"] == "block"  # 2.3% over the 2% guideline

    credit = spread(max_loss=450.0, capital=450.0)  # a $5 wide put spread sold for 0.50
    credit["pick"]["entry"] = {"limit": 0.5, "side": "credit", "valid_until": "2026-11-04"}
    better = j.Sizing.of(credit["pick"], 1, 0.6)  # took ten cents more
    assert (better.max_loss, better.capital) == (pytest.approx(440.0), pytest.approx(440.0))
    stock = j.Sizing.of(shares()["pick"], 20, 61.0)  # a dollar over the 60 limit, the same stop
    assert (stock.max_loss, stock.capital) == (pytest.approx(80.0), pytest.approx(1220.0))


def test_shares_are_sized_per_share() -> None:
    s = j.Sizing.of(shares()["pick"], None, None)
    assert (s.units, s.unit, s.max_loss, s.capital, s.multiplier) == (20, "shares", 60, 1200, 1)
    assert verdicts(check(shares()))["position"] == "block"  # $1,200 is 12% of $10,000
    assert j.blocked(check(shares(), units=10)) == []


def test_find_an_idea() -> None:
    run = a_run(spread(), shares() | {"symbol": "XYZ"})
    with pytest.raises(LookupError, match="more than one list"):
        j.find_idea(run, "xyz")
    assert j.find_idea(run, "xyz", "stocks")["pick"]["kind"] == "shares"
    with pytest.raises(LookupError, match="isn't on any list"):
        j.find_idea(run, "NOPE")


def test_close_and_review() -> None:
    idea = spread()
    run = a_run(idea)
    sizing = j.Sizing.of(idea["pick"], 2, 1.4)
    e = j.entry("took", idea, run, at=AT, entries=[], sizing=sizing, checks=check(idea, units=2),
                override="sized up on purpose")  # fmt: skip
    assert (e["id"], e["structure"], e["setup"]) == (1, "call debit spread", "pullback")
    assert e["max_loss"] == pytest.approx(280.0)  # filled at 1.40, under the 1.50 limit
    j.close(e, 2.0, at=AT + timedelta(days=3))
    assert e["closed"]["pnl"] == pytest.approx(120.0)  # (2.00 - 1.40) x 2 x 100
    assert e["closed"]["r"] == pytest.approx(120 / 280)
    with pytest.raises(ValueError, match="already closed"):
        j.close(e, 2.0, at=AT)

    credit = spread(entry={"limit": 0.5, "side": "credit", "valid_until": "2026-11-04"})
    one = j.Sizing.of(credit["pick"], 1, None)
    c = j.entry("took", credit, run, at=AT, entries=[e], sizing=one, checks=[])
    j.close(c, 0.8, at=AT + timedelta(days=1))  # bought back for more than it was sold
    assert c["id"] == 2
    assert c["closed"]["pnl"] == pytest.approx(-30.0)
    passed = j.entry("passed", spread(), run, at=AT, entries=[e, c], sizing=None, checks=[])
    with pytest.raises(ValueError, match="passed on"):
        j.close(passed, 1.0, at=AT)

    r = j.review([e, c, passed])
    assert (r["took"], r["passed"], r["open"]) == (2, 1, 0)
    assert r["all"]["win_rate"] == 0.5
    assert r["all"]["pnl"] == pytest.approx(90.0)
    assert r["overrode a gate"]["closed"] == 1
    assert r["followed the gates"]["pnl"] == pytest.approx(-30.0)
    assert list(r["by_setup"]) == ["pullback"]


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lake:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TP_RISK_FILE", str(tmp_path / "no-risk.toml"))
    monkeypatch.delenv("TP_JOURNAL_FILE", raising=False)
    monkeypatch.setattr(journal_cli, "_now", lambda: AT)
    lake = Lake(tmp_path / "data")
    run = a_run(spread(), shares())
    path = lake.reports_dir("recommendations") / f"{run['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run))
    return lake


def test_the_cli_takes_passes_closes_and_reviews(lake: Lake) -> None:
    out = runner.invoke(app, ["check", "xyz"])
    assert out.exit_code == 0, out.output
    assert "✓ risk" in out.output

    blocked = runner.invoke(app, ["take", "KO"])  # 20 shares tie up 12% of the account
    assert blocked.exit_code == 1
    assert "not recorded: position blocks it" in blocked.output
    assert not (lake.root / "journal" / "journal.json").exists()

    out = runner.invoke(app, ["take", "KO", "--override", "a core holding", "--price", "59.5"])
    assert out.exit_code == 0, out.output
    assert "#1 took KO" in out.output
    assert runner.invoke(app, ["take", "XYZ", "--units", "1"]).exit_code == 0
    assert runner.invoke(app, ["pass", "XYZ", "--note", "earnings soon"]).exit_code == 0

    out = runner.invoke(app, ["close", "1", "62"])
    assert out.exit_code == 0, out.output
    assert "$+50.00" in out.output  # (62 - 59.50) x 20
    assert runner.invoke(app, ["close", "3", "1"]).exit_code == 1  # a pass: nothing to close

    out = runner.invoke(app, ["list"])
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert lines[0].startswith("#3")
    assert "earnings soon" in lines[0]
    assert "open, out by 2026-11-20" in lines[1]
    assert "past its exit date" not in lines[1]
    assert "override: a core holding" in lines[2]

    out = runner.invoke(app, ["review"])
    assert out.exit_code == 0, out.output
    assert "took 2 (1 open), passed on 1" in out.output
    assert "overrode a gate" in out.output


def test_the_cli_respects_the_kill_switch(lake: Lake) -> None:
    FileRiskState.under(lake.root).set_kill_switch("cooling off")
    out = runner.invoke(app, ["take", "XYZ"])
    assert out.exit_code == 1
    assert "✗ kill switch" in out.output
    assert "cooling off" in out.output


def test_the_cli_without_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path / "empty"))
    out = runner.invoke(app, ["check", "XYZ"])
    assert out.exit_code == 1
    assert "tp-recs run" in out.output
