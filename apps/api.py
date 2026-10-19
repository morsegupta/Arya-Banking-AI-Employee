"""Workspace API: what the finance team's browser talks to (architecture §4, layer 1).

    uv run python -m apps.api            # http://127.0.0.1:8100  (UI + /api)

Reads are plain JSON over the runtime's state. Anything that acts (create a mission, decide an
approval, tick, run a simulated day) runs as a background job, one at a time, holding the same
run lock as the CLI, so the UI and the CLI never write over each other. Progress streams over SSE:
every new event in the audit log and every line the job writes.

Only binds to 127.0.0.1. There is no login: this is a single-user prototype on the operator's machine.
Two checks keep other web pages from using it through the operator's browser: the Host must be a local
name (defeats DNS rebinding), and every request that acts must carry the X-Arya-Workspace header, which a
cross-site page cannot add without a CORS preflight this API never grants.
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.approvals import decide as decide_approval
from apps.flows import format_money, role_cfg
from apps.mission import MissionRunner, plan_text
from apps.runtime import Runtime, build_runtime
from core.approvals import ApprovalError
from core.mission import MissionStore
from core.models import Event, new_id
from tools.base import RunContext

UI_DIST = Path(__file__).parent / "workspace" / "dist"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
ACTION_HEADER = "X-Arya-Workspace"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(value: str) -> str:
    """'127.0.0.1:8100' -> '127.0.0.1', '[::1]:8100' -> '[::1]', 'http://localhost:5173' -> 'localhost'."""
    value = value.split("://", 1)[-1].split("/", 1)[0].lower()
    if value.startswith("["):                       # IPv6 literal: the port follows the closing bracket
        return value[:value.index("]") + 1]
    return value.rsplit(":", 1)[0]


# --- jobs ------------------------------------------------------------------------------------------

class Job(BaseModel):
    id: str
    name: str
    status: str = "running"            # running | done | failed
    log: list[str] = Field(default_factory=list)
    result: Any = None
    error: str | None = None


class Workspace:
    """One runtime for the process, and at most one acting job at a time."""

    def __init__(self, rt: Runtime, sim_factory: Callable[[], Any] | None = None, lock_dir: Path | None = None):
        self.rt, self.sim_factory, self.lock_dir = rt, sim_factory, lock_dir
        self.job: Job | None = None
        self.jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    def start(self, name: str, work: Callable[[Job], Awaitable[Any]]) -> Job:
        if self.job and self.job.status == "running":
            raise HTTPException(409, f"busy: {self.job.name} is still running")
        job = Job(id=new_id("job"), name=name)
        self.job, self.jobs[job.id] = job, job

        async def run() -> None:
            async with self._lock:
                lock = None
                try:
                    if self.lock_dir:
                        from sandbox.common.runlock import run_lock
                        lock = run_lock(self.lock_dir, f"workspace {name}")
                        lock.__enter__()
                    job.result = await work(job)
                    job.status = "done"
                except Exception as exc:  # noqa: BLE001 - a failed job is reported, never crashes the API
                    job.status, job.error = "failed", f"{exc}"
                    job.log.append("FAILED: " + "".join(traceback.format_exception_only(exc)).strip())
                finally:
                    if lock is not None:
                        lock.__exit__(None, None, None)
        asyncio.get_running_loop().create_task(run())
        return job


# --- serialisation helpers ---------------------------------------------------------------------------

def _entity_row(rt: Runtime, e, group_key: str, quarantined: set[str]) -> dict[str, Any]:
    rec, facts = e.data.get("record", {}), e.data.get("facts", {})
    return {"id": e.id, "ref": e.external_ref, "state": e.state, "group": str(rec.get(group_key, "")),
            "customer": rec.get("customer_name"), "balance": int(e.data.get("balance", e.value)), "value": e.value,
            "days_overdue": rec.get("days_overdue"), "due_date": rec.get("due_date"),
            "promise_date": facts.get("promise_date"), "promise_amount": facts.get("promise_amount"),
            "last_contact": (e.data.get("last_contact") or {}).get("channel"),
            "quarantined": e.id in quarantined, "escalation_reason": facts.get("escalation_reason")}


def describe(e: Event, refs: dict[str, str]) -> str:
    """One human line per event for activity feeds and timelines."""
    p, ref = e.payload, refs.get(e.entity_id or "", "")
    t = e.type
    if t == "entity.transitioned":
        return f"{ref}: {p['from']} → {p['to']}"
    if t == "entity.transition_refused":
        return f"{ref}: {p['from']} → {p['to']} refused ({p.get('reason')})"
    if t == "message.classified":
        return f"{ref}: reply read as {p['intent']} ({p['confidence']:.2f})" + (f" — “{p['quote']}”" if p.get("quote") else "")
    if t == "settlement.matched":
        return f"{ref}: bank line {p['record_id']} matched ({format_money(p['amount'])}, {p['kind']})"
    if t == "verification.completed":
        return f"{ref or 'mission'}: verification {'passed' if p.get('passed') else 'FAILED'} ({p.get('subject', '')})"
    if t == "mission.group_contacted":
        return f"{p['group']}: contacted {', '.join(p.get('refs', []))} by {p.get('channel') or '—'}" \
            + ("" if p.get("ok") else " — FAILED")
    if t.startswith("approval."):
        return f"approval {p.get('approval_id', '')}: {t.split('.')[1]} — {p.get('title', '')}"
    if t == "security.injection_suspected":
        return f"{ref or p.get('source', '')}: QUARANTINED suspected prompt injection"
    if t == "security.fraud_suspected":
        return f"{ref}: SUSPECTED FRAUD — bank-detail change request"
    if t in {"fallback.used", "statement.read", "fallback.exhausted"}:
        return f"bank statement via {p.get('rung') or 'NO ROUTE'}" + (f" after {len(p.get('attempts', []))} failed route(s)"
                                                                     if p.get("attempts") else "")
    if t == "mission.transitioned":
        return f"mission {p['from']} → {p['to']}" + (f" ({p['reason']})" if p.get("reason") else "")
    if t == "mission.reviewed":
        return f"mission review: {p.get('decision', {}).get('assessment', '')}"
    if t == "mission.chat":
        return f"chat: {p.get('question')}"
    if t.startswith("tool."):
        return f"{p.get('tool')}: {t.split('.')[1]}" + (f" — {p['error']}" if p.get("error") else "")
    if t.startswith("policy."):
        return f"policy {t.split('.')[1]}: {p.get('tool')} — {p.get('reason')}"
    return t


def _event_json(e: Event, refs: dict[str, str]) -> dict[str, Any]:
    return {"seq": e.seq, "type": e.type, "at": e.occurred_at.isoformat(), "mission_id": e.mission_id,
            "entity_id": e.entity_id, "ref": refs.get(e.entity_id or ""), "text": describe(e, refs)}


# --- request bodies ----------------------------------------------------------------------------------

class BriefIn(BaseModel):
    brief: str = Field(min_length=5, max_length=2000)


class MemoryDecisionIn(BaseModel):
    status: Literal["pinned", "rejected", "active"]
    by: str = Field(min_length=2, max_length=80)


class DecisionIn(BaseModel):
    approve: bool
    by: str = Field(min_length=2, max_length=80)
    note: str = Field("", max_length=2000)


class ChatIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    by: str = "manager"


class ResumeIn(BaseModel):
    by: str = Field(min_length=2, max_length=80)
    extra_tokens: int = Field(0, ge=0)


# --- app -----------------------------------------------------------------------------------------------

def create_app(rt_factory: Callable[[], Runtime] | None = None, sim_factory: Callable[[], Any] | None = None,
               lock_dir: Path | None = None, serve_ui: bool = True, ui_dir: Path = UI_DIST,
               allowed_hosts: frozenset[str] = LOCAL_HOSTS) -> FastAPI:
    holder: dict[str, Workspace] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = (rt_factory or build_runtime)()
        holder["ws"] = Workspace(rt, sim_factory, lock_dir)
        yield
        await rt.close()

    app = FastAPI(title="Arya workspace", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if _hostname(request.headers.get("host", "")) not in allowed_hosts:
            return JSONResponse({"detail": "unknown host"}, status_code=403)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if request.headers.get(ACTION_HEADER) != "1" or (origin and _hostname(origin) not in allowed_hosts):
                return JSONResponse({"detail": f"actions need the {ACTION_HEADER} header from the workspace"},
                                    status_code=403)
        return await call_next(request)

    def ws() -> Workspace:
        return holder["ws"]

    def runner(mission_id: str) -> MissionRunner:
        try:
            MissionStore(ws().rt.db, ws().rt.events, ws().rt.clock).get(mission_id)
        except KeyError:
            raise HTTPException(404, f"no mission {mission_id}")
        return MissionRunner(ws().rt, mission_id)

    def refs() -> dict[str, str]:
        with ws().rt.db.read() as conn:
            return {r["id"]: r["external_ref"] for r in conn.execute("SELECT id, external_ref FROM entities")}

    def quarantined() -> set[str]:
        with ws().rt.db.read() as conn:
            return {r["subject"].split(":", 1)[1] for r in conn.execute(
                "SELECT subject FROM quarantine WHERE cleared_at IS NULL AND subject LIKE 'entity:%'")}

    # --- reads --------------------------------------------------------------------------------------

    @app.get("/api/status")
    def status():
        rt = ws().rt
        store = MissionStore(rt.db, rt.events, rt.clock)
        missions = store.list()
        job = ws().job
        return {"now": rt.clock.now().isoformat(), "company": rt.company_cfg.get("display_name"),
                "employee": "Arya", "role": role_cfg(rt).get("title"),
                "missions": [{"id": m.id, "brief": m.brief, "status": m.status} for m in missions],
                "pending_approvals": len(rt.approvals.list(status="PENDING")),
                "job": job.model_dump() if job else None, "sandbox_sim": ws().sim_factory is not None}

    @app.get("/api/missions/{mission_id}")
    def mission(mission_id: str):
        r = runner(mission_id)
        m = r.mission
        prog = r.progress() if m.spec else None
        b = r.budget
        data = {k: v for k, v in m.data.items() if k in {"excluded", "groups", "scope_value", "first_batch",
                                                         "contacted_groups", "activated_on", "approved_by", "chat",
                                                         "actions", "prepaid"}}
        return {"id": m.id, "brief": m.brief, "status": m.status, "created_at": m.created_at,
                "spec": m.spec.model_dump(mode="json") if m.spec else None,
                "plan": m.plan.model_dump(mode="json") if m.plan else None, "selection": m.selection, "data": data,
                "progress": prog.as_dict() if prog else None, "plan_text": plan_text(m, format_money),
                "budget": {"used": b.used, "max": b.max_tokens}}

    @app.get("/api/missions/{mission_id}/board")
    def board(mission_id: str):
        r = runner(mission_id)
        rt = ws().rt
        group_key = role_cfg(rt)["mission"]["selection"]["group_key"]
        q = quarantined()
        return {"states": rt.pipeline.spec.states, "terminal": rt.pipeline.spec.terminal,
                "entities": [_entity_row(rt, e, group_key, q) for e in rt.pipeline.list(r.mission_id)]}

    @app.get("/api/entities/{entity_id}")
    async def entity(entity_id: str):
        rt = ws().rt
        try:
            e = rt.pipeline.get(entity_id)
        except KeyError:
            raise HTTPException(404, f"no entity {entity_id}")
        group_key = role_cfg(rt)["mission"]["selection"]["group_key"]
        names = refs()
        timeline = [_event_json(ev, names) for ev in rt.events.query(entity_id=entity_id)
                    if not ev.type.startswith("tool.")]
        thread = await _thread(rt, e)
        return {**_entity_row(rt, e, group_key, quarantined()), "mission_id": e.mission_id,
                "record": e.data.get("record", {}), "contact": e.data.get("contact", {}),
                "facts": e.data.get("facts", {}), "documents": e.data.get("documents", []),
                "timeline": timeline, "evidence": rt.evidence.for_entity(entity_id), "thread": thread}

    @app.get("/api/approvals")
    def approvals(status: str | None = "PENDING", mission_id: str | None = None):
        items = ws().rt.approvals.list(status=status or None, mission_id=mission_id)
        names = refs()
        return [{**a.model_dump(mode="json"), "ref": names.get(a.entity_id or "")} for a in reversed(items)]

    @app.get("/api/events")
    def events(mission_id: str | None = None, since: int = 0, limit: int = Query(200, ge=1, le=5000)):
        names = refs()
        evs = ws().rt.events.latest(limit, mission_id=mission_id, since_seq=since, exclude_prefix="tool.")
        return [_event_json(e, names) for e in evs]

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = ws().jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "no such job")
        return j.model_dump()

    @app.get("/api/stream")
    async def stream(request: Request, since: int = 0):
        """Server-sent events: `event` for each new audit-log entry, `job` when the current job changes."""
        async def gen():
            seq, last_job = since, None
            while not await request.is_disconnected():
                names = refs()
                for e in ws().rt.events.query(since_seq=seq, limit=500):
                    seq = e.seq
                    if not e.type.startswith("tool."):
                        yield f"event: event\ndata: {json.dumps(_event_json(e, names), default=str)}\n\n"
                j = ws().job
                snap = (j.id, j.status, len(j.log)) if j else None
                if snap != last_job and j:
                    last_job = snap
                    yield f"event: job\ndata: {j.model_dump_json()}\n\n"
                yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --- actions (background jobs) -------------------------------------------------------------------------

    def log_from(job: Job, r: MissionRunner, start: int = 0) -> int:
        job.log += r.log[start:]
        return len(r.log)

    @app.post("/api/missions", status_code=202)
    async def create_mission(body: BriefIn):
        async def work(job: Job):
            r = MissionRunner(ws().rt)
            job.log.append("Arya is reading the brief and the credit policy…")
            try:
                await r.create(body.brief)
            finally:
                log_from(job, r)
            return {"mission_id": r.mission_id}
        return ws().start("create mission", work).model_dump()

    @app.post("/api/approvals/{approval_id}", status_code=202)
    async def decide(approval_id: str, body: DecisionIn):
        try:
            a = ws().rt.approvals.get(approval_id)
        except ApprovalError:
            raise HTTPException(404, f"no approval {approval_id}")
        if a.status != "PENDING":
            raise HTTPException(409, f"approval is {a.status}, not pending")

        async def work(job: Job):
            lines = await decide_approval(ws().rt, approval_id, approve=body.approve, by=body.by, note=body.note)
            job.log += lines
            return {"approval_id": approval_id}
        return ws().start(f"{'approve' if body.approve else 'reject'} {approval_id}", work).model_dump()

    @app.post("/api/missions/{mission_id}/tick", status_code=202)
    async def tick(mission_id: str):
        r = runner(mission_id)

        async def work(job: Job):
            try:
                return await r.tick()
            finally:
                log_from(job, r)
        return ws().start("tick", work).model_dump()

    @app.post("/api/missions/{mission_id}/day", status_code=202)
    async def simulate_day(mission_id: str):
        """Sandbox demo: customers act, Arya works, customers answer, Arya works again, the clock moves a day."""
        if ws().sim_factory is None:
            raise HTTPException(400, "no customer simulator configured")
        r = runner(mission_id)

        async def work(job: Job):
            from sandbox.common import clock as sandbox_clock
            sim = ws().sim_factory()
            try:
                for label in ("overnight", "Arya works", "customers reply", "Arya follows up"):
                    if label in {"overnight", "customers reply"}:
                        world = await sim.tick()
                        job.log += [f"[world] {x['customer']} replied ({x['decision']}) about {x['invoices']}"
                                    for x in world["replies"]]
                        job.log += [f"[world] {p['customer']} paid {format_money(p['amount_paise'])} UTR {p['utr']}"
                                    for p in world["payments"]]
                    else:
                        start = len(r.log)
                        result = await r.tick()
                        log_from(job, r, start)
                        if result["status"] != "ACTIVE":
                            return result
                sandbox_clock.advance(timedelta(days=1))
                job.log.append(f"== {ws().rt.clock.now():%a %d %b %Y %H:%M}")
                return {"status": r.mission.status}
            finally:
                await sim.close()
        return ws().start("simulate a day", work).model_dump()

    @app.post("/api/missions/{mission_id}/resume", status_code=202)
    async def resume(mission_id: str, body: ResumeIn):
        r = runner(mission_id)

        async def work(job: Job):
            r.resume(by=body.by, extra_tokens=body.extra_tokens)
            log_from(job, r)
        return ws().start("resume", work).model_dump()

    @app.post("/api/missions/{mission_id}/learn", status_code=202)
    async def learn(mission_id: str):
        r = runner(mission_id)

        async def work(job: Job):
            out = await r.learn()
            log_from(job, r)
            return {"facts": len(out.get("facts", [])), "lessons": len(out.get("lessons", []))}
        return ws().start("learn from the mission", work).model_dump()

    @app.get("/api/memory")
    def memory(scope: str | None = None, all: bool = False):
        items = ws().rt.memory.list(scope_prefix=scope, statuses=None if all else ("pinned", "active"))
        return [m.as_dict() for m in items]

    @app.post("/api/memory/{memory_id}")
    def decide_memory(memory_id: str, body: MemoryDecisionIn):
        try:
            return ws().rt.memory.decide(memory_id, body.status, by=body.by).as_dict()
        except KeyError:
            raise HTTPException(404, f"no memory {memory_id}")

    @app.post("/api/missions/{mission_id}/chat")
    async def chat(mission_id: str, body: ChatIn):
        r = runner(mission_id)
        if r.mission.spec is None:
            raise HTTPException(409, "the mission has no spec yet")
        return await r.chat(body.question, by=body.by)

    # --- UI -------------------------------------------------------------------------------------------------

    if serve_ui and ui_dir.exists():
        root = ui_dir.resolve()
        if (root / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            # `path` arrives URL-decoded, so "%2e%2e/" is "../": serve only files inside the build folder
            target = (root / path).resolve()
            inside = target == root or target.is_relative_to(root)
            return FileResponse(target if path and inside and target.is_file() else root / "index.html")

    return app


async def _thread(rt: Runtime, e) -> list[dict[str, Any]]:
    """The real conversation with the customer, read from the message provider."""
    last = e.data.get("facts", {}).get("last_outreach") or e.data.get("last_contact") or {}
    if not last.get("thread_id"):
        return []
    channel = last.get("channel", "email")
    res = await rt.registry.invoke(f"{channel}.thread", {"thread_id": last["thread_id"]},
                                   RunContext(mission_id=e.mission_id, task_id=None, allowed_apps={channel},
                                              artifacts_dir=rt.artifacts_dir))
    if not res.ok:
        return []
    ch = rt.company_cfg.get("channels", {})
    ours = {_addr(ch.get("email_from")), _addr(ch.get("whatsapp_from"))}
    return [{**{k: m.get(k) for k in ("id", "from_addr", "to_addr", "subject", "body", "sent_at", "status", "channel",
                                      "attachments")}, "outbound": _addr(m.get("from_addr")) in ours}
            for m in res.output["messages"]]


def _addr(a: str | None) -> str:
    a = (a or "").strip().lower()
    return "+" + "".join(c for c in a if c.isdigit()) if a and not "@" in a else a


def _sim_factory():
    from sandbox.common.world import load_world
    from sandbox.customer_sim.__main__ import _voice
    from sandbox.customer_sim.engine import SimEngine
    return SimEngine(load_world(), "http://127.0.0.1:8103", "http://127.0.0.1:8102",
                     voice=_voice(os.environ.get("SIM_LLM") == "1"))


def main() -> None:
    import uvicorn

    from sandbox.common import config
    app = create_app(sim_factory=_sim_factory, lock_dir=config.var_dir())
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WORKSPACE_PORT", 8100)), log_level="info")


if __name__ == "__main__":
    main()
