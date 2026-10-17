"""Application flows that connect agents, tools and the pipeline.

Driven by the role pack (task_types, entity_source, outreach), so the same
flows serve any role. The orchestrator (evening block) will call these from
events; for now the CLI calls them directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from agents.conversation_agent import ConversationAgent, DraftRejected
from agents.operator_agent import OperatorAgent
from apps.runtime import Runtime
from core.llm import Budget, LLMError
from core.models import Entity, FailureReport, Task, TaskResult
from core.pipeline import TransitionResult
from tools.app_map import load_app_maps
from tools.base import RunContext


def role_cfg(rt: Runtime) -> dict[str, Any]:
    return yaml.safe_load((rt.role_dir / "role.yaml").read_text())


def role_prompt(rt: Runtime, name: str) -> str:
    path = role_cfg(rt).get("prompts", {}).get(name)
    return (rt.role_dir / path).read_text() if path else ""


def company_facts(rt: Runtime) -> dict[str, Any]:
    return yaml.safe_load((rt.company_dir / "facts.yaml").read_text())


def format_money(minor_units: int, symbol: str = "₹") -> str:
    """Indian digit grouping for an amount in minor units (paise)."""
    whole, frac = divmod(abs(int(minor_units)), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail, groups = digits[:-3], digits[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{'-' if minor_units < 0 else ''}{symbol}{digits}.{frac:02d}"


# --- operator tasks --------------------------------------------------------------------

def make_operator(rt: Runtime) -> OperatorAgent:
    notes = {name: m.notes for name, m in load_app_maps(rt.company_dir / "apps").items()}
    return OperatorAgent(rt.llm, rt.registry, rt.events, rt.clock, role_prompt=role_prompt(rt, "operator"),
                         app_notes=notes, artifacts_dir=rt.artifacts_dir)


async def run_task(rt: Runtime, mission_id: str, task_type: str, goal: str | None = None,
                   success_criteria: list[str] | None = None, entity_id: str | None = None,
                   context: dict[str, Any] | None = None) -> TaskResult | FailureReport:
    spec = role_cfg(rt)["task_types"][task_type]
    task = Task(mission_id=mission_id, entity_id=entity_id, type=task_type, goal=goal or spec["goal"],
                success_criteria=success_criteria or [], apps=spec["apps"], max_actions=spec.get("max_actions", 15))
    try:
        return await make_operator(rt).run(task, context=context)
    finally:
        await rt.sessions.end_task(task.id)


# --- entities -------------------------------------------------------------------------------

@dataclass
class LoadedEntity:
    entity: Entity
    facts: dict[str, Any]
    documents: list[str]
    contact: dict[str, str]


async def _read(rt: Runtime, tool: str, args: dict[str, Any], mission_id: str) -> dict[str, Any]:
    t = rt.registry.get(tool)
    res = await rt.registry.invoke(tool, args, RunContext(mission_id=mission_id, task_id=None, allowed_apps={t.app},
                                                          artifacts_dir=rt.artifacts_dir))
    if not res.ok:
        raise RuntimeError(f"{tool} failed: {res.error}")
    return res.output


async def load_entity(rt: Runtime, mission_id: str, external_ref: str) -> LoadedEntity:
    """Read the entity from its system of record and register it in the pipeline (idempotent)."""
    src = role_cfg(rt)["entity_source"]
    record = await _read(rt, src["detail_tool"], {src["detail_arg"]: external_ref}, mission_id)
    facts = {k: record.get(k) for k in src["facts"]}
    cp = src["counterparty"]
    party = await _read(rt, cp["tool"], {cp["arg"]: facts[cp["from_fact"]]}, mission_id)
    contact = {k: party.get(cp[k]) for k in ("name", "email", "whatsapp")}
    documents = [record[d] for d in src.get("documents", []) if record.get(d)]
    entity = rt.pipeline.add_entity(
        mission_id, external_ref, state=rt.pipeline.spec.initial[0], value=int(record[src["value"]]),
        data={"balance": int(record[src["value"]]), "record": facts, "contact": contact, "documents": documents})
    return LoadedEntity(entity, facts, documents, contact)


def conversation_facts(rt: Runtime, loaded: LoadedEntity) -> dict[str, Any]:
    money = set(role_cfg(rt)["entity_source"].get("money_facts", []))
    out = {k: (format_money(v) if k in money and v is not None else v) for k, v in loaded.facts.items()}
    out["contact_first_name"] = (loaded.contact.get("name") or "").split(" ")[0]
    return out


# --- outreach ---------------------------------------------------------------------------------

@dataclass
class OutreachOutcome:
    ok: bool
    channel: str | None
    message: dict[str, Any] | None
    transition: TransitionResult | None
    attempts: list[str]


async def outreach(rt: Runtime, mission_id: str, external_ref: str, *, step: str = "outreach1",
                   purpose: str | None = None, transition_to: str | None = "CONTACTED",
                   extra_facts: dict[str, Any] | None = None) -> OutreachOutcome:
    """Draft and send one message, falling back across channels; optionally move the entity."""
    cfg = role_cfg(rt)["outreach"]
    loaded = await load_entity(rt, mission_id, external_ref)
    agent = ConversationAgent(rt.llm, role_prompt=role_prompt(rt, "conversation"),
                              signature=rt.company_cfg["channels"].get("signature", ""))
    facts, cfacts = {**conversation_facts(rt, loaded), **(extra_facts or {})}, company_facts(rt)
    attempts: list[str] = []
    budget = Budget(40_000)
    for channel in cfg["channel_order"]:
        to = loaded.contact.get(channel)
        if not to:
            attempts.append(f"{channel}: no address")
            continue
        try:
            turn = await agent.draft(channel=channel, to=to, purpose=purpose or cfg["purpose"], entity_facts=facts,
                                     company_facts=cfacts, documents=loaded.documents, budget=budget)
        except (DraftRejected, LLMError) as exc:
            attempts.append(f"{channel}: draft rejected: {exc}")
            continue
        ctx = RunContext(mission_id=mission_id, task_id=None, entity_id=loaded.entity.id, allowed_apps={channel},
                         idempotency_key=f"{mission_id}:{loaded.entity.id}:{step}:{channel}",
                         artifacts_dir=rt.artifacts_dir)
        args = {"to": turn.to, "body": turn.body, "attachments": turn.attachments}
        if turn.subject:
            args["subject"] = turn.subject
        res = await rt.registry.invoke(f"{channel}.send", args, ctx)
        if not res.ok:
            attempts.append(f"{channel}: {res.error_kind}: {res.error}")
            if res.error_kind == "permanent":
                continue                     # L2 fallback: next channel
            break
        attempts.append(f"{channel}: delivered {res.output['id']}")
        if transition_to is None:
            return OutreachOutcome(True, channel, res.output, None, attempts)
        tr = rt.pipeline.propose(loaded.entity.id, transition_to,
                                 {"outreach": {"delivered": True, "message_id": res.output["id"],
                                               "channel": channel, "thread_id": res.output["thread_id"]}},
                                 facts={"last_outreach": {"channel": channel, "message_id": res.output["id"],
                                                          "thread_id": res.output["thread_id"]}})
        return OutreachOutcome(True, channel, res.output, tr, attempts)
    return OutreachOutcome(False, None, None, None, attempts)
