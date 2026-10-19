"""Day 3 morning acceptance, offline: scenario 6 (a different brief, no code for it) and completion of a
goal that counts confirmations rather than money.

"Send statements of account to all customers over ₹1L outstanding and get confirmations" runs on the
same runtime as the collections brief. Everything that changes is data in the role pack: the
`confirm_balance` goal (what counts, what the target is), its approach and levers, the CONFIRMED state
and its guard, a group-level selection parameter, and a goal-scoped exclusion. The scripted LLM only
plays the agents' choices; ERP, bank, hub, simulator, guards and the re-observation are real.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from apps.mission import MissionRunner, plan_text
from apps.runtime import build_runtime
from sandbox.common import clock
from sandbox.customer_sim.engine import SimEngine
from tests.apps.test_mission import _untrusted
from tests.apps.test_mission_week import _inbox
from tests.apps.test_mission_week import llm as week_llm
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM
from tests.tools.test_browser import servers  # noqa: F401  (fixture: real ERP + bank HTTP servers)
from tools.vault import Vault

BRIEF6 = "Send statements of account to all customers over ₹1L outstanding and get confirmations."
SEEN: list[dict] = []


def llm(messages):
    system = messages[0]["content"]
    if "TASK: understand" in system and "confirmations" in messages[1]["content"]:
        import json
        brief = json.loads(messages[1]["content"])["BRIEF"]
        selection = {"min_group_value": "₹1L"} if "₹1L" in brief else {"customer_id": "C02"}
        return {"objective": "Get every customer over ₹1L to confirm their balance", "goal": "confirm_balance",
                "scope": "Customers whose open balance is at least ₹1L", "target_text": None, "deadline": None,
                "selection": selection, "success_criteria": [], "assumptions": [], "open_questions": []}
    if "You classify one message" in system:
        text = _untrusted(messages)
        if "We confirm the balance" in text:
            quote = next(s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if "We confirm the balance" in s)
            return {"intent": "balance_confirmed", "confidence": 0.95, "about_entity": True, "entity_refs": [],
                    "quote": quote, "amount_text": re.search(r"₹[\d,]+\.\d{2}", text).group(0)}
    if "Conversation Agent" in system:
        import json
        SEEN.append(json.loads(messages[1]["content"]))
    return week_llm(messages)


def _run(rt, sim, runner, brief, days):
    async def run():
        try:
            await runner.create(brief)
            runner.approve(by="test")
            for _ in range(days):
                await runner.tick()
                await sim.tick()
                result = await runner.tick()
                if result["status"] != "ACTIVE":
                    break
                clock.advance(timedelta(days=1))
                await sim.tick()
        finally:
            await sim.close()
            await rt.close()
    asyncio.run(run())


def _setup(sandbox_var, asgi, world):
    rt = build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS), llm=ScriptedLLM([llm] * 2000),
                       hub_transport=asgi["hub"], erp_transport=asgi["erp"])
    sim = SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"], bank_transport=asgi["bank"])
    return rt, sim, MissionRunner(rt)


def test_scenario6_statement_confirmations_with_no_new_code(servers, sandbox_var, asgi, world):  # noqa: F811
    SEEN.clear()
    rt, sim, runner = _setup(sandbox_var, asgi, world)
    _run(rt, sim, runner, BRIEF6, days=4)
    log = "\n".join(runner.log)
    m = runner.mission

    # understood as a different goal; the target is every customer in scope, counted, not money
    assert m.spec.goal == "confirm_balance"
    assert m.selection == {"min_group_value": 1_00_000_00}, "₹1L parsed by code, in paise"
    groups = set(m.data["groups"])
    assert groups == {"C01", "C02", "C03", "C05", "C06", "C07", "C08", "C12", "C15"}, log
    assert m.spec.target_value == 9 and m.spec.target_unit == "customers"
    reasons = {x["ref"]: x["reason"] for x in m.data["excluded"]}
    assert "legal hold" in reasons["INV-2248"] and "payment plan" in reasons["INV-2259"]
    assert "min_group_value" in reasons["INV-2299"]          # C09: ₹66,000 in total
    assert "INV-2325" in m.data["scope"] and "INV-2328" in m.data["scope"], \
        "not-yet-due invoices are part of a balance confirmation (the exclusion is for chasing only)"

    # every batch uses the goal's approach (the scripted plan asked for a collections approach)
    assert {b.approach for b in m.plan.batches} == {"statement_confirmation"}
    assert any("unknown approach" in p for p in m.plan.problems)
    assert "goal: confirm_balance" in "\n".join(plan_text(m))

    # what Arya was told to write: statements for confirmation, never a payment chase
    purposes = [p["PURPOSE"] for p in SEEN]
    assert sum(p.startswith("Balance confirmation") for p in purposes) >= 9
    assert not any("ask for a payment date" in p.lower() for p in purposes), purposes
    first = asyncio.run(_inbox(asgi, "arjun.rao@initech-supplies.example"))[0]
    assert any(a["filename"].startswith("SOA-C02-") for a in first["attachments"])

    # confirmations: guarded (quoted, amount equal to our balance), the dispute goes to a human
    state = {e.external_ref: e.state for e in rt.pipeline.list(m.id)}
    confirmed = {g for g, d in m.data["groups"].items() if all(state[r] == "CONFIRMED" for r in d["refs"])}
    assert {"C01", "C02", "C06", "C07", "C12", "C15"} <= confirmed, log
    assert state["INV-2296"] == "DISPUTED"
    assert any(a.payload.get("action") == "dispute" for a in rt.approvals.list())
    assert runner.measured_value() >= 6 and runner.progress().target == 9
    assert m.status == "ACTIVE", "not every customer confirmed: the mission does not complete"
    ok, _ = rt.events.verify_chain()
    assert ok


def test_confirmation_goal_completes_only_after_re_reading_the_messages(servers, sandbox_var, asgi, world):  # noqa: F811
    rt, sim, runner = _setup(sandbox_var, asgi, world)
    _run(rt, sim, runner, "Send Initech a statement of account and get confirmations of the balance.", days=2)
    m = runner.mission
    assert m.selection == {"customer_id": "C02"} and m.spec.target_value == 1
    assert m.status == "COMPLETED", "\n".join(runner.log)
    check = [e.payload for e in rt.events.query(types=["verification.completed"]) if e.payload.get("subject") == "mission"][-1]
    assert check["passed"] and check["re_observed"] == 1 and "1 of 1 re-observed" in check["note"]


def test_a_confirmation_that_is_not_in_the_channel_does_not_count(sandbox_var, asgi, world):
    from apps.confirmations import fresh_confirmations

    rt, sim, runner = _setup(sandbox_var, asgi, world)

    async def run():
        try:
            e = rt.pipeline.add_entity("mis_x", "INV-2270", state="OVERDUE", value=1,
                                       data={"contact": {"email": "arjun.rao@initech-supplies.example"}})
            from core.models import Evidence
            rt.evidence.collect([Evidence(kind="message", ref="msg_forged", summary="We confirm the balance.",
                                          data={"channel": "email", "from": "arjun.rao@initech-supplies.example"})],
                                subject="transition:CONFIRMED", mission_id="mis_x", entity_id=e.id)
            return await fresh_confirmations(rt, "mis_x", {"C02": [e.id]})
        finally:
            await sim.close()
            await rt.close()

    count, _, note = asyncio.run(run())
    assert count == 0 and "not found: ['C02']" in note
