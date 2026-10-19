from __future__ import annotations

import asyncio
import csv

import httpx
import pytest
from openpyxl import Workbook

from tools.base import ToolRegistry
from tools.channels.hub import ChannelConfig, register_channel_tools
from tools.files import FileRoots, register_file_tools
from tools.vault import Vault

GLOBEX = "ritu.malhotra@globex-retail.example"
NILGIRI_EMAIL = "anand.k@nilgiriwholesale.example"
NILGIRI_WA = "+91-90000-10012"
SIM = {"Authorization": "Bearer hub-sim-token"}


@pytest.fixture()
def hub_transport(sandbox_var):
    from sandbox.message_hub.app import app
    return httpx.ASGITransport(app=app)


@pytest.fixture()
def tools(runtime_db, events, sandbox_var, hub_transport):
    vault = Vault({"HUB_ARYA_TOKEN": "hub-arya-token"})
    reg = ToolRegistry(runtime_db, events, vault=vault)
    roots = FileRoots(sandbox_var / "drive")
    config = ChannelConfig(hub_url="http://hub", hub_token="vault://HUB_ARYA_TOKEN",
                           email_from="ar@sharma-distributors.example", whatsapp_from="+91-90000-00000")
    register_channel_tools(reg, config, vault, roots, transport=hub_transport)
    register_file_tools(reg, roots)
    return reg


def hub_get(transport, path):
    async def go():
        async with httpx.AsyncClient(transport=transport, base_url="http://hub", headers=SIM) as c:
            return (await c.get(path)).json()
    return asyncio.run(go())


ALL = {"email", "whatsapp", "call", "files"}


def run(coro):
    return asyncio.run(coro)


# --- channels -----------------------------------------------------------------------

def test_email_send_delivered_and_verified(tools, ctx, hub_transport):
    res = run(tools.invoke("email.send", {"to": GLOBEX, "subject": "INV-2291", "body": "Reminder"},
                           ctx(ALL, key="m1:e1:email1")))
    assert res.ok and res.verified is True and res.output["status"] == "delivered"
    assert res.evidence[0].kind == "message" and res.evidence[0].ref == res.output["id"]
    assert len(hub_get(hub_transport, f"/api/v1/mailboxes/{GLOBEX}/messages")) == 1


def test_send_requires_idempotency_key(tools, ctx):
    res = run(tools.invoke("email.send", {"to": GLOBEX, "body": "x"}, ctx(ALL)))
    assert res.error_kind == "denied"


def test_retry_with_same_key_never_double_sends(tools, ctx, hub_transport, runtime_db):
    c = ctx(ALL, key="m1:e1:email1")
    run(tools.invoke("email.send", {"to": GLOBEX, "body": "Reminder"}, c))
    assert run(tools.invoke("email.send", {"to": GLOBEX, "body": "Reminder"}, c)).replayed
    # Even if the runtime's own ledger were lost, the provider dedupes on the same key.
    with runtime_db.transaction() as conn:
        conn.execute("DELETE FROM tool_calls")
    again = run(tools.invoke("email.send", {"to": GLOBEX, "body": "Reminder"}, c))
    assert again.ok and again.evidence[0].data["duplicate"] is True
    assert len(hub_get(hub_transport, f"/api/v1/mailboxes/{GLOBEX}/messages")) == 1


def test_bounce_is_permanent_then_whatsapp_works(tools, ctx):
    email = run(tools.invoke("email.send", {"to": NILGIRI_EMAIL, "body": "Reminder"}, ctx(ALL, key="k1")))
    assert not email.ok and email.error_kind == "permanent" and "bounced" in email.error
    wa = run(tools.invoke("whatsapp.send", {"to": NILGIRI_WA, "body": "Reminder"}, ctx(ALL, key="k2")))
    assert wa.ok and wa.output["to_addr"] == "+919000010012"


def test_whatsapp_has_no_subject(tools, ctx):
    res = run(tools.invoke("whatsapp.send", {"to": NILGIRI_WA, "body": "x", "subject": "s"}, ctx(ALL, key="k")))
    assert res.error_kind == "permanent"


def test_channel_scope_is_per_channel(tools, ctx):
    res = run(tools.invoke("whatsapp.send", {"to": NILGIRI_WA, "body": "x"}, ctx({"email"}, key="k")))
    assert res.error_kind == "denied"


def test_inbox_thread_and_attachment(tools, ctx, hub_transport, sandbox_var):
    sent = run(tools.invoke("email.send", {"to": GLOBEX, "subject": "INV-2291", "body": "Copy attached",
                                          "attachments": ["invoices/INV-2291.pdf"]},
                           ctx(ALL, key="k", attachable=["invoices/INV-2291.pdf"])))
    assert sent.output["attachments"][0]["filename"] == "INV-2291.pdf"

    async def customer_reply():
        async with httpx.AsyncClient(transport=hub_transport, base_url="http://hub", headers=SIM) as c:
            await c.post("/api/v1/messages", json={
                "channel": "email", "from": GLOBEX, "to": "ar@sharma-distributors.example",
                "subject": "Re: INV-2291", "body": "We will pay Thursday", "in_reply_to": sent.output["id"],
                "attachments": [{"filename": "remittance.pdf", "content_b64": "JVBERi0xLjQK"}]})
    asyncio.run(customer_reply())

    inbox = run(tools.invoke("email.inbox", {}, ctx(ALL)))
    assert inbox.untrusted and [m["body"] for m in inbox.output] == ["We will pay Thursday"]
    reply = inbox.output[0]
    assert run(tools.invoke("email.inbox", {"since_seq": reply["seq"]}, ctx(ALL))).output == []
    thread = run(tools.invoke("email.thread", {"thread_id": reply["thread_id"]}, ctx(ALL)))
    assert [m["body"] for m in thread.output["messages"]] == ["Copy attached", "We will pay Thursday"]
    att = run(tools.invoke("email.attachment", {"message_id": reply["id"], "filename": "remittance.pdf"}, ctx(ALL)))
    assert att.ok and open(att.output["path"], "rb").read().startswith(b"%PDF")


