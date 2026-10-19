"""One Tool interface over every app, and the registry that runs tools safely.

Every tool call goes through ToolRegistry.invoke, which enforces, in order:
  1. the tool exists and the task's capability scope includes its app
  2. arguments validate against the tool's input schema
  3. idempotency: a key that already succeeded returns the stored result and the
     tool is NOT run again (a replay changes nothing, so it needs no policy decision)
  4. the Policy Gate allows it (core.policy; `ask` holds the call for a human, `defer` means not now);
     non-idempotent tools need a key
  5. L0 retry with exponential backoff + jitter, for transient errors only, and only for calls that are
     idempotent or deduped by the remote system (a timed-out send may have gone out)
  6. the tool's own verify() check
  7. secret scrubbing, then an event for the audit trail

Safety by capability (ADR-6): a tool that moves money cannot be registered.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError

from core.clock import Clock, SystemClock
from core.events import EventStore
from core.models import RISK_ORDER, Event, Risk, ToolKind, ToolResult
from core.policy import PolicyDecision  # noqa: F401  (re-exported for tool code)
from core.store import Database
from tools.vault import Vault

KIND_PREFERENCE: dict[str, int] = {"api": 0, "channel": 0, "file": 0, "dom": 1, "vision": 2}


# --- errors -------------------------------------------------------------------

class ToolError(Exception):
    kind: Literal["transient", "permanent", "denied", "invalid_args"] = "permanent"


class TransientToolError(ToolError):
    """Timeouts, 5xx, rate limits, a page still loading. Safe to retry."""
    kind = "transient"


class PermanentToolError(ToolError):
    """Will fail the same way again. Do not retry; adapt or escalate."""
    kind = "permanent"


class ToolDenied(ToolError):
    kind = "denied"


class RegistrationError(Exception):
    pass


# --- tool contract -----------------------------------------------------------------

@dataclass
class RunContext:
    mission_id: str | None
    task_id: str | None
    allowed_apps: set[str]                      # capability scope of the current task
    entity_id: str | None = None
    idempotency_key: str | None = None          # required for non-idempotent tools: mission:entity:step
    counterparty: str | None = None             # who is being reached (e.g. a customer id): contact caps count per counterparty
    artifacts_dir: Path = field(default_factory=lambda: Path("var/runtime/artifacts"))
    # writes this task may make on apps with scoped writes: {app: [{"method", "path", "form": {field: value}}]}
    write_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # documents an outgoing message may carry besides this task's own files (paths as the drive tools
    # name them); None = only the task's own files. Keeps one counterparty's documents from reaching another.
    attachable: list[str] | None = None

    def task_dir(self) -> Path:
        path = self.artifacts_dir / (self.task_id or "adhoc")
        path.mkdir(parents=True, exist_ok=True)
        return path


class Tool(ABC):
    name: ClassVar[str]                 # dotted, e.g. "browser.click", "email.send"
    app: ClassVar[str]                  # app it touches; capability scope is per app
    risk: ClassVar[Risk]
    kind: ClassVar[ToolKind]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    idempotent: ClassVar[bool] = True   # True = repeating it has no extra effect
    dedupes_downstream: ClassVar[bool] = False  # passes the idempotency key to the remote system
    moves_money: ClassVar[bool] = False
    timeout_seconds: ClassVar[float] = 60.0
    target_field: ClassVar[str | None] = None   # argument naming the person/system reached (contact caps)
    content_fields: ClassVar[tuple[str, ...]] = ()  # free-text arguments the Policy Gate reads

    @abstractmethod
    async def run(self, args: BaseModel, ctx: RunContext) -> ToolResult: ...

    async def verify(self, args: BaseModel, result: ToolResult, ctx: RunContext) -> bool | None:
        return None


# --- policy hook ----------------------------------------------------------------------

PolicyCheck = Callable[[Tool, BaseModel, RunContext], Awaitable[PolicyDecision]]
AfterSuccess = Callable[[Tool, BaseModel, RunContext, ToolResult], None]


async def allow_all(tool: Tool, args: BaseModel, ctx: RunContext) -> PolicyDecision:
    return PolicyDecision("allow", "no policy gate configured")


# --- registry ---------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    idempotency_key TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STARTED', 'SUCCEEDED', 'FAILED')),
    result_json TEXT,
    mission_id TEXT,
    task_id TEXT,
    updated_at TEXT NOT NULL
);
"""


def _args_hash(tool: str, args: BaseModel) -> str:
    return hashlib.sha256(f"{tool}:{args.model_dump_json()}".encode()).hexdigest()


