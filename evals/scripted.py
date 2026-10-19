"""A deterministic stand-in for the models, so evals run offline (no key, same answer every time).

It answers every agent the way a competent but plain model would: it reads the brief literally, plans
every customer on day 0 with the prioritizer's suggestion, follows up when the cadence allows,
classifies replies by their wording, drafts one plain message per purpose, operates the bank portal
and the ERP form by their labels, and writes lessons only from facts it is shown.

What an offline eval measures is therefore the RUNTIME (selection, guards, matching, settlement,
policy, flows, recovery, memory), not model quality. `run_evals --live` uses the real models.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from core.llm import LLMResponse
from tests.fakes import ScriptedLLM, call

AMOUNT = re.compile(r"₹\s?[\d,]+(?:\.\d{2})?")


def _payload(messages) -> dict[str, Any]:
    try:
        return json.loads(messages[1]["content"])
    except (ValueError, IndexError, KeyError):
        return {}


def _untrusted(messages) -> str:
    text = next((m["content"] for m in reversed(messages) if "<untrusted_data>" in str(m.get("content"))), "")
    return text.replace("<untrusted_data>", "").replace("</untrusted_data>", "").strip()


def _sentence(text: str, needle: str) -> str:
    return next((s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if needle.lower() in s.lower()), "")


# --- Mission Agent --------------------------------------------------------------------------------

def understand(p: dict[str, Any]) -> dict[str, Any]:
    brief = p["BRIEF"]
    today = date.fromisoformat(p["TODAY"].split()[0])
    goals = p.get("GOALS") or {}
    confirm = "confirm" in brief.lower() and "confirm_balance" in goals
    selection: dict[str, Any] = {}
    m = re.search(r"(\d+)\+?\s*days?|(\d+)\+", brief)
    if m and not confirm:
        selection["min_days_overdue"] = int(m.group(1) or m.group(2))
    over = re.search(r"(?:over|above|more than)\s+(₹\s?[\d.,]+\s*(?:L|lakh|Cr|k)?)", brief, re.I)
    if over:
        selection["min_group_value"] = over.group(1).strip()
    cust = re.search(r"\b(C\d{2})\b", brief)
    if cust:
        selection["customer_id"] = cust.group(1)
    target = None if confirm else (AMOUNT.search(brief) and re.search(r"₹\s?[\d.,]+\s*(?:L|lakh|Cr|k)?", brief).group(0).strip())
    deadline = None
    days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
    for name, wd in days.items():
        if name in brief.lower() or ("end of week" in brief.lower() and name == "friday"):
            deadline = (today + timedelta(days=(wd - today.weekday()) % 7 or 7)).isoformat()
    return {"objective": brief[:120], "goal": "confirm_balance" if confirm else "collect",
            "scope": "as the brief says, within the credit policy", "target_text": target, "deadline": deadline,
            "selection": selection, "success_criteria": [], "assumptions": [], "open_questions": []}


def plan(p: dict[str, Any]) -> dict[str, Any]:
    by: dict[str, list[str]] = {}
    for g in p["GROUPS"]:
        by.setdefault(g.get("suggested_approach") or next(iter(p["APPROACHES"])), []).append(g["group"])
    return {"summary": "Largest and oldest first; everyone contacted as soon as capacity allows.", "risks": [],
            "human_asks": [], "batches": [{"name": f"{a} batch", "groups": gs, "approach": a, "start_day": 0,
                                           "rationale": "ranked by the prioritizer"} for a, gs in by.items()]}


def review(p: dict[str, Any]) -> dict[str, Any]:
    levers = p.get("LEVERS", {})
    actions = []
    for g in p["GROUPS"]:
        states, since = set(g.get("states", [])), g.get("days_since_contact")
        if "escalate" in levers and states & {"DISPUTED", "NO_RESPONSE"}:
            actions.append({"lever": "escalate", "groups": [g["group"]], "rationale": "needs a human"})
        elif "follow_up" in levers and "CONTACTED" in states and since is not None and since >= 3:
            actions.append({"lever": "follow_up", "groups": [g["group"]], "rationale": "no reply yet"})
        elif "retry_contact" in levers and g.get("first_contact_failed"):
            actions.append({"lever": "retry_contact", "groups": [g["group"]], "rationale": "first contact failed"})
    return {"assessment": f"verified {p['PROGRESS'].get('verified')} of {p['PROGRESS'].get('target')}.",
            "actions": actions}


# --- Conversation Agent ---------------------------------------------------------------------------

def draft(p: dict[str, Any]) -> dict[str, Any]:
    purpose, facts = p["PURPOSE"], p["COMPANY_FACTS"]
    rules = [
        ("bank details", f"Our bank details have not changed and never change by message. Please pay only to "
                         f"{facts.get('bank_account_name')}, A/c {facts.get('bank_account_number')}, IFSC "
                         f"{facts.get('bank_ifsc')}. Our team will call you to confirm."),
        ("Balance confirmation", "Please find the statement of account attached. Kindly check it against your books "
                                 "and confirm the balance, or tell us which items differ."),
        ("bank reference (UTR)", "Thank you. We could not find this payment in our bank yet. Could you share the "
                                 "payment date, amount and the UTR or a remittance advice so we can trace it?"),
        ("remaining balance", "Thank you for the payment received. When will the remaining balance be paid?"),
        ("disputes the invoice", "Thank you for letting us know. Our accounts team is reviewing it against the signed "
                                 "delivery note and will reply within 2 working days."),
        ("approved the concession", "The finance manager approved the discount you asked for. Please confirm the "
                                    "payment date."),
        ("cannot offer a discount", "We are unable to offer a discount on this invoice. Please share a payment date for "
                                    "the full balance."),
        ("statement of account", "Please find the statement of account attached. Please share a payment date."),
        ("one day before", "A friendly reminder of the payment you committed to for tomorrow."),
        ("Follow-up: we have not heard back about the statement", "Following up on the statement of account we sent: "
                                                                  "could you confirm the balance?"),
    ]
    body = next((text for needle, text in rules if needle.lower() in purpose.lower()),
                "Please share a payment date for the invoice(s) listed.")
    email = p["CHANNEL"] == "email"
    return {"subject": "Your account with us" if email else None, "body": body,
            "attachments": p.get("AVAILABLE_DOCUMENTS", []), "facts_used": []}


# --- Reply Classifier -------------------------------------------------------------------------------

def classify(messages) -> dict[str, Any]:
    text = _untrusted(messages)
    p = _payload(messages)
    rules = [("we confirm the balance", "balance_confirmed"), ("cannot pay", "dispute"), ("already paid", "paid_claim"),
             ("% off", "discount_request"), ("bank account has changed", "bank_detail_change"),
             ("account has changed", "bank_detail_change"), ("transfer", "promise_to_pay")]
    for needle, intent in rules:
        if needle in text.lower():
            out = {"intent": intent, "confidence": 0.95, "about_entity": True, "entity_refs": [],
                   "quote": _sentence(text, needle)}
            amount = AMOUNT.search(text)
            if intent in {"promise_to_pay", "balance_confirmed"} and amount:
                out["amount_text"] = amount.group(0)
            if intent == "promise_to_pay":
                d = re.search(r"\d{1,2} \w{3} \d{4}", text)
                if d:
                    out["date"] = datetime.strptime(d.group(0), "%d %b %Y").date().isoformat()
                out["entity_refs"] = re.findall(r"INV-\d{4}", text)
            return out
    refs = [e["ref"] for e in p.get("ENTITIES", [])]
    return {"intent": "unclear", "confidence": 0.3, "about_entity": bool(refs), "entity_refs": [], "quote": ""}


# --- Operator ----------------------------------------------------------------------------------------

def _obs(messages):
    content = [m for m in messages if m["role"] == "tool"][-1]["content"]
    return json.loads(content.replace("<untrusted_data>", "").replace("</untrusted_data>", ""))["output"]


def _ref(messages, *names):
    elements = _obs(messages).get("elements", [])
    for name in names:
        hit = next((e["ref"] for e in elements if e.get("name") == name), None)
        if hit:
            return hit
    raise LookupError(f"none of {names} on the page")


def operator(messages):
    goal = _payload(messages)["task"]
    step = sum(1 for m in messages if m["role"] == "assistant")
    tag = f"t{abs(hash(goal)) % 10**8}"

    def act(name, args):
        return LLMResponse(None, [call(name, args, f"{tag}-{step}")], {"total_tokens": 300})

    def fail(reason):
        return LLMResponse(None, [call("fail_task", {"reason": reason}, f"{tag}-{step}")], {"total_tokens": 100})

    try:
        if "bank account statement" in goal:
            start, end = re.findall(r"\d{4}-\d{2}-\d{2}", goal)[:2]
            return [lambda: act("bank_portal__open", {"path": f"/statement?from_date={start}&to_date={end}"}),
                    lambda: act("bank_portal__download", {"ref": _ref(messages, "Download CSV")}),
                    lambda: act("finish_task", {"summary": "statement saved", "result_call_ids": [f"{tag}-1"]}),
                    lambda: fail("download did not produce a statement")][min(step, 3)]()
        m = re.search(r"invoice (INV-\d+): amount ([\d.]+), receipt date ([\d-]+), payment mode (\w+), "
                      r"bank reference / UTR (\w+)", goal)
        if not m:
            return fail("task not understood")
        ref, amount, day, _, utr = m.groups()
        return [lambda: act("erp_web__open", {"path": f"/invoices/{ref}/receipts/new"}),
                lambda: act("erp_web__fill", {"ref": _ref(messages, "Receipt date"), "value": day}),
                lambda: act("erp_web__fill", {"ref": _ref(messages, "Amount (₹)"), "value": amount}),
                lambda: act("erp_web__fill", {"ref": _ref(messages, "Bank reference / UTR"), "value": utr}),
                lambda: act("erp_web__click", {"ref": _ref(messages, "Post receipt")}),
                lambda: act("finish_task", {"summary": "receipt posted", "result_call_ids": [f"{tag}-4"]}),
                lambda: fail("form did not accept the receipt")][min(step, 6)]()
    except (LookupError, IndexError, KeyError, ValueError) as exc:   # the page is not what this script knows
        return fail(f"scripted operator cannot handle this page: {exc}")


# --- Memory Writer -------------------------------------------------------------------------------------

def memory_writer(messages) -> dict[str, Any]:
    episodes = json.loads(_untrusted(messages)).get("EPISODES", [])
    lessons = []
    for ep in episodes:
        f = ep.get("facts") or {}
        if "email" in f.get("dead_channels", []):
            lessons.append({"scope": ep["scope"], "text": "Email does not reach this customer; WhatsApp does.",
                            "cites": [ep["id"]]})
        late = f.get("days_late") or []
        if late and sum(late) / len(late) >= 2:
            lessons.append({"scope": ep["scope"], "text": f"Pays about {round(sum(late) / len(late))} days after the "
                            "date they promise.", "cites": [ep["id"]]})
    return {"lessons": lessons}


# --- router ----------------------------------------------------------------------------------------------

def respond(messages):
    system = messages[0]["content"]
    if system.startswith("You are the Operator"):
        return operator(messages)
    if system.startswith("You are the Memory Writer"):
        return memory_writer(messages)
    if "You classify one message" in system:
        return classify(messages)
    if system.startswith("You are the Verifier"):
        return {"passed": True, "confidence": 0.9, "reason": "firm amount and date"}
    p = _payload(messages)
    if "TASK: understand" in system:
        return understand(p)
    if "TASK: plan" in system:
        return plan(p)
    if "TASK: review" in system:
        return review(p)
    if "TASK: answer" in system:
        return {"answer": "See the board.", "refs": [], "groups": [], "confidence": 0.5}
    if "Conversation Agent" in system:
        return draft(p)
    raise AssertionError(f"scripted brain: unexpected call {system[:80]!r}")


def scripted_llm(n: int = 5000) -> ScriptedLLM:
    return ScriptedLLM([respond] * n)
