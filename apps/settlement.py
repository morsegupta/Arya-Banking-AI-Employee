"""From money in the bank to a verified, settled entity.

    statement CSV  ->  records  ->  matcher  ->  ledger lock  ->  receipt via Operator (ERP web)
                   ->  Verifier re-observes (fresh bank session + ERP API)  ->  guarded transition

Everything that decides "is this settled?" is code. The LLM only drives the
ERP's web form to post the receipt, and its work is checked afterwards.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from agents.verifier_agent import VerifierAgent
from apps.flows import _read, role_cfg, run_task
from apps.runtime import Runtime
from core.models import CriterionResult, Entity, Event, Evidence, FailureReport, VerificationReport
from core.pipeline import TransitionResult
from subagents.matcher import Candidate, Match, Record, match
from apps.playbooks import run_playbook
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
    record_on TEXT,
    record_text TEXT,
    source TEXT,
    PRIMARY KEY (record_id, entity_id)
);
"""
# Columns added after Day 2 midday: older databases get them on first use.
LEDGER_UPGRADES = ["ALTER TABLE settlements ADD COLUMN record_on TEXT",
                   "ALTER TABLE settlements ADD COLUMN record_text TEXT",
                   "ALTER TABLE settlements ADD COLUMN source TEXT"]


# --- statement parsing ---------------------------------------------------------------------

class StatementFormatError(ValueError):
    pass


def _amount(cell: str) -> int:
    """'₹1,12,000.00', '112000.00', '1,12,000' -> paise."""
    return int((Decimal(re.sub(r"[^\d.]", "", cell)) * 100).to_integral_value())


def _date(cell: str, formats: str | list[str]) -> date:
    for fmt in [formats] if isinstance(formats, str) else formats:
        try:
            return datetime.strptime(cell.strip(), fmt).date()
        except ValueError:
            continue
    raise StatementFormatError(f"date {cell!r} matches none of {formats}")


def parse_rows(header: list[str], rows: list[list[str]], mapping: dict[str, Any], source: str = "statement") -> list[Record]:
    """Rows -> incoming money records, using the app map's column names (alternatives cover format changes)."""
    header = [h.strip() for h in header]

    def col(field: str) -> int:
        for name in mapping[field]["columns"]:
            if name in header:
                return header.index(name)
        raise StatementFormatError(f"none of {mapping[field]['columns']} in {source} header {header}")

    i_date, i_text, i_ref, i_credit = col("date"), col("text"), col("ref"), col("credit")
    formats = mapping["date"].get("format", "%Y-%m-%d")
    out = []
    for r in rows:
        if len(r) < len(header) or not re.search(r"\d", r[i_credit]):
            continue
        try:
            amount = _amount(r[i_credit])
        except InvalidOperation as exc:
            raise StatementFormatError(f"unreadable amount {r[i_credit]!r} in {source}") from exc
        out.append(Record(id=r[i_ref].strip(), on=_date(r[i_date], formats), text=r[i_text].strip(), amount=amount))
    return out


def parse_statement(path: str, mapping: dict[str, Any]) -> list[Record]:
    """CSV file -> records."""
    rows = list(csv.reader(io.StringIO(open(path, encoding="utf-8").read())))
    return parse_rows(rows[0], rows[1:], mapping, "CSV") if rows else []


# --- ledger -----------------------------------------------------------------------------------

def ensure_ledger(rt: Runtime) -> None:
    rt.db.ensure_schema(LEDGER_SCHEMA)
    with rt.db.read() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(settlements)")}
    for stmt in LEDGER_UPGRADES:
        if stmt.split("ADD COLUMN ")[1].split()[0] not in cols:
            with rt.db.transaction() as conn:
                conn.execute(stmt)


def ledger_applied(rt: Runtime) -> set[str]:
    ensure_ledger(rt)
    with rt.db.read() as conn:
        return {r["record_id"] for r in conn.execute("SELECT record_id FROM settlements WHERE status != 'FAILED'")}


