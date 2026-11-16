"""Blocked buys and their shadow trades (tp_paper.shadow, `tp-paper blocked`)."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import pandas as pd
import pytest
from typer.testing import CliRunner

from tp_core.bars import load_bars
from tp_core.calendar import NEW_YORK, sessions
from tp_paper import cli, jobs
from tp_paper.config import PaperBook, Sleeve
from tp_paper.ledger import PaperData
from tp_paper.shadow import SHADOW_COST_BPS, blocked_buys, shadow_entries, summarize
from tp_paper.store import FillRecord, Proposal
from tp_strategies.library import build
from tp_strategies.library.ibs import ibs

COST = SHADOW_COST_BPS / 10_000
DAYS = sessions(date(2024, 7, 1), date(2024, 7, 12))  # 9 sessions (no 4 July)
# QQQ ranges 100-110 every day; a close at 101 is IBS 0.1 (hold), at 105 IBS 0.5 (out).
LOW_IBS = {0, 1, 4, 7}
DAILY_LOSS = {
    "check": "daily_loss",
    "passed": False,
    "detail": "equity -2.50% since the prior close (limit -2%)",
}


def _bars(days: list[date]) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(days):
        for symbol, base in (("QQQ", 100.0), ("BIL", 91.0)):
            close = (101.0 if i in LOW_IBS else 105.0) if symbol == "QQQ" else base
            bar = {
                "open": base + i,
                "high": base + 10 if symbol == "QQQ" else base,
                "low": base,
                "close": close,
                "volume": 1e6,
            }
            rows.append(
                {"session": d, "symbol": symbol, **bar, **{f"adj_{k}": v for k, v in bar.items()}}
            )
    return pd.DataFrame(rows)


def _blocked(
    session: date,
    *,
    strategy: str = "ibs",
    symbol: str = "QQQ",
    side: str = "buy",
    checks: list[dict[str, Any]] | None = None,
    pid: str | None = None,
) -> Proposal:
    return Proposal(
        id=pid or f"{strategy}-{session}-{symbol}-{side}",
        strategy=strategy,
        session=session.isoformat(),
        symbol=symbol,
        side=side,
        quantity=20.0,
        order_type="market",
        reference_price=101.0,
        reason=f"{symbol} IBS 0.10 below 0.2: hold through the next session",
        checks=checks or [{"check": "kill_switch", "passed": True, "detail": "off"}, DAILY_LOSS],
        status="blocked",
        created_at=f"{session}T20:00:00+00:00",
        updated_at=f"{session}T20:00:00+00:00",
    )


def _net(entry: float, exit_: float) -> float:
    return exit_ / entry * (1 - COST) / (1 + COST) - 1


RULES = {"ibs": build("ibs")}


def test_a_shadow_extends_while_ibs_stays_low_and_folds_the_repeat_block() -> None:
    data = PaperData(_bars(DAYS))
    first, again = _blocked(DAYS[0]), _blocked(DAYS[1])
    (entry,) = shadow_entries([first, again], data, RULES, [])
    # In at the d1 open; d1 closed low again, so held; d2 closed high: out at the d3 open.
    assert entry.status == "closed"
    assert (entry.entry_session, entry.exit_session) == (DAYS[1].isoformat(), DAYS[3].isoformat())
    assert (entry.entry_price, entry.exit_price) == (101.0, 103.0)
    assert entry.sessions_held == 2
    assert entry.signal == pytest.approx(0.1)
    assert entry.shadow_return == pytest.approx(_net(101.0, 103.0))
    assert entry.missed_pnl == pytest.approx(20 * 101.0 * _net(101.0, 103.0))
    assert entry.folded == [again.id]  # had the first gone through, there'd be no second
    assert entry.blocked_by == {"daily_loss": DAILY_LOSS["detail"]}
    (summary,) = summarize([entry])
    assert (summary.blocked, summary.trades, summary.open) == (2, 1, 0)
    assert summary.missed_pnl == pytest.approx(entry.missed_pnl)


def test_a_later_block_after_the_shadow_ended_is_a_new_trade() -> None:
    data = PaperData(_bars(DAYS))
    entries = shadow_entries([_blocked(DAYS[0]), _blocked(DAYS[4])], data, RULES, [])
    assert [e.entry_session for e in entries] == [DAYS[1].isoformat(), DAYS[5].isoformat()]
    assert entries[1].exit_session == DAYS[6].isoformat()  # d5 closed high: out next open
    (summary,) = summarize(entries)
    assert (summary.blocked, summary.trades) == (2, 2)
    assert summary.avg_return == pytest.approx((_net(101.0, 103.0) + _net(105.0, 106.0)) / 2)


def test_a_real_buy_while_the_shadow_holds_ends_it_at_that_open() -> None:
    data = PaperData(_bars(DAYS))
    fill = FillRecord(
        "f1", "p1", "ibs", "QQQ", "buy", 20.0, 102.0, 0.0, DAYS[2].isoformat(), "", 101.0
    )
    (entry,) = shadow_entries([_blocked(DAYS[0])], data, RULES, [fill])
    assert entry.ended_by_fill
    assert entry.exit_session == DAYS[2].isoformat()
    assert entry.shadow_return == pytest.approx(_net(101.0, 102.0))


def test_a_shadow_still_holding_is_marked_at_the_close_and_left_out_of_the_totals() -> None:
    data = PaperData(_bars(DAYS[:2]))  # d1 closed low: still holding, no d2 yet
    (entry,) = shadow_entries([_blocked(DAYS[0])], data, RULES, [])
    assert entry.status == "open"
    assert entry.shadow_return == pytest.approx(_net(101.0, 101.0))  # the d1 close
    (summary,) = summarize([entry])
    assert (summary.trades, summary.open, summary.avg_return, summary.missed_pnl) == (
        0,
        1,
        None,
        None,
    )
    (waiting,) = shadow_entries([_blocked(DAYS[1])], data, RULES, [])
    assert waiting.status == "waiting"  # the next open hasn't happened


def test_without_a_rule_blocked_buys_are_still_listed_and_counted() -> None:
    data = PaperData(_bars(DAYS))
    rows = [
        _blocked(DAYS[0], strategy="rsi2"),
        _blocked(DAYS[0], symbol="BIL"),  # IBS's cash fund: no signal to replay
    ]
    entries = shadow_entries(rows, data, RULES, [])
    assert [e.status for e in entries] == ["no rule", "no rule"]
    assert [(s.strategy, s.symbol, s.blocked, s.without_rule) for s in summarize(entries)] == [
        ("ibs", "BIL", 1, 1),
        ("rsi2", "QQQ", 1, 1),
    ]


def test_only_blocked_buys_and_the_chosen_checks() -> None:
    liquidity = {"check": "liquidity", "passed": False, "detail": "too big"}
    rows = [
        _blocked(DAYS[0], pid="a"),
        _blocked(DAYS[0], pid="b", checks=[liquidity]),
        _blocked(DAYS[0], pid="c", side="sell"),
        replace(_blocked(DAYS[0], pid="d"), status="filled"),
    ]
    assert [p.id for p in blocked_buys(rows)] == ["a", "b"]
    assert [p.id for p in blocked_buys(rows, ["daily_loss"])] == ["a"]


# -- end to end: the account is down more than 2% and the IBS buy is blocked -------------------


def _signal_session(env: Any) -> date:
    """A QQQ session in the test lake with IBS below 0.2 and at least two sessions after it."""
    bars = load_bars(env.paper.lake, ["QQQ"], start=date(2024, 6, 1))
    days = list(bars["session"])
    for i, row in enumerate(bars.itertuples()):
        if i + 3 < len(days) and ibs(row.adj_high, row.adj_low, row.adj_close) < 0.2:
            return date.fromisoformat(str(row.session))
    pytest.skip("no low-IBS QQQ close in the test lake")


def _evening(day: date) -> datetime:
    return datetime.combine(day, time(19, 0), tzinfo=NEW_YORK).astimezone(UTC)


def test_an_ibs_buy_blocked_by_the_daily_loss_limit_is_logged_with_its_shadow_trade(
    env: Any,
) -> None:
    signal = _signal_session(env)
    ibs_sleeve = Sleeve(build("ibs", {"cash_symbol": "none"}), 10_000, "auto")
    env.paper.book = PaperBook((ibs_sleeve,))
    now = env.at(_evening(signal))
    jobs.sync(env.paper, now)  # the close is marked first, as the bot does
    env.broker.mark_close(103_000.0)  # yesterday's close: today the account is down 2.9%

    report = jobs.propose(env.paper, now)
    assert report.blocked["ibs"] == 1
    assert report.proposed["ibs"] == 0
    (p,) = env.paper.store.proposals(strategy="ibs")
    assert (p.status, p.side, p.symbol, p.session) == ("blocked", "buy", "QQQ", signal.isoformat())
    assert p.quantity > 0
    assert 0 < p.notional <= 10_000
    assert "below 0.2" in p.reason  # the signal IBS, in the strategy's words
    daily = next(c for c in p.checks if c["check"] == "daily_loss")
    assert daily == {
        "check": "daily_loss",
        "passed": False,
        "detail": "equity -2.91% since the prior close (limit -2%)",
    }
    logged = [e["message"] for e in env.paper.store.events() if e["kind"] == "blocked"]
    assert len(logged) == 1
    assert "daily_loss" in logged[0]
    assert "QQQ" in logged[0]

    later = env.at(datetime(2024, 8, 3, 12, tzinfo=UTC))
    result = jobs.blocked(env.paper, later, checks=["daily_loss"])
    (entry,) = result.entries
    assert entry.proposal_id == p.id
    assert entry.blocked_by == {"daily_loss": daily["detail"]}
    # Replay the rule by hand: in at the next open, out at the open after the first high close.
    bars = load_bars(env.paper.lake, ["QQQ"], start=signal).set_index("session")
    days = [date.fromisoformat(str(d)) for d in bars.index]
    t_in = days.index(signal) + 1
    t = t_in
    while t < len(days):
        b = bars.iloc[t]
        if not ibs(b.adj_high, b.adj_low, b.adj_close) < 0.2:
            break
        t += 1
    signal_bar = bars.iloc[t_in - 1]
    assert entry.signal == pytest.approx(
        ibs(signal_bar.adj_high, signal_bar.adj_low, signal_bar.adj_close)
    )
    assert entry.entry_session == days[t_in].isoformat()
    assert entry.entry_price == pytest.approx(bars.iloc[t_in].open)
    if t + 1 < len(days):
        expected = _net(bars.iloc[t_in].adj_open, bars.iloc[t + 1].adj_open)
        assert entry.status == "closed"
        assert entry.exit_session == days[t + 1].isoformat()
        assert entry.sessions_held == t + 1 - t_in
        assert entry.shadow_return == pytest.approx(expected)
        assert entry.missed_pnl == pytest.approx(p.quantity * bars.iloc[t_in].open * expected)
        (summary,) = result.summary
        assert (summary.strategy, summary.blocked, summary.trades) == ("ibs", 1, 1)
        assert summary.missed_pnl == pytest.approx(entry.missed_pnl)
    else:
        assert entry.status == "open"
    assert result.through == "2024-08-02"


def test_the_blocked_command_prints_counts_returns_and_missed_pnl(
    env: Any, monkeypatch: Any
) -> None:
    signal = _signal_session(env)
    env.paper.book = PaperBook((Sleeve(build("ibs", {"cash_symbol": "none"}), 10_000, "auto"),))
    now = env.at(_evening(signal))
    jobs.sync(env.paper, now)
    env.broker.mark_close(103_000.0)
    jobs.propose(env.paper, now)
    monkeypatch.setattr(cli, "_paper", lambda *a, **k: env.paper)
    monkeypatch.setattr(cli, "_now", lambda: datetime(2024, 8, 3, 12, tzinfo=UTC))
    runner = CliRunner()
    text = runner.invoke(cli.app, ["--broker", "fake", "blocked", "--check", "daily_loss"])
    assert text.exit_code == 0, text.output
    assert "missed P&L" in text.output
    assert "blocked by daily_loss" in text.output
    as_json = runner.invoke(cli.app, ["--broker", "fake", "blocked", "--json"])
    payload = json.loads(as_json.output)
    assert payload["summary"][0]["strategy"] == "ibs"
    assert payload["entries"][0]["blocked_by"]["daily_loss"].startswith("equity -2.91%")
    none = runner.invoke(cli.app, ["--broker", "fake", "blocked", "--check", "liquidity"])
    assert "no blocked buys" in none.output
