"""The live check script itself, run offline with scripted model answers (so it cannot rot unnoticed)."""

from __future__ import annotations

import asyncio
import json
import re

import apps.live_check as live
from tests.apps.test_mission import _untrusted
from tests.conftest import SANDBOX_SECRETS
from tests.fakes import ScriptedLLM
from tools.vault import Vault


def answer(messages):
    system, payload = messages[0]["content"], messages[1]["content"]
    if "TASK: understand" in system:
        brief = json.loads(payload)["BRIEF"]
        if "statements" in brief:
            return {"objective": "o", "goal": "confirm_balance", "scope": "s", "selection": {"min_group_value": "₹1L"}}
        return {"objective": "o", "goal": "collect", "scope": "s", "target_text": "₹12L", "deadline": "2026-10-23",
                "selection": {"min_days_overdue": 45}}
    if "You classify one message" in system:
        text = _untrusted(messages)
        intent = ("balance_confirmed" if "We confirm the balance" in text else "paid_claim" if "already paid" in text
                  else "dispute" if "cannot pay" in text else "promise_to_pay")
        amount = re.search(r"₹[\d,]+\.\d{2}", text)
        quote = re.split(r"(?<=[.!?])\s+", text)[1]
        return {"intent": intent, "confidence": 0.9, "about_entity": True, "entity_refs": [], "quote": quote,
                "amount_text": amount.group(0) if amount else None,
                "date": "2026-10-21" if intent == "promise_to_pay" else None}
    if "Conversation Agent" in system:
        p = json.loads(payload)
        body = {"Balance confirmation": "Please confirm the balance on the attached statement.",
                "The customer said": "Please share the UTR or remittance advice.",
                "Thank the customer": "When will the remaining balance be paid?",
                "The customer disputes": "Our accounts team is reviewing it."}
        text = next(v for k, v in body.items() if p["PURPOSE"].startswith(k))
        return {"subject": "Re: your account", "body": text, "attachments": p["AVAILABLE_DOCUMENTS"], "facts_used": []}
    if system.startswith("You are the Memory Writer"):
        return {"lessons": [{"scope": "customer:C12", "text": "Email bounces; use WhatsApp.", "cites": ["cp:C12"]}]}
    raise AssertionError(system[:60])


def test_live_check_runs_end_to_end_offline(monkeypatch, tmp_path, capsys):
    real = live.build_runtime
    scripted = ScriptedLLM([answer] * 50, guard_scores={"SYSTEM:": 0.9})
    monkeypatch.setattr(live, "build_runtime", lambda **kw: real(var_dir=tmp_path, llm=scripted,
                                                                  vault=Vault({**SANDBOX_SECRETS, "GROQ_API_KEY": "test"})))
    assert asyncio.run(live.main()) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "0 fail" in out and "FAIL" not in out.replace("0 fail", "")
