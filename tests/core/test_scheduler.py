from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.clock import ManualClock
from core.events import EventBus
from core.scheduler import Scheduler

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=IST)


@pytest.fixture()
def clock():
    return ManualClock(T0)


@pytest.fixture()
def bus(events):
    return EventBus(events)


@pytest.fixture()
def scheduler(runtime_db, bus, clock):
    return Scheduler(runtime_db, bus, clock)


def test_fires_only_when_due_and_exactly_once(scheduler, clock, bus, events):
    got = []

    async def handler(e):
        got.append(e)

    bus.subscribe("followup.", handler)
    assert scheduler.schedule("m1:e1:followup1", T0 + timedelta(days=3), "followup.due",
                              mission_id="m1", entity_id="e1", payload={"step": 1})
    assert asyncio.run(scheduler.fire_due()) == []
    clock.advance(timedelta(days=3))
    fired = asyncio.run(scheduler.fire_due())
    assert [e.type for e in fired] == ["followup.due"]
    assert fired[0].payload == {"step": 1, "timer_key": "m1:e1:followup1", "due_at": (T0 + timedelta(days=3)).isoformat()}
    assert fired[0].entity_id == "e1" and fired[0].occurred_at == clock.now()
    assert asyncio.run(scheduler.fire_due()) == []
    assert len(got) == 1 and len(events.query(types=["followup.due"])) == 1


def test_schedule_is_idempotent_and_reschedules_pending(scheduler, clock):
    assert scheduler.schedule("k", T0 + timedelta(days=1), "x.due")
    assert not scheduler.schedule("k", T0 + timedelta(days=5), "x.due")
    assert scheduler.pending()[0]["due_at"] == (T0 + timedelta(days=5)).isoformat()
    clock.advance(timedelta(days=2))
    assert asyncio.run(scheduler.fire_due()) == []


def test_fired_timer_is_not_rearmed(scheduler, clock):
    scheduler.schedule("k", T0, "x.due")
    asyncio.run(scheduler.fire_due())
    assert not scheduler.schedule("k", T0 + timedelta(days=1), "x.due")
    clock.advance(timedelta(days=2))
    assert asyncio.run(scheduler.fire_due()) == []


def test_cancel(scheduler, clock):
    scheduler.schedule("k", T0 + timedelta(hours=1), "x.due")
    assert scheduler.cancel("k") and not scheduler.cancel("k")
    clock.advance(timedelta(days=1))
    assert asyncio.run(scheduler.fire_due()) == []


def test_timers_survive_restart(runtime_db, events, clock):
    Scheduler(runtime_db, EventBus(events), clock).schedule("k", T0 + timedelta(hours=2), "x.due")
    # "crash": brand-new objects over the same database file
    from core.events import EventStore
    fresh = Scheduler(runtime_db, EventBus(EventStore(runtime_db)), clock)
    clock.advance(timedelta(hours=2))
    assert [e.type for e in asyncio.run(fresh.fire_due())] == ["x.due"]


def test_due_order_across_utc_offsets(scheduler, clock):
    utc = timezone.utc
    scheduler.schedule("late", datetime(2026, 10, 19, 4, 0, tzinfo=utc), "x.late")     # 09:30 IST
    scheduler.schedule("early", datetime(2026, 10, 19, 9, 15, tzinfo=IST), "x.early")  # 03:45 UTC
    clock.advance(timedelta(hours=1))
    assert [e.type for e in asyncio.run(scheduler.fire_due())] == ["x.early", "x.late"]


def test_naive_datetime_rejected(scheduler):
    with pytest.raises(ValueError):
        scheduler.schedule("k", datetime(2026, 10, 20), "x")


def test_run_loop_stops(scheduler, clock):
    scheduler.schedule("k", T0, "x.due")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop, poll_seconds=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(go())
    assert scheduler.pending() == []
