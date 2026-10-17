from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sandbox.common import clock
from sandbox.erp.app import app

KEY = {"X-API-Key": "erp-sandbox-readonly-key"}


@pytest.fixture()
def client(sandbox_var):
    return TestClient(app)


@pytest.fixture()
def web(client):
    resp = client.post("/login", data={"username": "ar.clerk", "password": "erp-sandbox-pass", "next": "/"},
                       follow_redirects=False)
    assert resp.status_code == 303 and "erp_session" in resp.cookies
    return client


def post_receipt(web, number, amount, reference, receipt_date="2026-10-19", mode="NEFT"):
    return web.post(f"/invoices/{number}/receipts", follow_redirects=False, data={
        "receipt_date": receipt_date, "amount": amount, "mode": mode, "reference": reference})


# --- REST ---------------------------------------------------------------------

def test_rest_requires_api_key(client):
    assert client.get("/api/v1/invoices").status_code == 401
    assert client.get("/api/v1/invoices", headers={"X-API-Key": "wrong"}).status_code == 401


def test_rest_overdue_query(client, world):
    data = client.get("/api/v1/invoices", params={"status": "OPEN", "min_days_overdue": 45}, headers=KEY).json()
    assert data["as_of"] == "2026-10-19"
    numbers = {i["number"] for i in data["invoices"]}
    # Raw ERP query includes payment-plan and legal-hold invoices: deciding scope is the agent's job.
    assert numbers == set(world.ground_truth["in_scope_45_plus"]) | {"INV-2259", "INV-2248"}
    by_number = {i["number"]: i for i in data["invoices"]}
    assert by_number["INV-2291"]["days_overdue"] == 62
    assert by_number["INV-2291"]["total_paise"] == 340000_00
    assert by_number["INV-2291"]["customer_name"] == "Globex Retail Pvt Ltd"


def test_rest_exposes_flags_needed_for_scoping(client):
    customers = {c["id"]: c for c in client.get("/api/v1/customers", headers=KEY).json()}
    assert customers["C13"]["payment_plan"] is True
    assert customers["C14"]["legal_hold"] is True
    assert customers["C01"]["payment_plan"] is False
    disputed = client.get("/api/v1/invoices", params={"status": "DISPUTED"}, headers=KEY).json()
    assert [i["number"] for i in disputed["invoices"]] == ["INV-2240"]


def test_rest_is_read_only(client):
    assert client.post("/api/v1/receipts", json={}, headers=KEY).status_code == 405
    assert client.post("/api/v1/invoices/INV-2291", json={}, headers=KEY).status_code == 405
    write_routes = [r for r in app.routes if r.path.startswith("/api/") and getattr(r, "methods", set()) - {"GET", "HEAD"}]
    assert write_routes == []


def test_rest_invoice_pdf(client):
    resp = client.get("/api/v1/invoices/INV-2291/pdf", headers=KEY)
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF")


def test_days_overdue_follow_the_sandbox_clock(client):
    from datetime import timedelta

    clock.advance(timedelta(days=4))
    inv = client.get("/api/v1/invoices/INV-2291", headers=KEY).json()
    assert inv["days_overdue"] == 66


# --- web ----------------------------------------------------------------------

def test_web_requires_login(client):
    resp = client.get("/invoices/INV-2291", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login?next=")


def test_wrong_password_rejected(client):
    resp = client.post("/login", data={"username": "ar.clerk", "password": "nope"}, follow_redirects=False)
    assert "error=" in resp.headers["location"] and "erp_session" not in resp.cookies


def test_invoice_list_paginates(web):
    page1 = web.get("/invoices").text
    assert "25 invoices · page 1 of 2" in page1
    assert "Next page" in page1
    assert "page 2 of 2" in web.get("/invoices?page=2").text


def test_full_receipt_marks_paid(web, client):
    resp = post_receipt(web, "INV-2263", "88000", "N287261234")
    assert resp.status_code == 303 and "posted=RCPT-0001" in resp.headers["location"]
    inv = client.get("/api/v1/invoices/INV-2263", headers=KEY).json()
    assert inv["status"] == "PAID" and inv["balance_paise"] == 0
    assert inv["receipts"][0]["reference"] == "N287261234"
    assert inv["receipts"][0]["posted_by"] == "ar.clerk"
    assert "Receipt RCPT-0001 posted." in web.get(resp.headers["location"]).text


def test_partial_then_rest(web, client):
    post_receipt(web, "INV-2284", "75,000.00", "UPI111")
    inv = client.get("/api/v1/invoices/INV-2284", headers=KEY).json()
    assert inv["status"] == "PARTIALLY_PAID" and inv["balance_paise"] == 75000_00
    post_receipt(web, "INV-2284", "75000", "UPI222")
    assert client.get("/api/v1/invoices/INV-2284", headers=KEY).json()["status"] == "PAID"


def test_one_transfer_across_many_invoices(web, client):
    """Initech pays one UTR for three invoices: one receipt per invoice, same reference."""
    for number, amount in [("INV-2270", "120000"), ("INV-2275", "95000"), ("INV-2288", "60000")]:
        assert "posted=" in post_receipt(web, number, amount, "N299000111").headers["location"]
    receipts = client.get("/api/v1/receipts", headers=KEY).json()
    assert {r["invoice_number"] for r in receipts} == {"INV-2270", "INV-2275", "INV-2288"}


@pytest.mark.parametrize("number, amount, ref, rdate, expected", [
    ("INV-2291", "340001", "X1", "2026-10-19", "exceeds the invoice balance"),
    ("INV-2240", "100", "X2", "2026-10-19", "under dispute"),
    ("INV-2291", "100", "X3", "2026-10-20", "cannot be in the future"),
    ("INV-2291", "0", "X4", "2026-10-19", "greater than zero"),
    ("INV-2291", "abc", "X5", "2026-10-19", "must be a number"),
    ("INV-2291", "100", "  ", "2026-10-19", "UTR is required"),
])
def test_receipt_validation(web, client, number, amount, ref, rdate, expected):
    resp = post_receipt(web, number, amount, ref, receipt_date=rdate)
    assert "/receipts/new?error=" in resp.headers["location"]
    assert expected in web.get(resp.headers["location"]).text
    assert client.get("/api/v1/receipts", headers=KEY).json() == []


def test_duplicate_reference_rejected(web, client):
    post_receipt(web, "INV-2291", "1000", "DUP1")
    resp = post_receipt(web, "INV-2291", "1000", "DUP1")
    assert "already posted" in web.get(resp.headers["location"]).text
    assert len(client.get("/api/v1/receipts", headers=KEY).json()) == 1


def test_paid_invoice_blocks_further_receipts(web):
    post_receipt(web, "INV-2297", "54000", "R1")
    page = web.get("/invoices/INV-2297").text
    assert "Record receipt" not in page
    resp = post_receipt(web, "INV-2297", "1", "R2")
    assert "already fully paid" in web.get(resp.headers["location"]).text
