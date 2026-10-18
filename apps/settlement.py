"""From money in the bank to a verified, settled entity.

    statement CSV  ->  records  ->  matcher  ->  ledger lock  ->  receipt via Operator (ERP web)
                   ->  Verifier re-observes (fresh bank session + ERP API)  ->  guarded transition

Everything that decides "is this settled?" is code. The LLM only drives the
ERP's web form to post the receipt, and its work is checked afterwards.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from apps.flows import _read, role_cfg, run_task
from apps.runtime import Runtime
from core.models import CriterionResult, Entity, Event, Evidence, FailureReport, VerificationReport
from core.pipeline import TransitionResult
from subagents.matcher import Candidate, Match, Record, match
from tools.app_map import load_app_maps
from tools.base import RunContext

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    record_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('MATCHED', 'POSTED', 'VERIFIED', 'FAILED')),
    receipt_id TEXT,
    PRIMARY KEY (record_id, entity_id)
);
"""


# --- statement parsing ---------------------------------------------------------------------

class StatementFormatError(ValueError):
    pass


def parse_statement(path: str, mapping: dict[str, Any]) -> list[Record]:
    """CSV -> records, using the app map's column names (with alternatives for format changes)."""
    rows = list(csv.reader(io.StringIO(open(path, encoding="utf-8").read())))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]

    def col(field: str) -> int:
        for name in mapping[field]["columns"]:
            if name in header:
                return header.index(name)
        raise StatementFormatError(f"none of {mapping[field]['columns']} in CSV header {header}")

    i_date, i_text, i_ref, i_credit = col("date"), col("text"), col("ref"), col("credit")
    fmt = mapping["date"].get("format", "%Y-%m-%d")
    out = []
    for r in rows[1:]:
        if len(r) < len(header) or not r[i_credit].strip():
            continue
        amount = int((Decimal(r[i_credit].replace(",", "")) * 100).to_integral_value())
        out.append(Record(id=r[i_ref].strip(), on=datetime.strptime(r[i_date].strip(), fmt).date(),
                          text=r[i_text].strip(), amount=amount))
    return out


# --- ledger -----------------------------------------------------------------------------------

def ledger_applied(rt: Runtime) -> set[str]:
    rt.db.ensure_schema(LEDGER_SCHEMA)
    with rt.db.read() as conn:
        return {r["record_id"] for r in conn.execute("SELECT record_id FROM settlements WHERE status != 'FAILED'")}


def ledger_set(rt: Runtime, m: Match, status: str, receipt_id: str | None = None) -> None:
    rt.db.ensure_schema(LEDGER_SCHEMA)
    with rt.db.transaction() as conn:
        conn.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?) ON CONFLICT(record_id, entity_id) "
                     "DO UPDATE SET status = excluded.status, receipt_id = COALESCE(excluded.receipt_id, receipt_id)",
                     (m.record_id, m.entity_id, m.matched_amount, m.kind, status, receipt_id))


# --- steps --------------------------------------------------------------------------------------

async def download_statement(rt: Runtime, mission_id: str, start: date, end: date) -> tuple[str | None, Any]:
    res = await run_task(rt, mission_id, "download_statement",
                         goal=f"Download the bank account statement as CSV for {start.isoformat()} to "
                              f"{end.isoformat()} from the bank portal.",
                         success_criteria=["a CSV file covering the date range is saved"])
    if isinstance(res, FailureReport):
        return None, res
    for out in res.outputs.values():
        if isinstance(out, dict) and str(out.get("path", "")).endswith(".csv"):
            return out["path"], res
    return None, res


def candidates(rt: Runtime, mission_id: str) -> list[tuple[Entity, Candidate]]:
    cfg = role_cfg(rt)["reconciliation"]
    out = []
    for state in cfg["states"]:
        for e in rt.pipeline.list(mission_id, state):
            record = e.data.get("record", {})
            due = record.get(cfg.get("order_field", ""))
            out.append((e, Candidate(e.id, e.external_ref, record.get(cfg["counterparty_field"], ""),
                                     int(e.data.get("balance", e.value)),
                                     date.fromisoformat(due) if isinstance(due, str) else None)))
    return out


