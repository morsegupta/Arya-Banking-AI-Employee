"""Typed contracts moved between the orchestrator, agents and tools.

ROLE-AGNOSTIC: nothing here may name a domain concept. Domain meaning arrives
through Role Packs as data (entity kinds, pipeline states, task types).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# --- risk tiers (architecture §8.2) ------------------------------------------------

Risk = Literal["read", "write", "external_comm", "financial", "irreversible"]
RISK_ORDER: dict[str, int] = {"read": 0, "write": 1, "external_comm": 2, "financial": 3, "irreversible": 4}

# Preference order when several tools can do the same job (ADR-7: API -> DOM -> vision).
ToolKind = Literal["api", "dom", "vision", "file", "channel"]


# --- mission ----------------------------------------------------------------------

class MissionStatus(StrEnum):
    DRAFT = "DRAFT"
    CLARIFYING = "CLARIFYING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ESCALATED = "ESCALATED"
    COMPLETED = "COMPLETED"


class MissionSpec(BaseModel):
    """What the Mission Agent understood from a brief."""
    objective: str
    scope: str                      # human-readable entity selection rule
    target_value: int | None = None  # in the role pack's unit (smallest currency unit, count...); None if not numeric
    target_unit: str | None = None
    deadline: datetime | None = None
    success_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Mission(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mis"))
    role: str                       # role pack name
    company: str                    # company pack name
    brief: str
    status: MissionStatus = MissionStatus.DRAFT
    spec: MissionSpec | None = None
    created_at: datetime


# --- entity -------------------------------------------------------------------------

class Entity(BaseModel):
    """One thing a mission moves through the role pack's pipeline."""
    id: str = Field(default_factory=lambda: new_id("ent"))
    mission_id: str
    kind: str                       # from role pack, e.g. what role.yaml calls `entity`
    external_ref: str               # id in the system of record
    state: str                      # a pipeline state name from the role pack
    value: int = 0                  # amount the entity contributes to the mission target
    data: dict[str, Any] = Field(default_factory=dict)


# --- task ---------------------------------------------------------------------------

class TaskStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    REPLANNING = "REPLANNING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tsk"))
    mission_id: str
    entity_id: str | None = None
    type: str                       # task type from the role pack
    goal: str
    success_criteria: list[str] = Field(default_factory=list)
    apps: list[str] = Field(default_factory=list)   # capability scope: only these apps' tools
    status: TaskStatus = TaskStatus.RECEIVED
    attempts: int = 0
    max_actions: int = 40


# --- evidence and results ---------------------------------------------------------------

class Evidence(BaseModel):
    kind: Literal["screenshot", "file", "message", "record", "snapshot", "note"]
    ref: str                        # path, message id, record id...
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None
    error_kind: Literal["transient", "permanent", "denied", "invalid_args"] | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    attempts: int = 1
    replayed: bool = False          # true when an idempotency key returned a stored result
    verified: bool | None = None    # result of tool.verify, None if the tool has no verifier
    untrusted: bool = False         # output contains third-party content (emails, pages, PDFs)


class TaskResult(BaseModel):
    task_id: str
    summary: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class FailureReport(BaseModel):
    task_id: str
    stage: str
    error: str
    tried: list[str] = Field(default_factory=list)
    suggestion: str | None = None


class CriterionResult(BaseModel):
    criterion: str
    passed: bool
    evidence: list[Evidence] = Field(default_factory=list)
    note: str | None = None


class VerificationReport(BaseModel):
    subject_id: str                 # task or entity id
    passed: bool
    criteria: list[CriterionResult]
    confidence: float = Field(ge=0, le=1)


# --- conversation ------------------------------------------------------------------

class ProposedTransition(BaseModel):
    entity_id: str
    to_state: str
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    channel: str
    to: str
    subject: str | None = None
    body: str
    attachments: list[str] = Field(default_factory=list)   # file paths
    in_reply_to: str | None = None
    proposed_transition: ProposedTransition | None = None
    extracted_facts: dict[str, Any] = Field(default_factory=dict)


# --- events ---------------------------------------------------------------------------

class Event(BaseModel):
    """Append-only record of something that happened. Also what wakes the employee."""
    id: str = Field(default_factory=lambda: new_id("evt"))
    seq: int | None = None          # assigned by the store
    type: str                       # dotted, e.g. "task.started", "timer.fired", "message.received"
    mission_id: str | None = None
    entity_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime           # business (simulated) time
    key: str | None = None          # optional dedupe key: same key -> stored once
    hash: str | None = None         # hash chain, assigned by the store
