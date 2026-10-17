from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.message_hub import app as hub_app
from sandbox.message_hub.app import app

ARYA = {"Authorization": "Bearer hub-arya-token"}
SIM = {"Authorization": "Bearer hub-sim-token"}
AR_EMAIL = "ar@sharma-distributors.example"
AR_WA = "+91-90000-00000"
GLOBEX = "ritu.malhotra@globex-retail.example"


@pytest.fixture()
def client(sandbox_var):
    return TestClient(app)


def send(client, headers, **body):
    body.setdefault("channel", "email")
    body.setdefault("body", "hello")
    return client.post("/api/v1/messages", json=body, headers=headers)


def test_requires_token(client):
    assert send(client, {}, **{"from": AR_EMAIL, "to": GLOBEX}).status_code == 401
    assert send(client, {"Authorization": "Bearer nope"}, **{"from": AR_EMAIL, "to": GLOBEX}).status_code == 401


def test_email_round_trip_threads(client):
    out = send(client, ARYA, **{"from": AR_EMAIL, "to": GLOBEX, "subject": "INV-2291 reminder",
                                "body": "Gentle reminder"})
    assert out.status_code == 201 and out.json()["status"] == "delivered"
    msg = out.json()
    inbox = client.get(f"/api/v1/mailboxes/{GLOBEX}/messages", headers=SIM).json()
    assert [m["id"] for m in inbox] == [msg["id"]]
    reply = send(client, SIM, **{"from": GLOBEX, "to": AR_EMAIL, "subject": "Re: INV-2291 reminder",
                                 "body": "We will pay on Thursday", "in_reply_to": msg["id"]}).json()
    assert reply["thread_id"] == msg["thread_id"]
    thread = client.get(f"/api/v1/threads/{msg['thread_id']}", headers=ARYA).json()
    assert [m["body"] for m in thread["messages"]] == ["Gentle reminder", "We will pay on Thursday"]
    arya_inbox = client.get(f"/api/v1/mailboxes/{AR_EMAIL}/messages", headers=ARYA).json()
    assert [m["id"] for m in arya_inbox] == [reply["id"]]
    # since_seq polling returns only newer messages
    assert client.get(f"/api/v1/mailboxes/{AR_EMAIL}/messages", params={"since_seq": reply["seq"]},
                      headers=ARYA).json() == []


def test_arya_cannot_impersonate_or_snoop(client):
    assert send(client, ARYA, **{"from": GLOBEX, "to": AR_EMAIL}).status_code == 403
    assert client.get(f"/api/v1/mailboxes/{GLOBEX}/messages", headers=ARYA).status_code == 403
    # A customer-to-customer message is invisible to Arya.
    other = send(client, SIM, **{"from": GLOBEX, "to": "kunal@mehta-electricals.example"}).json()
    assert client.get(f"/api/v1/messages/{other['id']}", headers=ARYA).status_code == 404


def test_dead_mailbox_bounces_and_whatsapp_works(client):
    out = send(client, ARYA, **{"from": AR_EMAIL, "to": "anand.k@nilgiriwholesale.example", "subject": "INV-2273"}).json()
    assert out["status"] == "bounced" and "550" in out["status_detail"]
    inbox = client.get(f"/api/v1/mailboxes/{AR_EMAIL}/messages", headers=ARYA).json()
    assert inbox[0]["from_addr"] == "mailer-daemon@hub.example" and inbox[0]["in_reply_to"] == out["id"]
    wa = send(client, ARYA, channel="whatsapp", **{"from": AR_WA, "to": "+91 90000 10012"}).json()
    assert wa["status"] == "delivered" and wa["to_addr"] == "+919000010012"


def test_unknown_recipients(client):
    assert send(client, ARYA, **{"from": AR_EMAIL, "to": "nobody@nowhere.example"}).json()["status"] == "bounced"
    assert send(client, ARYA, channel="whatsapp", **{"from": AR_WA, "to": "+91-99999-99999"}).json()["status"] == "failed"


def test_channel_must_match_mailbox(client):
    assert send(client, ARYA, channel="whatsapp", **{"from": AR_EMAIL, "to": GLOBEX}).status_code == 403


def test_idempotency_key(client):
    first = send(client, ARYA, idempotency_key="m1:INV-2291:reminder1", **{"from": AR_EMAIL, "to": GLOBEX}).json()
    again = send(client, ARYA, idempotency_key="m1:INV-2291:reminder1", **{"from": AR_EMAIL, "to": GLOBEX}).json()
    assert again["id"] == first["id"] and again["duplicate"] is True
    assert len(client.get(f"/api/v1/mailboxes/{GLOBEX}/messages", headers=SIM).json()) == 1


def test_attachments_round_trip_and_sanitised(client, sandbox_var):
    pdf = (sandbox_var / "drive" / "invoices" / "INV-2291.pdf").read_bytes()
    out = send(client, ARYA, **{"from": AR_EMAIL, "to": GLOBEX, "attachments": [
        {"filename": "../../etc/INV-2291.pdf", "content_b64": base64.b64encode(pdf).decode(),
         "content_type": "application/pdf"}]}).json()
    assert out["attachments"] == [{"filename": "INV-2291.pdf", "content_type": "application/pdf", "size": len(pdf)}]
    got = client.get(f"/api/v1/messages/{out['id']}/attachments/INV-2291.pdf", headers=SIM)
    assert got.status_code == 200 and got.content == pdf
    bad = send(client, ARYA, **{"from": AR_EMAIL, "to": GLOBEX,
                                "attachments": [{"filename": "x.pdf", "content_b64": "%%%"}]})
    assert bad.status_code == 400


def test_webhook_fires_on_inbound(client, monkeypatch):
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append((str(request.url), request.read()))
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(hub_app.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    hook = client.post("/api/v1/webhooks", json={"mailbox": AR_EMAIL, "url": "http://runtime.local/hooks/hub"},
                       headers=ARYA)
    assert hook.status_code == 201
    assert client.post("/api/v1/webhooks", json={"mailbox": GLOBEX, "url": "http://x/"}, headers=ARYA).status_code == 403
    msg = send(client, SIM, **{"from": GLOBEX, "to": AR_EMAIL, "body": "Paid already!"}).json()
    assert len(received) == 1
    url, payload = received[0]
    assert url == "http://runtime.local/hooks/hub" and msg["id"].encode() in payload
    # Outbound mail to a customer must not trigger Arya's webhook.
    send(client, ARYA, **{"from": AR_EMAIL, "to": GLOBEX})
    assert len(received) == 1


def test_call_request_and_transcript(client):
    call = client.post("/api/v1/calls", json={"from": AR_WA, "to": "+91-90000-10008", "purpose": "INV-2266 follow-up"},
                       headers=ARYA)
    assert call.status_code == 201 and call.json()["status"] == "requested"
    cid = call.json()["id"]
    assert client.post(f"/api/v1/calls/{cid}/transcript", json={"status": "completed", "transcript": "x"},
                       headers=ARYA).status_code == 403
    done = client.post(f"/api/v1/calls/{cid}/transcript", json={"status": "no_answer"}, headers=SIM).json()
    assert done["status"] == "no_answer"
    assert client.get(f"/api/v1/calls/{cid}", headers=ARYA).json()["status"] == "no_answer"


def test_observer_page(client):
    send(client, ARYA, **{"from": AR_EMAIL, "to": GLOBEX, "subject": "Visible in observer"})
    assert "Visible in observer" in client.get("/").text
