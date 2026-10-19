"""Memory wiring (architecture §8.3): what the employee learns from a mission, and where it uses it.

Learning (after a mission's daily review, and when it ends):
    remember_facts   code: per counterparty, what happened this mission (contacts, replies and their
                     channels, dead channels, promises kept / broken and how late, disputes, claims
                     that did not hold) -> one fact per counterparty per mission
                     code: when a scripted route failed and the Operator did the task -> a playbook
                     memory for that app (what failed, the steps that worked)
    distill          Memory Writer (light LLM): lessons from those episodes, each citing them;
                     screened like any untrusted text before it is stored

Using (retrieval):
    counterparty_memory  history summed over missions + lessons -> the Mission Agent's GROUPS
                         (plan, daily review, chat), and a per-counterparty promise-kept rate for the
                         forecast
    channel_order        outreach tries the channel a counterparty answered on first and a channel
                         that bounced last
    app_notes            the Operator reads lessons and playbooks for the apps of its task

A human can pin or reject any memory (CLI `memory`, API). Rejected memories are never used again.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from apps.flows import role_cfg, role_prompt
from apps.runtime import Runtime
from core.llm import LLMError
from core.memory import MemoryItem
from subagents.memory_writer import MemoryWriter, near_duplicate


def _cfg(rt: Runtime) -> dict[str, Any]:
    return role_cfg(rt).get("memory", {})


def _scope(rt: Runtime, group: str) -> str:
    return f"{_cfg(rt).get('counterparty_scope', 'counterparty')}:{group}"


def _group_key(rt: Runtime) -> str:
    return role_cfg(rt)["mission"]["selection"]["group_key"]


def _groups(rt: Runtime, mission_id: str) -> dict[str, str]:
    """entity id -> group id for the mission's entities."""
    key = _group_key(rt)
    return {e.id: str(e.data.get("record", {}).get(key)) for e in rt.pipeline.list(mission_id)}


def _day(ts: Any) -> date | None:
    if isinstance(ts, datetime):
        return ts.date()
    try:
        return datetime.fromisoformat(str(ts)).date()
    except ValueError:
        return None


# --- learning: facts by code ----------------------------------------------------------------------

def counterparty_stats(rt: Runtime, mission_id: str) -> dict[str, dict[str, Any]]:
    cfg = _cfg(rt).get("commitment", {})
    promised, kept_states, broken_states = cfg.get("state"), set(cfg.get("kept", [])), set(cfg.get("broken", []))
    date_fact = cfg.get("date_fact")
    contested = set(_cfg(rt).get("contested", []))
    owner = _groups(rt, mission_id)
    stats: dict[str, dict[str, Any]] = {g: {"contacts": 0, "replies": 0, "replies_by_channel": {}, "dead_channels": [],
                                            "promises": 0, "kept": 0, "broken": 0, "days_late": [],
                                            "paid_unprompted": 0, "disputes": 0, "unfounded_claims": 0,
                                            "intents": [], "first_contact": None, "first_reply": None}
                                        for g in set(owner.values())}
    open_promise: dict[str, str | None] = {}
    contacted: dict[str, set[str]] = {}
    replied: dict[str, set[str]] = {}
    for ev in rt.events.query(mission_id=mission_id, limit=1_000_000):
        g = owner.get(ev.entity_id or "")
        if g is None:
            continue
        s, p = stats[g], ev.payload
        if ev.type == "message.classified":
            if p.get("message_id") not in replied.setdefault(g, set()):
                replied[g].add(p.get("message_id"))
                s["replies"] += 1
                ch = p.get("channel")
                if ch:
                    s["replies_by_channel"][ch] = s["replies_by_channel"].get(ch, 0) + 1
                s["first_reply"] = s["first_reply"] or ev.occurred_at.isoformat()
                if p.get("intent") and p["intent"] not in s["intents"]:
                    s["intents"].append(p["intent"])
        elif ev.type == "tool.failed" and str(p.get("tool", "")).endswith(".send") and p.get("error_kind") == "permanent":
            ch = p["tool"].split(".")[0]
            if ch not in s["dead_channels"]:
                s["dead_channels"].append(ch)
        elif ev.type == "entity.transitioned":
            to, facts = p.get("to"), p.get("facts") or {}
            if to == promised:
                if facts.get(date_fact) and facts.get(date_fact) != open_promise.get(ev.entity_id):
                    s["promises"] += 1
                open_promise[ev.entity_id] = facts.get(date_fact) or open_promise.get(ev.entity_id)
            elif to in kept_states:
                due = open_promise.pop(ev.entity_id, None)
                on = (facts.get("settlement") or {}).get("on")
                if due:
                    s["kept"] += 1
                    if on and _day(on) and _day(due):
                        s["days_late"].append((_day(on) - _day(due)).days)
                else:
                    s["paid_unprompted"] += 1
            elif to in broken_states:
                open_promise.pop(ev.entity_id, None)
                s["broken"] += 1
            elif to in contested:
                s["disputes"] += 1
    for e in rt.pipeline.list(mission_id):
        g = owner[e.id]
        for x in rt.evidence.for_entity(e.id):
            if x["subject"].startswith("contact:") and x["ref"] not in contacted.setdefault(g, set()):
                contacted[g].add(x["ref"])
                stats[g]["contacts"] += 1
                stats[g]["first_contact"] = min(filter(None, [stats[g]["first_contact"], x["at"]]))
        if (e.data.get("paid_claim") or {}).get("status") == "reference_requested":
            stats[g]["unfounded_claims"] += 1
    return stats