def _mode(record_text: str) -> str:
    head = record_text.split("/", 1)[0].upper()
    return head if head in {"NEFT", "RTGS", "IMPS", "UPI"} else "NEFT"


async def receipts_for(rt: Runtime, mission_id: str, ref: str) -> list[dict]:
    return await _read(rt, "erp.list_receipts", {"invoice": ref}, mission_id)


async def post_receipt(rt: Runtime, mission_id: str, entity: Entity, m: Match) -> dict | None:
    """Ask the Operator to post the receipt in the ERP web UI, unless it already exists. Returns the receipt."""
    existing = [r for r in await receipts_for(rt, mission_id, entity.external_ref) if r["reference"] == m.record_id]
    if not existing:
        spec = role_cfg(rt)["task_types"]["post_receipt"]
        goal = spec["goal_template"].format(ref=entity.external_ref, amount=f"{m.matched_amount / 100:.2f}",
                                            date=m.record_on.isoformat(), reference=m.record_id,
                                            mode=_mode(m.record_text))
        res = await run_task(rt, mission_id, "post_receipt", goal=goal, entity_id=entity.id,
                             success_criteria=spec.get("success_criteria", []))
        rt.events.append(Event(type="settlement.receipt_task", mission_id=mission_id, entity_id=entity.id,
                               occurred_at=rt.clock.now(),
                               payload={"ok": not isinstance(res, FailureReport), "record_id": m.record_id,
                                        "detail": res.error if isinstance(res, FailureReport) else res.summary}))
        existing = [r for r in await receipts_for(rt, mission_id, entity.external_ref) if r["reference"] == m.record_id]
    return existing[0] if existing else None


async def fresh_statement_has(rt: Runtime, mission_id: str, m: Match) -> tuple[bool, Evidence | None, str]:
    """Verifier's own observation: new browser session, scripted (no LLM), statement for the record's date."""
    task_id = f"verify-{m.entity_id}-{m.record_id}"
    ctx = RunContext(mission_id=mission_id, task_id=task_id, entity_id=m.entity_id, allowed_apps={"bank_portal"},
                     artifacts_dir=rt.artifacts_dir)
    try:
        day = m.record_on.isoformat()
        opened = await rt.registry.invoke("bank_portal.open", {"path": f"/statement?from_date={day}&to_date={day}"}, ctx)
        if not opened.ok:
            return False, None, f"could not open statement: {opened.error}"
        link = next((e for e in opened.output["elements"] if e["name"] == "Download CSV"), None)
        if link is None:
            return False, None, "no CSV link on the statement page"
        dl = await rt.registry.invoke("bank_portal.download", {"ref": link["ref"]}, ctx)
        if not dl.ok:
            return False, None, f"download failed: {dl.error}"
        mapping = load_app_maps(rt.company_dir / "apps")["bank_portal"].exports["statement_csv"]
        rec = next((r for r in parse_statement(dl.output["path"], mapping) if r.id == m.record_id), None)
        ok = rec is not None and rec.amount >= m.matched_amount
        return ok, dl.evidence[0], "record found with sufficient amount" if ok else "record not in fresh statement"
    finally:
        await rt.sessions.end_task(task_id)


