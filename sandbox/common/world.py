"""Load a seeded world YAML into typed objects.

The world file is the single source of truth for the sandbox. Every system is
seeded from the same objects, so the ERP, the invoice PDFs and the bank agree.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from sandbox.common.money import rupees_to_paise

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORLD = REPO_ROOT / "evals" / "worlds" / "sharma_default.yaml"

GST_RATE_BP = 1800  # 18.00 % in basis points


class BankDetails(BaseModel):
    bank_name: str
    account_name: str
    account_number: str
    ifsc: str
    branch: str
    opening_balance: int  # rupees


class Company(BaseModel):
    name: str
    gstin: str
    address: str
    ar_mailbox: str
    ar_whatsapp: str
    late_fee_percent_per_month: float
    bank: BankDetails


class Contact(BaseModel):
    name: str
    role: str
    email: str
    whatsapp: str
    email_bounces: bool = False


class CustomerFlags(BaseModel):
    payment_plan: bool = False
    legal_hold: bool = False


class Customer(BaseModel):
    id: str
    name: str
    city: str
    payment_terms_days: int
    contact: Contact
    flags: CustomerFlags = Field(default_factory=CustomerFlags)
    hidden: dict[str, Any] = Field(default_factory=dict)  # ground truth, never seeded


class InvoiceLine(BaseModel):
    description: str
    hsn: str
    qty: int
    unit: str
    rate_paise: int

    @property
    def amount_paise(self) -> int:
        return self.qty * self.rate_paise


class Invoice(BaseModel):
    number: str
    customer: str
    issue_date: date
    amount: int  # rupees, GST-inclusive total
    status: Literal["OPEN", "DISPUTED"] = "OPEN"
    dispute_note: str | None = None
    delivery_short_qty: int = 0

    @property
    def total_paise(self) -> int:
        return rupees_to_paise(self.amount)


class BankTxn(BaseModel):
    date: date
    narration: str
    ref: str
    credit: int = 0  # rupees
    debit: int = 0

    @model_validator(mode="after")
    def one_side(self) -> "BankTxn":
        if (self.credit > 0) == (self.debit > 0):
            raise ValueError(f"bank txn {self.ref} must have exactly one of credit/debit")
        return self


class World(BaseModel):
    world_id: str
    start: datetime
    company: Company
    customers: list[Customer]
    invoices: list[Invoice]
    bank_transactions: list[BankTxn]
    ground_truth: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_resolve(self) -> "World":
        ids = {c.id for c in self.customers}
        if len(ids) != len(self.customers):
            raise ValueError("duplicate customer id")
        numbers = [i.number for i in self.invoices]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate invoice number")
        for inv in self.invoices:
            if inv.customer not in ids:
                raise ValueError(f"{inv.number} references unknown customer {inv.customer}")
        return self

    def customer(self, customer_id: str) -> Customer:
        return next(c for c in self.customers if c.id == customer_id)

    def due_date(self, inv: Invoice) -> date:
        return inv.issue_date + timedelta(days=self.customer(inv.customer).payment_terms_days)


def load_world(path: Path | str = DEFAULT_WORLD) -> World:
    with open(path) as fh:
        return World.model_validate(yaml.safe_load(fh))


# --- invoice line items -----------------------------------------------------

CATALOG = [
    ("Sunflower oil 15L tin", "1512", "tin", 2150_00),
    ("Basmati rice 25kg bag", "1006", "bag", 2875_00),
    ("Toor dal 30kg bag", "0713", "bag", 4320_00),
    ("Detergent powder 1kg x 24 carton", "3402", "carton", 1890_00),
    ("Toilet soap 100g x 72 carton", "3401", "carton", 2340_00),
    ("Biscuits assorted x 96 carton", "1905", "carton", 1180_00),
    ("Tea 500g x 40 carton", "0902", "carton", 6120_00),
    ("Sugar 50kg bag", "1701", "bag", 2210_00),
]


def split_total(total_paise: int) -> tuple[int, int]:
    """GST-inclusive total -> (taxable, gst). Taxable rounded to the paisa, GST absorbs rounding."""
    taxable = (total_paise * 10000 + (10000 + GST_RATE_BP) // 2) // (10000 + GST_RATE_BP)
    return taxable, total_paise - taxable


def build_lines(inv: Invoice) -> list[InvoiceLine]:
    """Deterministic line items whose sum is exactly the taxable value.

    Seeded from the invoice number so the ERP and the PDF always agree and a
    re-seed reproduces identical documents.
    """
    taxable, _ = split_total(inv.total_paise)
    rng = random.Random(int(hashlib.sha256(inv.number.encode()).hexdigest(), 16))
    products = rng.sample(CATALOG, k=3)
    lines: list[InvoiceLine] = []
    remaining = taxable
    for desc, hsn, unit, rate in products[:2]:
        # Each goods line takes ~30-45 % of the taxable value.
        target = taxable * rng.randint(30, 45) // 100
        qty = max(1, target // rate)
        if qty * rate >= remaining:
            continue
        lines.append(InvoiceLine(description=desc, hsn=hsn, qty=qty, unit=unit, rate_paise=rate))
        remaining -= qty * rate
    # Third product line uses whatever whole units fit; freight absorbs the rest.
    desc, hsn, unit, rate = products[2]
    qty = remaining // rate
    if qty > 0:
        lines.append(InvoiceLine(description=desc, hsn=hsn, qty=qty, unit=unit, rate_paise=rate))
        remaining -= qty * rate
    if remaining > 0:
        lines.append(InvoiceLine(description="Freight and handling", hsn="9965", qty=1, unit="lot", rate_paise=remaining))
    assert sum(line.amount_paise for line in lines) == taxable
    return lines