def _fact_text(s: dict[str, Any]) -> str:
    parts = [f"{s['contacts']} message(s) sent"]
    if s["replies"]:
        chans = ", ".join(f"{k} {v}" for k, v in s["replies_by_channel"].items())
        lag = None
        if s["first_contact"] and s["first_reply"]:
            lag = (_day(s["first_reply"]) - _day(s["first_contact"])).days
        parts.append(f"{s['replies']} reply(ies) ({chans})" + (f", first after {lag} day(s)" if lag is not None else ""))
    elif s["contacts"]:
        parts.append("no reply")
    if s["dead_channels"]:
        parts.append(f"undeliverable: {', '.join(s['dead_channels'])}")
    if s["promises"]:
        late = s["days_late"]
        parts.append(f"{s['promises']} promise(s), {s['kept']} kept" + (
            f" (paid {sum(late) / len(late):+.0f} day(s) vs the promised date)" if late else "")
            + (f", {s['broken']} broken" if s["broken"] else ""))
    if s["paid_unprompted"]:
        parts.append(f"{s['paid_unprompted']} paid without a promise")
    if s["disputes"]:
        parts.append(f"{s['disputes']} dispute(s)")
    if s["unfounded_claims"]:
        parts.append(f"{s['unfounded_claims']} 'already paid' claim(s) not found in the bank")
    return "; ".join(parts) + "."


def remember_facts(rt: Runtime, mission_id: str) -> list[MemoryItem]:
    """Code-computed facts: one per counterparty per mission, replaced as the mission goes on."""
    if rt.memory is None:
        return []
    out = []
    for g, s in counterparty_stats(rt, mission_id).items():
        if not s["contacts"] and not s["replies"] and not s["kept"] and not s["paid_unprompted"]:
            continue
        item = rt.memory.put(_scope(rt, g), "fact", f"mission:{mission_id}", _fact_text(s), data=s, source=mission_id)
        if item:
            out.append(item)
    out += remember_playbooks(rt, mission_id)
    return out


