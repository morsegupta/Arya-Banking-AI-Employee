"""Per-entity pipeline: states and guarded transitions come from the role pack; code enforces them.

"The LLM proposes a transition; code checks the guard" (architecture §5.2).

- A transition must exist in role.yaml from the entity's current state.
- Its actor must match: `human` transitions can never be taken by an agent.
- Its guard must pass against the evidence context supplied with the proposal.
- On success, the state change and an `entity.transitioned` event commit in
  one transaction (write-ahead), timers for the new state are scheduled and
  timers belonging to the old state are cancelled.
- On refusal, an `entity.transition_refused` event records which clauses failed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from core.clock import Clock
from core.events import EventStore
from core.guards import MISSING, GuardResult, compile_guard, evaluate, lookup
from core.models import Entity, Event
from core.scheduler import Scheduler
from core.store import Database

Actor = Literal["agent", "human", "system"]


class TransitionSpec(BaseModel):
    from_: list[str] = Field(alias="from")
    to: str
    guard: Any = None
    actor: Actor = "agent"

    @field_validator("from_", mode="before")
    @classmethod
    def listify(cls, v):
        return [v] if isinstance(v, str) else v


class TimerSpec(BaseModel):
    on: str                 # state that arms the timer
    at: str                 # "<path> [+|- N(d|h|m)]", e.g. "facts.promise_date - 1d", "entered_at + 3d"
    do: str                 # fired event becomes "<do>" with entity context
    name: str | None = None
    min_lead: str | None = None   # e.g. "12h": not armed if it would fire sooner than this after entering


class PipelineSpec(BaseModel):
    entity: str
    states: list[str]
    initial: list[str]
    terminal: list[str] = Field(default_factory=list)
    constants: dict[str, Any] = Field(default_factory=dict)
    transitions: list[TransitionSpec]
    timers: list[TimerSpec] = Field(default_factory=list)
    timer_time_of_day: str = "10:00"


def load_pipeline(role_yaml: Path | str) -> PipelineSpec:
    data = yaml.safe_load(Path(role_yaml).read_text())
    # YAML 1.1 reads a bare `on:` key as boolean True; pack authors should not have to know that.
    timers = [{("on" if k is True else k): v for k, v in t.items()} for t in data.get("timers", [])]
    spec = PipelineSpec.model_validate({"entity": data["entity"], **data["pipeline"], "timers": timers})
    states = set(spec.states)
    for t in spec.transitions:
        for s in [*t.from_, t.to]:
            if s not in states:
                raise ValueError(f"role pack transition uses unknown state {s!r}")
    for s in spec.initial + spec.terminal + [tm.on for tm in spec.timers]:
        if s not in states:
            raise ValueError(f"role pack references unknown state {s!r}")
    return spec


ENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (mission_id, external_ref)
);
"""


@dataclass
class TransitionResult:
    ok: bool
    entity: Entity
    from_state: str
    to_state: str
    guard: GuardResult | None
    reason: str = ""


_AT = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(?:([+-])\s*(\d+)\s*([dhm]))?\s*$")


