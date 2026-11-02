"""The decision journal: what you took or passed on from the ideas, the gates each was checked
against first, and how what you took turned out.

An idea is taken from the latest recommendations run (``tp-recs run``). Before it's written down
it goes through the gates:

    kill switch     ``tp-risk kill`` is engaged                                      block
    contract        the idea has no contract or shares to take                       block
    fresh           its entry limit has expired: make a new run                      block
    risk            the most it can lose is over TP_OPTIONS_RISK of the account      block
    position        the money it ties up is over max_position (config/risk.toml)     block
    buying power    it needs more than the account's buying power                    block
    pace            TP_JOURNAL_MAX_PER_DAY trades taken today already                block
    losing streak   TP_JOURNAL_LOSS_STREAK losses in a row, the last one today       block
    edge            the model expects it to lose money at the entry limit            warn
    event           the options price a jump (usually earnings) inside the hold      warn
    flags           the idea's own risks (news against it, already held, ...)        warn

A block stops ``tp-journal take`` unless you override it with a reason, which is recorded next
to the trade so the review can show how overrides did. Nothing here places an order: the
journal is what you did by hand, at your broker.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from tp_core.calendar import NEW_YORK
from tp_core.config import Settings
from tp_core.storage import write_json
from tp_risk.limits import Limits
from tp_risk.state import Switch
from tp_strategies.recommendations import LISTS, _structure

Verdict = Literal["ok", "warn", "block"]
Decision = Literal["took", "passed"]
MULTIPLIER = 100  # shares per option contract


@dataclass(frozen=True)
class Check:
    gate: str
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class Rules:
    max_position: float = Limits.max_position
    max_per_day: int = 3
    loss_streak: int = 3

    @classmethod
    def from_settings(cls, settings: Settings) -> Rules:
        limits = Limits.load(settings.risk_file) if settings.risk_file.exists() else Limits()
        return cls(limits.max_position, settings.journal_max_per_day, settings.journal_loss_streak)


def journal_path(settings: Settings) -> Path:
    return settings.journal_file or settings.data_root / "journal" / "journal.json"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = json.loads(path.read_text()).get("entries", [])
    return entries


def save(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    write_json({"entries": list(entries)}, path)


def _day(at: str) -> date:
    return datetime.fromisoformat(at).astimezone(NEW_YORK).date()


def _f(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


# -- the idea and what taking it means ------------------------------------------------------------


def find_idea(run: Mapping[str, Any], symbol: str, idea_list: str | None = None) -> dict[str, Any]:
    """The idea on ``symbol`` in ``run`` (on ``idea_list`` if given); LookupError if there's
    none, or more than one and no list to choose by."""
    research = run.get("research") or {}
    lists = [idea_list] if idea_list else list(LISTS)
    found: list[dict[str, Any]] = [
        {**idea, "list": k}
        for k in lists
        for idea in research.get(k) or []
        if str(idea.get("symbol", "")).upper() == symbol.upper()
    ]
    if not found:
        where = f"the {idea_list} list" if idea_list else "any list"
        raise LookupError(f"{symbol.upper()} isn't on {where} in run {run.get('run_id')}")
    if len(found) > 1:
        names = ", ".join(str(i["list"]) for i in found)
        raise LookupError(f"{symbol.upper()} is on more than one list ({names}): pick one --list")
    return found[0]


def _is_shares(pick: Mapping[str, Any]) -> bool:
    return pick.get("kind") == "shares"


@dataclass(frozen=True)
class Sizing:
    """What ``units`` contracts (or shares) of an idea's pick would take, in dollars."""

    units: float
    unit: str  # "contracts" or "shares"
    suggested: float
    entry: float | None  # per share: the limit price
    side: str  # "debit" (paid) or "credit" (received)
    max_loss: float | None
    capital: float | None

    @classmethod
    def of(cls, pick: Mapping[str, Any], units: float | None, price: float | None) -> Sizing:
        size = pick.get("size") or {}
        entry = pick.get("entry") or {}
        shares = _is_shares(pick)
        suggested = float(size.get("shares" if shares else "contracts") or 0)
        n = units if units is not None else (suggested or 1.0)
        limit = price if price is not None else _f(entry.get("limit"))
        if shares:
            per_loss = _f(pick.get("risk_per_share"))
            per_capital = limit if limit is not None else _f(pick.get("price"))
        else:
            per_loss, per_capital = _f(pick.get("max_loss")), _f(pick.get("capital"))
        return cls(
            units=n,
            unit="shares" if shares else "contracts",
            suggested=suggested,
            entry=limit,
            side=str(entry.get("side") or "debit"),
            max_loss=per_loss * n if per_loss is not None else None,
            capital=per_capital * n if per_capital is not None else None,
        )

    @property
    def amount(self) -> str:
        return f"{self.units:g} {self.unit[:-1] if self.units == 1 else self.unit}"

    @property
    def multiplier(self) -> int:
        return 1 if self.unit == "shares" else MULTIPLIER


