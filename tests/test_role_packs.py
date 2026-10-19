"""Generalization (ADR-8): a second job runs on the same runtime with no code.

The vendor_onboarding pack is a sketch (its tools and sandbox are not built), but everything the core
does with a role pack works on it unchanged: the pipeline loads and its guards decide, the Mission
Agent's checks accept its goals, selection, approaches and levers, and the evidence rule holds
("the vendor says it is done" never makes a vendor ACTIVE or created).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from agents.mission_agent import MissionAgent, PlanProposal, TickProposal, Understanding
from core.clock import ManualClock
from core.events import EventBus
from core.pipeline import Pipeline, load_pipeline
from core.scheduler import Scheduler

ROLE = Path(__file__).resolve().parents[1] / "packs" / "roles" / "vendor_onboarding" / "role.yaml"


def test_vendor_onboarding_pipeline_runs_on_the_core(runtime_db, events):
    clock = ManualClock(datetime(2026, 10, 19, 10, tzinfo=timezone.utc))
    spec = load_pipeline(ROLE)
    p = Pipeline(spec, runtime_db, events, Scheduler(runtime_db, EventBus(events), clock), clock, ZoneInfo("Asia/Kolkata"))
    e = p.add_entity("mis_v", "VR-101", state="REQUESTED", value=1)
    assert p.propose(e.id, "DOCS_REQUESTED", {"outreach": {"delivered": True, "message_id": "m1"}}).ok
    # "we have sent everything" without readable documents is not DOCS_RECEIVED
    claim = {"classifier": {"intent": "documents_sent", "confidence": 0.95}, "evidence": {"quote": "sent all"},
             "extractor": {"documents": 0}}
    assert not p.propose(e.id, "DOCS_RECEIVED", claim).ok
    claim["extractor"] = {"documents": 3}
    assert p.propose(e.id, "DOCS_RECEIVED", claim).ok
    assert p.propose(e.id, "DOCS_VERIFIED", {"extractor": {"pan_in_gstin": True, "names_match": True,
                                                            "cheque_name_matches": True}, "verifier": {"pass": True}}).ok
    # the portal, not the vendor's PDF: an inactive GSTIN never verifies
    assert not p.propose(e.id, "GSTIN_VERIFIED", {"portal": {"gstin_status": "Cancelled", "legal_name_matches": True,
                                                             "observed_fresh": True}}).ok
    assert p.propose(e.id, "GSTIN_VERIFIED", {"portal": {"gstin_status": "Active", "legal_name_matches": True,
                                                         "observed_fresh": True}}).ok
    assert not p.propose(e.id, "CREATED_IN_ERP", {"erp": {"vendor_exists": False}}).ok
    assert p.propose(e.id, "CREATED_IN_ERP", {"erp": {"vendor_exists": True, "details_match": True}}).ok
    assert not p.propose(e.id, "ACTIVE", {}).ok, "activation is human-only"
    assert p.propose(e.id, "ACTIVE", {}, actor="human").ok
    timers = {t.on for t in spec.timers}
    assert timers == {"DOCS_REQUESTED", "NEEDS_FIX"}


def test_mission_agent_checks_accept_the_vendor_pack():
    cfg = yaml.safe_load(ROLE.read_text())
    m = cfg["mission"]
    agent = MissionAgent(None, minor_units=m["minor_units"], value_unit=cfg["value_unit"])
    u = agent.check_understanding(
        Understanding(objective="Onboard the new packaging vendors", goal="onboard", scope="NEW requests, packaging",
                      selection={"category": "packaging", "unknown": 1}),
        brief="Onboard the new packaging vendors by Friday", today=date(2026, 10, 19),
        selection_params=m["selection"]["params"], goals={k: v["description"] for k, v in m["goals"].items()})
    assert u.spec.goal == "onboard" and u.selection == {"category": "packaging"}
    assert any("unknown selection parameter" in x for x in u.problems)
    approaches = {k: v["description"] for k, v in m["approaches"].items()}
    groups = [{"group": "VR-101", "suggested_approach": "standard_kyc"}, {"group": "VR-102"}]
    plan = MissionAgent.check_plan(PlanProposal(summary="s", batches=[{"name": "b", "groups": ["VR-101"],
                                                                       "approach": "firm_with_statement"}]),
                                   groups=groups, approaches=approaches, capacity=m["capacity_per_day"])
    assert {b.approach for b in plan.batches} == {"standard_kyc"} and len(plan.problems) == 2
    tick = MissionAgent.check_tick(TickProposal(assessment="a", actions=[
        {"lever": "follow_up", "groups": ["VR-101"]}, {"lever": "propose_payment_plan", "groups": ["VR-101"]}]),
        groups=[{"group": "VR-101", "states": ["DOCS_REQUESTED"], "days_since_contact": 3}], levers=m["levers"])
    assert [a.lever for a in tick.actions] == ["follow_up"], "collections levers do not exist in this job"
