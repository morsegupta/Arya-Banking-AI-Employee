"""Wire one AI employee: role pack + company pack + tools + state + models.

This is the only place that knows where packs, config and secrets live.
Everything below it (core/, agents/) receives what it needs as arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

from core.approvals import Approval, ApprovalBroker
from core.clock import Clock, FileClock, SystemClock
from core.events import EventBus, EventStore
from core.injection import InjectionGuard
from core.llm import Budget, LLMClient, PersistentBudget
from core.memory import MemoryStore
from core.pii import Tokenizer
from core.pipeline import Pipeline, load_pipeline
from core.policy import PolicyGate
from core.scheduler import Scheduler
from core.store import Database
from subagents.evidence import EvidenceCollector
from tools.app_map import load_app_maps
from tools.base import RunContext, ToolRegistry
from tools.browser import BrowserSessions, register_browser_tools
from tools.channels.hub import ChannelConfig, register_channel_tools
from tools.connectors.erp_rest import ErpConfig, register_erp_tools
from tools.files import FileRoots, register_file_tools
from tools.sheets import load_sheet_maps, register_sheet_tools
from tools.vault import Vault

ROOT = Path(__file__).resolve().parents[1]


def _url(name: str, default: str) -> str:
    return os.environ.get(f"APP_URL_{name.upper()}", default)


@dataclass
class Runtime:
    role: str
    company: str
    role_dir: Path
    company_dir: Path
    company_cfg: dict[str, Any]
    db: Database
    events: EventStore
    bus: EventBus
    clock: Clock
    scheduler: Scheduler
    vault: Vault
    registry: ToolRegistry
    sessions: BrowserSessions
    pipeline: Pipeline
    llm: LLMClient | None
    artifacts_dir: Path
    drive_dir: Path | None = None
    policies: dict[str, Any] = field(default_factory=dict)
    approvals: ApprovalBroker | None = None
    gate: PolicyGate | None = None
    guard: InjectionGuard | None = None          # inbound messages and documents
    obs_guard: InjectionGuard | None = None      # tool observations (pages, files) on every Operator step
    evidence: EvidenceCollector | None = None
    memory: MemoryStore | None = None          # long-lived: survives a reset of the working state
    _closers: list[Any] = field(default_factory=list)

    def mission_budget(self, mission_id: str | None) -> Budget | None:
        """The mission's token budget, kept in the database (role pack: mission.token_budget)."""
        if not mission_id:
            return None
        limit = (yaml.safe_load((self.role_dir / "role.yaml").read_text()).get("mission", {})
                 .get("token_budget", 500_000))
        return PersistentBudget(self.db, f"mission:{mission_id}", limit)

    def budget(self, mission_id: str | None, tokens: int) -> Budget:
        """A budget for one call site or task, charged to its mission's budget as well."""
        return Budget(tokens, parent=self.mission_budget(mission_id))

    def tokenizer(self) -> Tokenizer:
        """A fresh PII tokenizer (one per prompt/draft) with the company's patterns."""
        return Tokenizer(self.policies.get("pii", {}))

    async def close(self) -> None:
        await self.sessions.shutdown()
        for c in self._closers:
            await c.close()
        if self.llm:
            await self.llm.close()


