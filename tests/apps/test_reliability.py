"""Day 2 afternoon acceptance: the world misbehaves (sandbox chaos), the employee recovers or escalates.

Real Chromium against the real ERP and bank servers, real hub, scripted LLM decisions.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from apps.flows import load_entity, outreach
from apps.mission import MissionRunner
from apps.orchestrator import Orchestrator
from apps.runtime import build_runtime
from core.llm import LLMResponse
from sandbox import chaos
from tests.apps.test_mission import BRIEF
from tests.apps.test_mission_week import llm as week_llm
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM, call
from tests.tools.test_browser import servers  # noqa: F401  (fixture: real ERP + bank HTTP servers)
from tools.vault import Vault

ERP_KEY = {"X-API-Key": "erp-sandbox-readonly-key"}


def operator_gives_up_on_statements(messages):
    """An Operator that cannot get a statement either (the portal's download is broken for everyone)."""
    if messages[0]["content"].startswith("You are the Operator") and "bank account statement" in messages[1]["content"]:
        return LLMResponse(None, [call("fail_task", {"reason": "download keeps failing", "tried": ["download"]},
                                       "f1")], {"total_tokens": 10})
    return week_llm(messages)


@pytest.fixture()
def runtime(servers, sandbox_var, asgi):  # noqa: F811
    def make(script=None):
        return build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS),
                             llm=ScriptedLLM(script or [operator_gives_up_on_statements] * 200),
                             hub_transport=asgi["hub"], erp_transport=asgi["erp"])
    return make


def drive(*runtimes, body):
    """Run a test body and close its runtimes in ONE event loop (Playwright is bound to the loop it starts in)."""
    async def go():
        try:
            return await body()
        finally:
            for rt in runtimes:
                await rt.close()
    return asyncio.run(go())


def state(rt, ref, mission="m1"):
    return next(e for e in rt.pipeline.list(mission) if e.external_ref == ref).state


def ladder_events(rt):
    return [(e.type, e.payload.get("rung")) for e in rt.events.query(types=["statement.read", "fallback.used",
                                                                             "fallback.exhausted"])]


async def sync_one(rt, ref="INV-2263"):
    await load_entity(rt, "m1", ref)
    orch = Orchestrator(rt, "m1")
    await orch.bank_sync(force=True)
    return orch


def test_playbook_reads_statement_without_any_llm(runtime):
    rt = runtime()
    drive(rt, body=lambda: sync_one(rt))
    assert state(rt, "INV-2263") == "PAID"
    assert ladder_events(rt) == [("statement.read", "playbook")]
    assert not [c for c in rt.llm.seen if "bank account statement" in json.dumps(c["messages"])]


def test_renamed_csv_columns_still_parse(runtime):
    chaos.set_switches(**{"bank.csv_schema": "v2"})
    rt = runtime()
    drive(rt, body=lambda: sync_one(rt))
    assert state(rt, "INV-2263") == "PAID" and ladder_events(rt) == [("statement.read", "playbook")]


def test_broken_download_falls_back_to_the_screen(runtime):
    chaos.set_switches(**{"bank.csv_download": "error"})
    rt = runtime()
    orch = drive(rt, body=lambda: sync_one(rt))
    assert state(rt, "INV-2263") == "PAID", orch.log
    assert ladder_events(rt) == [("fallback.used", "screen")]
    attempts = rt.events.query(types=["fallback.used"])[0].payload["attempts"]
    assert attempts[0].startswith("playbook download") and attempts[1].startswith("operator")


def test_transient_bank_errors_are_retried(runtime):
    chaos.set_switches(**{"bank.transient_errors": 2})
    rt = runtime()
    drive(rt, body=lambda: sync_one(rt))
    assert state(rt, "INV-2263") == "PAID" and ladder_events(rt) == [("statement.read", "playbook")]


def test_bank_down_everywhere_goes_to_a_human(runtime):
    chaos.set_switches(**{"bank.transient_errors": 500})
    rt = runtime()
    drive(rt, body=lambda: sync_one(rt))
    assert state(rt, "INV-2263") == "OVERDUE"
    assert ladder_events(rt) == [("fallback.exhausted", None)]
    asked = rt.approvals.list(status="PENDING")
    assert asked and asked[0].payload["action"] == "failure" and asked[0].payload["what"] == "bank_sync"


def test_email_outage_falls_back_to_whatsapp(runtime):
    chaos.set_switches(**{"hub.email_outage": True})
    rt = runtime()
    out = drive(rt, body=lambda: outreach(rt, "m1", "INV-2291"))
    assert out.ok and out.channel == "whatsapp" and out.attempts[0].startswith("email: transient")
    assert state(rt, "INV-2291") == "CONTACTED"


