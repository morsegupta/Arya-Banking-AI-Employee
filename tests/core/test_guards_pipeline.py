from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.clock import ManualClock
from core.events import EventBus
from core.guards import GuardSyntaxError, compile_guard, evaluate
from core.pipeline import Pipeline, load_pipeline
from core.scheduler import Scheduler

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
ROLE = "packs/roles/collections/role.yaml"


# --- guard language ------------------------------------------------------------------------

@pytest.mark.parametrize("clause, ctx, expected", [
    ("a.b == 'x'", {"a": {"b": "x"}}, True),
    ("a.b == \"x\"", {"a": {"b": "y"}}, False),
    ("n >= 0.8", {"n": 0.8}, True),
    ("n >= 0.8", {"n": 0.79}, False),
    ("m >= bal - tol", {"m": 9900, "bal": 10000, "tol": 100}, True),
    ("m >= bal - tol", {"m": 9899, "bal": 10000, "tol": 100}, False),
    ("m < bal - 100 + 1", {"m": 9900, "bal": 10000}, True),
    ("x exists", {"x": "q"}, True),
    ("x exists", {"x": ""}, False),
    ("x exists", {}, False),
    ("flag", {"flag": True}, True),
    ("flag == true", {"flag": False}, False),
    ("missing.path > 1", {}, False),
    ("s > 1", {"s": "abc"}, False),          # incomparable -> fail closed
    ("b > 0", {"b": True}, False),           # booleans are not numbers
])
def test_clauses(clause, ctx, expected):
    assert evaluate(compile_guard(clause), ctx).passed is expected


@pytest.mark.parametrize("bad", ["a ==", "== a", "a = b", "a; drop", "__import__('os')", "a == b c", {"none": []}])
def test_bad_guards_fail_at_load(bad):
    with pytest.raises(GuardSyntaxError):
        compile_guard(bad)


def test_all_any_and_audit():
    g = compile_guard({"all": ["a > 1", {"any": ["b == 1", "c == 1"]}]})
    r = evaluate(g, {"a": 2, "b": 0, "c": 1})
    assert r.passed and len(r.clauses) == 3 and r.clauses[0].values == {"a": 2}
    r = evaluate(g, {"a": 0, "b": 1})
    assert not r.passed and r.failures() == ["a > 1", "c == 1 (missing value)"]


def test_no_code_execution():
    ctx = {"a": {"__class__": "x"}}
    assert evaluate(compile_guard("a.__class__ == 'x'"), ctx).passed  # plain dict lookup, nothing more


# --- pipeline -------------------------------------------------------------------------------

@pytest.fixture()
def clock():
    return ManualClock(T0)


@pytest.fixture()
def pipe(runtime_db, events, clock):
    sched = Scheduler(runtime_db, EventBus(events), clock)
    return Pipeline(load_pipeline(ROLE), runtime_db, events, sched, clock, IST)


def add(pipe):
    return pipe.add_entity("m1", "INV-2291", state="OVERDUE", value=340000_00, data={"balance": 340000_00})


def test_add_is_idempotent(pipe):
    a, b = add(pipe), add(pipe)
    assert a.id == b.id and len(pipe.list("m1")) == 1
    with pytest.raises(ValueError):
        pipe.add_entity("m1", "X", state="PAID")


def test_contacted_requires_delivery_evidence(pipe, events):
    e = add(pipe)
    r = pipe.propose(e.id, "CONTACTED", {"outreach": {"delivered": False, "message_id": "msg_1"}})
    assert not r.ok and "outreach.delivered == true" in r.guard.failures()
    r = pipe.propose(e.id, "CONTACTED", {"outreach": {"delivered": True, "message_id": "msg_1"}})
    assert r.ok and r.entity.state == "CONTACTED"
    assert [x.type for x in events.query(entity_id=e.id)] == ["entity.added", "entity.transition_refused",
                                                               "entity.transitioned"]


def contacted(pipe):
    e = add(pipe)
    pipe.propose(e.id, "CONTACTED", {"outreach": {"delivered": True, "message_id": "m"}})
    return e


PROMISE_CTX = {"classifier": {"intent": "promise_to_pay", "confidence": 0.93},
               "evidence": {"quote": "We will transfer on 22 Oct"}, "verifier": {"pass": True}}


