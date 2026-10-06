"""Paper-trading state in one SQLite file (``<data root>/state/paper.sqlite``).

Orders have a lifecycle (proposed, approved, submitted, filled...), so they live in a small
database rather than the append-only Parquet lake. SQLite needs no server, handles the dashboard
reading while a job writes (WAL mode), and moves to Postgres with the platform phase.

Proposal statuses::

    pending -> approved -> submitted -> partially_filled -> filled
            -> rejected (by you)        -> canceled (broker, kill switch, unfilled at the open)
            -> expired (not approved in time, or stale)
    blocked (by the risk checks; never tradable)          failed (the broker refused it)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    approved_quantity REAL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    stop_price REAL,
    target_weight REAL,
    reference_price REAL NOT NULL,
    reason TEXT NOT NULL,
    checks TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    broker_order_id TEXT,
    submitted_at TEXT,
    filled_quantity REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_by_status ON proposals (status);
CREATE INDEX IF NOT EXISTS proposals_by_strategy ON proposals (strategy, session);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    session TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    reference_price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_by_strategy ON fills (strategy, filled_at);
CREATE TABLE IF NOT EXISTS sleeve_days (
    strategy TEXT NOT NULL,
    session TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions TEXT NOT NULL,
    PRIMARY KEY (strategy, session)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

OPEN_AT_BROKER = ("submitted", "partially_filled")
TERMINAL = ("rejected", "blocked", "expired", "filled", "canceled", "failed")


@dataclass(frozen=True)
class Proposal:
    id: str  # also the broker's client order id
    strategy: str
    session: str  # signal session, ISO date
    symbol: str
    side: str
    quantity: float
    order_type: str
    reference_price: float
    reason: str
    checks: list[dict[str, Any]]
    status: str
    created_at: str
    updated_at: str
    approved_quantity: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    target_weight: float | None = None
    note: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    broker_order_id: str | None = None
    submitted_at: str | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None

    @property
    def order_quantity(self) -> float:
        return self.approved_quantity if self.approved_quantity is not None else self.quantity

    @property
    def notional(self) -> float:
        return self.order_quantity * self.reference_price

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"order_quantity": self.order_quantity, "notional": self.notional}


@dataclass(frozen=True)
class FillRecord:
    id: str
    proposal_id: str
    strategy: str
    symbol: str
    side: str
    quantity: float
    price: float
    fees: float
    session: str
    filled_at: str
    reference_price: float


@dataclass(frozen=True)
class SleeveDay:
    strategy: str
    session: str
    equity: float
    cash: float
    positions: dict[str, float]


class PaperStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @classmethod
    def under(cls, data_root: Path) -> PaperStore:
        return cls(data_root / "state" / "paper.sqlite")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- proposals --------------------------------------------------------------------------------

    def add_proposals(self, proposals: Sequence[Proposal]) -> None:
        if not proposals:
            return
        columns = [f.name for f in fields(Proposal)]
        sql = (
            f"INSERT INTO proposals ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        with self._connect() as db:
            db.executemany(sql, [_proposal_values(p, columns) for p in proposals])

    def proposals(
        self,
        *,
        status: str | Sequence[str] | None = None,
        strategy: str | None = None,
        session: str | None = None,
        limit: int | None = None,
    ) -> list[Proposal]:
        where, args = [], []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args.extend(statuses)
        if strategy is not None:
            where.append("strategy = ?")
            args.append(strategy)
        if session is not None:
            where.append("session = ?")
            args.append(session)
        sql = "SELECT * FROM proposals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY session DESC, created_at, id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as db:
            return [_proposal(row) for row in db.execute(sql, args)]

    def proposal(self, proposal_id: str) -> Proposal:
        with self._connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"no proposal {proposal_id}")
        return _proposal(row)

    def update(
        self, proposal_id: str, *, expect: Sequence[str] | None = None, **values: Any
    ) -> bool:
        """Change a proposal; with ``expect``, only if its status is one of those (so two
        processes can't both act on it). Returns whether a row changed."""
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in values)
        sql = f"UPDATE proposals SET {assignments} WHERE id = ?"
        args = [*values.values(), proposal_id]
        if expect is not None:
            sql += f" AND status IN ({', '.join('?' for _ in expect)})"
            args.extend(expect)
        with self._connect() as db:
            return db.execute(sql, args).rowcount == 1

    # -- fills ------------------------------------------------------------------------------------

    def add_fill(self, fill: FillRecord) -> bool:
        columns = [f.name for f in fields(FillRecord)]
        sql = (
            f"INSERT OR IGNORE INTO fills ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        with self._connect() as db:
            return db.execute(sql, [getattr(fill, c) for c in columns]).rowcount == 1

    def fills(self, strategy: str | None = None) -> list[FillRecord]:
        sql, args = "SELECT * FROM fills", []
        if strategy is not None:
            sql += " WHERE strategy = ?"
            args.append(strategy)
        sql += " ORDER BY filled_at, id"
        with self._connect() as db:
            return [FillRecord(**dict(row)) for row in db.execute(sql, args)]

    # -- sleeve history ---------------------------------------------------------------------------

    def record_sleeve_day(self, day: SleeveDay) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sleeve_days VALUES (?, ?, ?, ?, ?)",
                (day.strategy, day.session, day.equity, day.cash, json.dumps(day.positions)),
            )

    def sleeve_days(self, strategy: str | None = None) -> list[SleeveDay]:
        sql, args = "SELECT * FROM sleeve_days", []
        if strategy is not None:
            sql += " WHERE strategy = ?"
            args.append(strategy)
        sql += " ORDER BY strategy, session"
        with self._connect() as db:
            return [
                SleeveDay(
                    r["strategy"], r["session"], r["equity"], r["cash"], json.loads(r["positions"])
                )
                for r in db.execute(sql, args)
            ]

    # -- audit log and small values ----------------------------------------------------------------

    def log(self, kind: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (at, kind, message) VALUES (?, ?, ?)",
                (now_iso(), kind, message),
            )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def put(self, key: str, value: Any) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def get(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso(day: date) -> str:
    return day.isoformat()


def _proposal_values(p: Proposal, columns: list[str]) -> list[Any]:
    values = []
    for c in columns:
        v = getattr(p, c)
        values.append(json.dumps(v) if c == "checks" else v)
    return values


def _proposal(row: sqlite3.Row) -> Proposal:
    data = dict(row)
    data["checks"] = json.loads(data["checks"])
    return Proposal(**data)
