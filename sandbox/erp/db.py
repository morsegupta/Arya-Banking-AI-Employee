"""Mini ERP storage: customers, invoices, lines, receipts, notes."""

from __future__ import annotations

from sandbox.common import db
from sandbox.common.web import SESSIONS_SCHEMA
from sandbox.common.world import World, build_lines, split_total

DB_NAME = "erp"

SCHEMA = SESSIONS_SCHEMA + """
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    payment_terms_days INTEGER NOT NULL,
    contact_name TEXT NOT NULL,
    contact_role TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    contact_whatsapp TEXT NOT NULL,
    payment_plan INTEGER NOT NULL DEFAULT 0,
    legal_hold INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE invoices (
    number TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    taxable_paise INTEGER NOT NULL,
    gst_paise INTEGER NOT NULL,
    total_paise INTEGER NOT NULL,
    balance_paise INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIALLY_PAID', 'PAID', 'DISPUTED')),
    dispute_note TEXT,
    pdf_path TEXT NOT NULL,
    CHECK (balance_paise >= 0 AND balance_paise <= total_paise)
);

CREATE TABLE invoice_lines (
    invoice_number TEXT NOT NULL REFERENCES invoices(number),
    line_no INTEGER NOT NULL,
    description TEXT NOT NULL,
    hsn TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit TEXT NOT NULL,
    rate_paise INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL,
    PRIMARY KEY (invoice_number, line_no)
);

CREATE TABLE receipts (
    id TEXT PRIMARY KEY,
    invoice_number TEXT NOT NULL REFERENCES invoices(number),
    receipt_date TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    mode TEXT NOT NULL,
    reference TEXT NOT NULL,
    remarks TEXT,
    posted_by TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    UNIQUE (invoice_number, reference)
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    invoice_number TEXT REFERENCES invoices(number),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def seed(world: World) -> None:
    db.reset(DB_NAME, SCHEMA)
    with db.session(DB_NAME) as conn:
        for c in world.customers:
            conn.execute(
                "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (c.id, c.name, c.city, c.payment_terms_days, c.contact.name, c.contact.role,
                 c.contact.email, c.contact.whatsapp, int(c.flags.payment_plan), int(c.flags.legal_hold)),
            )
        for inv in world.invoices:
            taxable, gst = split_total(inv.total_paise)
            conn.execute(
                "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (inv.number, inv.customer, inv.issue_date.isoformat(), world.due_date(inv).isoformat(),
                 taxable, gst, inv.total_paise, inv.total_paise, inv.status, inv.dispute_note,
                 f"invoices/{inv.number}.pdf"),
            )
            for n, line in enumerate(build_lines(inv), start=1):
                conn.execute(
                    "INSERT INTO invoice_lines VALUES (?,?,?,?,?,?,?,?)",
                    (inv.number, n, line.description, line.hsn, line.qty, line.unit,
                     line.rate_paise, line.amount_paise),
                )
