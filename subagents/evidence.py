"""Evidence Collector: keep what proves each step, so "why is this marked done?" has an answer.

Each item records what it is (a file, a message, a record, a verification), where it lives,
a one-line summary and, for files, a SHA-256 of the bytes when it was stored, so later
tampering with an artifact is detectable. Plain code, no LLM.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.clock import Clock
from core.models import Evidence, new_id
from core.store import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    entity_id TEXT,
    subject TEXT NOT NULL,          -- what it supports, e.g. "transition:PROMISED", "verification"
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    summary TEXT NOT NULL,
    data_json TEXT NOT NULL,
    sha256 TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_entity ON evidence (entity_id, at);
"""


def file_digest(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


class EvidenceCollector:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock
        db.ensure_schema(SCHEMA)

    def collect(self, items: list[Evidence], *, subject: str, mission_id: str | None = None,
                entity_id: str | None = None) -> list[str]:
        ids = []
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            for ev in items:
                eid = new_id("evd")
                digest = file_digest(ev.ref) if ev.kind in {"file", "screenshot"} else None
                conn.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (eid, mission_id, entity_id, subject, ev.kind, ev.ref, ev.summary,
                              json.dumps(ev.data, default=str), digest, now))
                ids.append(eid)
        return ids

    def for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE entity_id = ? ORDER BY at, id",
                                                  (entity_id,))]

    def intact(self, evidence_id: str) -> bool | None:
        """Re-hash a collected file: True if unchanged, False if changed or gone, None if not a file."""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None or row["sha256"] is None:
            return None
        return file_digest(row["ref"]) == row["sha256"]