def test_promise_guard_and_timers(pipe):
    e = contacted(pipe)
    r = pipe.propose(e.id, "PROMISED", PROMISE_CTX, facts={"promise_amount": 340000_00, "promise_date": "2026-10-22"})
    assert r.ok
    timers = {t["key"].split(":")[-1]: t for t in pipe.scheduler.pending()}
    assert set(timers) == {"reminder", "check"}                      # followup timer of CONTACTED cancelled
    assert timers["reminder"]["due_at"] == "2026-10-21T10:00:00+05:30"
    assert timers["check"]["due_at"] == "2026-10-24T10:00:00+05:30"
    assert pipe.get(e.id).data["facts"]["promise_date"] == "2026-10-22"


def test_low_confidence_or_vague_promise_refused(pipe):
    e = contacted(pipe)
    low = {**PROMISE_CTX, "classifier": {"intent": "promise_to_pay", "confidence": 0.6}}
    assert not pipe.propose(e.id, "PROMISED", low, facts={"promise_amount": 1, "promise_date": "2026-10-22"}).ok
    assert not pipe.propose(e.id, "PROMISED", PROMISE_CTX, facts={"promise_amount": 1}).ok   # no date
    assert pipe.get(e.id).state == "CONTACTED"


def test_customer_claim_alone_never_marks_paid(pipe):
    e = contacted(pipe)
    claim = {"classifier": {"intent": "paid_claim", "confidence": 0.99}, "evidence": {"quote": "Already paid!"}}
    r = pipe.propose(e.id, "PAID", claim)
    assert not r.ok and len(r.guard.failures()) == 5


def test_paid_needs_match_receipt_and_verifier(pipe):
    e = contacted(pipe)
    proof = {"matcher": {"matched_amount": 340000_00, "counterparty_matches": True, "record_id": "T1"},
             "erp": {"receipt_exists": True}, "verifier": {"pass": True}}
    assert not pipe.propose(e.id, "PAID", {**proof, "verifier": {"pass": False}}).ok
    assert not pipe.propose(e.id, "PAID", {**proof, "matcher": {**proof["matcher"], "counterparty_matches": False}}).ok
    assert pipe.propose(e.id, "PAID", proof).ok


def test_partial_payment(pipe):
    e = contacted(pipe)
    proof = {"matcher": {"matched_amount": 170000_00, "counterparty_matches": True, "record_id": "T1"},
             "erp": {"receipt_exists": True}, "verifier": {"pass": True}}
    assert not pipe.propose(e.id, "PAID", proof).ok
    assert pipe.propose(e.id, "PARTIALLY_PAID", proof).ok


def test_human_only_and_system_only(pipe):
    e = contacted(pipe)
    assert "system" in pipe.propose(e.id, "NO_RESPONSE", {}).reason
    assert pipe.propose(e.id, "NO_RESPONSE", {}, actor="system").ok
    assert pipe.propose(e.id, "ESCALATED", {}).ok
    assert "human-only" in pipe.propose(e.id, "WRITTEN_OFF", {}).reason
    assert pipe.propose(e.id, "WRITTEN_OFF", {}, actor="human").ok


def test_unknown_transition_refused(pipe):
    e = add(pipe)
    assert "no transition OVERDUE -> PROMISED" in pipe.propose(e.id, "PROMISED", PROMISE_CTX).reason


def test_followup_timer_fires_after_three_days(pipe, clock, events):
    e = contacted(pipe)
    clock.advance(timedelta(days=3))
    fired = asyncio.run(pipe.scheduler.fire_due())
    assert [f.type for f in fired] == ["timer.followup_due"] and fired[0].entity_id == e.id


def test_role_pack_validation(tmp_path):
    bad = tmp_path / "role.yaml"
    bad.write_text("entity: x\npipeline:\n  states: [A]\n  initial: [A]\n  transitions:\n    - {from: A, to: B}\n")
    with pytest.raises(ValueError, match="unknown state 'B'"):
        load_pipeline(bad)


def test_promise_needs_the_verifier(pipe):
    e = contacted(pipe)
    ctx = {**PROMISE_CTX, "verifier": {"pass": False}}
    r = pipe.propose(e.id, "PROMISED", ctx, facts={"promise_amount": 340000_00, "promise_date": "2026-10-22"})
    assert not r.ok and "verifier.pass == true" in r.guard.failures()
