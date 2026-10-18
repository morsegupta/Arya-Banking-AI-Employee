"""Review defects 3-6: contact cap per customer, deferred timer messages, replayed sends, new promises."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from apps.flows import outreach
from apps.orchestrator import Orchestrator
from sandbox.common import clock
from sandbox.customer_sim.engine import SimEngine
from tests.apps.test_mission_week import llm
from tests.fakes import ScriptedLLM


def inbox(asgi, address):
    async def go():
        async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                     headers={"Authorization": "Bearer hub-sim-token"}) as c:
            return (await c.get(f"/api/v1/mailboxes/{address}/messages")).json()
    return asyncio.run(go())


def entity(rt, ref):
    return next(e for e in rt.pipeline.list("m1") if e.external_ref == ref)


def test_contact_cap_counts_all_channels_of_one_customer(make_runtime, asgi):
    rt = make_runtime(ScriptedLLM([llm] * 20))
    try:
        for step in ("s1", "s2", "s3"):
            assert asyncio.run(outreach(rt, "m1", "INV-2291", step=step, transition_to=None, channels=["email"])).ok
        fourth = asyncio.run(outreach(rt, "m1", "INV-2291", step="s4", transition_to=None, channels=["whatsapp"]))
        assert not fourth.ok and fourth.deferred_until, "WhatsApp is the same customer: the weekly cap applies"
        assert inbox(asgi, "+919000010001") == []
        other = asyncio.run(outreach(rt, "m1", "INV-2297", step="s1", transition_to=None, channels=["whatsapp"]))
        assert other.ok, "another customer is not affected"
    finally:
        asyncio.run(rt.close())


def test_follow_up_held_by_the_weekend_is_sent_on_monday(make_runtime, asgi):
    rt = make_runtime(ScriptedLLM([llm] * 20))
    orch = Orchestrator(rt, "m1")
    try:
        asyncio.run(outreach(rt, "m1", "INV-2291"))                  # Mon: email; follow-up due Thu 10:00
        clock.advance(timedelta(days=5))                             # Sat 10:00: the Thu timer is overdue
        asyncio.run(rt.scheduler.fire_due())
        retry = [t for t in rt.scheduler.pending() if t["key"].startswith("retry:timer.followup_due")]
        assert retry and retry[0]["due_at"].startswith("2026-10-26T09:30"), orch.log
        assert len(inbox(asgi, "+919000010001")) == 0
        clock.advance(timedelta(days=2))                             # Mon 26 Oct 10:00
        asyncio.run(rt.scheduler.fire_due())
        assert len(inbox(asgi, "+919000010001")) == 1, orch.log      # follow-up on WhatsApp, not lost
    finally:
        asyncio.run(rt.close())


PROMISE = {"classifier": {"intent": "promise_to_pay", "confidence": 0.95}, "evidence": {"quote": "q"},
           "verifier": {"pass": True}}


def test_a_replayed_send_never_moves_an_entity_again(make_runtime, asgi):
    rt = make_runtime(ScriptedLLM([llm] * 20))
    try:
        asyncio.run(outreach(rt, "m1", "INV-2291"))
        e = entity(rt, "INV-2291")
        for _ in range(2):                                           # two broken promises in a row
            assert rt.pipeline.propose(e.id, "PROMISED", PROMISE,
                                       facts={"promise_amount": 1, "promise_date": "2026-10-22"}).ok
            assert rt.pipeline.propose(e.id, "BROKEN_PROMISE", {"matcher": {"matched_amount": 0}}, actor="system").ok
            out = asyncio.run(outreach(rt, "m1", "INV-2291", step="reengage1"))
        assert not out.ok and out.replayed and "already sent earlier" in out.attempts[-1]
        assert entity(rt, "INV-2291").state == "BROKEN_PROMISE", "the second re-engage sent nothing, so nothing moves"
        assert len(inbox(asgi, "ritu.malhotra@globex-retail.example")) == 2   # first reminder + one re-engage
    finally:
        asyncio.run(rt.close())


def test_a_new_promise_replaces_the_old_date(make_runtime, asgi, world):
    rt = make_runtime(ScriptedLLM([llm] * 20))
    orch = Orchestrator(rt, "m1")
    sim = SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    try:
        async def first():
            await outreach(rt, "m1", "INV-2297")
            await sim.tick()
            await sim.close()
            await orch.process_inbox()
        asyncio.run(first())
        e = entity(rt, "INV-2297")
        assert e.state == "PROMISED" and e.data["facts"]["promise_date"] == "2026-10-20"
        reply_to = inbox(asgi, "ganesh@shreeganeshtraders.example")[-1]["id"]

        async def repromise():
            async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                         headers={"Authorization": "Bearer hub-sim-token"}) as c:
                (await c.post("/api/v1/messages", json={
                    "channel": "email", "from": "ganesh@shreeganeshtraders.example",
                    "to": "ar@sharma-distributors.example", "subject": "Re: INV-2297", "in_reply_to": reply_to,
                    "body": "Sorry, cash is tight. We will transfer ₹54,000.00 on 26 Oct 2026 instead."})).raise_for_status()
            await orch.process_inbox()
        asyncio.run(repromise())
        e = entity(rt, "INV-2297")
        assert e.state == "PROMISED" and e.data["facts"]["promise_date"] == "2026-10-26", orch.log
        assert e.data["facts"]["previous_promise"]["date"] == "2026-10-20"
        checks = [t["due_at"] for t in rt.scheduler.pending() if t["key"].endswith(":check")]
        assert checks and checks[0].startswith("2026-10-28"), "the broken-promise check moved to the new date"
    finally:
        asyncio.run(rt.close())