def app_episodes(rt: Runtime, mission_id: str) -> list[dict[str, Any]]:
    """Each time a scripted route failed: what failed, which app, how it was done in the end."""
    tasks: dict[str, dict[str, Any]] = {}
    out = []
    for ev in rt.events.query(mission_id=mission_id, limit=1_000_000):
        p = ev.payload
        if ev.type == "task.started":
            tasks[ev.task_id] = {"type": p.get("type"), "apps": p.get("apps", []), "steps": [], "done": False}
        elif ev.type == "task.step" and ev.task_id in tasks and p.get("ok"):
            tasks[ev.task_id]["steps"].append(p.get("tool"))
        elif ev.type == "task.completed" and ev.task_id in tasks:
            tasks[ev.task_id]["done"] = True
        elif ev.type in {"fallback.used", "fallback.exhausted"}:
            last = next((t for t in reversed(tasks.values())), None)
            app = (last or {}).get("apps", [None])[0]
            if not app:
                continue
            out.append({"id": ev.id, "scope": f"app:{app}", "what": p.get("what"), "route_that_worked":
                        p.get("rung") if ev.type == "fallback.used" else None, "failed_routes": p.get("attempts", []),
                        "task_type": last["type"], "operator_steps": last["steps"] if last["done"] else [],
                        "at": ev.occurred_at.date().isoformat()})
    return out


def remember_playbooks(rt: Runtime, mission_id: str) -> list[MemoryItem]:
    out = []
    for ep in app_episodes(rt, mission_id):
        if ep["route_that_worked"] != "operator" or not ep["operator_steps"]:
            continue
        steps = " -> ".join(s.split("__")[-1] for s in ep["operator_steps"])
        failed = "; ".join(str(a)[:120] for a in ep["failed_routes"]) or "unknown error"
        text = (f"{ep['what']}: the scripted route failed ({failed}); the Operator completed it in "
                f"{len(ep['operator_steps'])} steps: {steps}.")
        item = rt.memory.put(ep["scope"], "playbook", ep["task_type"], text, data=ep, cites=[ep["id"]],
                             source=mission_id)
        if item:
            out.append(item)
    return out


# --- learning: lessons by the Memory Writer ----------------------------------------------------------

def counterparty_episodes(rt: Runtime, mission_id: str) -> list[dict[str, Any]]:
    owner = _groups(rt, mission_id)
    stats = counterparty_stats(rt, mission_id)
    timeline: dict[str, list[dict[str, Any]]] = {g: [] for g in stats}
    keep = {"message.classified", "entity.transitioned", "mission.group_contacted", "handoff.requested",
            "security.injection_suspected", "security.fraud_suspected"}
    for ev in rt.events.query(mission_id=mission_id, types=sorted(keep), limit=1_000_000):
        g = owner.get(ev.entity_id or "") or ev.payload.get("group")
        if g not in timeline:
            continue
        p = ev.payload
        item = {"at": ev.occurred_at.strftime("%a %d %b"), "what": ev.type}
        item.update({k: p[k] for k in ("intent", "quote", "channel", "from", "to", "ok", "reason") if p.get(k)})
        timeline[g].append(item)
    tok = rt.tokenizer()
    return [{"id": f"cp:{g}", "scope": _scope(rt, g), "facts": {k: v for k, v in s.items()
                                                                if k not in {"first_contact", "first_reply"}},
             "timeline": tok.tokenize_obj(timeline[g][-15:])}
            for g, s in stats.items() if s["contacts"] or s["replies"]]


