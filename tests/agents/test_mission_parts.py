"""Day 2 morning: Prioritizer, Matcher (multi + partial), Reply Classifier refs, Document Generator,
Mission Agent output checks, mission state machine."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pypdf import PdfReader

from agents.mission_agent import MissionAgent, PlanProposal, TickProposal, Understanding
from core.clock import ManualClock
from core.mission import MissionStore, MissionTransitionError, progress
from core.models import MissionSpec, MissionStatus, NextAction, PlanBatch
from subagents.docgen import Column, TableDocument, render
from subagents.matcher import Candidate, Record, allocate, match, unique_subset
from subagents.reply_classifier import Proposal, ReplyClassifier, resolve_refs
from subagents.scorer import Item, ScorerConfig, rank

# --- Prioritizer ------------------------------------------------------------------------------

CFG = ScorerConfig(weights={"value": 0.5, "age": 0.3, "risk": 0.2}, risk_signals={"multi": 0.5, "contested_before": 0.5},
                   approach_rules=[{"when": "group.multi == true", "approach": "firm"},
                                   {"when": "group.max_age >= 75", "approach": "firm"}],
                   default_approach="soft")


def test_rank_groups_by_total_score_and_rules():
    items = [Item("A-1", "A", "Alpha", 340_000, 62), Item("B-1", "B", "Beta", 120_000, 75),
             Item("B-2", "B", "Beta", 95_000, 70), Item("C-1", "C", "Gamma", 41_000, 48)]
    groups = rank(items, CFG, {"C": {"contested_before": True}})
    assert [g.id for g in groups][0] in {"A", "B"} and groups[-1].id == "C"
    by = {g.id: g for g in groups}
    assert by["B"].approach == "firm" and "multi" in by["B"].rationale       # several items -> firm
    assert by["A"].approach == "soft"
    assert [i.ref for i in by["B"].items] == ["B-1", "B-2"]                   # oldest first
    assert by["C"].risk > by["A"].risk                                        # contested before
    assert rank([], CFG) == []


# --- Matcher ------------------------------------------------------------------------------------

def cands():
    return [Candidate("e1", "INV-2270", "Initech Supplies LLP", 120_000_00, date(2026, 8, 5)),
            Candidate("e2", "INV-2275", "Initech Supplies LLP", 95_000_00, date(2026, 8, 10)),
            Candidate("e3", "INV-2288", "Initech Supplies LLP", 60_000_00, date(2026, 8, 22))]


def rec(text, amount, rid="N1"):
    return Record(rid, date(2026, 10, 20), text, amount)


def test_one_transfer_for_three_invoices():
    ms = match([rec("NEFT/N1/INITECH SUPPLIES LLP/INV 2270 INV 2275 INV 2288", 275_000_00)], cands(), tolerance=100)
    assert [(m.entity_id, m.matched_amount, m.kind) for m in ms] == [
        ("e1", 120_000_00, "reference_multi"), ("e2", 95_000_00, "reference_multi"), ("e3", 60_000_00, "reference_multi")]


def test_short_multi_transfer_is_allocated_oldest_first():
    ms = match([rec("NEFT/N1/INITECH SUPPLIES LLP/INV 2270 INV 2275 INV 2288", 150_000_00)], cands(), tolerance=100)
    assert [(m.entity_id, m.matched_amount) for m in ms] == [("e1", 120_000_00), ("e2", 30_000_00)]
    assert all(m.kind == "reference_multi_partial" for m in ms)


def test_more_than_due_is_left_for_a_human():
    assert match([rec("NEFT/N1/INITECH SUPPLIES LLP/INV 2270 INV 2275", 300_000_00)], cands(), tolerance=100) == []


def test_no_reference_unique_sum_matches_and_ambiguous_does_not():
    ms = match([rec("NEFT/N1/INITECH SUPPLIES LLP/PAYMENT", 180_000_00)], cands(), tolerance=100)
    assert {m.entity_id for m in ms} == {"e1", "e3"} and all(m.kind == "amount_sum" for m in ms)
    twins = [Candidate("a", "X-1", "Initech Supplies LLP", 50), Candidate("b", "X-2", "Initech Supplies LLP", 50),
             Candidate("c", "X-3", "Initech Supplies LLP", 100)]
    assert [c.entity_id for c in unique_subset(100, twins, 0)] == ["a", "b"]
    triplets = [Candidate(x, f"X-{x}", "Initech Supplies LLP", 50) for x in "abc"]
    assert unique_subset(100, triplets, 0) is None                        # three different pairs: ambiguous
    # c alone has the same amount as {a, b}: which was paid is ambiguous, so nothing matches
    assert match([rec("NEFT/N1/INITECH SUPPLIES LLP", 100)], twins, tolerance=0) == []


def test_lookalike_payer_never_matches_by_sum():
    assert match([rec("IMPS/N9/INITECH TRADING CO", 180_000_00)], cands(), tolerance=100) == []


def test_allocate():
    assert allocate(100, [60, 60]) == [60, 40]
    assert allocate(119, [60, 60], tolerance=1) == [60, 60]
    assert allocate(30, [60, 60]) == [30, 0]


# --- Reply Classifier: several entities in one conversation -------------------------------------------

def test_refs_resolve_by_number_and_default_to_whole_conversation():
    known = ["INV-2270", "INV-2275", "INV-2288"]
    assert resolve_refs(["inv 2270"], "paying INV2275 too", known) == ["INV-2270", "INV-2275"]
    rc = ReplyClassifier(llm=None, intents={"promise_to_pay": "", "unclear": ""})
    msg = "We will clear all three, ₹2,75,000 on 21 Oct."
    c = rc.check(Proposal(intent="promise_to_pay", confidence=0.9, about_entity=True, quote=msg,
                          amount_text="₹2,75,000", date="2026-10-21"), msg, known_refs=known)
    assert c.entity_refs == known and c.amount_minor == 275_000_00
    other = rc.check(Proposal(intent="promise_to_pay", confidence=0.9, about_entity=True, quote=msg,
                              entity_refs=["INV-9999"]), msg, known_refs=known)
    assert other.entity_refs == [] and any("outside" in p for p in other.problems)


# --- Document Generator ----------------------------------------------------------------------------------

def test_table_document_renders_given_values_only(tmp_path):
    doc = TableDocument(title="STATEMENT", issuer_lines=["Acme"], fields=[("Party", "Beta (B)")],
                        columns=[Column("ref", "Ref"), Column("amt", "Amount", align="R")],
                        rows=[{"ref": "X-1", "amt": "₹1,20,000.00"}, {"ref": "X-2", "amt": "₹95,000.00"}],
                        totals=[("Total", "₹2,15,000.00")], notes=["Pay to account 123."],
                        created_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
    out = render(doc, tmp_path / "s" / "doc.pdf")
    text = "".join(p.extract_text() for p in PdfReader(out).pages)
    for s in ["STATEMENT", "Beta (B)", "X-1", "X-2", "Rs. 1,20,000.00", "Rs. 2,15,000.00", "Pay to account 123."]:
        assert s in text
    assert render(doc, tmp_path / "again.pdf").read_bytes() == out.read_bytes()   # deterministic


# --- Mission Agent: code checks on its outputs ----------------------------------------------------------------

PARAMS = {"min_days_overdue": {"type": "integer"}, "customer_id": {"type": "string"}}
BRIEF = "Collect the 45+ day overdue invoices — target ₹12L by Friday."
TODAY = date(2026, 10, 19)


def agent():
    return MissionAgent(llm=None, minor_units=100, value_unit="paise", tz="Asia/Kolkata")


def test_understanding_target_deadline_and_selection_are_checked():
    u = agent().check_understanding(Understanding(objective="o", scope="s", target_text="₹12L", deadline="2026-10-23",
                                                  selection={"min_days_overdue": "45", "colour": "red"}),
                                    brief=BRIEF, today=TODAY, selection_params=PARAMS)
    assert u.spec.target_value == 12_00_000_00 and u.spec.deadline.date() == date(2026, 10, 23)
    assert u.selection == {"min_days_overdue": 45} and any("colour" in p for p in u.problems)

    bad = agent().check_understanding(Understanding(objective="o", scope="s", target_text="₹50L", deadline="2026-10-01",
                                                    selection={"min_days_overdue": "lots"}),
                                      brief=BRIEF, today=TODAY, selection_params=PARAMS)
    assert bad.spec.target_value is None and bad.spec.deadline is None and bad.selection == {}
    assert len(bad.problems) == 3       # invented target, past deadline, wrong type


GROUPS = [{"group": g, "suggested_approach": "soft"} for g in ["A", "B", "C", "D"]]


def test_plan_is_corrected_to_cover_scope_within_capacity():
    p = PlanProposal(summary="s", batches=[
        PlanBatch(name="first", groups=["A", "B", "Z", "A"], approach="firm", start_day=0),
        PlanBatch(name="odd", groups=["C"], approach="carrier_pigeon", start_day=9)])
    plan = MissionAgent.check_plan(p, groups=GROUPS, approaches={"soft": "", "firm": ""}, capacity=1, max_day=4)
    placed = [(b.start_day, b.groups, b.approach) for b in plan.batches]
    assert placed == [(0, ["A"], "firm"), (1, ["B"], "firm"), (4, ["C"], "soft"), (5, ["D"], "soft")]
    joined = " ".join(plan.problems)
    for word in ["unknown group 'Z'", "twice", "after the deadline", "carrier_pigeon", "did not cover ['D']"]:
        assert word in joined


def test_tick_actions_limited_to_levers_and_states():
    groups = [{"group": "A", "states": ["CONTACTED"]}, {"group": "B", "states": ["PROMISED"]}]
    levers = {"follow_up": {"states": ["CONTACTED"]}, "check_bank": {"needs_groups": False}}
    d = MissionAgent.check_tick(TickProposal(assessment="ok", actions=[
        NextAction(lever="follow_up", groups=["A", "B", "Q"]), NextAction(lever="write_off", groups=["A"]),
        NextAction(lever="check_bank")]), groups=groups, levers=levers)
    assert [(a.lever, a.groups) for a in d.actions] == [("follow_up", ["A"]), ("check_bank", [])]
    assert len(d.problems) == 3


# --- Mission state machine -----------------------------------------------------------------------------------

def test_mission_needs_human_approval_and_verified_money(runtime_db, events):
    store = MissionStore(runtime_db, events, ManualClock(datetime(2026, 10, 19, 9, tzinfo=timezone.utc)))
    m = store.create("r", "c", "brief")
    store.update(m.id, spec=MissionSpec(objective="o", scope="s", target_value=100))
    store.transition(m.id, MissionStatus.AWAITING_PLAN_APPROVAL, actor="agent")
    with pytest.raises(MissionTransitionError, match="actor"):
        store.transition(m.id, MissionStatus.ACTIVE, actor="agent")          # an agent cannot approve its own plan
    store.transition(m.id, MissionStatus.ACTIVE, actor="human")
    with pytest.raises(MissionTransitionError, match="below target"):
        store.transition(m.id, MissionStatus.COMPLETED, actor="system", verified_value=99)
    assert store.transition(m.id, MissionStatus.COMPLETED, actor="system", verified_value=100).status == "COMPLETED"
    types = [e.type for e in events.query(mission_id=m.id)]
    assert types.count("mission.transition_refused") == 2 and types[-1] == "mission.transitioned"


def test_forecast_counts_verified_plus_weighted_open():
    p = progress(1000, 300, {"PROMISED": 400, "CONTACTED": 400}, {"PROMISED": 0.75, "CONTACTED": 0.25})
    assert (p.expected, p.gap, p.expected_shortfall) == (700, 700, 300)


def test_follow_ups_respect_the_cadence():
    groups = [{"group": "A", "states": ["CONTACTED"], "days_since_contact": 0},
              {"group": "B", "states": ["CONTACTED"], "days_since_contact": 3},
              {"group": "C", "states": ["CONTACTED"], "days_since_contact": None}]
    levers = {"follow_up": {"states": ["CONTACTED"], "min_days_since_contact": 3}}
    d = MissionAgent.check_tick(TickProposal(assessment="x", actions=[NextAction(lever="follow_up",
                                                                                 groups=["A", "B", "C"])]),
                                groups=groups, levers=levers)
    assert d.actions[0].groups == ["B", "C"] and "contacted 0 day(s) ago" in d.problems[0]


def test_retry_only_for_groups_whose_first_contact_failed():
    groups = [{"group": "A", "states": ["OVERDUE"], "first_contact_failed": True},
              {"group": "B", "states": ["OVERDUE"], "first_contact_failed": False}]
    levers = {"retry_contact": {"states": ["OVERDUE"], "requires": "first_contact_failed"}}
    d = MissionAgent.check_tick(TickProposal(assessment="x", actions=[NextAction(lever="retry_contact",
                                                                                 groups=["A", "B"])]),
                                groups=groups, levers=levers)
    assert d.actions[0].groups == ["A"] and "does not have first_contact_failed" in d.problems[0]


def test_deadline_words_are_resolved_by_code_not_the_model():
    from datetime import date

    from agents.mission_agent import MissionAgent, Understanding
    agent = MissionAgent(None, minor_units=100, value_unit="paise", tz="Asia/Kolkata")
    params = {"min_days_overdue": {"type": "integer"}, "min_group_value": {"type": "money", "scope": "group"}}
    # live, gpt-oss read "by Friday" on Monday 19 Oct as Thursday 22 Oct
    u = agent.check_understanding(Understanding(objective="o", scope="s", target_text="₹12L", deadline_text="by Friday",
                                                deadline="2026-10-22"),
                                  brief="Collect the 45+ day overdue invoices — target ₹12L by Friday.",
                                  today=date(2026, 10, 19), selection_params=params)
    assert u.spec.deadline.date() == date(2026, 10, 23) and any("does not match" in p for p in u.problems)
    # live, it also took a scope threshold as the target
    u = agent.check_understanding(Understanding(objective="o", scope="s", target_text="₹1L",
                                                selection={"min_group_value": "₹1L"}),
                                  brief="Send statements of account to all customers over ₹1L outstanding.",
                                  today=date(2026, 10, 19), selection_params=params)
    assert u.spec.target_value is None and u.selection == {"min_group_value": 1_00_000_00}
