"""Mini ERP for Sharma Distributors.

- Web UI (login required): dashboard, customers, invoices, receipts, and the
  only way to POST a receipt. Receipt posting is web-only on purpose, so the
  agent must operate the browser like a person would.
- REST under /api/v1 (X-API-Key): read-only. No write endpoints exist.

Run: uvicorn sandbox.erp.app:app --port 8101
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from sandbox import chaos
from sandbox.common import clock, config, db
from sandbox.common.money import rupees_to_paise
from sandbox.common.web import SessionStore, check_password, templates
from sandbox.erp.db import DB_NAME

COOKIE = "erp_session"
WEB_SESSION_TTL = 8 * 3600
PAGE_SIZE = 15
OPEN_STATUSES = ("OPEN", "PARTIALLY_PAID", "DISPUTED")
RECEIPT_MODES = ("NEFT", "RTGS", "IMPS", "UPI", "CHEQUE")

app = FastAPI(title="Sharma Distributors — Mini ERP", docs_url="/api/docs", openapi_url="/api/openapi.json")
tpl = templates(Path(__file__).parent / "templates")
sessions = SessionStore(DB_NAME, COOKIE)


# --- helpers ------------------------------------------------------------------

def ageing_bucket(days: int) -> str:
    if days <= 0:
        return "Not due"
    if days <= 30:
        return "1-30"
    if days <= 45:
        return "31-45"
    if days <= 60:
        return "46-60"
    if days <= 90:
        return "61-90"
    return "90+"


def invoice_dict(row: sqlite3.Row, today: date) -> dict:
    inv = dict(row)
    days = (today - date.fromisoformat(inv["due_date"])).days
    inv["days_overdue"] = days
    inv["ageing_bucket"] = ageing_bucket(days)
    return inv


def customer_dict(row: sqlite3.Row) -> dict:
    c = dict(row)
    c["payment_plan"] = bool(c["payment_plan"])
    c["legal_hold"] = bool(c["legal_hold"])
    return c


INVOICE_SELECT = """
SELECT i.*, c.name AS customer_name
FROM invoices i JOIN customers c ON c.id = i.customer_id
"""


def query_invoices(conn: sqlite3.Connection, *, statuses: list[str] | None = None,
                   customer_id: str | None = None, min_days: int | None = None,
                   max_days: int | None = None) -> list[dict]:
    sql, args = INVOICE_SELECT + " WHERE 1=1", []
    if statuses:
        sql += f" AND i.status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if customer_id:
        sql += " AND i.customer_id = ?"
        args.append(customer_id)
    today = clock.today()
    rows = [invoice_dict(r, today) for r in conn.execute(sql + " ORDER BY i.due_date, i.number", args)]
    if min_days is not None:
        rows = [r for r in rows if r["days_overdue"] >= min_days]
    if max_days is not None:
        rows = [r for r in rows if r["days_overdue"] <= max_days]
    return rows


def get_invoice(conn: sqlite3.Connection, number: str) -> dict:
    row = conn.execute(INVOICE_SELECT + " WHERE i.number = ?", (number,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Invoice {number} not found")
    inv = invoice_dict(row, clock.today())
    inv["lines"] = [dict(r) for r in conn.execute(
        "SELECT * FROM invoice_lines WHERE invoice_number = ? ORDER BY line_no", (number,))]
    inv["receipts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM receipts WHERE invoice_number = ? ORDER BY id", (number,))]
    return inv


def parse_statuses(status: str | None) -> list[str] | None:
    if not status:
        return None
    return [s.strip().upper() for s in status.split(",") if s.strip()]


# --- web auth -----------------------------------------------------------------

class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)


def web_user(request: Request) -> str:
    user, _ = sessions.lookup(request.cookies.get(COOKIE))
    if user is None:
        raise LoginRequired(str(request.url.path))
    return user


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str | None = None):
    return tpl.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if username != config.get("ERP_WEB_USER") or not check_password(password, config.get("ERP_WEB_PASSWORD")):
        return RedirectResponse(f"/login?error=Invalid+username+or+password&next={quote(next)}", status_code=303)
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    resp.set_cookie(COOKIE, sessions.create(username, WEB_SESSION_TTL), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    sessions.destroy(request.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- web pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        open_invoices = query_invoices(conn, statuses=list(OPEN_STATUSES))
    buckets: dict[str, int] = {}
    for inv in open_invoices:
        buckets[inv["ageing_bucket"]] = buckets.get(inv["ageing_bucket"], 0) + inv["balance_paise"]
    order = ["Not due", "1-30", "31-45", "46-60", "61-90", "90+"]
    return tpl.TemplateResponse(request, "dashboard.html", {
        "user": user, "today": clock.today(),
        "total_outstanding": sum(i["balance_paise"] for i in open_invoices),
        "open_count": len(open_invoices),
        "buckets": [(b, buckets.get(b, 0)) for b in order],
    })


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        rows = conn.execute("""
            SELECT c.*, COALESCE(SUM(CASE WHEN i.status != 'PAID' THEN i.balance_paise END), 0) AS outstanding_paise
            FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id
            GROUP BY c.id ORDER BY c.id""").fetchall()
    return tpl.TemplateResponse(request, "customers.html", {"user": user, "customers": [dict(r) for r in rows]})


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_page(request: Request, customer_id: str, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Customer not found")
        invoices = query_invoices(conn, customer_id=customer_id)
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE customer_id = ? ORDER BY id DESC", (customer_id,))]
    return tpl.TemplateResponse(request, "customer.html", {
        "user": user, "customer": customer_dict(row), "invoices": invoices, "notes": notes})


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "", min_days: str = "", customer: str = "",
                  page: int = 1, user: str = Depends(web_user)):
    min_days_int = int(min_days) if min_days.strip().lstrip("-").isdigit() else None
    with db.session(DB_NAME) as conn:
        rows = query_invoices(conn, statuses=parse_statuses(status), customer_id=customer or None,
                              min_days=min_days_int)
        customers = [dict(r) for r in conn.execute("SELECT id, name FROM customers ORDER BY id")]
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(1, page), pages)
    return tpl.TemplateResponse(request, "invoices.html", {
        "user": user, "invoices": rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        "total_rows": len(rows), "page": page, "pages": pages, "customers": customers,
        "filters": {"status": status, "min_days": min_days, "customer": customer},
    })


@app.get("/invoices/{number}", response_class=HTMLResponse)
def invoice_page(request: Request, number: str, posted: str | None = None, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        inv = get_invoice(conn, number)
    return tpl.TemplateResponse(request, "invoice.html", {"user": user, "inv": inv, "posted": posted})


@app.get("/invoices/{number}/pdf")
def invoice_pdf_web(number: str, user: str = Depends(web_user)):
    return _invoice_pdf(number)


@app.get("/invoices/{number}/receipts/new", response_class=HTMLResponse)
def receipt_form(request: Request, number: str, error: str | None = None, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        inv = get_invoice(conn, number)
    template = "receipt_form_v2.html" if chaos.get("erp.layout") == "v2" else "receipt_form.html"
    return tpl.TemplateResponse(request, template, {
        "user": user, "inv": inv, "modes": RECEIPT_MODES, "error": error, "today": clock.today()})


@app.post("/invoices/{number}/receipts")
def post_receipt(number: str, receipt_date: str = Form(...), amount: str = Form(...), mode: str = Form(...),
                 reference: str = Form(...), remarks: str = Form(""), user: str = Depends(web_user)):
    def fail(msg: str):
        return RedirectResponse(f"/invoices/{number}/receipts/new?error={quote(msg)}", status_code=303)

    reference = reference.strip()
    try:
        amount_paise = rupees_to_paise(amount)
    except ValueError:
        return fail("Amount must be a number")
    try:
        rdate = date.fromisoformat(receipt_date)
    except ValueError:
        return fail("Receipt date must be YYYY-MM-DD")
    if mode not in RECEIPT_MODES:
        return fail("Unknown payment mode")
    if not reference:
        return fail("Bank reference / UTR is required")
    if amount_paise <= 0:
        return fail("Amount must be greater than zero")
    if rdate > clock.today():
        return fail("Receipt date cannot be in the future")

    with db.session(DB_NAME) as conn:
        # Write lock before reading the balance: two concurrent posts must not both pass the checks.
        conn.execute("BEGIN IMMEDIATE")
        inv = get_invoice(conn, number)
        if inv["status"] == "DISPUTED":
            return fail("Invoice is under dispute; receipts are blocked until the dispute is resolved")
        if inv["status"] == "PAID":
            return fail("Invoice is already fully paid")
        if amount_paise > inv["balance_paise"]:
            return fail("Amount exceeds the invoice balance")
        if conn.execute("SELECT 1 FROM receipts WHERE invoice_number = ? AND reference = ?",
                        (number, reference)).fetchone():
            return fail(f"A receipt with reference {reference} is already posted on this invoice")
        seq = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] + 1
        receipt_id = f"RCPT-{seq:04d}"
        new_balance = inv["balance_paise"] - amount_paise
        conn.execute(
            "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (receipt_id, number, rdate.isoformat(), amount_paise, mode, reference, remarks.strip() or None,
             user, clock.now().isoformat()),
        )
        conn.execute("UPDATE invoices SET balance_paise = ?, status = ? WHERE number = ?",
                     (new_balance, "PAID" if new_balance == 0 else "PARTIALLY_PAID", number))
    return RedirectResponse(f"/invoices/{number}?posted={receipt_id}", status_code=303)


@app.get("/receipts", response_class=HTMLResponse)
def receipts_page(request: Request, user: str = Depends(web_user)):
    with db.session(DB_NAME) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT r.*, i.customer_id, c.name AS customer_name FROM receipts r
            JOIN invoices i ON i.number = r.invoice_number JOIN customers c ON c.id = i.customer_id
            ORDER BY r.id DESC""")]
    return tpl.TemplateResponse(request, "receipts.html", {"user": user, "receipts": rows})