def ledger_set(rt: Runtime, m: Match, status: str, receipt_id: str | None = None, source: str | None = None) -> None:
    ensure_ledger(rt)
    with rt.db.transaction() as conn:
        conn.execute("INSERT INTO settlements (record_id, entity_id, amount, kind, status, receipt_id, record_on, "
                     "record_text, source) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id, entity_id) "
                     "DO UPDATE SET status = excluded.status, receipt_id = COALESCE(excluded.receipt_id, receipt_id)",
                     (m.record_id, m.entity_id, m.matched_amount, m.kind, status, receipt_id,
                      m.record_on.isoformat(), m.record_text, source))


def ledger_pending(rt: Runtime, mission_id: str) -> list[tuple[Match, str]]:
    """Matches a crash left half-way (MATCHED or POSTED): money we saw but have not finished settling."""
    ensure_ledger(rt)
    with rt.db.read() as conn:
        rows = conn.execute("SELECT s.* FROM settlements s JOIN entities e ON e.id = s.entity_id "
                            "WHERE e.mission_id = ? AND s.status IN ('MATCHED', 'POSTED') AND s.record_on IS NOT NULL",
                            (mission_id,)).fetchall()
    return [(Match(r["entity_id"], r["record_id"], date.fromisoformat(r["record_on"]), r["amount"], True,
                   r["kind"].startswith("reference"), r["kind"], r["record_text"] or "", ["resumed after restart"]),
             r["source"] or "") for r in rows]


# --- steps --------------------------------------------------------------------------------------

@dataclass
class StatementRead:
    records: list[Record] | None
    source: str                       # path of the file, or "screen:<url>"
    rung: str                         # which step of the ladder produced it
    attempts: list[str]


async def statement_records(rt: Runtime, mission_id: str, start: date, end: date) -> StatementRead:
    """Bank statement for a date range, by the cheapest way that works (reliability ladder L2):
        1. playbook download (scripted, no LLM)    2. Operator (LLM adapts to a changed portal)
        3. playbook reading the on-screen table    4. give up -> the caller escalates to a human
    A CSV whose columns no longer match counts as a failed rung, so the next one is tried."""
    maps = load_app_maps(rt.company_dir / "apps")["bank_portal"].exports
    values = {"start": start.isoformat(), "end": end.isoformat()}
    stamp = rt.clock.now().strftime("%Y%m%d%H%M%S")
    attempts: list[str] = []

    def used(rung: str, read: StatementRead) -> StatementRead:
        rt.events.append(Event(type="fallback.used" if rung != "playbook" else "statement.read", mission_id=mission_id,
                               occurred_at=rt.clock.now(), payload={"what": "bank statement", "rung": rung,
                                                                    "records": len(read.records or []),
                                                                    "attempts": attempts}))
        return read

    pb = await run_playbook(rt, "bank_portal", "download_statement", values, mission_id=mission_id,
                            task_id=f"pb-statement-{stamp}")
    if pb.ok and pb.files:
        try:
            return used("playbook", StatementRead(parse_statement(pb.files[0], maps["statement_csv"]), pb.files[0],
                                                  "playbook", attempts))
        except StatementFormatError as exc:
            attempts.append(f"playbook download: {exc}")
    else:
        attempts.append(f"playbook download: {pb.error}")

    res = await run_task(rt, mission_id, "download_statement",
                         goal=f"Download the bank account statement as CSV for {start.isoformat()} to "
                              f"{end.isoformat()} from the bank portal.",
                         success_criteria=["a CSV file covering the date range is saved"],
                         context={"why": "the scripted download failed: " + "; ".join(attempts)})
    path = None
    if isinstance(res, FailureReport):
        attempts.append(f"operator: {res.stage}: {res.error}")
    else:
        path = next((o["path"] for o in res.outputs.values()
                     if isinstance(o, dict) and str(o.get("path", "")).endswith(".csv")), None)
        if path is None:
            attempts.append("operator: finished without a CSV file")
    if path:
        try:
            return used("operator", StatementRead(parse_statement(path, maps["statement_csv"]), path, "operator",
                                                  attempts))
        except StatementFormatError as exc:
            attempts.append(f"operator CSV: {exc}")

    table = await run_playbook(rt, "bank_portal", "read_statement_table", values, mission_id=mission_id,
                               task_id=f"pb-table-{stamp}")
    if table.ok and table.header:
        try:
            records = parse_rows(table.header, table.rows, maps["statement_table"], "on-screen table")
            return used("screen", StatementRead(records, f"screen:/statement?from_date={start}&to_date={end}",
                                                "screen", attempts))
        except StatementFormatError as exc:
            attempts.append(f"on-screen table: {exc}")
    else:
        attempts.append(f"on-screen table: {table.error}")
    rt.events.append(Event(type="fallback.exhausted", mission_id=mission_id, occurred_at=rt.clock.now(),
                           payload={"what": "bank statement", "attempts": attempts}))
    return StatementRead(None, "", "none", attempts)


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
        # The task may submit exactly this receipt: the matcher's amount and bank reference, nothing else.
        scope = {"erp_web": [{"method": "POST", "path": f"/invoices/{entity.external_ref}/receipts",
                              "form": {"amount": f"{m.matched_amount / 100:.2f}", "reference": m.record_id}}]}
        res = await run_task(rt, mission_id, "post_receipt", goal=goal, entity_id=entity.id,
                             success_criteria=spec.get("success_criteria", []), write_scope=scope)
        rt.events.append(Event(type="settlement.receipt_task", mission_id=mission_id, entity_id=entity.id,
                               occurred_at=rt.clock.now(),
                               payload={"ok": not isinstance(res, FailureReport), "record_id": m.record_id,
                                        "detail": res.error if isinstance(res, FailureReport) else res.summary}))
        existing = [r for r in await receipts_for(rt, mission_id, entity.external_ref) if r["reference"] == m.record_id]
    return existing[0] if existing else None