class Pipeline:
    def __init__(self, spec: PipelineSpec, db: Database, events: EventStore, scheduler: Scheduler,
                 clock: Clock, tz: tzinfo):
        self.spec, self.db, self.events, self.scheduler, self.clock, self.tz = spec, db, events, scheduler, clock, tz
        self._guards = {(f, t.to): compile_guard(t.guard) for t in spec.transitions for f in t.from_}
        self._actors = {(f, t.to): t.actor for t in spec.transitions for f in t.from_}
        db.ensure_schema(ENTITY_SCHEMA)

    # --- entities ---------------------------------------------------------------------

    def add_entity(self, mission_id: str, external_ref: str, *, state: str, value: int = 0,
                   data: dict[str, Any] | None = None) -> Entity:
        """Idempotent on (mission, external_ref): re-adding returns the existing entity."""
        if state not in self.spec.initial:
            raise ValueError(f"{state} is not an initial state ({self.spec.initial})")
        now = self.clock.now()
        entity = Entity(mission_id=mission_id, kind=self.spec.entity, external_ref=external_ref, state=state,
                        value=value, data=data or {})
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM entities WHERE mission_id = ? AND external_ref = ?",
                                    (mission_id, external_ref)).fetchone()
            if existing:
                return _row_entity(existing)
            conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,1)",
                         (entity.id, mission_id, entity.kind, external_ref, state, value,
                          json.dumps(entity.data, default=str), now.isoformat()))
            self.events.append_in(conn, Event(type="entity.added", mission_id=mission_id, entity_id=entity.id,
                                              occurred_at=now, payload={"external_ref": external_ref,
                                                                        "state": state, "value": value}))
        self._arm_timers(entity, state, {}, now)
        return entity

    def get(self, entity_id: str) -> Entity:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return _row_entity(row)

    def list(self, mission_id: str, state: str | None = None) -> list[Entity]:
        sql, args = "SELECT * FROM entities WHERE mission_id = ?", [mission_id]
        if state:
            sql, args = sql + " AND state = ?", [*args, state]
        with self.db.read() as conn:
            return [_row_entity(r) for r in conn.execute(sql + " ORDER BY external_ref", args)]

    def update_data(self, entity_id: str, patch: dict[str, Any]) -> Entity:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT data_json FROM entities WHERE id = ?", (entity_id,)).fetchone()
            data = {**json.loads(row["data_json"]), **patch}
            conn.execute("UPDATE entities SET data_json = ?, version = version + 1 WHERE id = ?",
                         (json.dumps(data, default=str), entity_id))
        return self.get(entity_id)

    # --- transitions ----------------------------------------------------------------------

    def allowed_targets(self, state: str) -> list[str]:
        return [to for (f, to) in self._guards if f == state]

    def propose(self, entity_id: str, to_state: str, context: dict[str, Any], *, actor: Actor = "agent",
                facts: dict[str, Any] | None = None) -> TransitionResult:
        """Try to move an entity. `context` holds the evidence namespaces the guard reads
        (e.g. classifier, matcher, verifier, evidence); `facts` are merged into entity data on success."""
        now = self.clock.now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                raise KeyError(entity_id)
            entity = _row_entity(row)
            current = entity.state
            key = (current, to_state)

            def refuse(reason: str, guard: GuardResult | None = None) -> TransitionResult:
                self.events.append_in(conn, Event(
                    type="entity.transition_refused", mission_id=entity.mission_id, entity_id=entity.id,
                    occurred_at=now, payload={"from": current, "to": to_state, "actor": actor, "reason": reason,
                                              "failed_clauses": guard.failures() if guard else []}))
                return TransitionResult(False, entity, current, to_state, guard, reason)

            if key not in self._guards:
                return refuse(f"no transition {current} -> {to_state} in the role pack")
            required = self._actors[key]
            if required == "human" and actor != "human":
                return refuse(f"{current} -> {to_state} is human-only")
            if required == "system" and actor == "agent":
                return refuse(f"{current} -> {to_state} is taken by the system, not an agent")

            merged_facts = {**entity.data.get("facts", {}), **(facts or {})}
            guard_ctx = {**self.spec.constants, **context, "facts": merged_facts,
                         "entity": {"state": current, "value": entity.value, **entity.data}}
            guard = evaluate(self._guards[key], guard_ctx)
            if not guard.passed:
                return refuse("guard failed", guard)

            data = {**entity.data, "facts": merged_facts}
            conn.execute("UPDATE entities SET state = ?, data_json = ?, entered_at = ?, version = version + 1 "
                         "WHERE id = ?", (to_state, json.dumps(data, default=str), now.isoformat(), entity.id))
            self.events.append_in(conn, Event(
                type="entity.transitioned", mission_id=entity.mission_id, entity_id=entity.id, occurred_at=now,
                payload={"from": current, "to": to_state, "actor": actor, "facts": facts or {},
                         "guard": [c.__dict__ for c in guard.clauses]}))
        moved = self.get(entity_id)
        self._disarm_timers(moved, current)
        self._arm_timers(moved, to_state, merged_facts, now)
        return TransitionResult(True, moved, current, to_state, guard)

    # --- timers ---------------------------------------------------------------------------

    def _timer_key(self, entity: Entity, spec: TimerSpec) -> str:
        return f"{entity.id}:{spec.on}:{spec.name or spec.do}"

    def _resolve_at(self, at: str, facts: dict, entered: datetime) -> datetime | None:
        m = _AT.match(at)
        if not m:
            raise ValueError(f"bad timer expression {at!r}")
        path, sign, n, unit = m.groups()
        base = entered if path == "entered_at" else lookup({"facts": facts}, path)
        if base is None or base is MISSING:
            return None
        if isinstance(base, str):
            base = datetime.fromisoformat(base) if "T" in base else date.fromisoformat(base)
        if isinstance(base, date) and not isinstance(base, datetime):
            hh, mm = (int(x) for x in self.spec.timer_time_of_day.split(":"))
            base = datetime.combine(base, time(hh, mm), tzinfo=self.tz)
        if sign:
            delta = {"d": timedelta(days=int(n)), "h": timedelta(hours=int(n)), "m": timedelta(minutes=int(n))}[unit]
            base = base + delta if sign == "+" else base - delta
        return base

    def _arm_timers(self, entity: Entity, state: str, facts: dict, entered: datetime) -> None:
        for spec in self.spec.timers:
            if spec.on != state:
                continue
            due = self._resolve_at(spec.at, facts, entered)
            if due is None:
                continue
            if spec.min_lead and due - entered < _duration(spec.min_lead):
                continue
            self.scheduler.schedule(self._timer_key(entity, spec), due, spec.do, mission_id=entity.mission_id,
                                    entity_id=entity.id, payload={"state": state})

    def _disarm_timers(self, entity: Entity, state: str) -> None:
        for spec in self.spec.timers:
            if spec.on == state:
                self.scheduler.cancel(self._timer_key(entity, spec))


def _duration(text: str) -> timedelta:
    n, unit = int(text[:-1]), text[-1]
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _row_entity(row: sqlite3.Row) -> Entity:
    return Entity(id=row["id"], mission_id=row["mission_id"], kind=row["kind"], external_ref=row["external_ref"],
                  state=row["state"], value=row["value"], data=json.loads(row["data_json"]))
