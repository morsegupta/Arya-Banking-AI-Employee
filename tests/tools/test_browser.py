"""Live tests: real Chromium driving real ERP + bank servers over HTTP."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from tools.app_map import load_app_maps
from tools.base import ToolRegistry
from tools.browser import BrowserSessions, register_browser_tools
from tools.vault import Vault

APPS_DIR = "packs/companies/sharma_distributors/apps"
SECRETS = {"BANK_USER_ID": "SHARMA001", "BANK_PASSWORD": "bank-sandbox-pass",
           "ERP_WEB_USER": "ar.clerk", "ERP_WEB_PASSWORD": "erp-sandbox-pass"}
ADMIN = {"X-Sandbox-Token": "sandbox-admin-token"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def servers(sandbox_var, monkeypatch):
    from sandbox.bank_portal.app import app as bank
    from sandbox.erp.app import app as erp

    urls, running = {}, []
    for name, app in (("erp_web", erp), ("bank_portal", bank)):
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        running.append(server)
        urls[name] = f"http://127.0.0.1:{port}"
        monkeypatch.setenv(f"APP_URL_{name.upper()}", urls[name])
    deadline = time.time() + 10
    while not all(s.started for s in running):
        assert time.time() < deadline, "servers did not start"
        time.sleep(0.05)
    yield urls
    for s in running:
        s.should_exit = True


@pytest.fixture()
def setup(servers, runtime_db, events):
    vault = Vault(SECRETS)
    sessions = BrowserSessions(load_app_maps(APPS_DIR), vault)
    reg = ToolRegistry(runtime_db, events, vault=vault)
    register_browser_tools(reg, sessions)
    return reg, sessions, servers


def run_session(sessions, coro_fn):
    async def go():
        try:
            return await coro_fn()
        finally:
            await sessions.shutdown()
    return asyncio.run(go())


BANK = {"bank_portal"}


def find(obs, name, role=None):
    return next(e for e in obs["elements"] if e["name"] == name and (role is None or e["role"] == role))


def test_bank_auto_login_statement_and_csv_download(setup, ctx):
    reg, sessions, _ = setup
    c = ctx(BANK)

    async def flow():
        opened = await reg.invoke("bank_portal.open", {"path": "/statement?from_date=2026-10-01"}, c)
        assert opened.ok, opened.error
        obs = opened.output
        assert "logged in to bank_portal" in obs["notices"]
        assert "NEFT/N287261234/MEHTA ELECTRICALS/INV 2263" in obs["text"]
        dl = await reg.invoke("bank_portal.download", {"ref": find(obs, "Download CSV")["ref"]}, c)
        assert dl.ok, dl.error
        return dl, opened

    dl, opened = run_session(sessions, flow)
    content = open(dl.output["path"]).read()
    assert content.startswith("Date,Narration,Chq./Ref.No.") and "MEHTA ELECTRICALS" in content
    assert dl.evidence[0].kind == "file" and len(dl.output["sha256"]) == 64
    assert "bank-sandbox-pass" not in opened.model_dump_json()


def test_bank_transfer_blocked_by_capability(setup, ctx):
    reg, sessions, urls = setup
    c = ctx(BANK)

    async def flow():
        obs = (await reg.invoke("bank_portal.open", {"path": "/"}, c)).output
        click = await reg.invoke("bank_portal.click", {"ref": find(obs, "Fund transfer")["ref"]}, c)
        driver = await sessions.driver(c)
        # Even a script on the page cannot POST to a read-only app.
        js = await driver.page.evaluate(
            "() => fetch('/transfer', {method: 'POST'}).then(r => 'sent ' + r.status).catch(e => 'blocked')")
        direct = await reg.invoke("bank_portal.open", {"path": "/transfer"}, c)
        return click, js, direct, list(driver.blocked)

    click, js, direct, blocked = run_session(sessions, flow)
    assert click.error_kind == "denied" and "/transfer is deny-listed" in click.error
    assert js == "blocked" and any("POST /transfer" in b for b in blocked)
    assert direct.error_kind == "denied"
    log = httpx.get(f"{urls['bank_portal']}/_sandbox/transactions", headers=ADMIN).json()
    assert len(log) == 10  # nothing changed in the bank


def test_bank_session_expiry_recovers(setup, ctx):
    reg, sessions, urls = setup
    httpx.post(f"{urls['bank_portal']}/_sandbox/settings", json={"session_ttl_seconds": 2}, headers=ADMIN)
    c = ctx(BANK)

    async def flow():
        obs = (await reg.invoke("bank_portal.open", {"path": "/"}, c)).output
        await asyncio.sleep(2.2)
        after = await reg.invoke("bank_portal.click", {"ref": find(obs, "Account statement")["ref"]}, c)
        return after, (await sessions.driver(c)).relogins

    after, relogins = run_session(sessions, flow)
    assert after.ok and relogins == 2
    assert "had expired" in after.output["notices"][0] and "repeat it" in after.output["notices"][0]


def test_unknown_host_blocked(setup, ctx):
    reg, sessions, _ = setup
    c = ctx(BANK)

    async def flow():
        await reg.invoke("bank_portal.open", {"path": "/"}, c)
        driver = await sessions.driver(c)
        return await driver.page.evaluate(
            "() => fetch('https://example.com/x').then(() => 'reached').catch(() => 'blocked')")

    assert run_session(sessions, flow) == "blocked"


def test_model_cannot_type_credentials(setup, ctx):
    reg, sessions, _ = setup
    c = ctx(BANK)

    async def flow():
        await reg.invoke("bank_portal.open", {"path": "/"}, c)
        await reg.invoke("bank_portal.click", {"ref": find((await reg.invoke("bank_portal.snapshot", {}, c)).output,
                                                             "Logout")["ref"]}, c)
        obs = (await reg.invoke("bank_portal.snapshot", {}, c)).output
        pw = await reg.invoke("bank_portal.fill", {"ref": find(obs, "Password")["ref"], "value": "guess"}, c)
        ref = await reg.invoke("bank_portal.fill", {"ref": find(obs, "User ID")["ref"],
                                                    "value": "vault://BANK_PASSWORD"}, c)
        return pw, ref

    pw, ref = run_session(sessions, flow)
    assert pw.error_kind == "denied" and ref.error_kind == "denied"


def test_erp_post_receipt_through_browser(setup, ctx):
    reg, sessions, urls = setup
    scope = {"erp_web"}

    async def flow():
        c = ctx(scope)
        c.write_scope = {"erp_web": [{"method": "POST", "path": "/invoices/INV-2263/receipts",
                                      "form": {"amount": "88000.00", "reference": "N287261234"}}]}
        obs = (await reg.invoke("erp_web.open", {"path": "/invoices/INV-2263/receipts/new"}, c)).output
        await reg.invoke("erp_web.fill", {"ref": find(obs, "Amount (₹)")["ref"], "value": "88000"}, c)
        obs = (await reg.invoke("erp_web.fill", {"ref": find(obs, "Bank reference / UTR")["ref"],
                                                 "value": "N287261234"}, c)).output
        submit = find(obs, "Post receipt")["ref"]
        no_key = await reg.invoke("erp_web.click", {"ref": submit}, c)
        c.idempotency_key = "m1:INV-2263:post_receipt"
        posted = await reg.invoke("erp_web.click", {"ref": submit}, c)
        replay = await reg.invoke("erp_web.click", {"ref": submit}, c)
        return no_key, posted, replay

    no_key, posted, replay = run_session(sessions, flow)
    assert no_key.error_kind == "denied"
    assert posted.ok and "Receipt RCPT-0001 posted." in posted.output["alerts"]
    assert replay.replayed
    inv = httpx.get(f"{urls['erp_web']}/api/v1/invoices/INV-2263",
                    headers={"X-API-Key": "erp-sandbox-readonly-key"}).json()
    assert inv["status"] == "PAID" and len(inv["receipts"]) == 1


def _receipt_attempt(reg, c, ref, amount, utr):
    async def go():
        obs = (await reg.invoke("erp_web.open", {"path": f"/invoices/{ref}/receipts/new"}, c)).output
        await reg.invoke("erp_web.fill", {"ref": find(obs, "Amount (₹)")["ref"], "value": amount}, c)
        obs = (await reg.invoke("erp_web.fill", {"ref": find(obs, "Bank reference / UTR")["ref"], "value": utr},
                                c)).output
        c.idempotency_key = f"k:{ref}:{amount}:{utr}"
        return await reg.invoke("erp_web.click", {"ref": find(obs, "Post receipt")["ref"]}, c)
    return go()


def test_erp_writes_limited_to_the_granted_receipt(setup, ctx):
    reg, sessions, urls = setup
    c = ctx({"erp_web"})
    c.write_scope = {"erp_web": [{"method": "POST", "path": "/invoices/INV-2263/receipts",
                                  "form": {"amount": "88000.00", "reference": "N287261234"}}]}

    async def flow():
        wrong_amount = await _receipt_attempt(reg, c, "INV-2263", "90000", "N287261234")
        other_invoice = await _receipt_attempt(reg, c, "INV-2291", "88000", "N287261234")
        right = await _receipt_attempt(reg, c, "INV-2263", "88000", "N287261234")
        return wrong_amount, other_invoice, right

    wrong_amount, other_invoice, right = run_session(sessions, flow)
    assert wrong_amount.error_kind == "denied" and "amount" in wrong_amount.error
    assert other_invoice.error_kind == "denied" and "not a write this task was granted" in other_invoice.error
    assert right.ok
    receipts = httpx.get(f"{urls['erp_web']}/api/v1/receipts", headers={"X-API-Key": "erp-sandbox-readonly-key"}).json()
    assert [(r["invoice_number"], r["amount_paise"]) for r in receipts] == [("INV-2263", 88000_00)]


def test_erp_rest_paths_denied_in_browser(setup, ctx):
    reg, sessions, _ = setup
    res = run_session(sessions, lambda: reg.invoke("erp_web.open", {"path": "/api/v1/invoices"}, ctx({"erp_web"})))
    assert res.error_kind == "denied"


def test_tasks_get_isolated_browser_contexts(setup, ctx):
    reg, sessions, _ = setup

    async def flow():
        await reg.invoke("bank_portal.open", {"path": "/"}, ctx(BANK, task_id="t1"))
        second = await reg.invoke("bank_portal.open", {"path": "/"}, ctx(BANK, task_id="t2"))
        return second

    second = run_session(sessions, flow)
    assert "logged in to bank_portal" in second.output["notices"]  # no cookie shared from t1