def test_attachment_paths_confined(tools, ctx):
    res = run(tools.invoke("email.send", {"to": GLOBEX, "body": "x", "attachments": ["../erp.db"]},
                           ctx(ALL, key="k")))
    assert res.error_kind == "denied" and "outside" in res.error


def test_only_documents_offered_for_this_message_can_be_attached(tools, ctx):
    """Another customer's invoice or the internal credit policy never leaves with a message."""
    offered = ["invoices/INV-2291.pdf"]
    for other in ("invoices/INV-2240.pdf", "contracts/C02_supply_agreement.pdf"):
        res = run(tools.invoke("email.send", {"to": GLOBEX, "body": "x", "attachments": [other]},
                               ctx(ALL, key=f"k-{other}", attachable=offered)))
        assert res.error_kind == "denied" and "not offered" in res.error, other
    res = run(tools.invoke("email.send", {"to": GLOBEX, "body": "x", "attachments": offered},
                           ctx(ALL, key="k-own")))
    assert res.error_kind == "denied", "nothing offered: only the task's own files may go out"


def test_call_request_and_status(tools, ctx):
    call = run(tools.invoke("call.request", {"to": "+91-90000-10008", "purpose": "INV-2266"}, ctx(ALL, key="k")))
    assert call.ok and call.output["status"] == "requested"
    status = run(tools.invoke("call.status", {"call_id": call.output["id"]}, ctx(ALL)))
    assert status.output["status"] == "requested" and status.untrusted


def test_hub_token_never_leaks(tools, ctx, events):
    run(tools.invoke("email.send", {"to": GLOBEX, "body": "x"}, ctx(ALL, key="k")))
    assert all("hub-arya-token" not in e.model_dump_json() for e in events.query())


# --- files -----------------------------------------------------------------------------

def test_list_and_read_pdf(tools, ctx):
    listing = run(tools.invoke("files.list", {"folder": "invoices", "pattern": "INV-22*.pdf"}, ctx(ALL)))
    assert len(listing.output) == 17 and listing.output[0]["path"] == "invoices/INV-2240.pdf"
    pdf = run(tools.invoke("files.read_pdf", {"path": "invoices/INV-2291.pdf"}, ctx(ALL)))
    assert pdf.untrusted and "Rs. 3,40,000.00" in pdf.output["pages"][0]


def test_read_policy_text(tools, ctx):
    res = run(tools.invoke("files.read_text", {"path": "policies/credit_policy.md"}, ctx(ALL)))
    assert "Threaten legal action" in res.output["text"]


@pytest.mark.parametrize("path", ["../erp.db", "/etc/passwd", "invoices/../../bank.db"])
def test_path_escape_denied(tools, ctx, path):
    assert run(tools.invoke("files.read_text", {"path": path}, ctx(ALL))).error_kind == "denied"


def test_symlink_escape_denied(tools, ctx, sandbox_var):
    (sandbox_var / "drive" / "sneaky.md").symlink_to(sandbox_var / "clock.json")
    assert run(tools.invoke("files.read_text", {"path": "sneaky.md"}, ctx(ALL))).error_kind == "denied"


def test_read_csv_and_xlsx_from_task_dir(tools, ctx):
    c = ctx(ALL)
    folder = c.task_dir() / "downloads"
    folder.mkdir(parents=True)
    with open(folder / "s.csv", "w", newline="") as fh:
        csv.writer(fh).writerows([["Date", "Narration"], ["14/10/26", "NEFT/N1/MEHTA"], ["15/10/26", "X"]])
    res = run(tools.invoke("files.read_table", {"path": str(folder / "s.csv"), "max_rows": 1}, c))
    assert res.output["header"] == ["Date", "Narration"] and res.output["rows"] == [["14/10/26", "NEFT/N1/MEHTA"]]
    assert res.output["truncated"] and res.output["row_count"] == 2
    wb = Workbook()
    wb.active.append(["Customer", "Outstanding"])
    wb.active.append(["Globex", 340000])
    wb.save(folder / "tracker.xlsx")
    xl = run(tools.invoke("files.read_table", {"path": str(folder / "tracker.xlsx")}, c))
    assert xl.output["rows"] == [["Globex", "340000"]]


def test_missing_file_is_permanent(tools, ctx):
    assert run(tools.invoke("files.read_pdf", {"path": "invoices/NOPE.pdf"}, ctx(ALL))).error_kind == "permanent"


def test_attachment_message_id_is_an_id_not_a_path(tools, ctx):
    res = run(tools.invoke("email.attachment", {"message_id": "../../packs", "filename": "role.yaml"}, ctx(ALL)))
    assert res.error_kind == "invalid_args"
