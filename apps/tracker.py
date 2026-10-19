"""Keep the team's own tracker in sync with what Arya verified (company.yaml `trackers`).

After every orchestration pass, each entity whose state, balance or promised date changed since the
last sync gets its row updated: status in the team's own words (sheet map `values.status`), balance,
promised date, a one-line last action written by code (never a counterparty's words), and a stamp.
Only the sheet map's writable columns can be written; people's columns are untouched.

The workbook may be open on someone's desktop: then nothing is written (a person's unsaved edits win),
the rows stay pending and are retried on the next pass, and once a day a human is asked to close it.
Driving the open app on screen would need a vision model; this runtime's models are text-only.
"""

from __future__ import annotations

from typing import Any

from apps.runtime import Runtime
from core.models import Entity, Event
from tools.base import RunContext
from tools.sheets import load_sheet_maps


def _rupees(paise: int) -> float:
    return round(int(paise) / 100, 2)


def row_values(rt: Runtime, e: Entity, status: dict[str, str]) -> dict[str, Any]:
    facts = e.data.get("facts", {})
    balance = 0 if e.state == "PAID" else int(e.data.get("balance", e.value))
    promised = facts.get("promise_date") if e.state in {"PROMISED", "BROKEN_PROMISE"} else ""
    last = e.data.get("last_contact") or {}
    action = {
        "PAID": lambda: f"Paid in full: bank ref {(facts.get('settlement') or {}).get('record_id')}, "
                        f"receipt {(facts.get('settlement') or {}).get('receipt_id')} (verified)",
        "PARTIALLY_PAID": lambda: f"Part payment {_rupees((facts.get('settlement') or {}).get('amount', 0)):,.2f} "
                                  f"received (bank ref {(facts.get('settlement') or {}).get('record_id')}); balance asked",
        "PROMISED": lambda: f"Customer promised {_rupees(facts.get('promise_amount', 0)):,.2f} by {promised} (in writing)",
        "BROKEN_PROMISE": lambda: f"Promised date {promised} passed without payment",
        "CONFIRMED": lambda: "Customer confirmed the balance in writing",
        "DISPUTED": lambda: "Customer disputes the invoice; with the team, documents attached",
        "ESCALATED": lambda: f"Handed to the team: {facts.get('escalation_reason') or 'needs a person'}",
        "NO_RESPONSE": lambda: "No reply after follow-ups",
        "CONTACTED": lambda: f"Reminder sent by {last.get('channel', 'message')}",
        "ACKNOWLEDGED": lambda: "Customer replied",
    }.get(e.state, lambda: "")()
    return {"Status": status.get(e.state, e.state), "Balance (₹)": _rupees(balance), "Promised on": promised or "",
            "Last action": action[:200], "Updated": f"{rt.clock.now():%d-%b-%Y} by Arya"}


def _snapshot(e: Entity) -> dict[str, Any]:
    return {"state": e.state, "balance": e.data.get("balance", e.value),
            "promise_date": e.data.get("facts", {}).get("promise_date")}


async def sync_trackers(rt: Runtime, mission_id: str) -> dict[str, Any]:
    """Returns {"updated": [...refs], "pending": [...refs], "error": str|None} summed over trackers."""
    out: dict[str, Any] = {"updated": [], "pending": [], "error": None}
    sheets = load_sheet_maps(rt.company_dir / "sheets")
    for spec in rt.company_cfg.get("trackers", []):
        sheet = sheets.get(spec["sheet"])
        if sheet is None:
            continue
        status = sheet.values.get("status", {})
        ctx = RunContext(mission_id=mission_id, task_id=f"tracker-{sheet.name}", allowed_apps={sheet.name},
                         artifacts_dir=rt.artifacts_dir)
        for e in rt.pipeline.list(mission_id):
            if (e.data.get("tracker") or {}).get(sheet.name) == _snapshot(e):
                continue
            res = await rt.registry.invoke(f"{sheet.name}.update_row",
                                           {"key": e.external_ref, "values": row_values(rt, e, status)}, ctx)
            if res.ok and res.verified is not False:
                rt.pipeline.update_data(e.id, {"tracker": {**(e.data.get("tracker") or {}), sheet.name: _snapshot(e)}})
                if rt.evidence:
                    rt.evidence.collect(res.evidence, subject=f"tracker:{sheet.name}", mission_id=mission_id,
                                        entity_id=e.id)
                out["updated"].append(e.external_ref)
            else:
                out["pending"].append(e.external_ref)
                out["error"] = res.error or "read-back did not match"
                if res.error_kind == "transient":
                    break                       # the workbook is busy: everything else waits too
        if out["pending"]:
            rt.events.append(Event(type="tracker.sync_pending", mission_id=mission_id, occurred_at=rt.clock.now(),
                                   payload={"sheet": sheet.name, "error": out["error"], "pending": out["pending"]}))
    return out
