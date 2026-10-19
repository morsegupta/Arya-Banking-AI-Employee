"""Approval Broker: the human decisions the employee asks for, and what happens after them.

An approval is one question for a human with full context and a proposed next step:
- `tool_call`: the Policy Gate held a concrete action (a message, a write). On approval
  the broker runs exactly that call, with the same arguments and idempotency key, so
  what the human approved is what happens: nothing is re-drafted afterwards.
- `decision`: a judgment the role reserves for humans (a concession, a plan, a review
  of quarantined content). The caller acts on the decision when it sees it.

Requests are idempotent on `key`: asking twice for the same thing yields one approval. An approval
whose approved action FAILED is reopened (PENDING again) when the same thing is asked for again.
Only a human actor may decide. Every step is an `approval.*` event.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.clock import Clock
from core.events import EventStore
from core.models import Event, new_id
from core.store import Database

Status = Literal["PENDING", "APPROVED", "REJECTED", "EXECUTED", "FAILED"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    mission_id TEXT,
    entity_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    note TEXT,
    result_json TEXT
);
"""


class Approval(BaseModel):
    id: str
    key: str
    mission_id: str | None = None
    entity_id: str | None = None
    kind: str
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Status = "PENDING"
    requested_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None
    result: dict[str, Any] | None = None


class ApprovalError(ValueError):
    pass


def _row(r: sqlite3.Row) -> Approval:
    return Approval(id=r["id"], key=r["key"], mission_id=r["mission_id"], entity_id=r["entity_id"], kind=r["kind"],
                    title=r["title"], payload=json.loads(r["payload_json"]), status=r["status"],
                    requested_at=r["requested_at"], decided_by=r["decided_by"], decided_at=r["decided_at"],
                    note=r["note"], result=json.loads(r["result_json"]) if r["result_json"] else None)


Executor = Callable[[Approval], Awaitable[dict[str, Any]]]


class ApprovalBroker:
    def __init__(self, db: Database, events: EventStore, clock: Clock):
        self.db, self.events, self.clock = db, events, clock
        db.ensure_schema(SCHEMA)
        self.executors: dict[str, Executor] = {}

    def _event(self, conn, type_: str, a: Approval, **extra) -> None:
        self.events.append_in(conn, Event(type=type_, mission_id=a.mission_id, entity_id=a.entity_id,
                                          occurred_at=self.clock.now(),
                                          payload={"approval_id": a.id, "kind": a.kind, "title": a.title, **extra}))

    def request(self, *, key: str, kind: str, title: str, payload: dict[str, Any] | None = None,
                mission_id: str | None = None, entity_id: str | None = None) -> Approval:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE key = ?", (key,)).fetchone()
            if row and row["status"] == "FAILED":
                # the approved action ran and failed: asking again must reach a human, not a closed card
                a = _row(row).model_copy(update={"status": "PENDING", "title": title, "payload": payload or {},
                                                 "requested_at": self.clock.now(), "decided_by": None,
                                                 "decided_at": None, "note": None, "result": None})
                conn.execute("UPDATE approvals SET status = 'PENDING', title = ?, payload_json = ?, requested_at = ?, "
                             "decided_by = NULL, decided_at = NULL, note = NULL, result_json = NULL WHERE id = ?",
                             (title, json.dumps(a.payload, default=str), a.requested_at.isoformat(), a.id))
                self._event(conn, "approval.requested", a, payload=a.payload, reopened=True)
                return a
            if row:
                return _row(row)
            a = Approval(id=new_id("apr"), key=key, mission_id=mission_id, entity_id=entity_id, kind=kind,
                         title=title, payload=payload or {}, requested_at=self.clock.now())
            conn.execute("INSERT INTO approvals (id, key, mission_id, entity_id, kind, title, payload_json, status, "
                         "requested_at) VALUES (?,?,?,?,?,?,?, 'PENDING', ?)",
                         (a.id, key, mission_id, entity_id, kind, title, json.dumps(a.payload, default=str),
                          a.requested_at.isoformat()))
            self._event(conn, "approval.requested", a, payload=a.payload)
        return a

    def get(self, approval_id: str) -> Approval:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"no approval {approval_id}")
        return _row(row)

    def by_key(self, key: str) -> Approval | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE key = ?", (key,)).fetchone()
        return _row(row) if row else None

    def list(self, *, status: str | None = None, mission_id: str | None = None, kind: str | None = None,
             entity_id: str | None = None) -> list[Approval]:
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        for col, val in (("status", status), ("mission_id", mission_id), ("kind", kind), ("entity_id", entity_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        with self.db.read() as conn:
            return [_row(r) for r in conn.execute(sql + " ORDER BY requested_at, id", args)]

    def decide(self, approval_id: str, *, approve: bool, by: str, actor: str = "human", note: str = "") -> Approval:
        if actor != "human":
            raise ApprovalError("only a human can decide an approval")
        if not by:
            raise ApprovalError("a decision needs the name of the person who made it")
        now = self.clock.now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalError(f"no approval {approval_id}")
            a = _row(row)
            if a.status != "PENDING":
                raise ApprovalError(f"{approval_id} is already {a.status}")
            status = "APPROVED" if approve else "REJECTED"
            conn.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
                         (status, by, now.isoformat(), note, approval_id))
            self._event(conn, "approval.decided", a, status=status, by=by, note=note)
        return self.get(approval_id)

    def annotate(self, approval_id: str, patch: dict[str, Any]) -> Approval:
        """Add context to a pending approval (e.g. what to do after the held call runs)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            payload = {**json.loads(row["payload_json"]), **patch}
            conn.execute("UPDATE approvals SET payload_json = ? WHERE id = ?", (json.dumps(payload, default=str),
                                                                               approval_id))
        return self.get(approval_id)

    def mark(self, approval_id: str, status: Status, result: dict[str, Any] | None = None) -> Approval:
        with self.db.transaction() as conn:
            a = _row(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
            conn.execute("UPDATE approvals SET status = ?, result_json = ? WHERE id = ?",
                         (status, json.dumps(result or {}, default=str), approval_id))
            self._event(conn, f"approval.{status.lower()}", a, result=result or {})
        return self.get(approval_id)

    def register_executor(self, kind: str, fn: Executor) -> None:
        """What to run when an approval of this kind is approved (e.g. the held tool call)."""
        self.executors[kind] = fn

    async def execute_approved(self, approval_id: str) -> Approval:
        a = self.get(approval_id)
        if a.status != "APPROVED":
            raise ApprovalError(f"{approval_id} is {a.status}, not APPROVED")
        fn = self.executors.get(a.kind)
        if fn is None:
            return a                                    # a decision: the caller acts on it
        result = await fn(a)
        return self.mark(a.id, "EXECUTED" if result.get("ok") else "FAILED", result)
