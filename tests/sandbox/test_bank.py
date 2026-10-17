from __future__ import annotations

import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from sandbox.bank_portal.app import app

ADMIN = {"X-Sandbox-Token": "sandbox-admin-token"}


@pytest.fixture()
def client(sandbox_var):
    return TestClient(app)


def login(client):
    resp = client.post("/login", data={"user_id": "SHARMA001", "password": "bank-sandbox-pass"},
                       follow_redirects=False)
    assert resp.status_code == 303 and "bcb_session" in resp.cookies
    return client


def statement_csv(client, **params):
    resp = client.get("/statement/download", params={"format": "csv", "from_date": "2026-10-01", **params})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(resp.text)))


def test_no_api_surface(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_requires_login(client):
    resp = client.get("/statement", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_wrong_password(client):
    resp = client.post("/login", data={"user_id": "SHARMA001", "password": "x"}, follow_redirects=False)
    assert "error=" in resp.headers["location"]


def test_statement_page_and_csv(client):
    login(client)
    page = client.get("/statement?from_date=2026-10-01&to_date=2026-10-19").text
    assert "10 transactions" in page and "page 1 of 1" in page
    assert "NEFT/N287261234/MEHTA ELECTRICALS/INV 2263" in page
    rows = statement_csv(client)
    assert rows[0] == ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
    mehta = next(r for r in rows if "MEHTA" in r[1])
    assert mehta[0] == "14/10/26" and mehta[2] == "N287261234" and mehta[5] == "88000.00" and mehta[4] == ""


def test_summary_balance(client, world):
    login(client)
    w = world
    expected = (w.company.bank.opening_balance + sum(t.credit for t in w.bank_transactions)
                - sum(t.debit for t in w.bank_transactions))
    assert expected == 997_910
    assert "₹9,97,910.00" in client.get("/").text


def test_date_filter(client):
    login(client)
    rows = statement_csv(client, from_date="2026-10-14", to_date="2026-10-14")
    assert len(rows) == 2 and "MEHTA" in rows[1][1]
    assert client.get("/statement?from_date=2026-10-15&to_date=2026-10-01").status_code == 400


def test_pdf_download(client):
    login(client)
    resp = client.get("/statement/download", params={"format": "pdf"})
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF")


def test_transfer_is_refused(client):
    login(client)
    assert client.get("/transfer").status_code == 403
    assert client.post("/transfer", data={"to": "x", "amount": "1"}).status_code == 403


def test_session_expires(client):
    assert client.post("/_sandbox/settings", json={"session_ttl_seconds": 1}, headers=ADMIN).status_code == 200
    login(client)
    assert client.get("/statement").status_code == 200
    time.sleep(1.2)
    resp = client.get("/statement", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?expired=1"
    assert "Your session has expired" in client.get("/login?expired=1").text


def test_admin_endpoints_need_token(client):
    assert client.post("/_sandbox/credit", json={"payer": "X", "amount_paise": 100}).status_code == 401
    assert client.post("/_sandbox/credit", json={"payer": "X", "amount_paise": 100},
                       headers={"X-Sandbox-Token": "guess"}).status_code == 401
    assert client.get("/_sandbox/transactions").status_code == 401


def test_payment_engine_credit_appears_on_statement(client):
    resp = client.post("/_sandbox/credit", headers=ADMIN, json={
        "payer": "Globex Retail Pvt Ltd", "amount_paise": 340000_00, "mode": "NEFT",
        "remark": "INV 2291", "utr": "N300000001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["narration"] == "NEFT/N300000001/GLOBEX RETAIL PVT LTD/INV 2291"
    assert body["balance_paise"] == (997_910 + 340_000) * 100
    login(client)
    rows = statement_csv(client)
    assert rows[-1][1] == body["narration"] and rows[-1][5] == "340000.00" and rows[-1][6] == "1337910.00"


def test_credit_rejects_bad_input(client):
    assert client.post("/_sandbox/credit", headers=ADMIN, json={"payer": "X", "amount_paise": 0}).status_code == 422
    assert client.post("/_sandbox/credit", headers=ADMIN,
                       json={"payer": "X", "amount_paise": 100, "mode": "SWIFT"}).status_code == 400
    assert client.post("/_sandbox/credit", headers=ADMIN,
                       json={"payer": "X", "amount_paise": 100, "txn_date": "2026-10-25"}).status_code == 400


def test_chaos_renamed_csv_columns(client):
    client.post("/_sandbox/settings", json={"csv_schema": "v2"}, headers=ADMIN)
    login(client)
    rows = statement_csv(client)
    assert rows[0] == ["Txn Date", "Description", "Reference", "Value Date", "Debit", "Credit", "Balance"]
    assert len(rows) == 11
    assert client.post("/_sandbox/settings", json={"csv_schema": "v9"}, headers=ADMIN).status_code == 400
