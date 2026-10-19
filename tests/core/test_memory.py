"""Memory store, Memory Writer checks, and the forecast using remembered history."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.clock import ManualClock
from core.memory import MemoryStore
from core.mission import progress
from subagents.memory_writer import Lesson, Lessons, MemoryWriter


@pytest.fixture()
def memory(runtime_db, events):
    return MemoryStore(runtime_db, ManualClock(datetime(2026, 10, 19, 10, tzinfo=timezone.utc)), events)


def test_put_replaces_by_key_and_keeps_pins(memory):
    a = memory.put("customer:C01", "fact", "mission:m1", "2 messages sent.", data={"contacts": 2})
    b = memory.put("customer:C01", "fact", "mission:m1", "3 messages sent.", data={"contacts": 3})
    assert a.id == b.id and memory.get(a.id).text == "3 messages sent."
    memory.decide(a.id, "pinned", by="Priya")
    c = memory.put("customer:C01", "fact", "mission:m1", "4 messages sent.", data={"contacts": 4})
    assert c.status == "pinned", "a newer value keeps the human's pin"
    assert [m.text for m in memory.list(scope="customer:C01")] == ["4 messages sent."]


def test_rejected_memory_is_never_used_or_relearned(memory):
    m = memory.put("customer:C05", "lesson", "k1", "Pays on Fridays.")
    memory.decide(m.id, "rejected", by="Priya")
    assert memory.list(scope="customer:C05") == []
    assert memory.put("customer:C05", "lesson", "k1", "Pays on Fridays.") is None
    with pytest.raises(ValueError):
        memory.decide(m.id, "active", by="")


def test_memory_writer_keeps_only_cited_scoped_short_new_lessons():
    p = Lessons(lessons=[
        Lesson(scope="customer:C12", text="Email to this customer bounces; WhatsApp reaches them.", cites=["cp:C12"]),
        Lesson(scope="customer:C99", text="Unknown customer.", cites=["cp:C12"]),
        Lesson(scope="customer:C01", text="Pays three days late.", cites=["evt_made_up"]),
        Lesson(scope="customer:C01", text="Contact {{pii:email_1}} first.", cites=["cp:C01"]),
        Lesson(scope="customer:C01", text="x" * 300, cites=["cp:C01"]),
        Lesson(scope="customer:C01", text="Already known lesson.", cites=["cp:C01"]),
    ])
    kept, problems = MemoryWriter.check(p, episode_ids={"cp:C12", "cp:C01"},
                                        scopes={"customer:C12": "", "customer:C01": ""},
                                        known=["Already known lesson"])
    assert [k.text for k in kept] == ["Email to this customer bounces; WhatsApp reaches them."]
    assert len(problems) == 5


def test_forecast_uses_a_remembered_promise_kept_rate():
    weights = {"PROMISED": 0.8, "CONTACTED": 0.5}
    plain = progress(1000, 0, {"PROMISED": 600, "CONTACTED": 200}, weights)
    assert plain.expected == 580
    # 400 of the promised value belongs to a customer who kept 1 promise in 4
    remembered = progress(1000, 0, {"PROMISED": 600, "CONTACTED": 200}, weights, priors={"PROMISED": [(400, 0.25)]})
    assert remembered.expected == int(200 * 0.8 + 400 * 0.25) + 100


def test_a_rejected_lesson_reworded_is_still_recognised():
    from subagents.memory_writer import near_duplicate
    rejected = "Pays three days after the promised date."
    assert near_duplicate("Usually pays three days after the promise date.", rejected)
    assert not near_duplicate("Email bounces; WhatsApp reaches them.", rejected)
    assert not near_duplicate("Pays on Fridays.", "Pays on Mondays.")
