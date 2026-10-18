"""Orchestrator for one mission: turns events into agent work and guarded transitions.

Wakes on:
- new inbound messages  -> Reply Classifier -> propose ACKNOWLEDGED / PROMISED / DISPUTED, send
                           requested documents; one message may cover several entities of one
                           counterparty (a promised total is allocated oldest first)
- timers (scheduler)    -> follow-up, promise reminder, promise check (-> BROKEN_PROMISE if no money)
- the daily bank sync   -> Operator downloads the statement -> settle (match, receipt, verify, PAID)

Agents propose; the pipeline's guards decide. Every step leaves events.
"""

from __future__ import annotations

from typing import Any

from datetime import datetime, timedelta
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from agents.verifier_agent import FuzzyCriterion, VerifierAgent
from apps.flows import conversation_facts, outreach, role_cfg, role_prompt
from apps.runtime import Runtime
from apps.settlement import Settled, lookback, settle, statement_records
from core.injection import redacted
from core.llm import Budget, LLMError
from core.models import CriterionResult, Entity, Event, Evidence, VerificationReport
from subagents.matcher import allocate
from subagents.reply_classifier import ReplyClassifier
from tools.base import RunContext

KV_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
SYSTEM_SENDERS = {"mailer-daemon@hub.example"}
MAX_MESSAGE_TRIES = 3


