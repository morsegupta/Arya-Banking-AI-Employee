"""Orchestrator for one mission: turns events into agent work and guarded transitions.

Wakes on:
- new inbound messages  -> Reply Classifier -> propose ACKNOWLEDGED / PROMISED / DISPUTED
- timers (scheduler)    -> promise reminder, promise check (-> BROKEN_PROMISE if no money)
- the daily bank sync   -> Operator downloads the statement -> settle (match, receipt, verify, PAID)

Agents propose; the pipeline's guards decide. Every step leaves events.
"""

from __future__ import annotations

from typing import Any

from apps.flows import conversation_facts, outreach, role_cfg
from apps.runtime import Runtime
from apps.settlement import Settled, download_statement, lookback, settle
from core.llm import Budget, LLMError
from core.models import Entity, Event
from subagents.reply_classifier import ReplyClassifier
from tools.base import RunContext

KV_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
SYSTEM_SENDERS = {"mailer-daemon@hub.example"}


class Orchestrator:
    def __init__(self, rt: Runtime, mission_id: str):
        self.rt, self.mission_id = rt, mission_id
        rt.db.ensure_schema(KV_SCHEMA)
        cfg = role_cfg(rt)["reply_classifier"]
        self.classifier = ReplyClassifier(rt.llm, cfg["intents"], minor_units=cfg.get("minor_units", 100))
        self.sync_requested = False
        self.log: list[str] = []
        rt.bus.subscribe("timer.", self.on_timer)

    # --- small state ----------------------------------------------------------------------

    def _kv(self, key: str, default: str = "") -> str:
        with self.rt.db.read() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (f"{self.mission_id}:{key}",)).fetchone()
        return row["value"] if row else default

    def _set_kv(self, key: str, value: str) -> None:
        with self.rt.db.transaction() as conn:
            conn.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (f"{self.mission_id}:{key}", value))

    def _say(self, text: str) -> None:
        self.log.append(f"[{self.rt.clock.now():%a %d %b %H:%M}] {text}")

    def _event(self, type_: str, entity: Entity | None = None, **payload) -> None:
        self.rt.events.append(Event(type=type_, mission_id=self.mission_id, entity_id=entity.id if entity else None,
                                    occurred_at=self.rt.clock.now(), payload=payload))

    # --- start ------------------------------------------------------------------------------

    async def start(self, external_ref: str):
        out = await outreach(self.rt, self.mission_id, external_ref)
        self._say(f"outreach {external_ref}: " + ("; ".join(out.attempts)))
        if out.transition:
            self._say(f"{external_ref}: {out.transition.from_state} -> {out.transition.to_state} "
                      f"{'ok' if out.transition.ok else out.transition.reason}")
        return out

    # --- inbox ------------------------------------------------------------------------------

    def _route(self, msg: dict) -> Entity | None:
        entities = self.rt.pipeline.list(self.mission_id)
        for e in entities:
            last = e.data.get("facts", {}).get("last_outreach", {})
            if last.get("thread_id") and last["thread_id"] == msg["thread_id"]:
                return e
        text = f"{msg.get('subject') or ''} {msg['body']}"
        hits = [e for e in entities if e.external_ref in text]
        return hits[0] if len(hits) == 1 else None

    async def process_inbox(self) -> list[dict[str, Any]]:
        handled = []
        for channel in ("email", "whatsapp"):
            since = int(self._kv(f"cursor:{channel}", "0"))
            ctx = RunContext(mission_id=self.mission_id, task_id=None, allowed_apps={channel},
                             artifacts_dir=self.rt.artifacts_dir)
            res = await self.rt.registry.invoke(f"{channel}.inbox", {"since_seq": since}, ctx)
            if not res.ok:
                self._say(f"{channel} inbox unavailable: {res.error}")
                continue
            for msg in res.output:
                self._set_kv(f"cursor:{channel}", str(msg["seq"]))
                if msg["from_addr"] in SYSTEM_SENDERS:
                    continue
                handled.append(await self.handle_message(msg))
        return handled

    async def handle_message(self, msg: dict) -> dict[str, Any]:
        entity = self._route(msg)
        if entity is None:
            self._event("message.unrouted", message_id=msg["id"], sender=msg["from_addr"])
            self._say(f"message {msg['id']} from {msg['from_addr']} not linked to an entity (needs a human)")
            return {"message": msg["id"], "entity": None}
        loaded_facts = {k: v for k, v in conversation_facts(self.rt, _Loaded(entity)).items()
                        if k in {"number", "customer_name", "balance_paise", "due_date"}}
        try:
            c = await self.classifier.classify(msg["body"], entity_ref=entity.external_ref,
                                               entity_summary=loaded_facts, today=self.rt.clock.now().date(),
                                               budget=Budget(20_000))
        except LLMError as exc:
            self._event("message.classification_failed", entity, message_id=msg["id"], error=str(exc)[:300])
            self._say(f"{entity.external_ref}: could not classify {msg['id']}: {exc}")
            return {"message": msg["id"], "entity": entity.external_ref, "error": str(exc)}
        self._event("message.classified", entity, message_id=msg["id"], intent=c.intent, confidence=c.confidence,
                    quote=c.quote, amount=c.amount_minor, date=c.date.isoformat() if c.date else None,
                    problems=c.problems)
        self._say(f"{entity.external_ref}: reply classified {c.intent} ({c.confidence:.2f})"
                  + (f", amount {c.amount_minor}, date {c.date}" if c.intent == "promise_to_pay" else "")
                  + (f" — problems: {c.problems}" if c.problems else ""))
        ctx = {**c.guard_context(), "message": {"id": msg["id"]}}
        moves = []
        if c.intent == "promise_to_pay":
            moves.append(("PROMISED", c.facts()))
        if c.intent == "dispute":
            moves += [("ACKNOWLEDGED", {}), ("DISPUTED", {"dispute_quote": c.quote})]
        if c.intent == "paid_claim":
            self.sync_requested = True      # a claim triggers a bank check; it never moves the entity itself
        if not moves:
            moves.append(("ACKNOWLEDGED", {}))
        results = []
        for target, facts in moves:
            current = self.rt.pipeline.get(entity.id).state
            if target == current or target not in self.rt.pipeline.allowed_targets(current):
                continue
            tr = self.rt.pipeline.propose(entity.id, target, ctx, facts=facts)
            results.append(f"{tr.from_state}->{tr.to_state}:{'ok' if tr.ok else 'refused'}")
            self._say(f"{entity.external_ref}: {tr.from_state} -> {target} "
                      + ("ok" if tr.ok else f"refused ({tr.reason}: {tr.guard.failures() if tr.guard else ''})"))
        return {"message": msg["id"], "entity": entity.external_ref, "intent": c.intent, "moves": results}

    # --- timers -------------------------------------------------------------------------------

    async def on_timer(self, event: Event) -> None:
        if event.mission_id != self.mission_id or event.entity_id is None:
            return
        entity = self.rt.pipeline.get(event.entity_id)
        if event.type == "timer.promise_reminder_due" and entity.state == "PROMISED":
            facts = entity.data.get("facts", {})
            out = await outreach(self.rt, self.mission_id, entity.external_ref, step="promise_reminder",
                                 transition_to=None,
                                 purpose="Polite reminder, one day before the date the customer committed to.",
                                 extra_facts={"promised_date": facts.get("promise_date"),
                                              "promise_quote": facts.get("promise_quote")})
            self._say(f"{entity.external_ref}: promise reminder " + ("; ".join(out.attempts)))
        elif event.type == "timer.promise_check_due" and entity.state == "PROMISED":
            await self.bank_sync(force=True)
            entity = self.rt.pipeline.get(entity.id)
            if entity.state == "PROMISED":
                tr = self.rt.pipeline.propose(entity.id, "BROKEN_PROMISE", {"matcher": {"matched_amount": 0}},
                                              actor="system")
                self._say(f"{entity.external_ref}: promise date passed without money -> BROKEN_PROMISE "
                          f"{'ok' if tr.ok else tr.reason}")
        elif event.type == "timer.followup_due" and entity.state == "CONTACTED":
            self._say(f"{entity.external_ref}: follow-up due (cadence handling arrives with the Mission Agent)")

    # --- bank sync ---------------------------------------------------------------------------

    async def bank_sync(self, *, force: bool = False) -> list[Settled]:
        today = self.rt.clock.now().date()
        if not force and not self.sync_requested and self._kv("last_sync") == today.isoformat():
            return []
        open_states = role_cfg(self.rt)["reconciliation"]["states"]
        if not any(self.rt.pipeline.list(self.mission_id, s) for s in open_states):
            return []
        start, end = lookback(self.rt)
        self._say(f"bank sync: Operator downloading statement {start} .. {end}")
        path, res = await download_statement(self.rt, self.mission_id, start, end)
        self._set_kv("last_sync", today.isoformat())
        self.sync_requested = False
        if path is None:
            self._say(f"bank sync failed: {getattr(res, 'error', res)}")
            return []
        settled = await settle(self.rt, self.mission_id, path)
        for s in settled:
            tr = s.transition
            self._say(f"{s.entity_ref}: matched {s.match.record_id} ({s.match.kind}, {s.match.matched_amount}) "
                      f"receipt {s.receipt['id'] if s.receipt else 'NOT POSTED'}; verifier "
                      f"{'pass' if s.report and s.report.passed else 'fail'}; "
                      + (f"{tr.from_state} -> {tr.to_state} {'ok' if tr.ok else 'refused: ' + tr.reason}" if tr else ""))
        if not settled:
            self._say("bank sync: no new matching payments")
        return settled

    # --- one pass ---------------------------------------------------------------------------------

    async def step(self) -> None:
        """One orchestration pass. Facts first (new messages, money in the bank), then timers, so a
        timer never acts on stale state (e.g. a payment reminder after the money already arrived)."""
        await self.process_inbox()
        await self.bank_sync()
        await self.rt.scheduler.fire_due()


class _Loaded:
    """Adapter so conversation_facts can read an Entity's stored record."""

    def __init__(self, entity: Entity):
        self.facts = entity.data.get("record", {})
        self.contact = entity.data.get("contact", {})

