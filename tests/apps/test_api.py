"""Workspace API over the real sandbox: brief -> plan approval -> simulated day -> board, timeline, chat."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.runtime import build_runtime
from sandbox.customer_sim.engine import SimEngine
from tests.apps.test_mission import BRIEF
from tests.apps.test_mission_week import llm
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM
from tests.tools.test_browser import servers  # noqa: F401  (fixture: real ERP + bank HTTP servers)
from tools.vault import Vault


@pytest.fixture()
def client(servers, sandbox_var, asgi, world):  # noqa: F811
    app = create_app(
        rt_factory=lambda: build_runtime(var_dir=sandbox_var, vault=Vault(SANDBOX_SECRETS),
                                         llm=ScriptedLLM([llm] * 2000), hub_transport=asgi["hub"],
                                         erp_transport=asgi["erp"]),
        sim_factory=lambda: SimEngine(world, "http://hub", "http://bank", hub_transport=asgi["hub"],
                                      bank_transport=asgi["bank"]),
        lock_dir=sandbox_var, serve_ui=False)
    with TestClient(app) as c:
        yield c


def wait(c, job, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            assert j["status"] == "done", j
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job['name']} did not finish")


def test_brief_to_first_day_through_the_api(client):
    c = client
    assert c.get("/api/status").json()["missions"] == []
    job = c.post("/api/missions", json={"brief": BRIEF})
    assert job.status_code == 202
    mid = wait(c, job.json())["result"]["mission_id"]

    m = c.get(f"/api/missions/{mid}").json()
    assert m["status"] == "AWAITING_PLAN_APPROVAL" and m["spec"]["target_value"] == 12_00_000_00
    assert len(m["data"]["first_batch"]) == 6 and m["progress"]["verified"] == 0
    board = c.get(f"/api/missions/{mid}/board").json()
    assert len(board["entities"]) == 15 and {e["state"] for e in board["entities"]} == {"OVERDUE"}

    plan = c.get("/api/approvals").json()[0]
    assert plan["payload"]["action"] == "mission_plan" and plan["payload"]["messages"]
    assert c.post(f"/api/approvals/{plan['id']}", json={"approve": True, "by": "x"}).status_code == 422  # name too short
    wait(c, c.post(f"/api/approvals/{plan['id']}", json={"approve": True, "by": "Priya (FM)"}).json())
    assert c.get(f"/api/missions/{mid}").json()["status"] == "ACTIVE"
    assert c.post(f"/api/approvals/{plan['id']}", json={"approve": True, "by": "Priya (FM)"}).status_code == 409

    day = wait(c, c.post(f"/api/missions/{mid}/day").json())
    assert any("[world]" in line for line in day["log"]) and any("CONTACTED" in line for line in day["log"])
    board = c.get(f"/api/missions/{mid}/board").json()
    by_ref = {e["ref"]: e for e in board["entities"]}
    assert by_ref["INV-2263"]["state"] == "PAID"                  # already in the bank: settled, never chased
    assert by_ref["INV-2270"]["state"] == "PROMISED" and by_ref["INV-2270"]["promise_date"]

    e = c.get(f"/api/entities/{by_ref['INV-2270']['id']}").json()
    assert e["thread"][0]["outbound"] and not e["thread"][-1]["outbound"]
    assert any("CONTACTED → PROMISED" in t["text"] for t in e["timeline"])
    assert {ev["subject"] for ev in e["evidence"]} >= {"contact:outreach1", "transition:PROMISED"}

    turn = c.post(f"/api/missions/{mid}/chat", json={"question": "Who promised to pay this week?"}).json()
    assert turn["refs"] == ["INV-2270"] and turn["groups"] == ["C02"]     # invented links dropped
    assert c.get(f"/api/missions/{mid}").json()["data"]["chat"][-1]["question"].startswith("Who promised")

    feed = c.get(f"/api/events?mission_id={mid}").json()
    assert any(f["text"].startswith("INV-2263: OVERDUE → PAID") for f in feed)


def test_one_job_at_a_time_and_unknown_things_are_404(client):
    c = client
    assert c.get("/api/missions/mis_nope").status_code == 404
    assert c.get("/api/entities/ent_nope").status_code == 404
    assert c.post("/api/approvals/apr_nope", json={"approve": True, "by": "Priya"}).status_code == 404
    first = c.post("/api/missions", json={"brief": BRIEF}).json()
    second = c.post("/api/missions", json={"brief": BRIEF})
    assert second.status_code == 409 and "busy" in second.json()["detail"]
    wait(c, first)