def test_settlement_resumes_after_a_crash(runtime, monkeypatch, servers):  # noqa: F811
    import apps.settlement as settlement
    rt, rt2 = runtime(), runtime()
    real = settlement.post_receipt

    async def crash(*a, **k):
        raise RuntimeError("process killed")

    async def body():
        monkeypatch.setattr(settlement, "post_receipt", crash)
        with pytest.raises(RuntimeError):
            await sync_one(rt)
        with rt.db.read() as conn:
            assert conn.execute("SELECT status FROM settlements").fetchone()["status"] == "MATCHED"
        await rt.close()
        monkeypatch.setattr(settlement, "post_receipt", real)
        orch = Orchestrator(rt2, "m1")              # a fresh process on the same state
        await orch.bank_sync(force=True)
        return orch
    orch = drive(rt2, body=body)
    assert state(rt2, "INV-2263") == "PAID", orch.log
    assert rt2.events.query(types=["settlement.resumed"])
    receipts = httpx.get(f"{servers['erp_web']}/api/v1/receipts", headers=ERP_KEY).json()
    assert len([r for r in receipts if r["reference"] == "N287261234"]) == 1, "exactly one receipt"


async def _customer_reply(asgi, frm, body):
    async with httpx.AsyncClient(transport=asgi["hub"], base_url="http://hub",
                                 headers={"Authorization": "Bearer hub-sim-token"}) as c:
        (await c.post("/api/v1/messages", json={"channel": "email", "from": frm,
                                               "to": "ar@sharma-distributors.example", "subject": "Re",
                                               "body": body})).raise_for_status()


def test_inbox_message_survives_a_crash_and_poison_goes_to_a_human(runtime, asgi, monkeypatch):
    rt = runtime()
    orch = Orchestrator(rt, "m1")
    real = orch.handle_message
    failures = {"n": 1}

    async def flaky(msg):
        if failures["n"]:
            failures["n"] -= 1
            raise RuntimeError("killed mid-message")
        return await real(msg)

    async def body():
        await outreach(rt, "m1", "INV-2297")
        await _customer_reply(asgi, "ganesh@shreeganeshtraders.example",
                              "Noted on INV-2297. We will transfer ₹54,000.00 on 20 Oct 2026 (Tuesday). Ganesh")
        monkeypatch.setattr(orch, "handle_message", flaky)
        await orch.process_inbox()
        assert state(rt, "INV-2297") == "CONTACTED"            # not processed, not lost
        await orch.process_inbox()
        assert state(rt, "INV-2297") == "PROMISED", orch.log
        await _customer_reply(asgi, "ganesh@shreeganeshtraders.example", "another message")
        failures["n"] = 99
        for _ in range(3):
            await orch.process_inbox()
        assert any(a.payload.get("what", "").startswith("message:") for a in rt.approvals.list(status="PENDING"))
        assert await orch.process_inbox() == []                # past it now
    drive(rt, body=body)


def test_budget_spent_pauses_the_mission_until_a_human_resumes(runtime):
    from apps.approvals import decide
    rt = runtime([week_llm] * 100)
    r = MissionRunner(rt)

    async def body():
        await r.create(BRIEF)
        r.approve(by="test")
        b = r.budget
        b.charge(b.max_tokens)
        assert (await r.tick())["status"] == "PAUSED"
        a = rt.approvals.list(status="PENDING")[0]
        assert a.payload["action"] == "budget"
        await decide(rt, a.id, approve=True, by="fm")
        assert r.mission.status == "ACTIVE" and rt.mission_budget(r.mission_id).max_tokens == 2 * b.max_tokens
    drive(rt, body=body)


def test_failures_trigger_a_replan(runtime):
    from core.models import Event
    reviews = []

    def script(messages):
        if "TASK: review" in messages[0]["content"]:
            reviews.append(json.loads(messages[1]["content"])["RECENT_FAILURES"])
            return {"assessment": "Adapting.", "actions": []}
        return week_llm(messages)
    rt = runtime([script] * 200)
    r = MissionRunner(rt)

    async def body():
        await r.create(BRIEF)
        r.approve(by="test")
        r.orch._set_kv("last_sync", rt.clock.now().date().isoformat())
        await r.tick()                                       # day 0, nothing moved yet: no review
        assert reviews == []
        rt.events.append(Event(type="mission.group_contacted", mission_id=r.mission_id, occurred_at=rt.clock.now(),
                               payload={"group": "C05", "ok": False,
                                        "attempts": ["email: transient", "whatsapp: transient"]}))
        await r.tick()
        assert any("replanning after 1 failure" in line for line in r.log)
        assert reviews[-1] and reviews[-1][0]["group"] == "C05"
        await r.tick()                                       # nothing new since: no extra review
        assert len(reviews) == 1
    drive(rt, body=body)
