"""Mission runner: one brief -> understood spec -> selected, ranked entities -> approved plan -> ticks.

    create(brief)  Mission Agent understands it (CLARIFYING if a blocking question remains)
    plan()         code selects entities (agent's parameters + policy exclusions), loads them into
                   the pipeline, Prioritizer ranks counterparties, Mission Agent plans batches
                   -> AWAITING_PLAN_APPROVAL
    approve()      human only -> ACTIVE
    tick()         orchestrator pass (replies, bank, timers) -> due batches go out -> progress
                   -> COMPLETED if verified >= target -> Mission Agent reviews once a day and
                   chooses levers, which code executes

What counts as progress is the mission goal's (role pack `mission.goals`): for `collect`, value read
from the settlement ledger (matched, posted, verified), never from promises; for a goal measured by
`groups_reached`, counterparties whose entities all reached the goal's states through guarded transitions.
Completion is re-observed in the systems of record either way.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from agents.mission_agent import MissionAgent
from apps.flows import _read, load_entity, mission_goal, outreach, role_cfg, role_prompt
from apps.memory import counterparty_memory, distill, remember_facts
from apps.orchestrator import Orchestrator
from apps.runtime import Runtime
from apps.confirmations import fresh_confirmations
from apps.settlement import ensure_ledger, fresh_verified_total, peek_payments
from core.guards import compile_guard, evaluate
from core.injection import redacted
from core.llm import Budget, LLMError
from core.mission import MissionStore, Progress, progress
from core.models import Entity, Event, Mission, MissionPlan, MissionStatus, TickDecision
from subagents.scorer import Group, Item, ScorerConfig, rank

S = MissionStatus


class MissionRunner:
    def __init__(self, rt: Runtime, mission_id: str | None = None):
        self.rt, self.mission_id = rt, mission_id
        self.store = MissionStore(rt.db, rt.events, rt.clock)
        self.cfg = role_cfg(rt)
        self.mcfg = self.cfg["mission"]
        self.agent = MissionAgent(rt.llm, role_prompt=role_prompt(rt, "mission"),
                                  minor_units=self.mcfg.get("minor_units", 100), value_unit=self.cfg.get("value_unit"),
                                  tz=rt.company_cfg.get("timezone", "UTC"),
                                  plan_role="mission_plan" if rt.llm is not None and "mission_plan" in
                                  getattr(rt.llm, "roles", {}) else "mission")
        self._budget = None
        self.log: list[str] = []
        self._orch: Orchestrator | None = None

    # --- small helpers ---------------------------------------------------------------------

    @property
    def budget(self) -> Budget:
        if self._budget is None or self._budget.key != f"mission:{self.mission_id}":
            self._budget = self.rt.mission_budget(self.mission_id)
        return self._budget

    @property
    def mission(self) -> Mission:
        return self.store.get(self.mission_id)

    @property
    def orch(self) -> Orchestrator:
        if self._orch is None:
            self._orch = Orchestrator(self.rt, self.mission_id)
        return self._orch

    def _say(self, text: str) -> None:
        self.log.append(f"[{self.rt.clock.now():%a %d %b %H:%M}] {text}")

    def _today(self) -> date:
        return self.rt.clock.now().date()

    def _group_of(self, e: Entity) -> str:
        return str(e.data.get("record", {}).get(self.mcfg["selection"]["group_key"]))

    # --- goal ------------------------------------------------------------------------------------

    def goal(self) -> dict[str, Any]:
        """The mission's goal from the role pack (name, measure, target, approaches, levers...)."""
        return mission_goal(self.rt, self.mission_id)

    def approaches(self) -> dict[str, dict[str, Any]]:
        allowed = self.goal().get("approaches")
        return {k: v for k, v in self.mcfg["approaches"].items() if not allowed or k in allowed}

    def levers(self) -> dict[str, dict[str, Any]]:
        allowed = self.goal().get("levers")
        return {k: v for k, v in self.mcfg["levers"].items() if not allowed or k in allowed}

    def forecast_weights(self) -> dict[str, float]:
        return self.goal().get("forecast_weights") or self.mcfg.get("forecast_weights", {})

    def counts_groups(self) -> bool:
        return self.goal().get("measure") == "groups_reached"

    async def _policy_text(self) -> str:
        texts = []
        for path in self.rt.company_cfg.get("policy_docs", []):
            try:
                text = (await _read(self.rt, "files.read_text", {"path": path}, self.mission_id))["text"]
            except RuntimeError as exc:
                self._say(f"could not read policy {path}: {exc}")
                continue
            # a document on a shared drive is still untrusted: screen it before the Mission Agent reads it
            screening = await self.rt.guard.screen(text)
            if screening.flagged:
                self.rt.events.append(Event(type="security.injection_suspected", mission_id=self.mission_id,
                                            occurred_at=self.rt.clock.now(),
                                            payload={"source": path, "screening": screening.as_dict()}))
                self._say(f"policy document {path} looks tampered with (score {screening.score:.2f}); withheld")
                text = redacted(text, screening)
            texts.append(self.rt.tokenizer().tokenize(text))
        return "\n\n".join(texts)

    # --- understand ------------------------------------------------------------------------------

    async def create(self, brief: str) -> Mission:
        m = self.store.create(self.rt.role, self.rt.company, brief)
        self.mission_id = m.id
        self._say(f"mission {m.id} created: {brief!r}")
        await self.understand()
        return self.mission

    async def understand(self) -> Mission:
        m = self.mission
        params = self.mcfg["selection"]["params"]
        goals = {k: " ".join(str(v.get("description", k)).split()) for k, v in self.mcfg.get("goals", {}).items()}
        u = await self.agent.understand(m.brief, today=self._today(), selection_params=params,
                                        policy=await self._policy_text(), answers=m.data.get("answers", []),
                                        goals=goals or None, budget=self.budget)
        spec = u.spec
        goal = self.mcfg.get("goals", {}).get(spec.goal, {}) if spec.goal else {}
        if goal.get("target") == "all_groups":
            if spec.target_value is not None:
                u.problems.append(f"goal {spec.goal} counts {goal.get('unit', 'groups')}; the amount in the brief "
                                  "is not its target")
            spec.target_value, spec.target_unit = None, goal.get("unit", "groups")   # set from the scope at plan()
        criteria = goal.get("success_criteria") or self.mcfg.get("success_criteria", [])
        spec.success_criteria = list(dict.fromkeys([*criteria, *spec.success_criteria]))
        m = self.store.update(m.id, spec=spec, selection=u.selection, event="mission.understood",
                              payload={"spec": spec.model_dump(mode="json"), "selection": u.selection,
                                       "problems": u.problems})
        self._say(f"understood: {spec.objective} | goal {spec.goal} | target {spec.target_value} by {spec.deadline} | "
                  f"selection {u.selection}" + (f" | problems {u.problems}" if u.problems else ""))
        if spec.open_questions:
            self.store.transition(m.id, S.CLARIFYING, actor="agent", reason="; ".join(spec.open_questions))
            self._say("needs answers: " + " | ".join(spec.open_questions))
            return self.mission
        return await self.plan()

    async def answer(self, text: str) -> Mission:
        """A human answers the open questions; the brief is understood again with the answer."""
        m = self.mission
        self.store.update(m.id, data={"answers": [*m.data.get("answers", []), text]}, event="mission.answered",
                          payload={"answer": text})
        self.store.transition(m.id, S.DRAFT, actor="human", reason="questions answered")
        return await self.understand()

    # --- select + rank + plan -------------------------------------------------------------------

    async def select(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Entities the mission covers: the agent's selection on top of fixed params, minus policy exclusions.
        Returns (included items, excluded [{ref, reason}], groups by id)."""
        sel = self.mcfg["selection"]
        params = sel.get("params", {})
        chosen = self.mission.selection
        args = {**sel.get("fixed", {}), **{k: v for k, v in chosen.items() if params.get(k, {}).get("scope") != "group"}}
        listed = await _read(self.rt, sel["list_tool"], args, self.mission_id)
        items = listed[sel["list_key"]] if sel.get("list_key") else listed
        groups_raw = await _read(self.rt, sel["group_tool"], {}, self.mission_id)
        groups = {str(g["id"]): g for g in groups_raw}
        goal = self.goal()["name"]
        rules = [(compile_guard(r["when"]), r["reason"]) for r in sel.get("exclude", [])
                 if not r.get("goals") or goal in r["goals"]]
        ref_field = self.cfg["prioritizer"]["fields"]["ref"]
        value_field = self.cfg["prioritizer"]["fields"]["value"]
        totals: dict[str, int] = {}
        for item in items:
            gid = str(item.get(sel["group_key"]))
            totals[gid] = totals.get(gid, 0) + int(item.get(value_field) or 0)
        group_rules = [(k, compile_guard(params[k]["keep"]), v) for k, v in chosen.items()
                       if params.get(k, {}).get("scope") == "group" and params[k].get("keep")]
        included, excluded = [], []
        for item in items:
            gid = str(item.get(sel["group_key"]))
            ctx = {"item": item, "group": groups.get(gid, {})}
            why = next((reason for guard, reason in rules if evaluate(guard, ctx).passed), None)
            for key, keep, value in group_rules:
                if not why and not evaluate(keep, {"group": {"total": totals[gid]}, "param": value}).passed:
                    why = f"customer total {totals[gid]} does not meet {key} {value}"
            if why:
                excluded.append({"ref": item[ref_field], "reason": why})
            else:
                included.append(item)
        return included, excluded, groups

    async def _signals(self, group_ids: set[str]) -> dict[str, dict[str, Any]]:
        p = self.cfg["prioritizer"]
        contested = set(p.get("contested_statuses", []))
        if not contested:
            return {}
        sel = self.mcfg["selection"]
        listed = await _read(self.rt, sel["list_tool"], {"status": ",".join(sorted(contested))}, self.mission_id)
        rows = listed[sel["list_key"]] if sel.get("list_key") else listed
        hit = {str(r.get(sel["group_key"])) for r in rows}
        return {g: {"contested_before": g in hit} for g in group_ids}

    def rank(self, items: list[dict[str, Any]], signals: dict[str, dict[str, Any]]) -> list[Group]:
        p = self.cfg["prioritizer"]
        f = p["fields"]
        ranked_items = [Item(ref=i[f["ref"]], group=str(i[f["group"]]), group_name=i.get(f["group_name"], ""),
                             value=int(i[f["value"]]), age=int(i[f["age"]]), data=i) for i in items]
        old = p.get("old_after_days")
        for gid in {i.group for i in ranked_items}:
            if old is not None:
                signals.setdefault(gid, {})["old"] = max(i.age for i in ranked_items if i.group == gid) >= old
        cfg = ScorerConfig(weights=p["weights"], risk_signals=p.get("risk_signals", {}),
                           approach_rules=p.get("approach_rules", []), default_approach=p["default_approach"])
        return rank(ranked_items, cfg, signals)

    async def plan(self) -> Mission:
        m = self.mission
        included, excluded, _ = await self.select()
        for item in included:
            await load_entity(self.rt, m.id, item[self.cfg["prioritizer"]["fields"]["ref"]])
        groups = self.rank(included, await self._signals({str(i[self.mcfg["selection"]["group_key"]])
                                                           for i in included}))
        allowed = self.approaches()
        for g in groups:                    # the prioritizer's suggestion must be an approach this goal allows
            if g.approach not in allowed:
                g.approach = next(iter(allowed))
        total = sum(g.value for g in groups)
        weights = self.forecast_weights()
        if self.counts_groups():
            m.spec.target_value = len(groups)
            m = self.store.update(m.id, spec=m.spec)
            if_contacted = progress(m.spec.target_value, 0, {"CONTACTED": len(groups)}, weights)
        else:
            if_contacted = progress(m.spec.target_value, 0, {"CONTACTED": total}, weights)
        self._say(f"scope: {len(included)} entities in {len(groups)} groups, value {total}; "
                  f"excluded {len(excluded)}: " + ", ".join(f"{x['ref']} ({x['reason']})" for x in excluded))
        approaches = {k: v.get("description", "") for k, v in allowed.items()}
        remembered = counterparty_memory(self.rt, [g.id for g in groups])
        rows = [{**g.as_row(), **({"memory": remembered[g.id]} if g.id in remembered else {})} for g in groups]
        try:
            plan = await self.agent.plan(m.spec, rows, approaches=approaches,
                                         capacity=self.mcfg.get("capacity_per_day", 5), today=self._today(),
                                         forecast={"in_scope_value": total,
                                                   "expected_if_all_contacted": if_contacted.expected},
                                         budget=self.budget)
        except LLMError as exc:
            partial = getattr(exc, "failed_generation", "") or ""
            self.store.update(m.id, event="mission.plan_failed",
                              payload={"error": str(exc)[:500], "failed_generation": partial[:4000]})
            self._say(f"planning failed, mission stays {m.status}: {str(exc)[:200]} (retry: mission plan)"
                      + (f"\n    model output started: {partial[:300]!r} ({len(partial)} chars)" if partial
                         else " — the model returned no output (likely cut off by the output cap)"))
            return self.mission
        plan.expected_value = if_contacted.expected
        if not self.counts_groups() and m.spec.target_value is not None and total < m.spec.target_value:
            plan.risks.insert(0, f"target {m.spec.target_value} exceeds the whole in-scope value {total}")
        self.store.update(m.id, plan=plan, data={
            "scope": [i[self.cfg["prioritizer"]["fields"]["ref"]] for i in included], "excluded": excluded,
            "groups": {g.id: {"name": g.name, "refs": [i.ref for i in g.items], "score": round(g.score, 3),
                              "suggested_approach": g.approach} for g in groups},
            "scope_value": total}, event="mission.planned", payload=plan.model_dump(mode="json"))
        prepaid = await self.already_paid() if self.first_batch_needs_approval() else {}
        first = await self.draft_first_batch(plan, skip=set(prepaid)) if self.first_batch_needs_approval() else {}
        self.store.update(m.id, data={"prepaid": list(prepaid.values())})
        if first:
            self.store.update(m.id, plan=plan)               # draft problems are recorded on the plan
        self.store.transition(m.id, S.AWAITING_PLAN_APPROVAL, actor="agent", reason="plan ready")
        version = self.store.get(m.id).data.get("plan_version", 0) + 1
        self.store.update(m.id, data={"plan_version": version, "first_batch": first})
        self.rt.approvals.request(
            key=f"plan:{m.id}:{version}", kind="decision", mission_id=m.id, title=f"Approve the plan for: {m.brief}",
            payload={"action": "mission_plan", "summary": plan.summary,
                     "batches": [b.model_dump() for b in plan.batches], "messages": first,
                     "question": "Approve = start contacting customers as planned"
                                 + (", sending the first-batch messages exactly as shown." if first else ".")
                                 + " Reject with a note to get a new plan and new drafts."})
        self._say(f"plan ready ({len(plan.batches)} batches); waiting for approval"
                  + (f" | code corrected: {plan.problems}" if plan.problems else ""))
        return self.mission

    def _plan_approval(self):
        version = self.mission.data.get("plan_version")
        return self.rt.approvals.by_key(f"plan:{self.mission_id}:{version}") if version else None

    # --- first batch: exact wording approved with the plan -----------------------------------------------

    def first_batch_needs_approval(self) -> bool:
        return bool(self.rt.policies.get("external_comm", {}).get("first_batch_requires_approval"))

    async def already_paid(self) -> dict[str, dict[str, Any]]:
        """Entities whose money is already in the bank (matched, not yet settled): never chase them.
        They are settled on the first run; here we only avoid drafting reminders to them."""
        matches, note = await peek_payments(self.rt, self.mission_id)
        if matches is None:
            self._say(f"could not check the bank before drafting ({note}); drafting for every customer")
            return {}
        refs = {e.id: e.external_ref for e in self.rt.pipeline.list(self.mission_id)}
        found = {m.entity_id: {"ref": refs[m.entity_id], "record_id": m.record_id, "amount": m.matched_amount,
                               "kind": m.kind} for m in matches}
        if found:
            self._say("already paid in the bank, not drafting reminders: "
                      + ", ".join(f"{v['ref']} ({v['record_id']})" for v in found.values()))
        return found

    async def draft_first_batch(self, plan: MissionPlan, skip: set[str] | None = None) -> dict[str, dict[str, Any]]:
        """Draft the real day-0 messages (no sending) so the manager approves their exact wording.
        `skip`: entity ids whose payment is already in the bank."""
        skip = skip or set()
        drafts: dict[str, dict[str, Any]] = {}
        feedback = self.mission.data.get("message_feedback", [])
        for batch in [b for b in plan.batches if b.start_day == 0]:
            spec = self.approaches()[batch.approach]
            for gid in batch.groups:
                ents = [e for e in self._open_initial(gid) if e.id not in skip]
                if not ents:
                    continue
                extra = []
                if "statement_of_account" in spec.get("documents", []):
                    from apps.documents import generate
                    extra.append(await generate(self.rt, self.mission_id, "statement_of_account", group=gid))
                purpose = spec["purpose"] + (" Manager feedback on earlier drafts: " + " | ".join(feedback)
                                             if feedback else "")
                out = await outreach(self.rt, self.mission_id, [e.external_ref for e in ents], purpose=purpose,
                                     extra_documents=extra, draft_only=True)
                if out.draft:
                    drafts[gid] = {**out.draft, "approach": batch.approach}
                else:
                    plan.problems.append(f"could not draft the first message for {gid}: {out.attempts}; "
                                         "it will not be sent until a new plan is approved")
        self._say(f"drafted {len(drafts)} first-batch message(s) for approval")
        return drafts

    def _open_initial(self, gid: str) -> list[Entity]:
        ents = [e for e in self.rt.pipeline.list(self.mission_id)
                if self._group_of(e) == gid and e.state in self.rt.pipeline.spec.initial]
        return self.orch._oldest_first(ents)

    def approve(self, *, by: str = "finance_manager") -> Mission:
        a = self._plan_approval()
        if a and a.status == "PENDING":
            self.rt.approvals.decide(a.id, approve=True, by=by)
        m = self.store.transition(self.mission_id, S.ACTIVE, actor="human", reason=f"plan approved by {by}")
        self.store.update(m.id, data={"activated_on": self._today().isoformat(), "approved_by": by})
        self._say(f"plan approved by {by}; mission ACTIVE")
        return self.mission

    async def reject(self, reason: str, *, by: str = "finance_manager") -> Mission:
        a = self._plan_approval()
        if a and a.status == "PENDING":
            self.rt.approvals.decide(a.id, approve=False, by=by, note=reason)
        m = self.store.transition(self.mission_id, S.DRAFT, actor="human", reason=reason)
        self.store.update(m.id, data={"answers": [*m.data.get("answers", []), f"Plan rejected: {reason}"],
                                      "message_feedback": [*m.data.get("message_feedback", []), reason],
                                      "first_batch": {}})
        return await self.plan()

    # --- progress -----------------------------------------------------------------------------------

    def verified_value(self) -> int:
        ensure_ledger(self.rt)
        with self.rt.db.read() as conn:
            row = conn.execute("SELECT COALESCE(SUM(s.amount), 0) AS v FROM settlements s JOIN entities e "
                               "ON e.id = s.entity_id WHERE e.mission_id = ? AND s.status = 'VERIFIED'",
                               (self.mission_id,)).fetchone()
        return int(row["v"])

    def reached_groups(self) -> dict[str, list[str]]:
        """groups_reached goals: groups whose every entity has entered one of the goal's states (a guarded
        transition, so with evidence), with the entity ids. Leaving the state later (a confirmed balance
        that is then promised) does not undo having reached it."""
        states = set(self.goal().get("states", []))
        reached = {e.entity_id for e in self.rt.events.query(mission_id=self.mission_id, types=["entity.transitioned"],
                                                             limit=100000) if e.payload.get("to") in states}
        by: dict[str, list[Entity]] = {}
        for e in self.rt.pipeline.list(self.mission_id):
            by.setdefault(self._group_of(e), []).append(e)
        return {g: [e.id for e in ents] for g, ents in by.items() if ents and all(e.id in reached for e in ents)}

    def measured_value(self) -> int:
        """Progress the goal counts: verified money, or groups that reached the goal's states."""
        return len(self.reached_groups()) if self.counts_groups() else self.verified_value()

    def progress(self) -> Progress:
        open_by_state: dict[str, int] = {}
        m = self.mission
        if self.counts_groups():
            done = self.reached_groups()
            order = self.rt.pipeline.spec.states
            by: dict[str, list[Entity]] = {}
            for e in self.rt.pipeline.list(self.mission_id):
                by.setdefault(self._group_of(e), []).append(e)
            for gid, ents in by.items():
                if gid not in done:          # a group counts once, at its least advanced entity
                    state = min((e.state for e in ents), key=lambda s: order.index(s) if s in order else 0)
                    open_by_state[state] = open_by_state.get(state, 0) + 1
            return progress(m.spec.target_value if m.spec else None, len(done), open_by_state, self.forecast_weights())
        priors: dict[str, list[tuple[int, float]]] = {}
        state_with_history = self.cfg.get("memory", {}).get("forecast_state")
        ents = [e for e in self.rt.pipeline.list(self.mission_id) if e.state not in self.rt.pipeline.spec.terminal]
        rates = {g: h["history"]["promise_kept_rate"] for g, h in
                 counterparty_memory(self.rt, list({self._group_of(e) for e in ents})).items()
                 if "promise_kept_rate" in h.get("history", {})}
        for e in ents:
            value = int(e.data.get("balance", e.value))
            open_by_state[e.state] = open_by_state.get(e.state, 0) + value
            if e.state == state_with_history and self._group_of(e) in rates:
                priors.setdefault(e.state, []).append((value, rates[self._group_of(e)]))
        return progress(m.spec.target_value if m.spec else None, self.verified_value(), open_by_state,
                        self.forecast_weights(), priors=priors)

    async def reobserve(self) -> tuple[int, Any, str]:
        """Before COMPLETED: count only what is still there in the systems of record."""
        if self.counts_groups():
            return await fresh_confirmations(self.rt, self.mission_id, self.reached_groups())
        return await fresh_verified_total(self.rt, self.mission_id)

    def group_rows(self) -> list[dict[str, Any]]:
        by: dict[str, list[Entity]] = {}
        for e in self.rt.pipeline.list(self.mission_id):
            by.setdefault(self._group_of(e), []).append(e)
        names = {g: d["name"] for g, d in self.mission.data.get("groups", {}).items()}
        failed = self._failed_first_contacts()
        rows = []
        for gid, ents in by.items():
            items = []
            for e in ents:
                f = e.data.get("facts", {})
                items.append({"ref": e.external_ref, "state": e.state, "open": int(e.data.get("balance", e.value)),
                              **({"promise_date": f["promise_date"]} if f.get("promise_date") else {})})
            contacts = [self._last_contact_at(e) for e in ents]
            last = max((c for c in contacts if c), default=None)
            days = (self._today() - datetime.fromisoformat(last).date()).days if last else None
            rows.append({"group": gid, "name": names.get(gid, ""), "states": sorted({e.state for e in ents}),
                         "open_value": sum(i["open"] for i in items if i["state"] not in
                                           self.rt.pipeline.spec.terminal), "items": items,
                         "days_since_contact": days,
                         "first_contact_failed": gid in failed and any(e.state in self.rt.pipeline.spec.initial
                                                                       for e in ents)})
        remembered = counterparty_memory(self.rt, list(by))
        for r in rows:
            if r["group"] in remembered:
                r["memory"] = remembered[r["group"]]
        return sorted(rows, key=lambda r: -r["open_value"])

    def _failed_first_contacts(self) -> set[str]:
        """Groups whose latest contact attempt failed on every channel (and none succeeded after it)."""
        last: dict[str, bool] = {}
        for e in self.rt.events.query(mission_id=self.mission_id, types=["mission.group_contacted"], limit=100000):
            last[e.payload.get("group")] = bool(e.payload.get("ok"))
        return {g for g, ok in last.items() if not ok}

    def _last_contact_at(self, e: Entity) -> str | None:
        """When we last reached out about this entity: the contact's own timestamp, or (for contacts
        recorded before timestamps existed) the evidence stored when it was sent."""
        at = (e.data.get("last_contact") or {}).get("at")
        if at:
            return at
        sent = [x["at"] for x in self.rt.evidence.for_entity(e.id) if x["subject"].startswith("contact:")]
        return max(sent, default=None)

    # --- tick -------------------------------------------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        m = self.mission
        if m.status != S.ACTIVE:
            self._say(f"mission is {m.status}; nothing to do")
            return {"status": m.status}
        budget = self.budget
        if budget.used >= budget.max_tokens:
            return self.pause_for_budget(budget)
        await self.orch.step()
        self.log += self.orch.log
        self.orch.log.clear()
        sent = await self.send_due_batches()
        await self.orch.sync_tracker()              # the batches just sent show in the team's tracker
        self.log += self.orch.log
        self.orch.log.clear()
        prog = self.progress()
        if prog.target is not None and prog.verified >= prog.target:
            fresh, ev, note = await self.reobserve()
            self.rt.events.append(Event(type="verification.completed", mission_id=m.id, occurred_at=self.rt.clock.now(),
                                        payload={"subject": "mission", "ledger": prog.verified, "re_observed": fresh,
                                                 "target": prog.target, "note": note,
                                                 "passed": fresh >= prog.target}))
            if fresh >= prog.target:
                if ev:
                    self.rt.evidence.collect([ev], subject="mission:completed", mission_id=m.id)
                self.store.transition(m.id, S.COMPLETED, actor="system", reason="target reached and re-verified",
                                      verified_value=fresh)
                self._say(f"TARGET REACHED: {fresh} re-observed >= {prog.target}; mission COMPLETED")
                await self.learn()
                return {"status": S.COMPLETED, "progress": prog.as_dict(), "sent": sent}
            self._say(f"records say {prog.verified} but only {fresh} re-observed ({note}); not completed")
        decision = None
        failures = self.failures_since_review()
        today = self._today().isoformat()
        replans = self.mission.data.get("replans", {}).get(today, 0)
        if self.mission.data.get("last_review") != today and self._worth_review():
            decision = await self.review(prog, failures)
        elif failures and replans < self.mcfg.get("max_replans_per_day", 2):
            self._say(f"replanning after {len(failures)} failure(s)")
            self.store.update(m.id, data={"replans": {today: replans + 1}})
            decision = await self.review(prog, failures)
        deadline = m.spec.deadline if m.spec else None
        if deadline and self.rt.clock.now() > deadline:
            self.store.transition(m.id, S.ESCALATED, actor="system",
                                  reason=f"deadline passed with {prog.verified} of {prog.target} verified")
            self._say("deadline passed below target: mission ESCALATED to the finance manager")
            await self.learn()
        return {"status": self.mission.status, "progress": prog.as_dict(), "sent": sent,
                "decision": decision.model_dump() if decision else None}

    def pause_for_budget(self, budget: Budget) -> dict[str, Any]:
        self.store.transition(self.mission_id, S.PAUSED, actor="system",
                              reason=f"token budget spent ({budget.used}/{budget.max_tokens})")
        self.rt.approvals.request(
            key=f"budget:{self.mission_id}:{budget.max_tokens}", kind="decision", mission_id=self.mission_id,
            title=f"Mission paused: token budget spent ({budget.used:,}/{budget.max_tokens:,})",
            payload={"action": "budget", "used": budget.used, "max": budget.max_tokens,
                     "question": "Approve = resume with double the budget. Reject = keep it paused."})
        self._say(f"token budget spent ({budget.used}/{budget.max_tokens}); mission PAUSED for a human")
        return {"status": S.PAUSED}

    def resume(self, *, by: str, extra_tokens: int = 0) -> Mission:
        if extra_tokens:
            self.budget.raise_limit(extra_tokens)
        m = self.store.transition(self.mission_id, S.ACTIVE, actor="human", reason=f"resumed by {by}")
        self._say(f"mission resumed by {by}")
        return m

    def _is_day0_group(self, gid: str) -> bool:
        plan = self.mission.plan
        return bool(plan) and any(gid in b.groups for b in plan.batches if b.start_day == 0)

    def _worth_review(self) -> bool:
        """The agent reviews at most once a day, and only once there is something to review: a reply,
        a payment, a timer outcome, or a day without news."""
        moved = any(e.state not in self.rt.pipeline.spec.initial + ["CONTACTED"]
                    for e in self.rt.pipeline.list(self.mission_id))
        return moved or self._days_active() >= 1

    def _days_active(self) -> int:
        on = self.mission.data.get("activated_on")
        return (self._today() - date.fromisoformat(on)).days if on else 0

    async def send_due_batches(self) -> list[str]:
        m = self.mission
        done = set(m.data.get("contacted_groups", []))
        day = self._days_active()
        sent = []
        for batch in m.plan.batches if m.plan else []:
            if batch.start_day > day:
                continue
            for gid in batch.groups:
                if gid in done:
                    continue
                result = await self.contact_group(gid, batch.approach)
                if result in {"deferred", "awaiting approved wording"}:
                    continue                    # not now (quiet hours, cap, unapproved wording); retried later
                done.add(gid)
                self.store.update(m.id, data={"contacted_groups": sorted(done)})
                sent.append(f"{gid}:{result}")
        return sent

    async def contact_group(self, gid: str, approach: str, *, step: str = "outreach1") -> str:
        spec = self.mcfg["approaches"][approach]
        ents = self._open_initial(gid)
        if not ents:
            return "nothing open"
        refs = [e.external_ref for e in ents]
        first = self.mission.data.get("first_batch", {})
        prepared = first.get(gid)                         # first contact: always the approved text if there is one
        if prepared and prepared.get("refs") != refs:
            self._say(f"{gid}: invoices changed since the drafts were approved ({prepared.get('refs')} -> {refs}); "
                      "not sending the approved text")
            prepared = None
        if not prepared and self._is_day0_group(gid) and self.first_batch_needs_approval():
            self._say(f"{gid}: no approved wording for this first-batch message; not sent (awaiting approved wording)")
            return "awaiting approved wording"           # policy: first-batch wording must be approved
        extra = []
        if prepared:
            extra = [a for a in prepared.get("attachments", []) if a not in {d for e in ents
                                                                            for d in e.data.get("documents", [])}]
        elif "statement_of_account" in spec.get("documents", []):
            from apps.documents import generate
            extra.append(await generate(self.rt, self.mission_id, "statement_of_account", group=gid))
        examples = [d["body"] for d in first.values()][:2] if not prepared else None
        out = await outreach(self.rt, self.mission_id, refs, purpose=spec["purpose"], extra_documents=extra, step=step,
                             prepared=prepared, examples=examples)
        refs = ",".join(e.external_ref for e in ents)
        self._say(f"{gid} [{approach}] {refs}: " + "; ".join(out.attempts)
                  + (f" -> {sum(t.ok for t in out.transitions)}/{len(ents)} CONTACTED" if out.ok else ""))
        self.rt.events.append(Event(type="mission.group_contacted", mission_id=self.mission_id,
                                    occurred_at=self.rt.clock.now(),
                                    payload={"group": gid, "approach": approach, "refs": refs.split(","),
                                             "ok": out.ok, "channel": out.channel, "attempts": out.attempts}))
        if out.held:
            return "held"
        if out.deferred_until:
            return "deferred"
        return "sent" if out.ok else "failed"

    FAILURE_EVENTS = ["task.failed", "fallback.exhausted", "mission.group_contacted", "settlement.receipt_task",
                      "policy.deny", "message.classification_failed"]

    CHAT_EVENTS = ["entity.transitioned", "message.classified", "settlement.matched", "mission.group_contacted",
                   "security.injection_suspected", "security.fraud_suspected", "approval.requested",
                   "approval.decided", "mission.reviewed", "fallback.used", "fallback.exhausted"]

    async def chat(self, question: str, *, by: str = "manager") -> dict[str, Any]:
        """Mission chat: the Mission Agent answers from the mission's own state, with links."""
        m = self.mission
        refs = {e.id: e.external_ref for e in self.rt.pipeline.list(m.id)}
        recent = []
        for e in self.rt.events.query(mission_id=m.id, types=self.CHAT_EVENTS,
                                      since_seq=max(0, self.rt.events.last_seq(mission_id=m.id) - 600)):
            p = e.payload
            recent.append({"at": e.occurred_at.strftime("%a %d %b %H:%M"), "type": e.type,
                           "ref": refs.get(e.entity_id),
                           **{k: p[k] for k in ("from", "to", "intent", "quote", "amount", "date", "record_id",
                                                "group", "ok", "title", "status", "rung") if k in p}})
        a = await self.agent.answer(question, m.spec, today=self._today(), progress=self.progress().as_dict(),
                                    groups=self.group_rows(), recent_events=recent, budget=self.budget)
        turn = {"at": self.rt.clock.now().isoformat(), "by": by, "question": question, **a.model_dump()}
        self.store.update(m.id, data={"chat": [*m.data.get("chat", []), turn][-30:]}, event="mission.chat",
                          payload={"question": question, "answer": a.answer, "refs": a.refs})
        return turn

    def failures_since_review(self) -> list[dict[str, Any]]:
        """What went wrong since the agent last reviewed (L3 replan input). Successes are filtered out."""
        since = int(self.mission.data.get("reviewed_seq", 0))
        out = []
        for e in self.rt.events.query(mission_id=self.mission_id, types=self.FAILURE_EVENTS, since_seq=since):
            p = e.payload
            if e.type in {"mission.group_contacted", "settlement.receipt_task"} and p.get("ok", True):
                continue
            out.append({"type": e.type, "at": e.occurred_at.isoformat(),
                        **{k: p[k] for k in ("group", "refs", "stage", "error", "attempts", "what", "tool", "reason",
                                             "rule", "detail") if k in p}})
        return out[-15:]

    async def review(self, prog: Progress, failures: list[dict[str, Any]] | None = None) -> TickDecision:
        m = self.mission
        recent = m.data.get("actions", [])[-12:]
        last_seq = self.rt.events.last_seq(mission_id=m.id)
        decision = await self.agent.tick(m.spec, today=self._today(), progress=prog.as_dict(), groups=self.group_rows(),
                                         levers=self.levers(), recent_actions=recent,
                                         recent_failures=failures or [], budget=self.budget)
        self.store.update(m.id, data={"reviewed_seq": last_seq})
        remember_facts(self.rt, m.id)              # what happened so far, for chat and the next mission
        self._say(f"review: {decision.assessment}" + (f" | code dropped: {decision.problems}" if decision.problems else ""))
        taken = []
        for action in decision.actions:
            taken.append({"date": self._today().isoformat(), "lever": action.lever, "groups": action.groups,
                          "result": await self.execute(action.lever, action.groups, action.rationale)})
        self.store.update(m.id, data={"last_review": self._today().isoformat(), "actions": [*m.data.get("actions", []),
                                                                                            *taken]},
                          event="mission.reviewed", payload={"progress": prog.as_dict(),
                                                             "decision": decision.model_dump(), "taken": taken})
        return decision

    async def learn(self) -> dict[str, Any]:
        """Memory Writer pass: facts from this mission's events, then lessons. Never fails the mission."""
        try:
            out = await distill(self.rt, self.mission_id)
        except Exception as exc:  # noqa: BLE001 - learning is best effort
            self._say(f"memory: could not learn from this mission: {exc!r}")
            return {"error": repr(exc)}
        self._say(f"memory: {len(out['facts'])} fact(s), {len(out['lessons'])} lesson(s) written"
                  + (f"; dropped: {out['problems']}" if out["problems"] else ""))
        for item in out["lessons"]:
            self._say(f"  learned [{item.scope}] {item.text}")
        return out

    async def execute(self, lever: str, groups: list[str], rationale: str = "") -> str:
        spec = self.mcfg["levers"][lever]
        if spec.get("needs_approval"):
            ids = []
            for gid in groups:
                ents = [e for e in self.rt.pipeline.list(self.mission_id) if self._group_of(e) == gid
                        and e.state not in self.rt.pipeline.spec.terminal]
                if not ents:
                    continue
                a = self.rt.approvals.request(
                    key=f"{lever}:{self.mission_id}:{gid}", kind="decision", mission_id=self.mission_id,
                    entity_id=ents[0].id, title=f"{lever.replace('_', ' ')} for {gid}",
                    payload={"action": spec.get("action", lever), "lever": lever, "group": gid,
                             "entity_ids": [e.id for e in ents], "rationale": rationale,
                             "grants_phrases": spec.get("grants_phrases", []),
                             "question": f"{spec.get('description', lever)} Reason: {rationale}"})
                ids.append(a.id)
            self._say(f"{lever} for {groups}: needs approval ({', '.join(ids)})")
            return "approval requested"
        if lever == "check_bank":
            settled = await self.orch.bank_sync(force=True)
            self.log += self.orch.log
            self.orch.log.clear()
            return f"{len(settled)} settled"
        states = set(spec.get("states", []))
        results = []
        for gid in groups:
            ents = [e for e in self.rt.pipeline.list(self.mission_id) if self._group_of(e) == gid and e.state in states]
            if not ents:
                results.append(f"{gid}: nothing in {sorted(states)}")
                continue
            today = self._today().isoformat()
            if lever == "follow_up":
                out = await self.orch.follow_up(ents[0], step=f"followup:{today}")
                results.append(f"{gid}: {'sent' if out and out.ok else 'skipped'}")
            elif lever == "send_statement":
                out = await self.orch.send_documents(ents, reason="Send the customer a statement of account of all "
                                                                  "their open invoices and ask for a payment date.")
                results.append(f"{gid}: {'sent' if out.ok else 'failed'}")
            elif lever == "retry_contact":
                approach = self.mission.data.get("groups", {}).get(gid, {}).get("suggested_approach") \
                    or next(iter(self.approaches()))
                results.append(f"{gid}: {await self.contact_group(gid, approach, step=f'retry:{today}')}")
            elif lever == "re_engage":
                out = await outreach(self.rt, self.mission_id, [e.external_ref for e in ents], step="reengage1",
                                     purpose="The promised payment has not arrived. Politely ask for a new firm date.")
                results.append(f"{gid}: {'sent' if out.ok else 'failed'}")
            elif lever == "escalate":
                for e in ents:
                    tr = self.rt.pipeline.propose(e.id, "ESCALATED", {}, facts={"escalation_reason": rationale})
                    results.append(f"{e.external_ref}: {'ESCALATED' if tr.ok else tr.reason}")
                self.rt.events.append(Event(type="handoff.requested", mission_id=self.mission_id,
                                            occurred_at=self.rt.clock.now(),
                                            payload={"group": gid, "refs": [e.external_ref for e in ents],
                                                     "reason": rationale}))
        self.log += self.orch.log
        self.orch.log.clear()
        self._say(f"{lever} {groups}: " + "; ".join(results))
        return "; ".join(results)