async def verify(rt: Runtime, mission_id: str, entity: Entity, m: Match, receipt: dict | None) -> VerificationReport:
    """Deterministic checks against fresh observations of the systems of record."""
    criteria: list[CriterionResult] = []
    in_bank, ev, note = await fresh_statement_has(rt, mission_id, m)
    criteria.append(CriterionResult(criterion="money is in the bank (fresh session)", passed=in_bank,
                                    evidence=[ev] if ev else [], note=note))
    rec_ok = bool(receipt) and receipt["reference"] == m.record_id and receipt["amount_paise"] == m.matched_amount
    criteria.append(CriterionResult(criterion="receipt posted with this reference and amount", passed=rec_ok,
                                    evidence=[Evidence(kind="record", ref=f"erp:{receipt['id']}",
                                                       summary=f"{receipt['id']} {receipt['reference']}")] if receipt else [],
                                    note=None if rec_ok else f"receipt={receipt}"))
    inv = await _read(rt, "erp.get_invoice", {"number": entity.external_ref}, mission_id)
    expected = int(entity.data.get("balance", entity.value)) - m.matched_amount
    bal_ok = inv["balance_paise"] == max(expected, 0)
    criteria.append(CriterionResult(criterion="system of record balance reflects the settlement", passed=bal_ok,
                                    note=f"balance {inv['balance_paise']} expected {max(expected, 0)}"))
    passed = all(c.passed for c in criteria)
    report = VerificationReport(subject_id=entity.id, passed=passed, criteria=criteria, confidence=1.0 if passed else 0.0)
    rt.events.append(Event(type="verification.completed", mission_id=mission_id, entity_id=entity.id,
                           occurred_at=rt.clock.now(), payload=report.model_dump(mode="json")))
    return report


@dataclass
class Settled:
    entity_ref: str
    match: Match
    receipt: dict | None
    report: VerificationReport | None
    transition: TransitionResult | None


async def settle(rt: Runtime, mission_id: str, statement_path: str) -> list[Settled]:
    """Match a downloaded statement and carry every match through receipt, verification and transition."""
    mapping = load_app_maps(rt.company_dir / "apps")["bank_portal"].exports["statement_csv"]
    records = parse_statement(statement_path, mapping)
    pairs = candidates(rt, mission_id)
    by_id = {e.id: e for e, _ in pairs}
    tol = rt.pipeline.spec.constants.get("tolerance", 0)
    results = []
    for m in match(records, [c for _, c in pairs], tolerance=tol, applied=ledger_applied(rt)):
        entity = by_id[m.entity_id]
        ledger_set(rt, m, "MATCHED")
        rt.events.append(Event(type="settlement.matched", mission_id=mission_id, entity_id=entity.id,
                               occurred_at=rt.clock.now(),
                               payload={"record_id": m.record_id, "amount": m.matched_amount, "kind": m.kind,
                                        "text": m.record_text, "reasons": m.reasons}))
        receipt = await post_receipt(rt, mission_id, entity, m)
        if receipt is None:
            ledger_set(rt, m, "FAILED")
            results.append(Settled(entity.external_ref, m, None, None, None))
            continue
        ledger_set(rt, m, "POSTED", receipt["id"])
        report = await verify(rt, mission_id, entity, m, receipt)
        ctx = {**m.guard_context(), "erp": {"receipt_exists": True, "receipt_id": receipt["id"]},
               "verifier": {"pass": report.passed}}
        balance = int(entity.data.get("balance", entity.value))
        target = "PAID" if m.matched_amount >= balance - tol else "PARTIALLY_PAID"
        tr = rt.pipeline.propose(entity.id, target, ctx, facts={
            "settlement": {"record_id": m.record_id, "amount": m.matched_amount, "receipt_id": receipt["id"],
                           "on": m.record_on.isoformat()}})
        if tr.ok:
            ledger_set(rt, m, "VERIFIED")
            remaining = max(0, int(entity.data.get("balance", entity.value)) - m.matched_amount)
            rt.pipeline.update_data(entity.id, {"balance": remaining})
        results.append(Settled(entity.external_ref, m, receipt, report, tr))
    return results


def lookback(rt: Runtime) -> tuple[date, date]:
    today = rt.clock.now().date()
    return today - timedelta(days=role_cfg(rt)["reconciliation"].get("lookback_days", 21)), today

