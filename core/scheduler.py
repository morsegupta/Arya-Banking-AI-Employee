"""Durable timers: follow-ups, reminders, daily syncs.

Timers live in the runtime DB, so they survive a crash. Firing is exactly-once:
the `timer.fired` event (dedupe key `timer:<key>`) and the timer's FIRED status
commit in one transaction. If the process dies after commit but before
subscribers ran, the event is still in the store and can be replayed from
there on restart (the orchestrator's resume, built in the afternoon block).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from core.clock import Clock
from core.events import EventBus
from core.models import Event
from core.store import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
    key TEXT PRIMARY KEY,
    due_at TEXT NOT NULL,
    fire_type TEXT NOT NULL,
    mission_id TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'FIRED', 'CANCELLED')),
    fired_event_seq INTEGER
);
CREATE INDEX IF NOT EXISTS timers_due ON timers(status, due_at);
"""


class Scheduler:
    def __init__(self, db: Database, bus: EventBus, clock: Clock):
        self.db, self.bus, self.clock = db, bus, clock
        db.ensure_schema(SCHEMA)

    def schedule(self, key: str, due_at: datetime, fire_type: str, *, mission_id: str | None = None,
                 entity_id: str | None = None, payload: dict[str, Any] | None = None) -> bool:
        """Idempotent by key. Returns True if newly scheduled. A PENDING timer with the same key is
        moved to the new due time; FIRED/CANCELLED timers are left alone."""
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM timers WHERE key = ?", (key,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO timers VALUES (?,?,?,?,?,?, 'PENDING', NULL)",
                             (key, due_at.isoformat(), fire_type, mission_id, entity_id,
                              json.dumps(payload or {}, default=str)))
                return True
            if row["status"] == "PENDING":
                conn.execute("UPDATE timers SET due_at = ? WHERE key = ?", (due_at.isoformat(), key))
            return False

    def cancel(self, key: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE timers SET status = 'CANCELLED' WHERE key = ? AND status = 'PENDING'", (key,))
            return cur.rowcount == 1

    def pending(self) -> list[dict]:
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM timers WHERE status = 'PENDING' ORDER BY due_at")]

    async def fire_due(self) -> list[Event]:
        """Fire every PENDING timer due at or before clock.now(). Returns newly fired events."""
        now = self.clock.now()
        fired: list[Event] = []
        with self.db.transaction() as conn:
            # Compare as datetimes, not strings: due_at may carry different UTC offsets.
            due = [r for r in conn.execute("SELECT * FROM timers WHERE status = 'PENDING'")
                   if datetime.fromisoformat(r["due_at"]) <= now]
            due.sort(key=lambda r: datetime.fromisoformat(r["due_at"]))
            for row in due:
                event, created = self.bus.store.append_in(conn, Event(
                    type=row["fire_type"], mission_id=row["mission_id"], entity_id=row["entity_id"],
                    payload={**json.loads(row["payload_json"]), "timer_key": row["key"], "due_at": row["due_at"]},
                    occurred_at=now, key=f"timer:{row['key']}"))
                conn.execute("UPDATE timers SET status = 'FIRED', fired_event_seq = ? WHERE key = ?",
                             (event.seq, row["key"]))
                if created:
                    fired.append(event)
        for event in fired:
            await self.bus.notify(event)
        return fired

    async def run(self, stop: asyncio.Event, poll_seconds: float = 1.0) -> None:
        while not stop.is_set():
            await self.fire_due()
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