async def distill(rt: Runtime, mission_id: str) -> dict[str, Any]:
    """Facts (code), then lessons (Memory Writer). Returns what was written and what code dropped."""
    if rt.memory is None:
        return {"facts": [], "lessons": [], "problems": ["no memory store"]}
    facts = remember_facts(rt, mission_id)
    if rt.llm is None:
        return {"facts": facts, "lessons": [], "problems": ["no LLM: lessons skipped"]}
    episodes = counterparty_episodes(rt, mission_id) + app_episodes(rt, mission_id)
    scopes = {ep["scope"]: ep["id"] for ep in episodes}
    known = [m.text for m in rt.memory.list(statuses=("active", "pinned", "rejected")) if m.kind != "fact"]
    writer = MemoryWriter(rt.llm, role_prompt=role_prompt(rt, "memory"))
    try:
        lessons, problems = await writer.distill(episodes, scopes=scopes, known=known,
                                                 budget=rt.budget(mission_id, 20_000))
    except LLMError as exc:
        return {"facts": facts, "lessons": [], "problems": [f"memory writer failed: {exc}"]}
    rejected = {}
    for m in rt.memory.list(statuses=("rejected",)):
        if m.kind == "lesson":
            rejected.setdefault(m.scope, []).append(m.text)
    written = []
    for lesson in lessons:
        if any(near_duplicate(lesson.text, r) for r in rejected.get(lesson.scope, [])):
            problems.append(f"a human rejected this lesson before (reworded): {lesson.text[:60]!r} dropped")
            continue
        screening = await rt.guard.screen(lesson.text) if rt.guard else None
        if screening is not None and screening.flagged:
            problems.append(f"lesson looked like an instruction (score {screening.score:.2f}): dropped")
            continue
        key = hashlib.sha256(lesson.text.lower().encode()).hexdigest()[:12]
        item = rt.memory.put(lesson.scope, "lesson", key, lesson.text, cites=lesson.cites, source=mission_id)
        if item:
            written.append(item)
    return {"facts": facts, "lessons": written, "problems": problems}


# --- using memory ---------------------------------------------------------------------------------

def counterparty_memory(rt: Runtime, groups: list[str]) -> dict[str, dict[str, Any]]:
    """Per group: history summed over every mission remembered, and lessons (pinned first)."""
    if rt.memory is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for g in groups:
        items = rt.memory.list(scope=_scope(rt, g))
        facts = [m.data for m in items if m.kind == "fact"]
        notes = [m.text for m in items if m.kind == "lesson"]
        if not facts and not notes:
            continue
        h: dict[str, Any] = {"missions": len(facts)}
        for k in ("contacts", "replies", "promises", "kept", "broken", "paid_unprompted", "disputes", "unfounded_claims"):
            h[k] = sum(int(f.get(k, 0)) for f in facts)
        late = [d for f in facts for d in f.get("days_late", [])]
        if late:
            h["avg_days_late"] = round(sum(late) / len(late), 1)
        if h["promises"]:
            h["promise_kept_rate"] = round(h["kept"] / h["promises"], 2)
        chans: dict[str, int] = {}
        for f in facts:
            for c, n in f.get("replies_by_channel", {}).items():
                chans[c] = chans.get(c, 0) + n
        if chans:
            h["replies_by_channel"] = chans
        dead = sorted({c for f in facts for c in f.get("dead_channels", [])})
        if dead:
            h["undeliverable_channels"] = dead
        out[g] = {"history": {k: v for k, v in h.items() if v}, **({"notes": notes} if notes else {})}
    return out


def channel_order(rt: Runtime, group: str | None, order: list[str]) -> list[str]:
    """Channels that bounced go last; the channel the counterparty answers on goes first."""
    if rt.memory is None or not group:
        return order
    h = counterparty_memory(rt, [group]).get(group, {}).get("history", {})
    dead = set(h.get("undeliverable_channels", []))
    replies = h.get("replies_by_channel", {})
    return sorted(order, key=lambda c: (c in dead, -replies.get(c, 0), order.index(c)))


def app_notes(rt: Runtime, apps: list[str]) -> dict[str, list[str]]:
    """Learned notes per app for the Operator: playbooks first, then lessons."""
    if rt.memory is None:
        return {}
    out: dict[str, list[str]] = {}
    for app in apps:
        items = rt.memory.list(scope=f"app:{app}")
        texts = [f"(learned) {m.text}" for m in sorted(items, key=lambda m: m.kind != "playbook")
                 if m.kind in {"playbook", "lesson"}]
        if texts:
            out[app] = texts[:6]
    return out