# -- the gates ------------------------------------------------------------------------------------


def _streak(entries: Sequence[Mapping[str, Any]]) -> tuple[int, date | None]:
    """Losses in a row among the closed trades, newest back, and the day of the newest."""
    closed = sorted((e for e in entries if e.get("closed")), key=lambda e: str(e["closed"]["at"]))
    n = 0
    for e in reversed(closed):
        if (_f(e["closed"].get("pnl")) or 0.0) >= 0:
            break
        n += 1
    return n, _day(closed[-1]["closed"]["at"]) if closed else None


def gates(
    idea: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    sizing: Sizing | None,
    entries: Sequence[Mapping[str, Any]],
    rules: Rules,
    today: date,
    kill: Switch | None,
) -> list[Check]:
    """Every gate's verdict on taking ``idea`` (sized by ``sizing``) today."""
    out: list[Check] = []

    def add(gate: str, verdict: Verdict, reason: str) -> None:
        out.append(Check(gate, verdict, reason))

    if kill is not None:
        add("kill switch", "block", f"engaged {kill.at}: {kill.reason}")
    else:
        add("kill switch", "ok", "off")

    pick = idea.get("pick")
    if not pick or sizing is None:
        add("contract", "block", "no contract or shares to take (a watch-list name)")
        return out

    valid = (pick.get("entry") or {}).get("valid_until")
    if valid and date.fromisoformat(str(valid)) < today:
        add("fresh", "block", f"the entry limit was good until {valid}: `tp-recs run` again")
    else:
        add("fresh", "ok", f"run {run.get('run_id')}" + (f", good until {valid}" if valid else ""))

    budget = (run.get("research") or {}).get("budget") or {}
    account = _f(budget.get("account_value"))
    risk_share = _f(budget.get("risk_per_trade"))
    if not account:
        add("risk", "warn", "no account value from the brokerage sync: can't size it")
    else:
        if sizing.max_loss is not None and risk_share is not None:
            share = sizing.max_loss / account
            line = (
                f"{sizing.amount} can lose ${sizing.max_loss:,.0f}, "
                f"{share:.1%} of the account; the guideline is {risk_share:.0%} (TP_OPTIONS_RISK)"
            )
            add("risk", "block" if share > risk_share + 1e-9 else "ok", line)
        if sizing.capital is not None:
            share = sizing.capital / account
            line = (
                f"ties up ${sizing.capital:,.0f}, {share:.1%} of the account; the limit is "
                f"{rules.max_position:.0%} (max_position)"
            )
            add("position", "block" if share > rules.max_position + 1e-9 else "ok", line)
    power = _f(budget.get("buying_power"))
    if power is not None and sizing.capital is not None:
        verdict: Verdict = "block" if sizing.capital > power else "ok"
        add("buying power", verdict, f"needs ${sizing.capital:,.0f} of ${power:,.0f}")

    taken = sum(1 for e in entries if e.get("decision") == "took" and _day(e["at"]) == today)
    add(
        "pace",
        "block" if taken >= rules.max_per_day else "ok",
        f"{taken} taken today; the limit is {rules.max_per_day} (TP_JOURNAL_MAX_PER_DAY)",
    )
    losses, last = _streak(entries)
    if losses >= rules.loss_streak and last == today:
        add("losing streak", "block", f"{losses} losses in a row, the last today: stop for the day")
    else:
        add("losing streak", "ok", f"{losses} in a row")

    expected = _f((pick.get("model") or {}).get("expected_return"))
    if expected is not None and expected <= 0:
        add("edge", "warn", f"the model expects {expected:+.1%} at this price")
    elif expected is not None:
        add("edge", "ok", f"the model expects {expected:+.1%}")

    event = (idea.get("vol") or {}).get("event")
    held_to = (pick.get("exit") or {}).get("out_by") or pick.get("expiration")
    if event and (not held_to or str(event.get("after") or "") < str(held_to)):
        add("event", "warn", f"the options price a jump (usually earnings) by {event.get('by')}")

    flags = [str(r) for r in idea.get("risks") or []]
    if flags:
        add("flags", "warn", " ".join(flags))
    return out