async def fresh_records(rt: Runtime, mission_id: str, start: date, end: date, task_id: str,
                        entity_id: str | None = None) -> tuple[list[Record] | None, Evidence | None, str]:
    """Verifier's own observation: new browser session, scripted (no LLM). The CSV if the bank serves
    it, else the on-screen table: the Verifier must not depend on the route that settled the money."""
    maps = load_app_maps(rt.company_dir / "apps")["bank_portal"].exports
    values = {"start": start.isoformat(), "end": end.isoformat()}
    pb = await run_playbook(rt, "bank_portal", "download_statement", values, mission_id=mission_id,
                            task_id=task_id, entity_id=entity_id)
    if pb.ok and pb.files:
        try:
            return parse_statement(pb.files[0], maps["statement_csv"]), pb.evidence[-1] if pb.evidence else None, "csv"
        except StatementFormatError:
            pass
    table = await run_playbook(rt, "bank_portal", "read_statement_table", values, mission_id=mission_id,
                               task_id=f"{task_id}-screen", entity_id=entity_id)
    if table.ok and table.header:
        try:
            records = parse_rows(table.header, table.rows, maps["statement_table"], "on-screen table")
            return records, Evidence(kind="record", ref=f"screen:/statement?from_date={start}&to_date={end}",
                                     summary=f"{len(records)} credits read from the screen"), "screen"
        except StatementFormatError as exc:
            return None, None, f"could not read the statement: {exc}"
    return None, None, f"could not read the statement: {pb.error}; {table.error}"


async def fresh_statement_has(rt: Runtime, mission_id: str, m: Match) -> tuple[bool, Evidence | None, str]:
    records, ev, note = await fresh_records(rt, mission_id, m.record_on, m.record_on,
                                            f"verify-{m.entity_id}-{m.record_id}", m.entity_id)
    if records is None:
        return False, None, note
    rec = next((r for r in records if r.id == m.record_id), None)
    ok = rec is not None and rec.amount >= m.matched_amount
    return ok, ev, "record found with sufficient amount" if ok else "record not in fresh statement"