def plan_text(m: Mission, fmt=str) -> list[str]:
    """Human-readable mission + plan, for the CLI and approval screens."""
    lines = [f"Mission {m.id}  [{m.status}]", f"  brief: {m.brief}"]
    if m.spec:
        s = m.spec
        target = "-" if s.target_value is None else (f"{s.target_value} {s.target_unit}" if s.target_unit and
                                                         s.target_unit != "paise" else fmt(s.target_value))
        deadline = f"{s.deadline:%a %d %b %Y %H:%M}" if s.deadline else "-"
        lines += [f"  objective: {s.objective}", f"  goal: {s.goal or '-'}", f"  scope: {s.scope}",
                  f"  target: {target}   deadline: {deadline}"]
        lines += [f"  success: {c}" for c in s.success_criteria]
        lines += [f"  assumption: {a}" for a in s.assumptions]
        lines += [f"  QUESTION: {q}" for q in s.open_questions]
    if m.selection:
        lines.append(f"  selection: {m.selection}")
    for x in m.data.get("excluded", []):
        lines.append(f"  excluded {x['ref']}: {x['reason']}")
    p: MissionPlan | None = m.plan
    if p:
        groups = m.data.get("groups", {})
        lines += [f"  plan: {p.summary}",
                  f"  in scope {fmt(m.data.get('scope_value', 0))}; expected if all contacted {fmt(p.expected_value or 0)}"]
        for b in p.batches:
            names = ", ".join(f"{g} {groups.get(g, {}).get('name', '')} ({len(groups.get(g, {}).get('refs', []))})"
                              for g in b.groups)
            lines.append(f"  day {b.start_day}: {b.name} [{b.approach}] {names}")
            if b.rationale:
                lines.append(f"         why: {b.rationale}")
        for x in m.data.get("prepaid") or []:
            lines.append(f"  already in the bank: {x['ref']} ({x['record_id']}, {fmt(x['amount'])}) — "
                         "settled on the first run, not chased")
        first = m.data.get("first_batch") or {}
        if first:
            lines.append("  first-batch messages (sent exactly as approved):")
            for gid, d in first.items():
                lines.append(f"  ┌ {gid} [{d['approach']}] {d['channel']} to {d['to']}: {d.get('subject') or ''}")
                lines += [f"  │ {line}" for line in d["body"].splitlines()]
                if d.get("attachments"):
                    lines.append(f"  │ attachments: {', '.join(d['attachments'])}")
                lines.append("  └")
        lines += [f"  risk: {r}" for r in p.risks]
        lines += [f"  needs a human: {h}" for h in p.human_asks]
        lines += [f"  (code corrected the plan: {x})" for x in p.problems]
    return lines