def spec_name(name: str) -> str:
    """OpenAI-style function names (Groq) allow [a-zA-Z0-9_-]; dots become double underscores."""
    return name.replace(".", "__")


class ToolRegistry:
    def __init__(self, db: Database, events: EventStore, *, vault: Vault | None = None,
                 policy: PolicyCheck = allow_all, clock: Clock | None = None, max_attempts: int = 3,
                 backoff_base: float = 0.5, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 after_success: list[AfterSuccess] | None = None):
        self.db, self.events = db, events
        self.vault = vault or Vault()
        self.policy = policy
        self.after_success = after_success or []
        self.clock = clock or SystemClock()
        self.max_attempts, self.backoff_base, self._sleep = max_attempts, backoff_base, sleep
        self._tools: dict[str, Tool] = {}
        db.ensure_schema(LEDGER_SCHEMA)

    # registration -----------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.moves_money:
            raise RegistrationError(f"{tool.name}: money-moving tools are never registered (ADR-6)")
        if tool.name in self._tools or spec_name(tool.name) in {spec_name(n) for n in self._tools}:
            raise RegistrationError(f"duplicate tool name {tool.name}")
        if tool.risk not in RISK_ORDER:
            raise RegistrationError(f"{tool.name}: unknown risk tier {tool.risk}")
        if not tool.idempotent and tool.risk == "read":
            raise RegistrationError(f"{tool.name}: a read tool must be idempotent")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name) or next((t for t in self._tools.values() if spec_name(t.name) == name), None)

    def available(self, apps: set[str]) -> list[Tool]:
        """Tools within a capability scope, preferred kind first (API -> DOM -> vision)."""
        tools = [t for t in self._tools.values() if t.app in apps]
        return sorted(tools, key=lambda t: (KIND_PREFERENCE[t.kind], t.name))

    def specs(self, apps: set[str]) -> list[dict[str, Any]]:
        """Function-calling definitions (OpenAI-compatible, as served by Groq), limited to the scope."""
        return [{"type": "function", "function": {
                    "name": spec_name(t.name),
                    "description": f"[{t.app}, risk={t.risk}] {t.description}",
                    "parameters": t.input_schema.model_json_schema()}}
                for t in self.available(apps)]

    # invocation ------------------------------------------------------------------------

    async def invoke(self, name: str, raw_args: dict[str, Any], ctx: RunContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return self._fail(name, ctx, "denied", f"unknown tool {name}")
        if tool.app not in ctx.allowed_apps:
            return self._fail(tool.name, ctx, "denied", f"app '{tool.app}' is outside this task's scope")
        try:
            args = tool.input_schema.model_validate(raw_args)
        except ValidationError as exc:
            return self._fail(tool.name, ctx, "invalid_args", str(exc))

        if not tool.idempotent and ctx.idempotency_key:
            done = self._ledger_succeeded(tool, args, ctx.idempotency_key)
            if done is not None:            # already happened: replaying sends nothing, so no policy question
                return done

        decision = await self.policy(tool, args, ctx)
        if decision.action != "allow":
            detail = {"policy_action": decision.action, "rule": decision.rule, "approval_id": decision.approval_id,
                      "retry_at": decision.retry_at.isoformat() if decision.retry_at else None}
            return self._fail(tool.name, ctx, "denied", f"policy {decision.action}: {decision.reason}", detail)

        key = ctx.idempotency_key
        if not tool.idempotent:
            if not key:
                return self._fail(tool.name, ctx, "denied", "non-idempotent tool requires an idempotency key")
            prior = self._ledger_claim(tool, args, ctx)
            if isinstance(prior, ToolResult):
                return prior

        result = await self._run_with_retry(tool, args, ctx)
        if not tool.idempotent and not tool.dedupes_downstream and result.error_kind == "transient":
            # A timeout or 5xx after a send/submit may have happened anyway; the remote side cannot dedupe,
            # so neither repeat it now nor let a later call repeat it blindly (same as a crash mid-call).
            result = result.model_copy(update={"error_kind": "permanent", "error": (
                f"outcome unknown after a transient error ({result.error}); reconcile before retrying")})
            self._record(tool, ctx, result)
            return result
        if result.ok:
            try:
                result.verified = await tool.verify(args, result, ctx)
            except ToolError as exc:
                result.verified, result.error = False, f"verify failed: {exc}"
        result = ToolResult.model_validate(self.vault.scrub(result.model_dump()))

        if not tool.idempotent:
            self._ledger_finish(key, result)
        self._record(tool, ctx, result)
        if result.ok and not result.replayed:
            for hook in self.after_success:
                hook(tool, args, ctx, result)
        return result

    async def _run_with_retry(self, tool: Tool, args: BaseModel, ctx: RunContext) -> ToolResult:
        """L0 retry. A side effect the remote system cannot dedupe gets one attempt only."""
        last: ToolResult | None = None
        attempts = self.max_attempts if tool.idempotent or tool.dedupes_downstream else 1
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_seconds)
                result.attempts = attempt
                return result
            except (TransientToolError, TimeoutError) as exc:
                last = ToolResult(ok=False, error=str(exc) or type(exc).__name__, error_kind="transient",
                                  attempts=attempt)
                if attempt < attempts:
                    delay = self.backoff_base * 2 ** (attempt - 1)
                    await self._sleep(delay + random.uniform(0, delay / 2))
            except ToolError as exc:
                return ToolResult(ok=False, error=str(exc), error_kind=exc.kind, attempts=attempt)
            except Exception as exc:  # noqa: BLE001 - a tool bug must not leave the ledger STARTED
                return ToolResult(ok=False, error=f"tool crashed: {exc!r}", error_kind="permanent", attempts=attempt)
        assert last is not None
        return last

    # idempotency ledger -----------------------------------------------------------------

    def _ledger_succeeded(self, tool: Tool, args: BaseModel, key: str) -> ToolResult | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM tool_calls WHERE idempotency_key = ?", (key,)).fetchone()
        if row and row["status"] == "SUCCEEDED" and row["tool"] == tool.name \
                and row["args_hash"] == _args_hash(tool.name, args):
            return ToolResult.model_validate_json(row["result_json"]).model_copy(update={"replayed": True})
        return None

    def _ledger_claim(self, tool: Tool, args: BaseModel, ctx: RunContext) -> ToolResult | None:
        """Claim the key. Returns a stored/blocking ToolResult if this call must not run."""
        key, digest = ctx.idempotency_key, _args_hash(tool.name, args)
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tool_calls WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None or row["status"] == "FAILED":
                conn.execute("INSERT OR REPLACE INTO tool_calls VALUES (?,?,?, 'STARTED', NULL, ?,?,?)",
                             (key, tool.name, digest, ctx.mission_id, ctx.task_id, now))
                return None
            if row["tool"] != tool.name or row["args_hash"] != digest:
                return ToolResult(ok=False, error_kind="denied",
                                  error=f"idempotency key {key} was already used for a different call")
            if row["status"] == "SUCCEEDED":
                stored = ToolResult.model_validate_json(row["result_json"])
                return stored.model_copy(update={"replayed": True})
            # STARTED: a previous attempt never finished (crash). Only safe to re-run if the
            # remote system dedupes on the same key; otherwise the outcome is unknown.
            if tool.dedupes_downstream:
                conn.execute("UPDATE tool_calls SET updated_at = ? WHERE idempotency_key = ?", (now, key))
                return None
            return ToolResult(ok=False, error_kind="permanent",
                              error=f"previous call with key {key} has unknown outcome; reconcile before retrying")

    def _ledger_finish(self, key: str | None, result: ToolResult) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE tool_calls SET status = ?, result_json = ?, updated_at = ? WHERE idempotency_key = ?",
                         ("SUCCEEDED" if result.ok else "FAILED", result.model_dump_json(),
                          datetime.now(timezone.utc).isoformat(), key))

    # audit ---------------------------------------------------------------------------------

    def _record(self, tool: Tool, ctx: RunContext, result: ToolResult) -> None:
        self.events.append(Event(
            type="tool.succeeded" if result.ok else "tool.failed",
            mission_id=ctx.mission_id, entity_id=ctx.entity_id, task_id=ctx.task_id,
            occurred_at=self.clock.now(),
            payload={"tool": tool.name, "app": tool.app, "risk": tool.risk, "attempts": result.attempts,
                     "replayed": result.replayed, "verified": result.verified, "error": result.error,
                     "error_kind": result.error_kind, "idempotency_key": ctx.idempotency_key,
                     "evidence": [e.model_dump() for e in result.evidence]},
        ))

    def _fail(self, name: str, ctx: RunContext, kind: str, error: str,
              policy: dict[str, Any] | None = None) -> ToolResult:
        result = ToolResult(ok=False, error=error, error_kind=kind, attempts=0,  # type: ignore[arg-type]
                            output={"policy": policy} if policy else None)
        self.events.append(Event(type="tool.rejected", mission_id=ctx.mission_id, entity_id=ctx.entity_id,
                                 task_id=ctx.task_id, occurred_at=self.clock.now(),
                                 payload={"tool": name, "error_kind": kind, "error": error, **(policy or {})}))
        return result