async def fresh_verified_total(rt: Runtime, mission_id: str) -> tuple[int, Evidence | None, str]:
    """Mission-level check: re-read the bank and sum the verified settlements whose money is really there.
    A record covering several entities must hold at least the sum applied from it."""
    ensure_ledger(rt)
    with rt.db.read() as conn:
        rows = conn.execute("SELECT s.record_id, SUM(s.amount) AS amount FROM settlements s JOIN entities e "
                            "ON e.id = s.entity_id WHERE e.mission_id = ? AND s.status = 'VERIFIED' "
                            "GROUP BY s.record_id", (mission_id,)).fetchall()
    if not rows:
        return 0, None, "nothing settled"
    start, end = lookback(rt)
    records, ev, note = await fresh_records(rt, mission_id, start, end, f"verify-mission-{mission_id}")
    if records is None:
        return 0, None, note
    seen = {r.id: r.amount for r in records}
    total = sum(r["amount"] for r in rows if seen.get(r["record_id"], 0) >= r["amount"])
    missing = [r["record_id"] for r in rows if seen.get(r["record_id"], 0) < r["amount"]]
    return total, ev, f"{len(rows) - len(missing)} of {len(rows)} records re-observed" + (
        f"; missing {missing}" if missing else "")


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
    report = await VerifierAgent(rt.llm).verify(entity.id, criteria)       # all deterministic: no model asked
    rt.events.append(Event(type="verification.completed", mission_id=mission_id, entity_id=entity.id,
                           occurred_at=rt.clock.now(), payload={"subject": "settlement",
                                                                **report.model_dump(mode="json")}))
    if rt.evidence:
        rt.evidence.collect([e for c in criteria for e in c.evidence], subject="verification",
                            mission_id=mission_id, entity_id=entity.id)
    return report


@dataclass
class Settled:
    entity_ref: str
    match: Match
    receipt: dict | None
    report: VerificationReport | None
    transition: TransitionResult | None


async def settle(rt: Runtime, mission_id: str, records: list[Record], source: str) -> list[Settled]:
    """Finish what a crash left half-way, then match new statement records and carry every match
    through receipt, verification and transition."""
    results = []
    for m, old_source in ledger_pending(rt, mission_id):
        entity = rt.pipeline.get(m.entity_id)
        if entity.state in rt.pipeline.spec.terminal:
            continue
        rt.events.append(Event(type="settlement.resumed", mission_id=mission_id, entity_id=entity.id,
                               occurred_at=rt.clock.now(), payload={"record_id": m.record_id, "amount": m.matched_amount}))
        results.append(await _carry(rt, mission_id, entity, m, old_source or source))
    pairs = candidates(rt, mission_id)
    by_id = {e.id: e for e, _ in pairs}
    tol = rt.pipeline.spec.constants.get("tolerance", 0)
    for m in match(records, [c for _, c in pairs], tolerance=tol, applied=ledger_applied(rt)):
        entity = by_id[m.entity_id]
        ledger_set(rt, m, "MATCHED", source=source)          # write-ahead: a crash after this resumes here
        rt.events.append(Event(type="settlement.matched", mission_id=mission_id, entity_id=entity.id,
                               occurred_at=rt.clock.now(),
                               payload={"record_id": m.record_id, "amount": m.matched_amount, "kind": m.kind,
                                        "text": m.record_text, "reasons": m.reasons}))
        results.append(await _carry(rt, mission_id, entity, m, source))
    return results


async def _carry(rt: Runtime, mission_id: str, entity: Entity, m: Match, source: str) -> Settled:
    """Receipt (idempotent: an existing receipt with this reference is reused) -> verify -> transition."""
    tol = rt.pipeline.spec.constants.get("tolerance", 0)
    receipt = await post_receipt(rt, mission_id, entity, m)
    if receipt is None:
        ledger_set(rt, m, "FAILED")
        return Settled(entity.external_ref, m, None, None, None)
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
        if rt.evidence:
            rt.evidence.collect([
                Evidence(kind="file" if source and not source.startswith("screen:") else "record",
                         ref=source or "statement", summary=f"statement with {m.record_id}"),
                Evidence(kind="record", ref=f"bank:{m.record_id}", summary=m.record_text,
                         data={"amount": m.matched_amount, "kind": m.kind, "on": m.record_on.isoformat(),
                               "reasons": m.reasons}),
                Evidence(kind="record", ref=f"erp:{receipt['id']}", summary=f"receipt {receipt['id']}")],
                subject=f"transition:{target}", mission_id=mission_id, entity_id=entity.id)
        rt.pipeline.update_data(entity.id, {"balance": max(0, balance - m.matched_amount)})
    return Settled(entity.external_ref, m, receipt, report, tr)


def lookback(rt: Runtime) -> tuple[date, date]:
    today = rt.clock.now().date()
    return today - timedelta(days=role_cfg(rt)["reconciliation"].get("lookback_days", 21)), today

