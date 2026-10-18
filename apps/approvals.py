"""What happens after a human decides an approval.

    tool_call            approve -> the held call runs exactly as held; a held message then counts
                         as sent (entities move as they would have)       reject -> nothing is sent
    review_quarantine    approve -> quarantine lifted, message handed to the agents
                         reject  -> stays quarantined; the account goes to a human
    concession           approve / reject -> the customer gets the answer (wording allowed by the decision)
    payment_plan         approve -> the customer is offered the plan      reject -> nothing is sent
    mission_plan         approve -> mission ACTIVE                        reject -> replan with the reason
"""

from __future__ import annotations

from apps.flows import apply_sent, outreach
from apps.runtime import Runtime
from core.approvals import Approval
from core.models import Event


async def decide(rt: Runtime, approval_id: str, *, approve: bool, by: str, note: str = "") -> list[str]:
    a = rt.approvals.decide(approval_id, approve=approve, by=by, note=note)
    log = [f"{a.id} {a.status} by {by}" + (f": {note}" if note else "")]
    if a.kind == "tool_call":
        if approve:
            log += await _run_held(rt, a)
        return log
    action = a.payload.get("action")
    handler = {"review_quarantine": _review, "concession": _concession, "payment_plan": _payment_plan,
               "mission_plan": _mission_plan}.get(action)
    if handler:
        log += await handler(rt, a, approve, by, note)
    return log


async def _run_held(rt: Runtime, a: Approval) -> list[str]:
    a = await rt.approvals.execute_approved(a.id)
    log = [f"held call {a.payload['tool']} {a.status}" + (f": {a.result.get('error')}" if a.status == "FAILED" else "")]
    cont = a.payload.get("continuation") or {}
    if a.status == "EXECUTED" and cont.get("kind") == "outreach":
        out = a.result["output"]
        sent = {"channel": cont["channel"], "message_id": out["id"], "thread_id": out["thread_id"]}
        entities = [rt.pipeline.get(i) for i in cont["entity_ids"]]
        for tr in apply_sent(rt, entities, sent, transition_to=cont.get("transition_to"), step=cont["step"]):
            log.append(f"{tr.entity.external_ref}: {tr.from_state} -> {tr.to_state} {'ok' if tr.ok else tr.reason}")
    return log


async def _review(rt: Runtime, a: Approval, approve: bool, by: str, note: str) -> list[str]:
    msg, ids = a.payload["message"], a.payload["entity_ids"]
    if not approve:
        rt.events.append(Event(type="handoff.requested", mission_id=a.mission_id, occurred_at=rt.clock.now(),
                               payload={"entity_ids": ids, "message_id": msg["id"],
                                        "reason": f"confirmed suspicious message ({by}): {note}"}))
        return ["message stays quarantined; account handed to a human"]
    for i in ids:
        rt.gate.clear(f"entity:{i}", by)
    from apps.orchestrator import Orchestrator
    orch = Orchestrator(rt, a.mission_id)
    await orch.handle_message({**msg, "released": True})
    return ["quarantine lifted; message released to the agents", *orch.log]


def _refs(rt: Runtime, ids: list[str]) -> list[str]:
    return [rt.pipeline.get(i).external_ref for i in ids]


async def _concession(rt: Runtime, a: Approval, approve: bool, by: str, note: str) -> list[str]:
    purpose = a.payload["reply"]["approved" if approve else "rejected"]
    out = await outreach(rt, a.mission_id, _refs(rt, a.payload["entity_ids"]), step=f"concession:{a.id}",
                         transition_to=None, channels=[a.payload["channel"]],
                         purpose=purpose + (f" Note from the finance manager: {note}" if note else ""))
    return [f"reply to the customer: {'; '.join(out.attempts)}"]


async def _payment_plan(rt: Runtime, a: Approval, approve: bool, by: str, note: str) -> list[str]:
    if not approve:
        return ["payment plan not offered"]
    out = await outreach(rt, a.mission_id, _refs(rt, a.payload["entity_ids"]), step=f"plan:{a.id}",
                         transition_to=None,
                         purpose="Offer the customer a payment plan for the overdue balance, as approved by the finance "
                                 f"manager: {note or 'two equal instalments within 30 days'}. Ask them to confirm "
                                 "the dates.")
    return [f"payment plan offered: {'; '.join(out.attempts)}"]


async def _mission_plan(rt: Runtime, a: Approval, approve: bool, by: str, note: str) -> list[str]:
    from apps.mission import MissionRunner
    runner = MissionRunner(rt, a.mission_id)
    if approve:
        runner.approve(by=by)
    else:
        await runner.reject(note or "rejected without a reason")
    return runner.log
