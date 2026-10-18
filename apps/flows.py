"""Application flows that connect agents, tools and the pipeline.

Driven by the role pack (task_types, entity_source, outreach), so the same
flows serve any role. The orchestrator (evening block) will call these from
events; for now the CLI calls them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from agents.conversation_agent import ConversationAgent, DraftRejected
from agents.operator_agent import OperatorAgent
from apps.runtime import Runtime
from core.llm import Budget, LLMError
from core.models import ConversationTurn, Entity, Evidence, FailureReport, Task, TaskResult
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
    def quarantine_task(task, screening) -> None:
        rt.gate.quarantine(f"task:{task.id}", "a page or file in this task looked like a prompt-injection attempt",
                           screening.score)

    return OperatorAgent(rt.llm, rt.registry, rt.events, rt.clock, role_prompt=role_prompt(rt, "operator"),
                         app_notes=notes, artifacts_dir=rt.artifacts_dir,
                         screen=rt.obs_guard.screen if rt.obs_guard else None,
                         on_flag=quarantine_task if rt.gate else None, tokenizer=rt.tokenizer)


async def run_task(rt: Runtime, mission_id: str, task_type: str, goal: str | None = None,
                   success_criteria: list[str] | None = None, entity_id: str | None = None,
                   context: dict[str, Any] | None = None,
                   write_scope: dict[str, list[dict[str, Any]]] | None = None) -> TaskResult | FailureReport:
    spec = role_cfg(rt)["task_types"][task_type]
    task = Task(mission_id=mission_id, entity_id=entity_id, type=task_type, goal=goal or spec["goal"],
                success_criteria=success_criteria or [], apps=spec["apps"], max_actions=spec.get("max_actions", 15),
                write_scope=write_scope or {})
    try:
        return await make_operator(rt).run(task, context=context, budget=rt.budget(mission_id, 80_000))
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
    transition: TransitionResult | None          # first entity's transition (single-entity callers)
    attempts: list[str]
    transitions: list[TransitionResult] = field(default_factory=list)
    held: str | None = None              # approval id: a human must approve this exact message first
    deferred_until: str | None = None    # policy: not now (quiet hours, contact cap); try again later
    draft: dict[str, Any] | None = None  # draft_only: the message that would be sent
    replayed: bool = False               # this exact step had already been sent and applied


def group_facts(rt: Runtime, loaded: list[LoadedEntity]) -> dict[str, Any]:
    """Facts for one message covering several entities of one counterparty: the facts they share once,
    the rest per item, and the total of the money facts."""
    if len(loaded) == 1:
        return conversation_facts(rt, loaded[0])
    per_item = [conversation_facts(rt, item) for item in loaded]
    shared = {k: v for k, v in per_item[0].items() if all(p.get(k) == v for p in per_item)}
    items = [{k: v for k, v in p.items() if k not in shared} for p in per_item]
    src = role_cfg(rt)["entity_source"]
    totals = {f"total_{k}": format_money(sum(int(i.facts.get(k) or 0) for i in loaded))
              for k in src.get("money_facts", [])}
    return {**shared, "items": items, "item_count": len(items), **totals}


def group_key(entities: list[Entity]) -> str:
    return "+".join(sorted(e.id for e in entities))


async def outreach(rt: Runtime, mission_id: str, external_ref: str | list[str], *, step: str = "outreach1",
                   purpose: str | None = None, transition_to: str | None = "CONTACTED",
                   extra_facts: dict[str, Any] | None = None, extra_documents: list[str] | None = None,
                   channels: list[str] | None = None, draft_only: bool = False,
                   prepared: dict[str, Any] | None = None, examples: list[str] | None = None) -> OutreachOutcome:
    """Draft and send one message about one entity, or several entities of the same counterparty,
    falling back across channels; optionally move every entity it covers.

    draft_only: draft for the first reachable channel and return it without sending (for approval).
    prepared:   a message a human approved: sent verbatim on its channel, no re-drafting. If that
                channel fails, the next channel gets a fresh draft (and the Policy Gate as usual).
    examples:   approved messages the draft should match in tone and structure."""
    cfg = role_cfg(rt)["outreach"]
    refs = [external_ref] if isinstance(external_ref, str) else list(external_ref)
    loaded = [await load_entity(rt, mission_id, ref) for ref in refs]
    contact = loaded[0].contact
    if any(item.contact != contact for item in loaded[1:]):
        raise ValueError(f"one message can only cover one counterparty: {refs}")
    agent = ConversationAgent(rt.llm, role_prompt=role_prompt(rt, "conversation"),
                              signature=rt.company_cfg["channels"].get("signature", ""), tokenizer=rt.tokenizer)
    facts, cfacts = {**group_facts(rt, loaded), **(extra_facts or {})}, company_facts(rt)
    documents = [d for item in loaded for d in item.documents] + list(extra_documents or [])
    entities = [item.entity for item in loaded]
    key = loaded[0].entity.id if len(loaded) == 1 else group_key(entities)
    cp_field = role_cfg(rt)["entity_source"].get("counterparty", {}).get("from_fact")
    counterparty = str(loaded[0].facts.get(cp_field)) if cp_field and loaded[0].facts.get(cp_field) else None
    attempts: list[str] = []
    budget = rt.budget(mission_id, 40_000)
    avoid: list[str] = []
    for channel in channels or cfg["channel_order"]:
        to = contact.get(channel)
        if not to:
            attempts.append(f"{channel}: no address")
            continue
        approved = prepared if prepared and prepared.get("channel") == channel and prepared.get("to") == to else None
        for _ in range(2):                    # a draft refused for its wording is rewritten once
            if approved:
                turn = ConversationTurn(channel=channel, to=to, subject=approved.get("subject"),
                                        body=approved["body"], attachments=approved.get("attachments", []))
            else:
                try:
                    turn = await agent.draft(channel=channel, to=to, purpose=purpose or cfg["purpose"],
                                             entity_facts=facts, company_facts=cfacts, documents=documents,
                                             budget=budget, avoid=avoid, examples=examples)
                except (DraftRejected, LLMError) as exc:
                    attempts.append(f"{channel}: draft rejected: {exc}")
                    turn = None
                    break
            if draft_only:
                return OutreachOutcome(True, channel, None, None, attempts, draft={
                    "channel": channel, "to": turn.to, "subject": turn.subject, "body": turn.body,
                    "attachments": turn.attachments, "refs": refs})
            ctx = RunContext(mission_id=mission_id, task_id=None, entity_id=loaded[0].entity.id,
                             allowed_apps={channel}, idempotency_key=f"{mission_id}:{key}:{step}:{channel}",
                             artifacts_dir=rt.artifacts_dir, counterparty=counterparty)
            args = {"to": turn.to, "body": turn.body, "attachments": turn.attachments}
            if turn.subject:
                args["subject"] = turn.subject
            res = await rt.registry.invoke(f"{channel}.send", args, ctx)
            policy = (res.output or {}).get("policy") if not res.ok and isinstance(res.output, dict) else None
            if policy and policy["policy_action"] == "deny" and policy["rule"].endswith("deny_phrases") \
                    and not avoid and not approved:
                avoid = list(rt.policies.get("external_comm", {}).get("deny_phrases", []))
                attempts.append(f"{channel}: wording refused by policy, redrafting")
                continue
            break
        if turn is None:
            continue
        if not res.ok:
            attempts.append(f"{channel}: {res.error_kind}: {res.error}")
            if policy and policy["policy_action"] == "ask":
                rt.approvals.annotate(policy["approval_id"], {"continuation": {
                    "kind": "outreach", "mission_id": mission_id, "entity_ids": [e.id for e in entities],
                    "transition_to": transition_to, "step": step, "channel": channel}})
                return OutreachOutcome(False, channel, None, None, attempts, held=policy["approval_id"])
            if policy and policy["policy_action"] == "defer":
                return OutreachOutcome(False, channel, None, None, attempts, deferred_until=policy["retry_at"])
            if res.error_kind in {"permanent", "transient"}:
                continue                     # L2 fallback: bounced, or still failing after L0 retries -> next channel
            break
        if res.replayed and any((rt.pipeline.get(e.id).data.get("last_contact") or {}).get("message_id")
                                == res.output["id"] for e in entities):
            # The same step was done before and its outcome already applied: nothing new went out, so
            # nothing may move (e.g. a second re-engage after another broken promise).
            attempts.append(f"{channel}: already sent earlier as {res.output['id']}; not sent again")
            return OutreachOutcome(False, channel, res.output, None, attempts, replayed=True)
        attempts.append(f"{channel}: delivered {res.output['id']}" + (" (approved wording)" if approved else "")
                        + (" (recovered after a restart)" if res.replayed else ""))
        sent = {"channel": channel, "message_id": res.output["id"], "thread_id": res.output["thread_id"]}
        transitions = apply_sent(rt, entities, sent, transition_to=transition_to, step=step)
        return OutreachOutcome(True, channel, res.output, transitions[0] if transitions else None, attempts,
                               transitions)
    return OutreachOutcome(False, None, None, None, attempts)


def apply_sent(rt: Runtime, entities: list[Entity], sent: dict[str, Any], *, transition_to: str | None,
               step: str) -> list[TransitionResult]:
    """After a message really went out: remember it on each entity and propose the transition."""
    transitions = []
    for e in entities:
        if transition_to is not None and transition_to in rt.pipeline.allowed_targets(rt.pipeline.get(e.id).state):
            transitions.append(rt.pipeline.propose(
                e.id, transition_to, {"outreach": {"delivered": True, **sent}},
                facts={"last_outreach": {**sent, "group": [x.external_ref for x in entities]}}))
        rt.pipeline.update_data(e.id, {"last_contact": {**sent, "step": step}})
        if rt.evidence:
            rt.evidence.collect([Evidence(kind="message", ref=sent["message_id"],
                                          summary=f"{step} sent by {sent['channel']}", data=sent)],
                                subject=f"contact:{step}", mission_id=e.mission_id, entity_id=e.id)
    return transitions
