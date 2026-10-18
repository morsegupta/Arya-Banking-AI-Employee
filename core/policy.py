"""Policy Gate: deterministic checks on every tool call, from the company's policy file (no LLM).

Order, first match wins:
  1. capability   tools an app may use (allow-list), risk tiers that are never allowed,
                  writes on an app that needs a per-task grant when the task has none      -> deny
  2. content      phrases the company never says (threats, ...)                            -> deny
  3. timing       quiet hours, weekdays only                                               -> defer
  4. volume       at most N contacts per person per window                                 -> defer
  5. a human already approved this exact call                                              -> allow
  6. quarantine   the entity or task touched content flagged as an injection attempt       -> ask
  7. concessions  phrases that commit the company (discounts, instalments...)              -> ask
                  unless a human decision granted them for this entity
  8. otherwise                                                                             -> allow

`ask` files an approval holding the exact call (Approval Broker); approving it runs that call.
`defer` says when it may run. A refusal never reaches the outside world.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from core.approvals import ApprovalBroker
from core.clock import Clock
from core.events import EventStore
from core.models import RISK_ORDER, Event
from core.store import Database


@dataclass
class PolicyDecision:
    action: Literal["allow", "deny", "ask", "defer"]
    reason: str = ""
    rule: str = ""
    approval_id: str | None = None      # for "ask": the approval that holds this call
    retry_at: datetime | None = None    # for "defer": the earliest time it may run


SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    at TEXT NOT NULL,
    tool TEXT NOT NULL,
    mission_id TEXT,
    entity_id TEXT
);
CREATE INDEX IF NOT EXISTS policy_contacts_target ON policy_contacts (target, at);
CREATE TABLE IF NOT EXISTS quarantine (
    subject TEXT PRIMARY KEY,           -- "entity:<id>" or "task:<id>"
    reason TEXT NOT NULL,
    score REAL,
    at TEXT NOT NULL,
    cleared_by TEXT,
    cleared_at TEXT
);
"""


def phrase_hits(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text, re.I)]


def _norm_target(value: Any) -> str:
    v = str(value or "").strip().lower()
    return re.sub(r"[^\d+]", "", v) if re.fullmatch(r"[\d\s()+-]+", v) else v


