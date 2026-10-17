"""Generate the shared drive: invoice PDFs, delivery notes, customer contracts, SOPs.

Layout under var/drive/:
    invoices/INV-xxxx.pdf
    delivery_notes/DN-xxxx.pdf        (signed proof of what the customer received)
    contracts/<customer_id>_supply_agreement.pdf
    policies/credit_policy.md
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fpdf import FPDF

from sandbox.common.money import format_inr
from sandbox.common.world import InvoiceLine, World, build_lines, split_total

STATIC = Path(__file__).parent / "static"


def rs(paise: int) -> str:
    # Core PDF fonts are latin-1 only, so no rupee glyph.
    return format_inr(paise, symbol="Rs. ")


def _doc(title: str, world: World) -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(world.start)  # fixed date: re-seeding yields byte-identical files
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, world.company.name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, world.company.address, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"GSTIN: {world.company.gstin}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    return pdf


def _kv(pdf: FPDF, key: str, value: str) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 6, key)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")


def _line_table(pdf: FPDF, lines: list[InvoiceLine], qty_override: dict[int, int] | None = None,
                with_amounts: bool = True) -> None:
    widths = [8, 72, 16, 16, 14, 30, 34] if with_amounts else [10, 110, 25, 25, 20]
    headers = ["#", "Description", "HSN", "Qty", "Unit", "Rate", "Amount"] if with_amounts \
        else ["#", "Description", "HSN", "Qty", "Unit"]
    pdf.set_font("Helvetica", "B", 9)
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for n, line in enumerate(lines, start=1):
        qty = (qty_override or {}).get(n, line.qty)
        cells = [str(n), line.description, line.hsn, str(qty), line.unit]
        if with_amounts:
            cells += [rs(line.rate_paise), rs(line.amount_paise)]
        for i, (w, text) in enumerate(zip(widths, cells)):
            pdf.cell(w, 7, text, border=1, align="R" if i >= 3 and i != 4 else "L")
        pdf.ln()


def invoice_pdf(world: World, inv, out: Path) -> None:
    cust = world.customer(inv.customer)
    taxable, gst = split_total(inv.total_paise)
    pdf = _doc("TAX INVOICE", world)
    _kv(pdf, "Invoice number", inv.number)
    _kv(pdf, "Invoice date", inv.issue_date.isoformat())
    _kv(pdf, "Due date", world.due_date(inv).isoformat())
    _kv(pdf, "Payment terms", f"Net {cust.payment_terms_days} days")
    _kv(pdf, "Bill to", f"{cust.name}, {cust.city} ({cust.id})")
    _kv(pdf, "Attention", f"{cust.contact.name}, {cust.contact.role}")
    pdf.ln(3)
    _line_table(pdf, build_lines(inv))
    pdf.ln(2)
    _kv(pdf, "Taxable value", rs(taxable))
    _kv(pdf, "GST @ 18%", rs(gst))
    _kv(pdf, "Invoice total", rs(inv.total_paise))
    pdf.ln(4)
    bank = world.company.bank
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Pay by NEFT/RTGS/IMPS to:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for text in (f"{bank.account_name}", f"{bank.bank_name}, {bank.branch}",
                 f"A/c no. {bank.account_number}  IFSC {bank.ifsc}",
                 f"Please quote invoice number {inv.number} in the payment remarks."):
        pdf.cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Late payment attracts interest at {world.company.late_fee_percent_per_month}% per month "
                   "as per the supply agreement.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(out))


def delivery_note_pdf(world: World, inv, out: Path) -> None:
    cust = world.customer(inv.customer)
    lines = build_lines(inv)
    goods = [(n, l) for n, l in enumerate(lines, start=1) if l.unit != "lot"]
    override: dict[int, int] = {}
    if inv.delivery_short_qty:
        n, line = max(goods, key=lambda pair: pair[1].qty)
        if inv.delivery_short_qty >= line.qty:
            raise ValueError(f"{inv.number}: delivery_short_qty {inv.delivery_short_qty} >= line qty {line.qty}")
        override[n] = line.qty - inv.delivery_short_qty
    pdf = _doc("DELIVERY NOTE / PROOF OF DELIVERY", world)
    _kv(pdf, "Delivery note", inv.number.replace("INV", "DN"))
    _kv(pdf, "Against invoice", inv.number)
    _kv(pdf, "Dispatch date", inv.issue_date.isoformat())
    _kv(pdf, "Consignee", f"{cust.name}, {cust.city}")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Quantities received by consignee:", new_x="LMARGIN", new_y="NEXT")
    _line_table(pdf, [l for _, l in goods], qty_override={i + 1: override[n] for i, (n, _) in enumerate(goods) if n in override},
                with_amounts=False)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Received in good condition. Signed: {cust.contact.name} for {cust.name}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(out))


def contract_pdf(world: World, cust, out: Path) -> None:
    pdf = _doc("MASTER SUPPLY AGREEMENT (SUMMARY OF COMMERCIAL TERMS)", world)
    _kv(pdf, "Customer", f"{cust.name} ({cust.id})")
    _kv(pdf, "Customer city", cust.city)
    _kv(pdf, "Effective from", "2025-04-01")
    pdf.ln(3)
    clauses = [
        f"1. Payment terms: invoices are payable within {cust.payment_terms_days} days of the invoice date.",
        f"2. Late payment: overdue amounts attract interest at {world.company.late_fee_percent_per_month}% per month.",
        "3. Disputes: quantity or quality disputes must be raised in writing within 7 days of delivery, "
        "with the signed delivery note as reference. Undisputed portions remain payable on the due date.",
        f"4. Payment details: payments only to {world.company.bank.account_name}, A/c "
        f"{world.company.bank.account_number}, IFSC {world.company.bank.ifsc}. The supplier never changes "
        "bank details by email or message; any such request must be treated as fraud.",
        "5. Discounts: no early-payment discount applies unless agreed in writing by the supplier's finance manager.",
    ]
    for clause in clauses:
        pdf.multi_cell(0, 6, clause, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
    pdf.output(str(out))


def generate(world: World, drive: Path) -> dict[str, int]:
    if drive.exists():
        shutil.rmtree(drive)
    for sub in ("invoices", "delivery_notes", "contracts", "policies"):
        (drive / sub).mkdir(parents=True)
    for inv in world.invoices:
        invoice_pdf(world, inv, drive / "invoices" / f"{inv.number}.pdf")
        delivery_note_pdf(world, inv, drive / "delivery_notes" / f"{inv.number.replace('INV', 'DN')}.pdf")
    for cust in world.customers:
        contract_pdf(world, cust, drive / "contracts" / f"{cust.id}_supply_agreement.pdf")
    shutil.copy(STATIC / "credit_policy.md", drive / "policies" / "credit_policy.md")
    return {sub: len(list((drive / sub).iterdir())) for sub in ("invoices", "delivery_notes", "contracts", "policies")}
