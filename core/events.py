"""Event store + in-process event bus.

- Append-only. Every state change in the runtime is written here first
  (write-ahead), so `resume()` can rebuild state after a crash.
- Hash-chained: each event stores sha256(previous hash + its own content), so
  any edit or deletion of history is detectable (the audit log, §7 #16).
- Dedupe keys: appending an event whose `key` already exists returns the
  stored event and does not notify subscribers again. Retries and timer
  re-fires therefore never produce duplicate side effects downstream.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from core.models import Event
from core.store import Database

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    mission_id TEXT,
    entity_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    key TEXT UNIQUE,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_mission ON events(mission_id, seq);
CREATE INDEX IF NOT EXISTS events_entity ON events(entity_id, seq);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
"""


def _content_hash(prev_hash: str, event: Event) -> str:
    body = {
        "id": event.id, "type": event.type, "mission_id": event.mission_id, "entity_id": event.entity_id,
        "task_id": event.task_id, "payload": event.payload, "occurred_at": event.occurred_at.isoformat(),
        "key": event.key,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(id=row["id"], seq=row["seq"], type=row["type"], mission_id=row["mission_id"],
                 entity_id=row["entity_id"], task_id=row["task_id"], payload=json.loads(row["payload_json"]),
                 occurred_at=datetime.fromisoformat(row["occurred_at"]), key=row["key"], hash=row["hash"])


class EventStore:
    def __init__(self, db: Database):
        self.db = db
        db.ensure_schema(SCHEMA)

    def append(self, event: Event) -> tuple[Event, bool]:
        """Returns (stored event, created). created=False means the dedupe key already existed."""
        with self.db.transaction() as conn:
            return self.append_in(conn, event)

    def append_in(self, conn: sqlite3.Connection, event: Event) -> tuple[Event, bool]:
        """Append inside a caller's transaction (so a state change and its event commit together)."""
        if event.key is not None:
            existing = conn.execute("SELECT * FROM events WHERE key = ?", (event.key,)).fetchone()
            if existing is not None:
                return _row_to_event(existing), False
        last = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS
        digest = _content_hash(prev_hash, event)
        cur = conn.execute(
            "INSERT INTO events (id, type, mission_id, entity_id, task_id, payload_json, occurred_at, "
            "recorded_at, key, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (event.id, event.type, event.mission_id, event.entity_id, event.task_id,
             json.dumps(event.payload, default=str), event.occurred_at.isoformat(),
             datetime.now(timezone.utc).isoformat(), event.key, prev_hash, digest),
        )
        return event.model_copy(update={"seq": cur.lastrowid, "hash": digest}), True

    def query(self, *, mission_id: str | None = None, entity_id: str | None = None, task_id: str | None = None,
              types: list[str] | None = None, since_seq: int = 0, limit: int = 1000) -> list[Event]:
        sql, args = "SELECT * FROM events WHERE seq > ?", [since_seq]
        for col, val in (("mission_id", mission_id), ("entity_id", entity_id), ("task_id", task_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        with self.db.read() as conn:
            return [_row_to_event(r) for r in conn.execute(sql + " ORDER BY seq LIMIT ?", [*args, limit])]

    def latest(self, limit: int, *, mission_id: str | None = None, since_seq: int = 0,
               exclude_prefix: str | None = None) -> list[Event]:
        """The newest `limit` events (oldest first), optionally leaving out one type prefix (e.g. "tool.")."""
        sql, args = "SELECT * FROM events WHERE seq > ?", [since_seq]
        if mission_id is not None:
            sql, args = sql + " AND mission_id = ?", [*args, mission_id]
        if exclude_prefix:
            sql, args = sql + " AND substr(type, 1, ?) != ?", [*args, len(exclude_prefix), exclude_prefix]
        with self.db.read() as conn:
            rows = conn.execute(sql + " ORDER BY seq DESC LIMIT ?", [*args, limit]).fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    def last_seq(self, *, mission_id: str | None = None) -> int:
        sql, args = "SELECT COALESCE(MAX(seq), 0) AS s FROM events", []
        if mission_id is not None:
            sql, args = sql + " WHERE mission_id = ?", [mission_id]
        with self.db.read() as conn:
            return int(conn.execute(sql, args).fetchone()["s"])

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute every hash. Returns (ok, first bad seq)."""
        prev = GENESIS
        with self.db.read() as conn:
            for row in conn.execute("SELECT * FROM events ORDER BY seq"):
                event = _row_to_event(row)
                if row["prev_hash"] != prev or _content_hash(prev, event) != row["hash"]:
                    return False, row["seq"]
                prev = row["hash"]
        return True, None


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Publish = durable append, then notify in-process subscribers.

    Subscribers match on type prefix ("task." matches "task.started").
    A failing subscriber does not stop others; the event is already durable,
    so nothing is lost and a resumed runtime can replay it.
    """

    def __init__(self, store: EventStore):
        self.store = store
        self._subs: list[tuple[str, Handler]] = []
        self.handler_errors: list[tuple[Event, str]] = []

    def subscribe(self, type_prefix: str, handler: Handler) -> None:
        self._subs.append((type_prefix, handler))

    async def publish(self, event: Event) -> Event:
        stored, created = self.store.append(event)
        if created:
            await self.notify(stored)
        return stored

    async def notify(self, event: Event) -> None:
        for prefix, handler in self._subs:
            if event.type.startswith(prefix):
                try:
                    await handler(event)
                except Exception as exc:  # noqa: BLE001 - isolate subscribers from each other
                    self.handler_errors.append((event, repr(exc)))
