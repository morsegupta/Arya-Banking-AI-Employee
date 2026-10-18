"""Day 2 morning acceptance: brief -> spec -> policy-filtered scope -> ranked plan -> human approval ->
batched outreach (one message per customer) -> simulated replies -> promises allocated per invoice.

Real: ERP, message hub, drive, customer simulator, pipeline, guards, prioritizer, document generator.
Scripted: the LLM's choices, routed by which agent is asking.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import httpx
import pytest
from pypdf import PdfReader

from apps.mission import MissionRunner, plan_text
from core.mission import MissionTransitionError
from sandbox.customer_sim.engine import SimEngine
from tests.fakes import ScriptedLLM

BRIEF = "Collect the 45+ day overdue invoices — target ₹12L by Friday."


def _untrusted(messages) -> str:
    text = next(m["content"] for m in reversed(messages) if "<untrusted_data>" in m["content"])
    return text.replace("<untrusted_data>", "").replace("</untrusted_data>", "").strip()


def router(messages):
    system = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    if "TASK: understand" in system:
        return {"objective": "Collect overdue receivables", "scope": "OPEN invoices 45+ days overdue",
                "target_text": "₹12L", "deadline": "2026-10-23", "selection": {"min_days_overdue": 45},
                "success_criteria": ["₹12L verified in the bank"], "assumptions": [], "open_questions": []}
    if "TASK: plan" in system:
        # everything on day 0: code must split it by capacity
        return {"summary": "Biggest first.", "risks": [], "human_asks": [],
                "batches": [{"name": "all", "groups": [g["group"] for g in payload["GROUPS"]],
                             "approach": "firm_with_statement", "start_day": 0, "rationale": "deadline is close"}]}
    if "TASK: review" in system:
        return {"assessment": "On track.", "actions": []}
    if "Conversation Agent" in system:
        email = payload["CHANNEL"] == "email"
        return {"subject": "Overdue invoices" if email else None, "body": "Please share a payment date.",
                "attachments": payload["AVAILABLE_DOCUMENTS"], "facts_used": []}
    if "You classify one message" in system:
        text = _untrusted(messages)
        refs = [e["ref"] for e in payload["ENTITIES"]]
        if "transfer" in text:
            quote = next(s.strip() + "." for s in text.split(".") if "transfer" in s)
            amount = re.search(r"₹[\d,]+\.\d{2}", text).group(0)
            day = datetime.strptime(re.search(r"\d{2} \w{3} \d{4}", text).group(0), "%d %b %Y").date()
            return {"intent": "promise_to_pay", "confidence": 0.95, "about_entity": True,
                    "entity_refs": re.findall(r"INV-\d{4}", text) or refs, "quote": quote, "amount_text": amount,
                    "date": day.isoformat()}
        return {"intent": "unclear", "confidence": 0.3, "about_entity": True, "entity_refs": [], "quote": ""}
    raise AssertionError(f"unexpected LLM call: {system[:80]}")


@pytest.fixture()
def runner(make_runtime):
    rt = make_runtime(ScriptedLLM([router] * 200))
    r = MissionRunner(rt)
    yield r
    asyncio.run(rt.close())


def test_brief_to_plan_needs_approval(runner, world):
    m = asyncio.run(runner.create(BRIEF))
    assert m.status == "AWAITING_PLAN_APPROVAL", "\n".join(runner.log)
    assert m.spec.target_value == 12_00_000_00 and m.spec.deadline.date().isoformat() == "2026-10-23"
    assert sorted(m.data["scope"]) == sorted(world.ground_truth["in_scope_45_plus"])
    excluded = {x["ref"]: x["reason"] for x in m.data["excluded"]}
    assert "legal hold" in excluded["INV-2248"] and "payment plan" in excluded["INV-2259"]
    assert sum(m.data["groups"][g]["refs"].__len__() for g in m.data["groups"]) == 15
    assert m.data["scope_value"] == world.ground_truth["in_scope_45_plus_total"] * 100

    # capacity 6 per day: the agent's single day-0 batch was split by code
    days = [b.start_day for b in m.plan.batches]
    assert days == sorted(days) and sum(len(b.groups) for b in m.plan.batches if b.start_day == 0) == 6
    assert "Mission" in plan_text(m)[0]
    with pytest.raises(MissionTransitionError):
        runner.store.transition(m.id, "ACTIVE", actor="agent")
    assert asyncio.run(runner.tick())["status"] == "AWAITING_PLAN_APPROVAL"    # nothing happens unapproved


def hub_inbox(asgi, address):
    async def go():
        async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                     headers={"Authorization": "Bearer hub-sim-token"}) as c:
            return (await c.get(f"/api/v1/mailboxes/{address}/messages")).json()
    return asyncio.run(go())


def test_day0_batch_one_message_per_customer_then_group_promise(runner, asgi, world, sandbox_var):
    asyncio.run(runner.create(BRIEF))
    runner.approve(by="test")
    today = runner.rt.clock.now().date().isoformat()
    runner.orch._set_kv("last_sync", today)        # no bank download in this test (covered elsewhere)
    asyncio.run(runner.tick())
    log = "\n".join(runner.log)

    contacted = runner.mission.data["contacted_groups"]
    assert len(contacted) == 6 and "C02" in contacted, log
    initech = hub_inbox(asgi, "arjun.rao@initech-supplies.example")
    assert len(initech) == 1, "one message covers all three Initech invoices"
    files = sorted(a["filename"] for a in initech[0]["attachments"])
    assert files == ["INV-2270.pdf", "INV-2275.pdf", "INV-2288.pdf", f"SOA-C02-{today}.pdf"]
    soa = sandbox_var / "drive" / "statements" / f"SOA-C02-{today}.pdf"
    text = PdfReader(soa).pages[0].extract_text()
    for ref in ["INV-2270", "INV-2275", "INV-2288", "INV-2328"]:      # statement lists every open invoice
        assert ref in text
    states = {e.external_ref: e.state for e in runner.rt.pipeline.list(runner.mission_id)}
    assert states["INV-2270"] == states["INV-2275"] == states["INV-2288"] == "CONTACTED"

    # the simulator answers the same day; Initech promises one transfer for all three
    sim = SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    asyncio.run(sim.tick())
    asyncio.run(sim.close())
    asyncio.run(runner.tick())
    by_ref = {e.external_ref: e for e in runner.rt.pipeline.list(runner.mission_id)}
    for ref, amount in [("INV-2270", 120_000_00), ("INV-2275", 95_000_00), ("INV-2288", 60_000_00)]:
        e = by_ref[ref]
        assert e.state == "PROMISED", "\n".join(runner.log)
        assert e.data["facts"]["promise_amount"] == amount                    # total split per invoice
        assert e.data["facts"]["promise_total"] == 275_000_00
    prog = runner.progress()
    assert prog.verified == 0, "promises are never counted as collected"
    assert prog.expected > 0 and prog.gap == 12_00_000_00
    assert runner.mission.status == "ACTIVE"


def test_review_levers_are_executed_by_code(make_runtime, asgi):
    review = {"assessment": "Globex silent.", "actions": [
        {"lever": "follow_up", "groups": ["C01"], "rationale": "no reply yet"},
        {"lever": "propose_payment_plan", "groups": ["C01"], "rationale": "large"},  # C01 only CONTACTED: dropped
        {"lever": "write_off", "groups": ["C01"], "rationale": "never allowed"}]}

    def llm(messages):
        return review if "TASK: review" in messages[0]["content"] else router(messages)

    rt = make_runtime(ScriptedLLM([llm] * 100))
    r = MissionRunner(rt)
    try:
        asyncio.run(r.create(BRIEF))
        r.approve(by="test")
        r.orch._set_kv("last_sync", rt.clock.now().date().isoformat())
        asyncio.run(r.tick())
        decision = asyncio.run(r.review(r.progress()))
        assert [a.lever for a in decision.actions] == ["follow_up"] and len(decision.problems) == 2
        assert r.mission.data["actions"][-1]["result"] == "C01: sent"
        globex = hub_inbox(asgi, "+919000010001")                        # follow-up went out on WhatsApp
        assert len(globex) == 1 and globex[0]["channel"] == "whatsapp"
        # approval levers never act: they only ask
        assert asyncio.run(r.execute("propose_payment_plan", ["C01"], "test")) == "approval requested"
        asked = rt.events.query(mission_id=r.mission_id, types=["approval.requested"])
        assert [(e.payload["lever"], e.payload["group"]) for e in asked] == [("propose_payment_plan", "C01")]
    finally:
        asyncio.run(rt.close())
