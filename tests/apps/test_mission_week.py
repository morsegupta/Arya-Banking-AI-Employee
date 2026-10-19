"""Day 2 morning acceptance, offline: a mission week from the brief, no network, no API key.

Everything is real except the LLM's choices: Chromium on the real ERP and bank portal, message hub,
customer simulator + payment engine, prioritizer, matcher, settlement, verifier, pipeline guards and
the mission state machine. A scripted LLM plays every agent; Operator steps read element refs from
the real page snapshots it is shown.

Scenarios checked against the world's hidden ground truth:
- already paid before the mission (Mehta): reconciled on day 0 and never chased,
- one transfer for three invoices (Initech): all three PAID from one bank line,
- partial payer (Patel): PARTIALLY_PAID with the right balance left, then asked for the balance and
  a new promise for exactly the remainder (scenario 2),
- "already paid" that is not (Sunrise): never PAID on the claim, the look-alike SUNRISE TRADING CO
  credit never matched; asked for the payment reference, admits it, pays for real (scenario 2),
- disputer: DISPUTED and handed to a human once, with the invoice and the signed delivery note
  (scenario 3); ghost: followed up on the next channel,
- zero false "paid", and verified value equals the money really matched.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import httpx

from apps.mission import MissionRunner
from apps.runtime import build_runtime
from core.llm import LLMResponse
from sandbox.common import clock
from sandbox.customer_sim.engine import SimEngine
from tests.apps.test_mission import BRIEF, _untrusted
from tests.apps.test_mission import router as mission_router
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM, call
from tests.tools.test_browser import servers  # noqa: F401  (fixture: real ERP + bank HTTP servers)
from tools.vault import Vault

ERP_KEY = {"X-API-Key": "erp-sandbox-readonly-key"}


def _obs(messages):
    content = [m for m in messages if m["role"] == "tool"][-1]["content"]
    return json.loads(content.replace("<untrusted_data>", "").replace("</untrusted_data>", ""))["output"]


def _ref(messages, name):
    return next(e["ref"] for e in _obs(messages)["elements"] if e["name"] == name)


def operator(messages):
    """Scripted Operator: picks the next step of its task from what it has done so far."""
    goal = json.loads(messages[1]["content"])["task"]
    step = sum(1 for m in messages if m["role"] == "assistant")
    tag = f"t{abs(hash(goal)) % 10**8}"

    def act(name, args):
        return LLMResponse(None, [call(name, args, f"{tag}-{step}")], {"total_tokens": 300})

    if "bank account statement" in goal:
        start, end = re.findall(r"\d{4}-\d{2}-\d{2}", goal)[:2]
        return [lambda: act("bank_portal__open", {"path": f"/statement?from_date={start}&to_date={end}"}),
                lambda: act("bank_portal__download", {"ref": _ref(messages, "Download CSV")}),
                lambda: act("finish_task", {"summary": "statement saved", "result_call_ids": [f"{tag}-1"]})][step]()
    m = re.search(r"invoice (INV-\d+): amount ([\d.]+), receipt date ([\d-]+), payment mode (\w+), "
                  r"bank reference / UTR (\w+)", goal)
    ref, amount, day, _, utr = m.groups()
    return [lambda: act("erp_web__open", {"path": f"/invoices/{ref}/receipts/new"}),
            lambda: act("erp_web__fill", {"ref": _ref(messages, "Receipt date"), "value": day}),
            lambda: act("erp_web__fill", {"ref": _ref(messages, "Amount (₹)"), "value": amount}),
            lambda: act("erp_web__fill", {"ref": _ref(messages, "Bank reference / UTR"), "value": utr}),
            lambda: act("erp_web__click", {"ref": _ref(messages, "Post receipt")}),
            lambda: act("finish_task", {"summary": "receipt posted", "result_call_ids": [f"{tag}-4"]})][step]()


def classify(messages):
    text = _untrusted(messages)
    kinds = [("cannot pay", "dispute"), ("already paid", "paid_claim"), ("% off", "discount_request"),
             ("bank account has changed", "bank_detail_change")]
    for needle, intent in kinds:
        if needle in text:
            sentence = next(s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if needle in s)
            return {"intent": intent, "confidence": 0.9, "about_entity": True, "entity_refs": [], "quote": sentence}
    return mission_router(messages)


def llm(messages):
    system = messages[0]["content"]
    if system.startswith("You are the Operator"):
        return operator(messages)
    if "You classify one message" in system:
        return classify(messages)
    return mission_router(messages)


def erp_invoice(servers, ref):  # noqa: F811
    return httpx.get(f"{servers['erp_web']}/api/v1/invoices/{ref}", headers=ERP_KEY).json()


def test_mission_week(servers, sandbox_var, asgi, world):  # noqa: F811
    rt = build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS), llm=ScriptedLLM([llm] * 2000),
                       hub_transport=asgi["hub"], erp_transport=asgi["erp"])
    sim = SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    runner = MissionRunner(rt)
    states_by_day = []

    async def run():
        try:
            await runner.create(BRIEF)
            runner.approve(by="test")
            for day in range(5):                     # Mon .. Fri
                await runner.tick()
                await sim.tick()                     # customers answer the same day
                await runner.tick()
                states_by_day.append({e.external_ref: e.state for e in rt.pipeline.list(runner.mission_id)})
                clock.advance(timedelta(days=1))
                await sim.tick()                     # overnight payments land
        finally:
            await sim.close()
            await rt.close()

    asyncio.run(run())
    log = "\n".join(runner.log)
    final = states_by_day[-1]

    # already paid before the mission: settled on day 0, never chased
    assert states_by_day[0]["INV-2263"] == "PAID", log
    kunal = asyncio.run(_inbox(asgi, "kunal@mehta-electricals.example"))
    assert kunal == [], "a customer who already paid must not get a reminder"

    # one transfer for three invoices
    for ref in ("INV-2270", "INV-2275", "INV-2288"):
        assert final[ref] == "PAID", log
        assert erp_invoice(servers, ref)["balance_paise"] == 0
    utrs = {erp_invoice(servers, r)["receipts"][0]["reference"] for r in ("INV-2270", "INV-2275", "INV-2288")}
    assert len(utrs) == 1, "all three settled by the same bank line"

    # partial payer: balance left, asked for it, new promise for exactly the remainder
    patel = next(e for e in rt.pipeline.list(runner.mission_id) if e.external_ref == "INV-2284")
    assert patel.data["balance"] == erp_invoice(servers, "INV-2284")["balance_paise"] == 75_000_00
    moves = {(e.entity_id, e.payload["to"]) for e in rt.events.query(mission_id=runner.mission_id,
                                                                     types=["entity.transitioned"], limit=100000)}
    assert (patel.id, "PARTIALLY_PAID") in moves, log
    assert "INV-2284: partial payment 7500000 received -> asked for the balance" in log
    assert final["INV-2284"] == "PROMISED" and patel.data["facts"]["promise_amount"] == 75_000_00, log

    # "already paid" without money: never PAID on the claim; asked for the reference; the look-alike
    # credit (SUNRISE TRADING CO) is never applied; PAID only from the customer's own later transfer
    asked = log.index("INV-2279: claimed paid, no money found -> asked for the payment reference (email: delivered")
    assert "INV-2279: reply classified paid_claim" in log[:asked]
    assert "-> PAID" not in "".join(line for line in log[:asked].splitlines() if "INV-2279" in line), log
    receipts = erp_invoice(servers, "INV-2279")["receipts"]
    assert all(r["reference"] not in world.ground_truth["bank_lures"] for r in receipts)
    if final["INV-2279"] == "PAID":
        sunrise = [p for p in SimEngine.ground_truth()["payments"] if p["customer_id"] == "C05" and p["status"] == "PAID"]
        assert sunrise and receipts[0]["reference"] == sunrise[0]["utr"]

    # dispute: one hand-over to a human with the documents, the customer told it is under review
    assert final["INV-2296"] == "DISPUTED"
    disputes = [a for a in rt.approvals.list() if a.payload.get("action") == "dispute"]
    assert len(disputes) == 1, [a.title for a in disputes]
    assert set(disputes[0].payload["documents"]) == {"DN-2296.pdf", "INV-2296.pdf"}
    lakshmi = asyncio.run(_inbox(asgi, "lakshmi@kaverikirana.example"))
    assert any("reviewing it against the signed delivery note" in m["body"] for m in lakshmi)

    # ghost: follow-up on the next channel after the email went unanswered
    priya = asyncio.run(_inbox(asgi, "+919000010008"))
    assert any(m["channel"] == "whatsapp" for m in priya), log

    # zero false "paid": every PAID/PARTIALLY_PAID entity has a verified ledger line and an ERP receipt
    with rt.db.read() as conn:
        ledger = {(r["entity_id"]): r for r in conn.execute("SELECT * FROM settlements WHERE status = 'VERIFIED'")}
    for e in rt.pipeline.list(runner.mission_id):
        if e.state in {"PAID", "PARTIALLY_PAID"}:
            assert e.id in ledger, f"{e.external_ref} {e.state} without verified money"
            assert erp_invoice(servers, e.external_ref)["receipts"]
    paid_in_sim = {inv for p in SimEngine.ground_truth()["payments"] if p["status"] == "PAID"
                   for inv in json.loads(p["invoices_json"])}
    for ref, state in final.items():
        if state == "PAID" and ref != "INV-2263":
            assert ref in paid_in_sim, f"{ref} PAID but the simulated customer never paid it"

    # mission bookkeeping: verified value = ledger sum; never counts promises
    assert runner.verified_value() == sum(r["amount"] for r in ledger.values())
    assert runner.mission.status in {"ACTIVE", "COMPLETED"}
    if runner.mission.status == "ACTIVE":
        assert runner.verified_value() < runner.mission.spec.target_value
    assert set(runner.mission.data["contacted_groups"]) == set(runner.mission.data["groups"]), \
        "every in-scope customer was covered by a batch"
    ok, _ = rt.events.verify_chain()
    assert ok


async def _inbox(asgi, address):
    async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                 headers={"Authorization": "Bearer hub-sim-token"}) as c:
        return (await c.get(f"/api/v1/mailboxes/{address}/messages")).json()


def test_small_target_completes_only_after_fresh_bank_check(servers, sandbox_var, asgi, world):  # noqa: F811
    rt = build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS), llm=ScriptedLLM([llm] * 1000),
                       hub_transport=asgi["hub"], erp_transport=asgi["erp"])
    sim = SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    runner = MissionRunner(rt)

    async def run():
        try:
            await runner.create("Collect the 45+ day overdue invoices — target ₹3L by Friday.")
            runner.approve(by="test")
            for _ in range(3):
                result = await runner.tick()
                if result["status"] != "ACTIVE":
                    return result
                await sim.tick()
                result = await runner.tick()
                if result["status"] != "ACTIVE":
                    return result
                clock.advance(timedelta(days=1))
                await sim.tick()
        finally:
            await sim.close()
            await rt.close()

    result = asyncio.run(run())
    assert result["status"] == "COMPLETED", "\n".join(runner.log)
    check = rt.events.query(types=["verification.completed"])[-1].payload
    assert check["subject"] == "mission" and check["passed"] and check["re_observed"] >= 3_00_000_00
    done = rt.events.query(mission_id=runner.mission_id, types=["mission.transitioned"])[-1].payload
    assert done["to"] == "COMPLETED" and done["verified_value"] == check["re_observed"]
