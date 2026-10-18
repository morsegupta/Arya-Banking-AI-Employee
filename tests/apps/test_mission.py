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
                "target_text": re.search(r"₹\d+L", payload["BRIEF"]).group(0), "deadline": "2026-10-23",
                "selection": {"min_days_overdue": 45},
                "success_criteria": ["₹12L verified in the bank"], "assumptions": [], "open_questions": []}
    if "TASK: plan" in system:
        # everything on day 0: code must split it by capacity
        return {"summary": "Biggest first.", "risks": [], "human_asks": [],
                "batches": [{"name": "all", "groups": [g["group"] for g in payload["GROUPS"]],
                             "approach": "firm_with_statement", "start_day": 0, "rationale": "deadline is close"}]}
    if "TASK: review" in system:
        return {"assessment": "On track.", "actions": []}
    if "TASK: answer" in system:
        return {"answer": f"About: {payload['QUESTION']}", "refs": ["INV-2270", "INV-9999"], "groups": ["C02", "ZZ"],
                "confidence": 0.8}
    if "Conversation Agent" in system:
        email = payload["CHANNEL"] == "email"
        body = "Please share a payment date."
        if "bank details" in payload["PURPOSE"]:
            body = f"Our bank details have not changed. Please pay only to A/c {payload['COMPANY_FACTS']['bank_account_number']}."
        if "approved the concession" in payload["PURPOSE"]:
            body = "Good news: the discount you asked for is approved. Please confirm the payment date."
        return {"subject": "Overdue invoices" if email else None, "body": body,
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
        asked = rt.approvals.list(mission_id=r.mission_id, status="PENDING")
        assert [(a.payload["action"], a.payload["group"]) for a in asked] == [("payment_plan", "C01")]
        assert rt.approvals.list(mission_id=r.mission_id, status="APPROVED")[0].payload["action"] == "mission_plan"
    finally:
        asyncio.run(rt.close())


def numbered_router():
    """Router whose drafts are numbered, so we can tell which draft was sent."""
    seen = {"n": 0, "purposes": []}

    def llm(messages):
        if "Conversation Agent" in messages[0]["content"]:
            seen["n"] += 1
            seen["purposes"].append(json.loads(messages[1]["content"])["PURPOSE"])
            d = router(messages)
            return {**d, "body": f"{d['body']} [draft {seen['n']}]"}
        return router(messages)
    return llm, seen


def test_first_batch_wording_is_approved_and_sent_verbatim(make_runtime, asgi):
    llm, seen = numbered_router()
    rt = make_runtime(ScriptedLLM([llm] * 200))
    r = MissionRunner(rt)
    try:
        m = asyncio.run(r.create(BRIEF))
        first = m.data["first_batch"]
        assert len(first) == 6 and seen["n"] == 6, "day-0 messages drafted with the plan, nothing sent"
        assert hub_inbox(asgi, "arjun.rao@initech-supplies.example") == []
        approval = rt.approvals.list(status="PENDING")[0]
        assert approval.payload["messages"] == first
        assert any("first-batch messages" in line for line in plan_text(m))

        # a reject with a note gives a new plan and new drafts that carry the note
        asyncio.run(r.reject("Too formal. Mention the statement is attached."))
        assert "Too formal" in seen["purposes"][-1] and r.mission.status == "AWAITING_PLAN_APPROVAL"
        first = r.mission.data["first_batch"]

        r.approve(by="fm")
        r.orch._set_kv("last_sync", rt.clock.now().date().isoformat())
        drafted_before = seen["n"]
        asyncio.run(r.tick())
        assert seen["n"] == drafted_before, "approved messages are sent as they are, never re-drafted"
        sent = hub_inbox(asgi, first["C02"]["to"])[0]
        assert sent["body"] == first["C02"]["body"] and sent["subject"] == first["C02"]["subject"]
    finally:
        asyncio.run(rt.close())


def test_day0_group_without_approved_wording_is_not_sent(make_runtime, asgi):
    rt = make_runtime(ScriptedLLM([router] * 200))
    r = MissionRunner(rt)
    try:
        asyncio.run(r.create(BRIEF))
        first = dict(r.mission.data["first_batch"])
        first.pop("C02")
        rt_store = r.store
        rt_store.update(r.mission_id, data={"first_batch": first})
        r.approve(by="fm")
        r.orch._set_kv("last_sync", rt.clock.now().date().isoformat())
        asyncio.run(r.tick())
        assert hub_inbox(asgi, "arjun.rao@initech-supplies.example") == []
        assert "C02" not in r.mission.data["contacted_groups"]
        assert any("C02" in line and "awaiting approved wording" in line for line in r.log)
    finally:
        asyncio.run(rt.close())