class Orchestrator:
    def __init__(self, rt: Runtime, mission_id: str):
        self.rt, self.mission_id = rt, mission_id
        rt.db.ensure_schema(KV_SCHEMA)
        cfg = role_cfg(rt)["reply_classifier"]
        self.classifier = ReplyClassifier(rt.llm, cfg["intents"], minor_units=cfg.get("minor_units", 100),
                                          tokenizer=rt.tokenizer)
        self.verifier = VerifierAgent(rt.llm, role_prompt=role_prompt(rt, "verifier"))
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

    def _thread(self, e: Entity) -> str | None:
        return (e.data.get("facts", {}).get("last_outreach") or e.data.get("last_contact") or {}).get("thread_id")

    def _route(self, msg: dict) -> list[Entity]:
        """Which entities a message is about: its thread first, then references in its text, then
        the sender's open entities. Several entities when one conversation covers a counterparty's items."""
        entities = self.rt.pipeline.list(self.mission_id)
        terminal = set(self.rt.pipeline.spec.terminal)
        by_thread = [e for e in entities if msg.get("thread_id") and self._thread(e) == msg["thread_id"]]
        if by_thread:
            return by_thread
        text = f"{msg.get('subject') or ''} {msg['body']}"
        hits = [e for e in entities if e.external_ref in text]
        if hits:
            return hits
        sender = (msg.get("from_addr") or "").lower()
        return [e for e in entities if e.state not in terminal and sender
                and sender in {str(v).lower() for v in e.data.get("contact", {}).values() if v}]

    def _order_field(self) -> str:
        return role_cfg(self.rt)["reconciliation"].get("order_field", "")

    def _oldest_first(self, entities: list[Entity]) -> list[Entity]:
        f = self._order_field()
        return sorted(entities, key=lambda e: (str(e.data.get("record", {}).get(f) or "9999"), e.external_ref))

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
                if msg["from_addr"] not in SYSTEM_SENDERS:
                    try:
                        handled.append(await self.handle_message(msg))
                    except Exception as exc:  # noqa: BLE001 - keep the message; retry it next pass
                        tries = int(self._kv(f"tries:{msg['id']}", "0")) + 1
                        self._set_kv(f"tries:{msg['id']}", str(tries))
                        self._event("message.handling_failed", message_id=msg["id"], error=repr(exc)[:300],
                                    tries=tries)
                        if tries < MAX_MESSAGE_TRIES:
                            self._say(f"message {msg['id']} failed ({exc!r}); will retry next pass")
                            break           # cursor stays before this message
                        self._say(f"message {msg['id']} failed {tries} times; handed to a human")
                        self.escalate_failure(f"message:{msg['id']}", f"Could not process message {msg['id']} "
                                              f"from {msg['from_addr']}", {"error": repr(exc)[:300]},
                                              "Read the message and handle it by hand.")
                # advance only after the message is fully handled: a crash before this re-reads it
                self._set_kv(f"cursor:{channel}", str(msg["seq"]))
        return handled

    async def handle_message(self, msg: dict) -> dict[str, Any]:
        routed = self._route(msg)
        if not routed:
            self._event("message.unrouted", message_id=msg["id"], sender=msg["from_addr"])
            self._say(f"message {msg['id']} from {msg['from_addr']} not linked to an entity (needs a human)")
            return {"message": msg["id"], "entity": None}
        refs_txt = ",".join(e.external_ref for e in routed)
        if not msg.get("released"):
            attached = await self.attachment_texts(msg)
            screening = await self.rt.guard.screen("\n".join([msg.get("subject") or "", msg["body"], *attached]))
            if screening.flagged:
                return self.quarantine_message(msg, routed, screening)
        summaries = [{"ref": e.external_ref,
                      **{k: v for k, v in conversation_facts(self.rt, _Loaded(e)).items()
                         if k in {"customer_name", "balance_paise", "due_date"}}} for e in routed]
        try:
            c = await self.classifier.classify(msg["body"], entities=summaries, today=self.rt.clock.now().date(),
                                               budget=self.rt.budget(self.mission_id, 20_000))
        except LLMError as exc:
            self._event("message.classification_failed", routed[0], message_id=msg["id"], error=str(exc)[:300])
            self._say(f"{refs_txt}: could not classify {msg['id']}: {exc}")
            return {"message": msg["id"], "entity": refs_txt, "error": str(exc)}
        targets = self._oldest_first([e for e in routed if e.external_ref in c.entity_refs])
        for e in targets or routed[:1]:
            self._event("message.classified", e, message_id=msg["id"], intent=c.intent, confidence=c.confidence,
                        quote=c.quote, amount=c.amount_minor, date=c.date.isoformat() if c.date else None,
                        refs=c.entity_refs, problems=c.problems)
        self._say(f"{refs_txt}: reply classified {c.intent} ({c.confidence:.2f})"
                  + (f", amount {c.amount_minor}, date {c.date}" if c.intent == "promise_to_pay" else "")
                  + (f", about {c.entity_refs}" if len(routed) > 1 else "")
                  + (f" — problems: {c.problems}" if c.problems else ""))
        ctx = {**c.guard_context(), "message": {"id": msg["id"]}}
        if c.intent == "paid_claim":
            self.sync_requested = True      # a claim triggers a bank check; it never moves the entity itself
        if c.intent == "doc_request" and targets:
            await self.send_documents(targets, msg)
        if c.intent == "bank_detail_change":
            return await self.suspected_fraud(msg, targets or routed, c, ctx)
        if c.intent == "discount_request" and targets:
            self.request_concession(msg, targets, c)
        if c.intent == "promise_to_pay":
            report = await self.verify_promise(targets[0] if targets else routed[0], msg, c)
            ctx["verifier"] = {"pass": report.passed}

        shares: list[int | None] = [None] * len(targets)
        if c.intent == "promise_to_pay" and c.amount_minor:
            tol = self.rt.pipeline.spec.constants.get("tolerance", 0)
            shares = allocate(c.amount_minor, [int(e.data.get("balance", e.value)) for e in targets], tol)
        results = []
        for e, share in zip(targets, shares):
            moves: list[tuple[str, dict]] = []
            if c.intent == "promise_to_pay" and share != 0:
                facts = c.facts()
                if share is not None:
                    facts["promise_amount"] = share
                if len(targets) > 1:
                    facts["promise_group"] = [t.external_ref for t in targets]
                    facts["promise_total"] = c.amount_minor
                moves.append(("PROMISED", facts))
            if c.intent == "dispute":
                moves += [("ACKNOWLEDGED", {}), ("DISPUTED", {"dispute_quote": c.quote})]
            if not moves:
                moves.append(("ACKNOWLEDGED", {}))
            for target, facts in moves:
                now_e = self.rt.pipeline.get(e.id)
                current = now_e.state
                if target not in self.rt.pipeline.allowed_targets(current):
                    continue                    # includes "already there" unless the role pack allows a re-entry
                if target == current:
                    old = now_e.data.get("facts", {})
                    if all(old.get(k) == facts.get(k) for k in ("promise_date", "promise_amount")):
                        continue                # the same promise repeated: nothing changes
                    facts = {**facts, "previous_promise": {"date": old.get("promise_date"),
                                                           "amount": old.get("promise_amount")}}
                tr = self.rt.pipeline.propose(e.id, target, ctx, facts=facts)
                if tr.ok and c.quote:
                    self.rt.evidence.collect([Evidence(kind="message", ref=msg["id"], summary=c.quote,
                                                       data={"from": msg["from_addr"], "channel": msg["channel"],
                                                             "intent": c.intent, "confidence": c.confidence})],
                                             subject=f"transition:{target}", mission_id=self.mission_id,
                                             entity_id=e.id)
                results.append(f"{e.external_ref}:{tr.from_state}->{tr.to_state}:{'ok' if tr.ok else 'refused'}")
                self._say(f"{e.external_ref}: {tr.from_state} -> {target} "
                          + ("ok" if tr.ok else f"refused ({tr.reason}: {tr.guard.failures() if tr.guard else ''})"))
        return {"message": msg["id"], "entity": refs_txt, "intent": c.intent, "moves": results}

    def escalate_failure(self, what: str, title: str, detail: dict[str, Any], next_step: str) -> None:
        """L4: what the ladder could not fix goes to a human, once per thing per day, with a proposed next step."""
        a = self.rt.approvals.request(
            key=f"failure:{self.mission_id}:{what}:{self.rt.clock.now().date()}", kind="decision",
            mission_id=self.mission_id, title=title,
            payload={"action": "failure", "what": what, "detail": detail, "question": next_step})
        self._event("handoff.requested", what=what, approval_id=a.id, detail=detail)

    # --- trust: injection, fraud, concessions, promise verification ------------------------------

    async def attachment_texts(self, msg: dict) -> list[str]:
        """Text of a message's attachments (PDFs, text files), so they are screened with the message.
        Files that cannot be read as text are noted; no agent can read them either."""
        out = []
        ctx = RunContext(mission_id=self.mission_id, task_id=f"screen-{msg['id']}", allowed_apps={msg["channel"]},
                         artifacts_dir=self.rt.artifacts_dir)
        for att in msg.get("attachments") or []:
            res = await self.rt.registry.invoke(f"{msg['channel']}.attachment",
                                                {"message_id": msg["id"], "filename": att["filename"]}, ctx)
            if not res.ok:
                self._event("message.attachment_unscreened", message_id=msg["id"], filename=att["filename"],
                            error=res.error)
                continue
            path = Path(res.output["path"])
            try:
                if path.suffix.lower() == ".pdf":
                    out.append("\n".join(p.extract_text() or "" for p in PdfReader(path).pages))
                elif path.suffix.lower() in {".txt", ".csv", ".md", ".html", ".json", ".xml"}:
                    out.append(path.read_text(errors="replace"))
                else:
                    self._event("message.attachment_unscreened", message_id=msg["id"], filename=att["filename"],
                                error="not a text document")
            except (PdfReadError, OSError, ValueError) as exc:
                self._event("message.attachment_unscreened", message_id=msg["id"], filename=att["filename"],
                            error=str(exc)[:200])
        return out

    def quarantine_message(self, msg: dict, routed: list[Entity], screening) -> dict[str, Any]:
        """Text that tries to instruct the AI never reaches an agent. The entities it concerns are
        quarantined (every write or message about them needs a human) and a human reviews it."""
        reason = f"message {msg['id']} looks like a prompt-injection attempt"
        for e in routed:
            self.rt.gate.quarantine(f"entity:{e.id}", reason, screening.score)
            self._event("security.injection_suspected", e, message_id=msg["id"], sender=msg["from_addr"],
                        screening=screening.as_dict(), agent_view=redacted(msg["body"], screening))
        a = self.rt.approvals.request(
            key=f"review:{msg['id']}", kind="decision", mission_id=self.mission_id, entity_id=routed[0].id,
            title=f"Review a quarantined message from {msg['from_addr']} about "
                  f"{', '.join(e.external_ref for e in routed)}",
            payload={"action": "review_quarantine", "entity_ids": [e.id for e in routed], "message": msg,
                     "screening": screening.as_dict(),
                     "question": "Approve = the message is genuine: release it to the agents. "
                                 "Reject = keep it quarantined and hand the account to a human."})
        self._say(f"{','.join(e.external_ref for e in routed)}: message {msg['id']} QUARANTINED "
                  f"(score {screening.score:.2f}, {screening.pattern_hits or 'classifier'}); review {a.id}")
        return {"message": msg["id"], "entity": ",".join(e.external_ref for e in routed), "quarantined": a.id}

    async def suspected_fraud(self, msg: dict, entities: list[Entity], c, ctx: dict) -> dict[str, Any]:
        """'Our/your bank details changed' is never acted on: escalate, and restate the official details."""
        for e in entities:
            self._event("security.fraud_suspected", e, message_id=msg["id"], sender=msg["from_addr"], quote=c.quote)
            for target in ("ACKNOWLEDGED", "ESCALATED"):
                current = self.rt.pipeline.get(e.id).state
                if target in self.rt.pipeline.allowed_targets(current):
                    self.rt.pipeline.propose(e.id, target, ctx, facts={"escalation_reason":
                                                                       "message asking to pay different account details"})
        self.rt.events.append(Event(type="handoff.requested", mission_id=self.mission_id, occurred_at=self.rt.clock.now(),
                                    payload={"refs": [e.external_ref for e in entities], "message_id": msg["id"],
                                             "reason": "possible payment-redirection fraud"}))
        out = await outreach(self.rt, self.mission_id, [e.external_ref for e in entities], step=f"fraud:{msg['id']}",
                             transition_to=None, channels=[msg["channel"]],
                             purpose="The customer was told our bank details changed. They have NOT changed and "
                                     "never change by message. Restate the official bank details from COMPANY_FACTS, "
                                     "ask them to pay only to that account, and say our team will call to confirm.")
        self._say(f"{','.join(e.external_ref for e in entities)}: SUSPECTED FRAUD (bank detail change) -> escalated; "
                  f"official details restated ({'; '.join(out.attempts)})")
        return {"message": msg["id"], "entity": ",".join(e.external_ref for e in entities), "intent": c.intent,
                "fraud": True}

    def request_concession(self, msg: dict, entities: list[Entity], c) -> None:
        a = self.rt.approvals.request(
            key=f"concession:{msg['id']}", kind="decision", mission_id=self.mission_id, entity_id=entities[0].id,
            title=f"{', '.join(e.external_ref for e in entities)}: customer asks for a concession",
            payload={"action": "concession", "entity_ids": [e.id for e in entities], "quote": c.quote,
                     "message_id": msg["id"], "channel": msg["channel"],
                     "grants_phrases": ["discount", "waive", "waiver"], "grants_on_reject": True,
                     "question": f"The customer wrote: {c.quote!r}. Approve the concession they ask for?",
                     "reply": {"approved": "Tell the customer the finance manager approved the concession they asked "
                                           "for, exactly as they asked (quote it), and ask for the payment date.",
                               "rejected": "Politely tell the customer we cannot offer a discount on this invoice, "
                                           "and ask for a payment date for the full balance."}})
        self._say(f"{','.join(e.external_ref for e in entities)}: concession requested -> approval {a.id}")

    async def verify_promise(self, entity: Entity, msg: dict, c) -> VerificationReport:
        """Deterministic checks on the promise; a second, independent LLM opinion only for borderline cases."""
        cfg = role_cfg(self.rt).get("verification", {}).get("promise", {})
        today = self.rt.clock.now().date()
        horizon = today + timedelta(days=cfg.get("max_days_ahead", 60))
        checks = [
            CriterionResult(criterion="the supporting quote appears verbatim in the message", passed=bool(c.quote)),
            CriterionResult(criterion="an amount is committed", passed=bool(c.amount_minor and c.amount_minor > 0)),
            CriterionResult(criterion="the date is today or later", passed=bool(c.date and c.date >= today)),
            CriterionResult(criterion=f"the date is within {cfg.get('max_days_ahead', 60)} days",
                            passed=bool(c.date and c.date <= horizon))]
        fuzzy = []
        if c.confidence < cfg.get("second_opinion_below", 0.9):
            tok = self.rt.tokenizer()
            fuzzy.append(FuzzyCriterion(
                criterion="The message is a firm commitment to pay the stated amount by the stated date "
                          "(not conditional, not 'we will try', not a question).",
                evidence=tok.tokenize(msg["body"]),
                context={"amount": c.amount_minor, "date": c.date.isoformat() if c.date else None}))
        try:
            report = await self.verifier.verify(entity.id, checks, fuzzy, budget=self.rt.budget(self.mission_id, 10_000))
        except LLMError as exc:
            report = VerificationReport(subject_id=entity.id, passed=False, confidence=0.0, criteria=[
                *checks, CriterionResult(criterion="second opinion", passed=False, note=f"verifier unavailable: {exc}")])
        self.rt.events.append(Event(type="verification.completed", mission_id=self.mission_id, entity_id=entity.id,
                                    occurred_at=self.rt.clock.now(),
                                    payload={"subject": "promise", **report.model_dump(mode="json")}))
        return report

    # --- actions on a counterparty's group of entities -------------------------------------------

    def group_of(self, entity: Entity, states: set[str] | None = None) -> list[Entity]:
        """Entities in the same conversation as `entity` (same thread), optionally only in `states`."""
        thread = self._thread(entity)
        members = [e for e in self.rt.pipeline.list(self.mission_id)
                   if (thread and self._thread(e) == thread) or e.id == entity.id]
        return self._oldest_first([e for e in members if states is None or e.state in states])

    async def send_documents(self, entities: list[Entity], msg: dict | None = None, *,
                             reason: str = "Reply to the customer's request: send the documents attached.") -> Any:
        from apps.documents import generate
        group_field = role_cfg(self.rt)["mission"]["selection"]["group_key"]
        group = entities[0].data.get("record", {}).get(group_field)
        statement = await generate(self.rt, self.mission_id, "statement_of_account", group=group) if group else None
        channel = msg["channel"] if msg else None
        out = await outreach(self.rt, self.mission_id, [e.external_ref for e in entities],
                             step=f"docs:{msg['id'] if msg else self.rt.clock.now().date().isoformat()}",
                             purpose=reason, transition_to=None, extra_documents=[statement] if statement else [],
                             channels=[channel] if channel else None)
        self._say(f"{','.join(e.external_ref for e in entities)}: documents sent ({'; '.join(out.attempts)})")
        return out

    async def follow_up(self, entity: Entity, *, step: str = "followup1") -> Any:
        """One follow-up for the entity's conversation, on the next channel after the one last used."""
        group = self.group_of(entity, {"CONTACTED"})
        if not group or any(e.data.get("last_contact", {}).get("step") == step for e in group):
            return None
        order = role_cfg(self.rt)["outreach"]["channel_order"]
        last = (group[0].data.get("last_contact") or {}).get("channel")
        channels = order[order.index(last) + 1:] + order[:order.index(last) + 1] if last in order else order
        out = await outreach(self.rt, self.mission_id, [e.external_ref for e in group], step=step,
                             transition_to=None, channels=channels,
                             purpose="Follow-up: we have not heard back about the invoice(s) listed. Politely ask "
                                     "for a payment date.")
        self._say(f"{','.join(e.external_ref for e in group)}: follow-up " + "; ".join(out.attempts))
        return out

    # --- timers -------------------------------------------------------------------------------

    async def on_timer(self, event: Event) -> None:
        if event.mission_id != self.mission_id or event.entity_id is None:
            return
        entity = self.rt.pipeline.get(event.entity_id)
        if event.type == "timer.promise_reminder_due" and entity.state == "PROMISED":
            facts = entity.data.get("facts", {})
            refs = facts.get("promise_group") or [entity.external_ref]
            group = [e for e in self.rt.pipeline.list(self.mission_id)
                     if e.external_ref in refs and e.state == "PROMISED"]
            step = f"promise_reminder:{facts.get('promise_date')}"
            if any(e.data.get("last_contact", {}).get("step") == step for e in group):
                return                      # one reminder per promise, even when it covers several entities
            out = await outreach(self.rt, self.mission_id, [e.external_ref for e in group], step=step,
                                 transition_to=None,
                                 purpose="Polite reminder, one day before the date the customer committed to.",
                                 extra_facts={"promised_date": facts.get("promise_date"),
                                              "promise_quote": facts.get("promise_quote")})
            self._say(f"{','.join(refs)}: promise reminder " + ("; ".join(out.attempts)))
            self._retry_later(event, out)
        elif event.type == "timer.promise_check_due" and entity.state == "PROMISED":
            await self.bank_sync(force=True)
            entity = self.rt.pipeline.get(entity.id)
            if entity.state == "PROMISED":
                tr = self.rt.pipeline.propose(entity.id, "BROKEN_PROMISE", {"matcher": {"matched_amount": 0}},
                                              actor="system")
                self._say(f"{entity.external_ref}: promise date passed without money -> BROKEN_PROMISE "
                          f"{'ok' if tr.ok else tr.reason}")
        elif event.type == "timer.followup_due" and entity.state == "CONTACTED":
            self._retry_later(event, await self.follow_up(entity))

    def _retry_later(self, event: Event, out: Any) -> None:
        """A timer's message held back by policy (quiet hours, weekend, contact cap) is not dropped:
        the same timer fires again when the Policy Gate says it may run."""
        if out is None or not getattr(out, "deferred_until", None):
            return
        due = datetime.fromisoformat(out.deferred_until)
        self.rt.scheduler.schedule(f"retry:{event.type}:{event.entity_id}:{due.isoformat()}", due, event.type,
                                   mission_id=event.mission_id, entity_id=event.entity_id,
                                   payload={**event.payload, "retry_of": event.id})
        self._say(f"{event.type.split('.')[-1]} deferred by policy; will try again {due:%a %d %b %H:%M}")

    # --- bank sync ---------------------------------------------------------------------------

    async def bank_sync(self, *, force: bool = False) -> list[Settled]:
        today = self.rt.clock.now().date()
        if not force and not self.sync_requested and self._kv("last_sync") == today.isoformat():
            return []
        open_states = role_cfg(self.rt)["reconciliation"]["states"]
        if not any(self.rt.pipeline.list(self.mission_id, s) for s in open_states):
            return []
        start, end = lookback(self.rt)
        read = await statement_records(self.rt, self.mission_id, start, end)
        self._set_kv("last_sync", today.isoformat())
        self.sync_requested = False
        if read.records is None:
            self._say(f"bank sync FAILED on every route: {read.attempts}")
            self.escalate_failure("bank_sync", f"Bank statement could not be read on {today}",
                                  {"attempts": read.attempts},
                                  "Check the bank portal; payments cannot be reconciled until it works.")
            return []
        if read.rung != "playbook":
            self._say(f"bank sync via fallback '{read.rung}' after: {read.attempts}")
        settled = await settle(self.rt, self.mission_id, read.records, read.source)
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

