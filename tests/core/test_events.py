from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from core.events import EventBus
from core.models import Event

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def ev(type_, **kw):
    return Event(type=type_, occurred_at=T0, **kw)


def test_append_assigns_seq_and_hash(events):
    a, created = events.append(ev("mission.created", mission_id="m1", payload={"brief": "x"}))
    b, _ = events.append(ev("task.started", mission_id="m1", task_id="t1"))
    assert created and a.seq == 1 and b.seq == 2
    assert a.hash and b.hash and a.hash != b.hash


def test_dedupe_key_stores_once(events):
    first, c1 = events.append(ev("timer.fired", key="timer:k1"))
    again, c2 = events.append(ev("timer.fired", key="timer:k1", payload={"different": True}))
    assert c1 and not c2 and again.id == first.id
    assert len(events.query()) == 1


def test_query_filters(events):
    events.append(ev("task.started", mission_id="m1", task_id="t1"))
    events.append(ev("task.completed", mission_id="m1", task_id="t1"))
    events.append(ev("task.started", mission_id="m2", task_id="t2"))
    assert [e.type for e in events.query(mission_id="m1")] == ["task.started", "task.completed"]
    assert len(events.query(types=["task.started"])) == 2
    assert [e.mission_id for e in events.query(since_seq=2)] == ["m2"]
    round_trip = events.query(task_id="t2")[0]
    assert round_trip.occurred_at == T0


def test_hash_chain_detects_tampering(events, runtime_db):
    for i in range(5):
        events.append(ev("tool.succeeded", payload={"i": i, "when": T0}))
    assert events.verify_chain() == (True, None)
    conn = sqlite3.connect(runtime_db.path)
    conn.execute("""UPDATE events SET payload_json = '{"i": 99}' WHERE seq = 3""")
    conn.commit()
    assert events.verify_chain() == (False, 3)


def test_hash_chain_detects_deletion(events, runtime_db):
    for i in range(4):
        events.append(ev("x", payload={"i": i}))
    conn = sqlite3.connect(runtime_db.path)
    conn.execute("DELETE FROM events WHERE seq = 2")
    conn.commit()
    assert events.verify_chain() == (False, 3)


def test_bus_prefix_subscribers_and_isolation(events):
    bus = EventBus(events)
    seen = []

    async def on_task(e):
        seen.append(("task", e.type))

    async def broken(e):
        raise RuntimeError("boom")

    async def on_all(e):
        seen.append(("all", e.type))

    bus.subscribe("task.", on_task)
    bus.subscribe("", broken)
    bus.subscribe("", on_all)

    async def go():
        await bus.publish(ev("task.started"))
        await bus.publish(ev("message.received"))
        await bus.publish(ev("task.done", key="k"))
        await bus.publish(ev("task.done", key="k"))   # duplicate: stored once, not re-notified

    asyncio.run(go())
    assert seen == [("task", "task.started"), ("all", "task.started"), ("all", "message.received"),
                    ("task", "task.done"), ("all", "task.done")]
    assert len(bus.handler_errors) == 3