def blocked(checks: Sequence[Check]) -> list[Check]:
    return [c for c in checks if c.verdict == "block"]


# -- entries --------------------------------------------------------------------------------------


def entry(
    decision: Decision,
    idea: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    at: datetime,
    entries: Sequence[Mapping[str, Any]],
    sizing: Sizing | None,
    checks: Sequence[Check],
    note: str | None = None,
    override: str | None = None,
) -> dict[str, Any]:
    pick = idea.get("pick") or None
    idea_list = str(idea.get("list") or idea.get("kind"))
    out: dict[str, Any] = {
        "id": max((int(e["id"]) for e in entries), default=0) + 1,
        "at": at.isoformat(timespec="seconds"),
        "decision": decision,
        "symbol": idea["symbol"],
        "list": idea_list,
        "setup": (idea.get("play") or {}).get("name"),
        "structure": _structure(pick, idea_list),
        "run_id": run.get("run_id"),
        "new": bool(idea.get("new")),
        "checks": [asdict(c) for c in checks],
        "override": override,
        "note": note,
    }
    if decision == "took" and sizing is not None and pick:
        out |= {
            "units": sizing.units,
            "unit": sizing.unit,
            "suggested": sizing.suggested,
            "entry": sizing.entry,
            "side": sizing.side,
            "max_loss": sizing.max_loss,
            "capital": sizing.capital,
            "plan": pick.get("exit"),
            "closed": None,
        }
    return out


def close(e: dict[str, Any], price: float, *, at: datetime, note: str | None = None) -> None:
    """Record the exit at ``price`` (per share, like the entry): the P&L in dollars and in R,
    the multiple of the most it could have lost."""
    if e.get("decision") != "took":
        raise ValueError(f"entry {e['id']} was passed on: nothing to close")
    if e.get("closed"):
        raise ValueError(f"entry {e['id']} is already closed")
    sign = 1 if e.get("side") == "debit" else -1
    mult = 1 if e.get("unit") == "shares" else MULTIPLIER
    entry_price = _f(e.get("entry"))
    pnl = (
        sign * (price - entry_price) * float(e["units"]) * mult if entry_price is not None else None
    )
    max_loss = _f(e.get("max_loss"))
    e["closed"] = {
        "at": at.isoformat(timespec="seconds"),
        "price": price,
        "pnl": pnl,
        "r": pnl / max_loss if pnl is not None and max_loss else None,
        "note": note,
    }


# -- the review -----------------------------------------------------------------------------------


def _stats(closed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [p for e in closed if (p := _f(e["closed"].get("pnl"))) is not None]
    rs = [r for e in closed if (r := _f(e["closed"].get("r"))) is not None]
    return {
        "closed": len(closed),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
        "pnl": sum(pnls) if pnls else None,
        "mean_r": sum(rs) / len(rs) if rs else None,
    }


def review(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Taken against passed, how the closed trades did, and the same for the overridden ones,
    by setup."""
    took = [e for e in entries if e.get("decision") == "took"]
    closed = [e for e in took if e.get("closed")]
    setups = sorted({str(e.get("setup")) for e in closed})
    return {
        "took": len(took),
        "passed": sum(1 for e in entries if e.get("decision") == "passed"),
        "open": len(took) - len(closed),
        "all": _stats(closed),
        "followed the gates": _stats([e for e in closed if not e.get("override")]),
        "overrode a gate": _stats([e for e in closed if e.get("override")]),
        "by_setup": {s: _stats([e for e in closed if str(e.get("setup")) == s]) for s in setups},
    }
