"""Long-lived memory of an AI employee (architecture §8.3): what it learned, kept across missions.

    scope   who or what a memory is about: "counterparty:<id>", "app:<name>", "company"
    kind    fact      computed by code from what happened (counts, rates, channels); always current
            lesson    distilled by the Memory Writer from episodes, citing the events behind it
            playbook  how a task on an app was done when the scripted route failed (procedural)
    status  active    used by the employee (the default)
            pinned    a human vouched for it; kept and shown first
            rejected  a human said it is wrong; never used again, and never re-learned with the same text

One row per (scope, kind, key): writing the same key again replaces the text, unless a human rejected
it. Every write and decision is an event. Memory lives in its own database so it survives a reset of
the working state: the employee keeps what it learned when the world is re-seeded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from core.clock import Clock
from core.events import EventStore
from core.models import Event, new_id
from core.store import Database

Kind = Literal["fact", "lesson", "playbook"]
Status = Literal["active", "pinned", "rejected"]
USABLE = ("pinned", "active")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    text TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    cites_json TEXT NOT NULL DEFAULT '[]',
    source TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    decided_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope, kind, key)
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories (scope, status);
"""


@dataclass
class MemoryItem:
    id: str
    scope: str
    kind: str
    key: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    cites: list[str] = field(default_factory=list)
    source: str | None = None
    status: str = "active"
    decided_by: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _row(r) -> MemoryItem:
    return MemoryItem(id=r["id"], scope=r["scope"], kind=r["kind"], key=r["key"], text=r["text"],
                      data=json.loads(r["data_json"]), cites=json.loads(r["cites_json"]), source=r["source"],
                      status=r["status"], decided_by=r["decided_by"], created_at=r["created_at"],
                      updated_at=r["updated_at"])


class MemoryStore:
    def __init__(self, db: Database, clock: Clock, events: EventStore | None = None):
        self.db, self.clock, self.events = db, clock, events
        db.ensure_schema(SCHEMA)

    def _event(self, type_: str, item: MemoryItem, **extra) -> None:
        if self.events:
            self.events.append(Event(type=type_, occurred_at=self.clock.now(),
                                     payload={"memory_id": item.id, "scope": item.scope, "kind": item.kind,
                                              "key": item.key, "text": item.text, "status": item.status, **extra}))

    def put(self, scope: str, kind: Kind, key: str, text: str, *, data: dict[str, Any] | None = None,
            cites: list[str] | None = None, source: str | None = None) -> MemoryItem | None:
        """Write or replace a memory. Returns None when a human rejected this exact text before."""
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM memories WHERE scope = ? AND kind = ? AND key = ?",
                               (scope, kind, key)).fetchone()
            if row is not None and row["status"] == "rejected" and row["text"] == text:
                return None
            if row is not None and row["text"] == text and json.loads(row["data_json"]) == (data or {}):
                return _row(row)                     # nothing new
            status = row["status"] if row is not None and row["status"] == "pinned" else "active"
            mid = row["id"] if row is not None else new_id("mem")
            conn.execute("INSERT INTO memories (id, scope, kind, key, text, data_json, cites_json, source, status, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(scope, kind, key) DO UPDATE SET text = excluded.text, "
                         "data_json = excluded.data_json, cites_json = excluded.cites_json, "
                         "source = excluded.source, status = excluded.status, updated_at = excluded.updated_at",
                         (mid, scope, kind, key, text, json.dumps(data or {}, default=str),
                          json.dumps(cites or []), source, status, row["created_at"] if row else now, now))
            item = _row(conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone())
        self._event("memory.written", item, source=source)
        return item

    def get(self, memory_id: str) -> MemoryItem:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return _row(row)

    def list(self, *, scope: str | None = None, scope_prefix: str | None = None, kind: str | None = None,
             statuses: tuple[str, ...] | None = USABLE) -> list[MemoryItem]:
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        if scope is not None:
            sql += " AND scope = ?"
            args.append(scope)
        if scope_prefix is not None:
            sql += " AND scope LIKE ?"
            args.append(scope_prefix + "%")
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        with self.db.read() as conn:
            rows = [_row(r) for r in conn.execute(sql + " ORDER BY scope, kind, key", args)]
        return sorted(rows, key=lambda m: (m.scope, m.status != "pinned", m.kind, m.key))

    def decide(self, memory_id: str, status: Status, *, by: str) -> MemoryItem:
        """A human pins, rejects or re-activates a memory."""
        if not by:
            raise ValueError("a memory decision needs a named human")
        with self.db.transaction() as conn:
            if conn.execute("UPDATE memories SET status = ?, decided_by = ?, updated_at = ? WHERE id = ?",
                            (status, by, self.clock.now().isoformat(), memory_id)).rowcount == 0:
                raise KeyError(memory_id)
        item = self.get(memory_id)
        self._event("memory.decided", item, by=by)
        return item

    def forget(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM memories").rowcount