def build_runtime(*, role: str = "collections", company: str = "sharma_distributors",
                  var_dir: Path | str | None = None, vault: Vault | None = None, clock: Clock | None = None,
                  llm: LLMClient | None = None, use_llm: bool = True,
                  hub_transport: httpx.AsyncBaseTransport | None = None,
                  erp_transport: httpx.AsyncBaseTransport | None = None) -> Runtime:
    var = Path(var_dir or os.environ.get("SANDBOX_VAR_DIR", ROOT / "var")).resolve()
    vault = vault or Vault.from_env_file(str(ROOT / ".env"))
    role_dir = ROOT / "packs" / "roles" / role
    company_dir = ROOT / "packs" / "companies" / company
    cfg = yaml.safe_load((company_dir / "company.yaml").read_text())

    db = Database(var / "runtime" / "runtime.db")
    events = EventStore(db)
    bus = EventBus(events)
    if clock is None:
        clock_file = var / "clock.json"   # shared simulated time, when running against the sandbox
        clock = FileClock(clock_file) if clock_file.exists() else SystemClock()
    scheduler = Scheduler(db, bus, clock)
    policies_file = company_dir / "policies.yaml"
    policies = yaml.safe_load(policies_file.read_text()) if policies_file.exists() else {}
    approvals = ApprovalBroker(db, events, clock)
    gate = PolicyGate(policies, db, events, clock, approvals, tz=cfg.get("timezone", "UTC"))
    registry = ToolRegistry(db, events, vault=vault, clock=clock, policy=gate.check, after_success=[gate.record])

    apps = load_app_maps(company_dir / "apps")
    sessions = BrowserSessions(apps, vault)
    register_browser_tools(registry, sessions)

    drive = Path(cfg["files"]["drive_root"])
    roots = FileRoots(drive if drive.is_absolute() else var / drive)
    register_file_tools(registry, roots)
    register_sheet_tools(registry, load_sheet_maps(company_dir / "sheets"), roots.drive_root)

    ch = dict(cfg["channels"])
    ch["hub_url"] = _url("hub", ch["hub_url"])
    hub = register_channel_tools(registry, ChannelConfig(**ch), vault, roots, transport=hub_transport)

    erp_cfg = dict(cfg["connectors"]["erp_api"])
    erp_cfg["base_url"] = _url("erp_api", erp_cfg["base_url"])
    erp = register_erp_tools(registry, ErpConfig(**erp_cfg), vault, transport=erp_transport)

    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    pipeline = Pipeline(load_pipeline(role_dir / "role.yaml"), db, events, scheduler, clock, tz)

    if llm is None and use_llm:
        def say(model: str, seconds: float, reason: str) -> None:
            print(f"  [llm] waiting {seconds:.0f}s for {model}: {reason[:160]}", flush=True)
        llm = LLMClient.from_config(ROOT / "config" / "models.yaml", vault, on_wait=say)

    inj = policies.get("injection", {})
    scorer = llm.guard_score if llm is not None else None
    on_error = inj.get("on_error", {})
    guard = InjectionGuard(scorer, db, threshold=inj.get("threshold", 0.5), patterns=inj.get("patterns", []),
                           on_error=on_error.get("messages", "flag"))
    obs_guard = InjectionGuard(scorer, db, threshold=inj.get("threshold", 0.5), patterns=inj.get("patterns", []),
                               on_error=on_error.get("observations", "patterns"))
    artifacts = var / "runtime" / "artifacts"

    async def run_held_call(a: Approval) -> dict[str, Any]:
        """An approved held tool call runs exactly as it was held: same tool, args and idempotency key."""
        p, c = a.payload, a.payload["context"]
        ctx = RunContext(mission_id=c.get("mission_id"), task_id=c.get("task_id"), entity_id=c.get("entity_id"),
                         allowed_apps={p["app"]}, idempotency_key=c.get("idempotency_key"), artifacts_dir=artifacts,
                         counterparty=c.get("counterparty"))
        res = await registry.invoke(p["tool"], p["args"], ctx)
        return {"ok": res.ok, "output": res.output, "error": res.error}
    approvals.register_executor("tool_call", run_held_call)

    return Runtime(role=role, company=company, role_dir=role_dir, company_dir=company_dir, company_cfg=cfg,
                   db=db, events=events, bus=bus, clock=clock, scheduler=scheduler, vault=vault,
                   registry=registry, sessions=sessions, pipeline=pipeline, llm=llm,
                   artifacts_dir=artifacts, drive_dir=roots.drive_root, policies=policies, approvals=approvals,
                   gate=gate, guard=guard, obs_guard=obs_guard, evidence=EvidenceCollector(db, clock),
                   memory=MemoryStore(Database(var / "memory" / "memory.db"), clock, events),
                   _closers=[hub, erp])
