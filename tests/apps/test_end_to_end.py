"""Day 1 evening acceptance: one invoice OVERDUE -> CONTACTED -> PROMISED -> PAID.

Everything is real except the LLM's choices: real Operator loop, real Chromium on real ERP and bank
servers, real message hub, real customer simulator and payment engine, real matcher, verifier and
pipeline guards. The scripted LLM reads element refs from the actual page snapshots it is shown.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

from apps.orchestrator import Orchestrator
from apps.runtime import build_runtime
from core.llm import LLMResponse
from sandbox.common import clock
from sandbox.customer_sim.engine import SimEngine
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM, call
from tests.tools.test_browser import servers  # noqa: F401  (fixture: real ERP + bank HTTP servers)
from tools.vault import Vault

REF = "INV-2297"


def last_obs(messages):
    content = [m for m in messages if m["role"] == "tool"][-1]["content"]
    content = content.replace("<untrusted_data>", "").replace("</untrusted_data>", "")
    return json.loads(content)["output"]


def ref_of(messages, name):
    return next(e["ref"] for e in last_obs(messages)["elements"] if e["name"] == name)


def act(name, args_fn, cid):
    """A scripted Operator step whose arguments are computed from what the model was shown."""
    return lambda msgs: LLMResponse(None, [call(name, args_fn(msgs), cid)], {"total_tokens": 300})


def statement_steps(tag):
    return [
        act("bank_portal__open", lambda m: {"path": "/statement?from_date=2026-09-28"}, f"{tag}-open"),
        act("bank_portal__download", lambda m: {"ref": ref_of(m, "Download CSV")}, f"{tag}-dl"),
        act("finish_task", lambda m: {"summary": "statement saved", "result_call_ids": [f"{tag}-dl"]}, f"{tag}-fin"),
    ]


RECEIPT_STEPS = [
    act("erp_web__open", lambda m: {"path": f"/invoices/{REF}/receipts/new"}, "r-open"),
    act("erp_web__fill", lambda m: {"ref": ref_of(m, "Amount (₹)"), "value": "54000.00"}, "r-amt"),
    act("erp_web__fill", lambda m: {"ref": ref_of(m, "Bank reference / UTR"),
                                    "value": json.loads(m[1]["content"])["task"].split("UTR ")[1].split(".")[0]},
        "r-utr"),
    act("erp_web__click", lambda m: {"ref": ref_of(m, "Post receipt")}, "r-post"),
    act("finish_task", lambda m: {"summary": "receipt posted", "result_call_ids": ["r-post"]}, "r-fin"),
]

DRAFT = {"subject": f"{REF} overdue", "body": f"Dear Ganesh, {REF} for ₹54,000.00 is overdue. Please share a date.",
         "attachments": [f"invoices/{REF}.pdf"], "facts_used": ["number", "balance_paise"]}


def classify(messages):
    text = messages[-2]["content"].replace("<untrusted_data>", "").replace("</untrusted_data>", "").strip()
    quote = next(s.strip() + "." for s in text.split(".") if "transfer" in s)
    return {"intent": "promise_to_pay", "confidence": 0.95, "about_entity": True, "entity_refs": [REF],
            "quote": quote, "amount_text": "₹54,000.00", "date": "2026-10-20"}


def test_invoice_goes_overdue_to_paid(servers, sandbox_var, asgi):  # noqa: F811
    # No reminder draft: the payment is reconciled before the reminder timer is handled.
    script = [DRAFT, classify, *statement_steps("mon"), *statement_steps("tue"), *RECEIPT_STEPS]
    llm = ScriptedLLM(script)
    rt = build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS), llm=llm,
                       hub_transport=asgi["hub"], erp_transport=asgi["erp"])
    from sandbox.common.world import load_world
    sim = SimEngine(load_world(), "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    orch = Orchestrator(rt, "m1")

    async def run():
        try:
            await orch.start(REF)
            states = []
            for _ in range(3):
                await sim.tick()
                await orch.step()
                states.append(next(e for e in rt.pipeline.list("m1") if e.external_ref == REF).state)
                if states[-1] == "PAID":
                    break
                clock.advance(timedelta(days=1))
            return states
        finally:
            await sim.close()
            await rt.close()

    states = asyncio.run(run())
    assert states == ["PROMISED", "PAID"], "\n".join(orch.log)
    assert not llm.script, "every scripted LLM step was used"

    path = [(e.payload["from"], e.payload["to"]) for e in rt.events.query(types=["entity.transitioned"])]
    assert path == [("OVERDUE", "CONTACTED"), ("CONTACTED", "PROMISED"), ("PROMISED", "PAID")]
    paid = rt.events.query(types=["entity.transitioned"])[-1]
    assert all(c["passed"] for c in paid.payload["guard"])
    report = rt.events.query(types=["verification.completed"])[-1].payload
    assert report["passed"] and len(report["criteria"]) == 3

    inv = httpx.get(f"{servers['erp_web']}/api/v1/invoices/{REF}", headers={"X-API-Key": "erp-sandbox-readonly-key"}).json()
    assert inv["status"] == "PAID" and inv["balance_paise"] == 0 and len(inv["receipts"]) == 1
    assert inv["receipts"][0]["reference"] == paid.payload["facts"]["settlement"]["record_id"]
    ok, _ = rt.events.verify_chain()
    assert ok
    assert next(e for e in rt.pipeline.list("m1") if e.external_ref == REF).data["balance"] == 0

    async def customer_inbox():
        async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                     headers={"Authorization": "Bearer hub-sim-token"}) as c:
            return (await c.get("/api/v1/mailboxes/ganesh@shreeganeshtraders.example/messages")).json()
    assert len(asyncio.run(customer_inbox())) == 1, "no reminder may be sent once the money has arrived"