# --- REST (read-only) --------------------------------------------------------

def api_key(x_api_key: str | None = Header(default=None)) -> None:
    if x_api_key is None or not check_password(x_api_key, config.get("ERP_API_KEY")):
        raise HTTPException(401, "Missing or invalid X-API-Key")


@app.get("/api/v1/customers", dependencies=[Depends(api_key)])
def api_customers():
    with db.session(DB_NAME) as conn:
        return [customer_dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY id")]


@app.get("/api/v1/customers/{customer_id}", dependencies=[Depends(api_key)])
def api_customer(customer_id: str):
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Customer not found")
    return customer_dict(row)


@app.get("/api/v1/invoices", dependencies=[Depends(api_key)])
def api_invoices(status: str | None = None, customer_id: str | None = None,
                 min_days_overdue: int | None = None, max_days_overdue: int | None = None):
    with db.session(DB_NAME) as conn:
        rows = query_invoices(conn, statuses=parse_statuses(status), customer_id=customer_id,
                              min_days=min_days_overdue, max_days=max_days_overdue)
    return {"as_of": clock.today().isoformat(), "currency": "INR", "count": len(rows), "invoices": rows}


@app.get("/api/v1/invoices/{number}", dependencies=[Depends(api_key)])
def api_invoice(number: str):
    with db.session(DB_NAME) as conn:
        return get_invoice(conn, number)


@app.get("/api/v1/invoices/{number}/pdf", dependencies=[Depends(api_key)])
def api_invoice_pdf(number: str):
    return _invoice_pdf(number)


@app.get("/api/v1/receipts", dependencies=[Depends(api_key)])
def api_receipts(invoice: str | None = None):
    sql, args = "SELECT * FROM receipts", []
    if invoice:
        sql, args = sql + " WHERE invoice_number = ?", [invoice]
    with db.session(DB_NAME) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def _invoice_pdf(number: str) -> FileResponse:
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT pdf_path FROM invoices WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Invoice {number} not found")
    path = config.drive_dir() / row["pdf_path"]
    if not path.exists():
        raise HTTPException(404, "Invoice PDF missing from drive")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "erp", "today": clock.today().isoformat()}
