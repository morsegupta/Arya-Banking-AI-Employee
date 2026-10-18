"""Mission state machine (architecture §5.1), enforced in code.

    DRAFT -> CLARIFYING -> DRAFT -> AWAITING_PLAN_APPROVAL -> ACTIVE <-> PAUSED
                                                            ACTIVE -> ESCALATED -> ACTIVE
                                                            ACTIVE -> COMPLETED

- Approving a plan (and resuming from ESCALATED) is human-only.
- COMPLETED needs the verified value to reach the target. "Verified" is passed in
  by the caller from its ledger of settled, verified entities, never from promises.
- Every change commits with a `mission.*` event in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from core.clock import Clock
from core.events import EventStore
from core.models import Event, Mission, MissionPlan, MissionSpec, MissionStatus
from core.store import Database

S = MissionStatus
Actor = Literal["agent", "human", "system"]

# (from, to) -> who may take it
TRANSITIONS: dict[tuple[MissionStatus, MissionStatus], set[str]] = {
    (S.DRAFT, S.CLARIFYING): {"agent", "system"},
    (S.CLARIFYING, S.DRAFT): {"human"},
    (S.DRAFT, S.AWAITING_PLAN_APPROVAL): {"agent", "system"},
    (S.AWAITING_PLAN_APPROVAL, S.ACTIVE): {"human"},
    (S.AWAITING_PLAN_APPROVAL, S.DRAFT): {"human"},          # plan rejected: replan
    (S.ACTIVE, S.PAUSED): {"human", "system"},
    (S.PAUSED, S.ACTIVE): {"human"},
    (S.ACTIVE, S.ESCALATED): {"agent", "system"},
    (S.ESCALATED, S.ACTIVE): {"human"},
    (S.ACTIVE, S.COMPLETED): {"system"},
}

MISSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    company TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_json TEXT,
    plan_json TEXT,
    selection_json TEXT NOT NULL DEFAULT '{}',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
"""


class MissionTransitionError(ValueError):
    pass


def _row(row: sqlite3.Row) -> Mission:
    return Mission(id=row["id"], role=row["role"], company=row["company"], brief=row["brief"],
                   status=MissionStatus(row["status"]),
                   spec=MissionSpec.model_validate_json(row["spec_json"]) if row["spec_json"] else None,
                   plan=MissionPlan.model_validate_json(row["plan_json"]) if row["plan_json"] else None,
                   selection=json.loads(row["selection_json"]), data=json.loads(row["data_json"]),
                   created_at=row["created_at"])


class MissionStore:
    def __init__(self, db: Database, events: EventStore, clock: Clock):
        self.db, self.events, self.clock = db, events, clock
        db.ensure_schema(MISSION_SCHEMA)

    def create(self, role: str, company: str, brief: str) -> Mission:
        m = Mission(role=role, company=company, brief=brief, created_at=self.clock.now())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO missions (id, role, company, brief, status, created_at) VALUES (?,?,?,?,?,?)",
                         (m.id, role, company, brief, m.status.value, m.created_at.isoformat()))
            self.events.append_in(conn, Event(type="mission.created", mission_id=m.id, occurred_at=m.created_at,
                                              payload={"brief": brief, "role": role, "company": company}))
        return m

    def get(self, mission_id: str) -> Mission:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise KeyError(mission_id)
        return _row(row)

    def list(self) -> list[Mission]:
        with self.db.read() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM missions ORDER BY created_at")]

    def update(self, mission_id: str, *, spec: MissionSpec | None = None, plan: MissionPlan | None = None,
               selection: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
               brief: str | None = None, event: str | None = None, payload: dict[str, Any] | None = None) -> Mission:
        """Change the mission's content (not its status). `data` is merged; the others replace."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if row is None:
                raise KeyError(mission_id)
            sets, args = [], []
            if spec is not None:
                sets.append("spec_json = ?"), args.append(spec.model_dump_json())
            if plan is not None:
                sets.append("plan_json = ?"), args.append(plan.model_dump_json())
            if selection is not None:
                sets.append("selection_json = ?"), args.append(json.dumps(selection, default=str))
            if data is not None:
                sets.append("data_json = ?"), args.append(json.dumps({**json.loads(row["data_json"]), **data},
                                                                    default=str))
            if brief is not None:
                sets.append("brief = ?"), args.append(brief)
            if sets:
                conn.execute(f"UPDATE missions SET {', '.join(sets)}, version = version + 1 WHERE id = ?",
                             [*args, mission_id])
            if event:
                self.events.append_in(conn, Event(type=event, mission_id=mission_id, occurred_at=self.clock.now(),
                                                  payload=payload or {}))
        return self.get(mission_id)

    def transition(self, mission_id: str, to: MissionStatus, *, actor: Actor, reason: str = "",
                   verified_value: int | None = None) -> Mission:
        now = self.clock.now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if row is None:
                raise KeyError(mission_id)
            m = _row(row)
            allowed = TRANSITIONS.get((m.status, to))

            def refuse(why: str):
                self.events.append_in(conn, Event(type="mission.transition_refused", mission_id=mission_id,
                                                  occurred_at=now, payload={"from": m.status, "to": to,
                                                                            "actor": actor, "reason": why}))
                return MissionTransitionError(f"{m.status} -> {to}: {why}")

            if allowed is None:
                err = refuse("no such mission transition")
            elif actor not in allowed:
                err = refuse(f"needs actor {sorted(allowed)}, got {actor}")
            elif to == S.COMPLETED and not _target_reached(m, verified_value):
                err = refuse(f"verified value {verified_value} below target "
                             f"{m.spec.target_value if m.spec else None}")
            else:
                err = None
                conn.execute("UPDATE missions SET status = ?, version = version + 1 WHERE id = ?",
                             (to.value, mission_id))
                self.events.append_in(conn, Event(type="mission.transitioned", mission_id=mission_id, occurred_at=now,
                                                  payload={"from": m.status, "to": to, "actor": actor,
                                                           "reason": reason, "verified_value": verified_value}))
        if err:
            raise err
        return self.get(mission_id)


def _target_reached(m: Mission, verified: int | None) -> bool:
    if verified is None or m.spec is None or m.spec.target_value is None:
        return False
    return verified >= m.spec.target_value


# --- progress and forecast ----------------------------------------------------------------

@dataclass
class Progress:
    target: int | None
    verified: int                     # settled AND verified (the only number that counts)
    open_by_state: dict[str, int]     # remaining value per pipeline state
    expected: int                     # verified + probability-weighted open value
    gap: int | None                   # target - verified
    expected_shortfall: int | None    # target - expected (positive = likely to miss)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def progress(target: int | None, verified: int, open_by_state: dict[str, int],
             weights: dict[str, float]) -> Progress:
    """Forecast = verified + sum(open value in state * probability that state turns into money)."""
    expected = verified + int(sum(v * float(weights.get(s, 0.0)) for s, v in open_by_state.items()))
    return Progress(target=target, verified=verified, open_by_state=open_by_state, expected=expected,
                    gap=None if target is None else max(0, target - verified),
                    expected_shortfall=None if target is None else target - expected)
