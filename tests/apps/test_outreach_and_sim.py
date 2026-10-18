"""Outreach flow + customer simulator + payment engine, all in-process over the seeded sandbox."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from apps.flows import format_money, outreach
from sandbox.common import clock
from sandbox.common.money import format_inr
from sandbox.customer_sim.engine import Personas, SimEngine
from tests.fakes import ScriptedLLM

SIM = {"Authorization": "Bearer hub-sim-token"}
ADMIN = {"X-Sandbox-Token": "sandbox-admin-token"}


def draft(subject, body, attachments=(), facts=("number", "balance_paise")):
    return {"subject": subject, "body": body, "attachments": list(attachments), "facts_used": list(facts)}


def run_flow(rt, coro):
    async def go():
        try:
            return await coro
        finally:
            await rt.close()
    return asyncio.run(go())


def hub_messages(asgi, mailbox):
    async def go():
        async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub", headers=SIM) as c:
            return (await c.get(f"/api/v1/mailboxes/{mailbox}/messages")).json()
    return asyncio.run(go())


@pytest.mark.parametrize("paise", [0, 5, 100_00, 340000_00, 12345678_90, -100_00])
def test_money_format_matches_sandbox(paise):
    assert format_money(paise) == format_inr(paise)


def test_outreach_email_moves_entity_to_contacted(make_runtime, asgi):
    llm = ScriptedLLM([draft("INV-2291 overdue", "Dear Ritu, INV-2291 for ₹3,40,000.00 is overdue.",
                             ["invoices/INV-2291.pdf"])])
    rt = make_runtime(llm)
    out = run_flow(rt, outreach(rt, "m1", "INV-2291"))
    assert out.ok and out.channel == "email" and out.transition.ok and out.transition.entity.state == "CONTACTED"
    inbox = hub_messages(asgi, "ritu.malhotra@globex-retail.example")
    assert inbox[0]["attachments"][0]["filename"] == "INV-2291.pdf"
    # The agent saw formatted money and the company's bank details, not raw paise.
    prompt = llm.seen[0]["messages"][1]["content"]
    assert "₹3,40,000.00" in prompt and "50200012345678" in prompt
    assert [t["key"].split(":")[-1] for t in rt.scheduler.pending()] == ["followup"]


def test_outreach_falls_back_to_whatsapp_on_bounce(make_runtime, asgi):
    llm = ScriptedLLM([draft("INV-2273", "Dear Anand, INV-2273 is overdue."),
                       draft(None, "Hi Anand, INV-2273 is overdue.")])
    rt = make_runtime(llm)
    out = run_flow(rt, outreach(rt, "m1", "INV-2273"))
    assert out.ok and out.channel == "whatsapp"
    assert "bounced" in out.attempts[0] and out.attempts[1].startswith("whatsapp: delivered")


def test_draft_with_unoffered_attachment_is_rejected(make_runtime):
    llm = ScriptedLLM([draft("x", "y", ["../erp.db"]), draft(None, "y", ["../erp.db"])])
    rt = make_runtime(llm)
    out = run_flow(rt, outreach(rt, "m1", "INV-2291"))
    assert not out.ok and all("draft rejected" in a for a in out.attempts)


def test_outreach_is_idempotent(make_runtime, asgi):
    llm = ScriptedLLM([draft("INV-2291", "Dear Ritu, reminder.")] * 2)
    rt = make_runtime(llm)
    run_flow(rt, outreach(rt, "m1", "INV-2291"))
    rt2 = make_runtime(llm)
    second = run_flow(rt2, outreach(rt2, "m1", "INV-2291"))
    assert len(hub_messages(asgi, "ritu.malhotra@globex-retail.example")) == 1
    assert second.transition is None         # already CONTACTED: no OVERDUE->CONTACTED again


# --- simulator ------------------------------------------------------------------------------

def sim(world, asgi):
    return SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])


def send_to(asgi, to, body, channel="email", frm="ar@sharma-distributors.example"):
    async def go():
        async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                     headers={"Authorization": "Bearer hub-arya-token"}) as c:
            r = await c.post("/api/v1/messages", json={"channel": channel, "from": frm, "to": to,
                                                       "subject": "Reminder", "body": body})
            return r.json()
    return asyncio.run(go())


def tick(engine):
    return asyncio.run(engine.tick())


def bank_txns(asgi):
    async def go():
        async with httpx.AsyncClient(transport=asgi["bank"], base_url="http://bank", headers=ADMIN) as c:
            return (await c.get("/_sandbox/transactions")).json()
    return asyncio.run(go())


def test_promise_then_late_pays_after_promise(world, asgi, sandbox_var):
    engine = sim(world, asgi)
    send_to(asgi, "ritu.malhotra@globex-retail.example", "INV-2291 is overdue, please share a payment date.")
    out = tick(engine)
    assert out["replies"][0]["decision"] == "promise" and out["replies"][0]["invoices"] == ["INV-2291"]
    assert out["replies"][0]["pay_on"] == "2026-10-25"          # promised Thu 22, pays 3 days late
    reply = hub_messages(asgi, "ar@sharma-distributors.example")[0]
    assert "22 Oct 2026" in reply["body"] and "3,40,000.00" in reply["body"] and reply["in_reply_to"]
    assert tick(engine) == {"replies": [], "payments": []}      # nothing new, nothing due
    clock.advance(timedelta(days=5))                            # Sat 24: promised date passed, not paid yet
    assert tick(engine)["payments"] == []
    clock.advance(timedelta(days=1))                            # Sun 25: money lands
    paid = tick(engine)["payments"]
    assert paid[0]["amount_paise"] == 340000_00
    last = bank_txns(asgi)[-1]
    assert last["narration"].startswith("NEFT/") and "GLOBEX RETAIL PVT LTD/INV 2291" in last["narration"]
    assert last["deposit_paise"] == 340000_00 and last["txn_date"] == "2026-10-25"
    asyncio.run(engine.close())


def test_combined_transfer_and_partial_payer(world, asgi):
    engine = sim(world, asgi)
    send_to(asgi, "arjun.rao@initech-supplies.example", "INV-2270 overdue")
    send_to(asgi, "hetal@patelprovision.example", "INV-2284 overdue")
    tick(engine)
    clock.advance(timedelta(days=3))
    paid = {p["customer"]: p for p in tick(engine)["payments"]}
    assert paid["C02"]["invoices"] == ["INV-2270", "INV-2275", "INV-2288"] and paid["C02"]["amount_paise"] == 275000_00
    assert paid["C06"]["amount_paise"] == 75000_00
    asyncio.run(engine.close())


@pytest.mark.parametrize("to, body, kind", [
    ("lakshmi@kaverikirana.example", "INV-2296 overdue", "dispute"),
    ("kunal@mehta-electricals.example", "INV-2263 overdue", "paid_claim_true"),
    ("deepak@sunrise-hardware.example", "INV-2279 overdue", "paid_claim_false"),
    ("sameer.q@deccanhome.example", "INV-2293 overdue", "discount_request"),
    ("priya.n@lotusfoods.example", "INV-2266 overdue", "silence"),
    ("vandana@vandanaimports.example", "INV-2299 overdue", "injection"),
    ("ramesh@coastalretail.example", "INV-2286 overdue", "bank_change"),
])
def test_personas_never_pay_without_reason(world, asgi, to, body, kind):
    engine = sim(world, asgi)
    send_to(asgi, to, body)
    out = tick(engine)
    assert out["replies"][0]["decision"] == kind and out["replies"][0]["pay_on"] is None
    clock.advance(timedelta(days=10))
    assert tick(engine)["payments"] == []
    if kind == "silence":
        assert hub_messages(asgi, "ar@sharma-distributors.example") == []
    asyncio.run(engine.close())


def test_bounced_mail_is_never_seen_by_customer(world, asgi):
    engine = sim(world, asgi)
    send_to(asgi, "anand.k@nilgiriwholesale.example", "INV-2273 overdue")
    assert tick(engine)["replies"] == []
    send_to(asgi, "+91-90000-10012", "INV-2273 overdue", channel="whatsapp", frm="+91-90000-00000")
    out = tick(engine)
    assert out["replies"][0]["decision"] == "promise"
    asyncio.run(engine.close())


def test_haggler_promises_after_discount_answer(world):
    p = Personas(world)
    c = world.customer("C07")
    from datetime import date
    first = p.decide(c, {"body": "INV-2293 overdue"}, 1, date(2026, 10, 19))
    second = p.decide(c, {"body": "Sorry, no discount is possible on INV-2293."}, 2, date(2026, 10, 19))
    assert first.kind == "discount_request" and second.kind == "promise" and second.pay_on