def args_digest(tool_name: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()[:16]


class PolicyGate:
    def __init__(self, config: dict[str, Any], db: Database, events: EventStore, clock: Clock,
                 broker: ApprovalBroker, *, tz: str = "UTC"):
        self.cfg, self.db, self.events, self.clock, self.broker = config or {}, db, events, clock, broker
        self.tz = ZoneInfo(self.cfg.get("timezone", tz))
        db.ensure_schema(SCHEMA)

    # --- the check ---------------------------------------------------------------------------

    async def check(self, tool: Any, args: Any, ctx: Any) -> PolicyDecision:
        decision = self.decide(tool, args.model_dump() if hasattr(args, "model_dump") else dict(args), ctx)
        if decision.action != "allow":
            self.events.append(Event(type=f"policy.{decision.action}", mission_id=ctx.mission_id,
                                     entity_id=ctx.entity_id, task_id=ctx.task_id, occurred_at=self.clock.now(),
                                     payload={"tool": tool.name, "rule": decision.rule, "reason": decision.reason,
                                              "approval_id": decision.approval_id,
                                              "retry_at": decision.retry_at.isoformat() if decision.retry_at else None}))
        return decision

    def decide(self, tool: Any, args: dict[str, Any], ctx: Any) -> PolicyDecision:
        tools_cfg = self.cfg.get("tools", {})
        risk = getattr(tool, "risk", "read")

        # 1. capability
        app_rule = tools_cfg.get("apps", {}).get(tool.app)
        if app_rule and "allow" in app_rule and tool.name not in app_rule["allow"]:
            return PolicyDecision("deny", f"{tool.name} is not an allowed action on {tool.app}", "apps.allow")
        if tools_cfg.get("by_risk", {}).get(risk) == "deny":
            return PolicyDecision("deny", f"risk tier {risk} is never allowed", "tools.by_risk")
        if (app_rule or {}).get("writes_need_scope") and RISK_ORDER.get(risk, 0) >= RISK_ORDER["write"] \
                and not (getattr(ctx, "write_scope", None) or {}).get(tool.app):
            return PolicyDecision("deny", f"this task was not granted any write on {tool.app}", "apps.writes_need_scope")

        comm = self.cfg.get("external_comm", {}) if risk == "external_comm" else {}
        text = " ".join(str(args.get(f) or "") for f in getattr(tool, "content_fields", ()) or ())

        # 2. content
        if comm and (hits := phrase_hits(text, comm.get("deny_phrases", []))):
            return PolicyDecision("deny", f"message contains forbidden wording {hits}", "external_comm.deny_phrases")

        # 3. timing
        if comm:
            when = self.next_allowed(self.clock.now(), comm)
            if when is not None:
                return PolicyDecision("defer", f"outside contact hours; next window {when:%a %d %b %H:%M}",
                                      "external_comm.hours", retry_at=when)

        # 4. volume
        target_field = getattr(tool, "target_field", None)
        cap = comm.get("max_per_target") if comm else None
        if cap and target_field and args.get(target_field):
            target = _norm_target(args[target_field])
            recent = self._contacts(target, timedelta(days=cap["days"]))
            if len(recent) >= cap["count"]:
                retry = min(recent) + timedelta(days=cap["days"])
                return PolicyDecision("defer", f"{len(recent)} contacts with this person in the last {cap['days']} "
                                               f"days (max {cap['count']})", "external_comm.max_per_target",
                                      retry_at=retry)

        # 5. already approved by a human
        key = self.approval_key(tool, args, ctx)
        prior = self.broker.by_key(key)
        if prior and prior.status in {"APPROVED", "EXECUTED"}:
            return PolicyDecision("allow", f"approved by {prior.decided_by}", "approved", approval_id=prior.id)
        if prior and prior.status == "REJECTED":
            return PolicyDecision("deny", f"rejected by {prior.decided_by}: {prior.note or ''}", "rejected",
                                  approval_id=prior.id)

        # 6. quarantine
        q = self.cfg.get("quarantine", {})
        if RISK_ORDER.get(risk, 0) >= RISK_ORDER["write"] and risk in q.get("needs_approval", []):
            flagged = self.quarantined(ctx)
            if flagged:
                return self._ask(tool, args, ctx, key, f"touches quarantined content ({flagged})", "quarantine")

        # 7. concessions
        if comm and (hits := phrase_hits(text, comm.get("approval_phrases", []))):
            granted = self.granted_phrases(ctx.entity_id)
            missing = [h for h in hits if h.lower() not in granted]
            if missing:
                return self._ask(tool, args, ctx, key, f"commits the company: {missing}", "external_comm.approval_phrases")
        return PolicyDecision("allow")

    # --- helpers ------------------------------------------------------------------------------

    def approval_key(self, tool: Any, args: dict[str, Any], ctx: Any) -> str:
        return f"tool:{tool.name}:{ctx.idempotency_key or '-'}:{args_digest(tool.name, args)}"

    def _ask(self, tool: Any, args: dict[str, Any], ctx: Any, key: str, reason: str, rule: str) -> PolicyDecision:
        a = self.broker.request(
            key=key, kind="tool_call", title=f"{tool.name}: {reason}", mission_id=ctx.mission_id,
            entity_id=ctx.entity_id,
            payload={"tool": tool.name, "app": tool.app, "args": args, "reason": reason, "rule": rule,
                     "context": {"mission_id": ctx.mission_id, "task_id": ctx.task_id, "entity_id": ctx.entity_id,
                                 "idempotency_key": ctx.idempotency_key}})
        return PolicyDecision("ask", f"{reason}; waiting for approval {a.id}", rule, approval_id=a.id)

    def next_allowed(self, now: datetime, comm: dict[str, Any]) -> datetime | None:
        """None if `now` is inside contact hours, else the next moment that is."""
        local = now.astimezone(self.tz)
        start, end = _hours(comm.get("quiet_hours"))
        weekdays = comm.get("weekdays_only", False)

        def ok(t: datetime) -> bool:
            if weekdays and t.weekday() >= 5:
                return False
            if start is None:
                return True
            tt = t.time()
            quiet = (tt >= start or tt < end) if start > end else (start <= tt < end)
            return not quiet

        if ok(local):
            return None
        probe = local
        for _ in range(8 * 24 * 4):           # 15-minute steps, at most 8 days
            probe = probe + timedelta(minutes=15)
            if end is not None:
                candidate = datetime.combine(probe.date(), end, tzinfo=self.tz)
                if candidate > local and ok(candidate) and candidate <= probe:
                    return candidate
            if ok(probe):
                return probe
        return None

    def _contacts(self, target: str, window: timedelta) -> list[datetime]:
        since = (self.clock.now() - window).isoformat()
        with self.db.read() as conn:
            return [datetime.fromisoformat(r["at"]) for r in conn.execute(
                "SELECT at FROM policy_contacts WHERE target = ? AND at > ?", (target, since))]

    def record(self, tool: Any, args: Any, ctx: Any, result: Any) -> None:
        """Registry hook after a successful call: count contacts with people."""
        if getattr(tool, "risk", "") != "external_comm" or not getattr(tool, "target_field", None):
            return
        target = (args.model_dump() if hasattr(args, "model_dump") else args).get(tool.target_field)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO policy_contacts (target, at, tool, mission_id, entity_id) VALUES (?,?,?,?,?)",
                         (_norm_target(target), self.clock.now().isoformat(), tool.name, ctx.mission_id,
                          ctx.entity_id))

    def contacts_this_window(self, target: str) -> int:
        cap = self.cfg.get("external_comm", {}).get("max_per_target", {"days": 7})
        return len(self._contacts(_norm_target(target), timedelta(days=cap["days"])))

    # --- quarantine -----------------------------------------------------------------------------

    def quarantine(self, subject: str, reason: str, score: float | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO quarantine (subject, reason, score, at) VALUES (?,?,?,?) "
                         "ON CONFLICT(subject) DO UPDATE SET reason = excluded.reason, score = excluded.score, "
                         "at = excluded.at, cleared_by = NULL, cleared_at = NULL",
                         (subject, reason, score, self.clock.now().isoformat()))

    def clear(self, subject: str, by: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE quarantine SET cleared_by = ?, cleared_at = ? WHERE subject = ?",
                         (by, self.clock.now().isoformat(), subject))

    def quarantined(self, ctx: Any) -> str | None:
        subjects = [s for s in (f"entity:{ctx.entity_id}" if ctx.entity_id else None,
                                f"task:{ctx.task_id}" if ctx.task_id else None) if s]
        if not subjects:
            return None
        with self.db.read() as conn:
            row = conn.execute(f"SELECT subject, reason FROM quarantine WHERE cleared_at IS NULL AND subject IN "
                               f"({','.join('?' * len(subjects))})", subjects).fetchone()
        return f"{row['subject']}: {row['reason']}" if row else None

    def granted_phrases(self, entity_id: str | None) -> set[str]:
        """Phrases a human decision allowed for this entity (e.g. an approved concession)."""
        if not entity_id:
            return set()
        out: set[str] = set()
        for a in self.broker.list(entity_id=entity_id, kind="decision"):
            # a human decided this matter; the answer (yes or no) may name it
            if a.status in {"APPROVED", "EXECUTED"} or (a.status == "REJECTED" and a.payload.get("grants_on_reject")):
                out |= {p.lower() for p in a.payload.get("grants_phrases", [])}
        return out


def _hours(spec: str | None) -> tuple[time | None, time | None]:
    """'20:00-09:30' -> quiet from 20:00 to 09:30."""
    if not spec:
        return None, None
    a, b = (time.fromisoformat(x.strip()) for x in spec.split("-"))
    return a, b
