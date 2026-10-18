"""Message hub: a fake email + WhatsApp provider for the sandbox.

Both Arya and the customer simulators talk through it, with different tokens:
- Arya's token may only send from / read the company's own mailboxes.
- The simulator's token may act for any customer mailbox.

Realistic provider behaviour: delivery status, bounces for dead mailboxes,
threads, attachments, idempotency keys, and webhooks on inbound messages.
Call requests are recorded; the simulator later attaches a transcript.

Run: uvicorn sandbox.message_hub.app:app --port 8103
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from sandbox import chaos
from sandbox.common import clock, config, db
from sandbox.common.web import templates
from sandbox.message_hub.db import DB_NAME, MAILER_DAEMON, normalise

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
WEBHOOK_ATTEMPTS = 3

app = FastAPI(title="Sandbox Message Hub (email + WhatsApp)", docs_url="/api/docs", openapi_url="/api/openapi.json")
tpl = templates(Path(__file__).parent / "templates")


# --- auth -------------------------------------------------------------------

class Caller(BaseModel):
    label: str
    allowed: set[str] | Literal["*"]

    def may_use(self, address: str) -> bool:
        return self.allowed == "*" or address in self.allowed


def caller(authorization: str | None = Header(default=None)) -> Caller:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token = ?", (authorization[7:],)).fetchone()
    if row is None:
        raise HTTPException(401, "Unknown token")
    allowed = "*" if row["allowed"] == "*" else set(row["allowed"].split(","))
    return Caller(label=row["label"], allowed=allowed)


# --- models -----------------------------------------------------------------

class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_b64: str
    content_type: str = "application/octet-stream"


class SendIn(BaseModel):
    channel: Literal["email", "whatsapp"]
    from_addr: str = Field(alias="from")
    to: str
    subject: str | None = None
    body: str = Field(min_length=1)
    attachments: list[AttachmentIn] = Field(default_factory=list)
    in_reply_to: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)

    model_config = {"populate_by_name": True}


class WebhookIn(BaseModel):
    mailbox: str
    url: str = Field(pattern=r"^https?://")


class CallIn(BaseModel):
    from_addr: str = Field(alias="from")
    to: str
    purpose: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class TranscriptIn(BaseModel):
    status: Literal["completed", "no_answer"]
    transcript: str | None = None


# --- helpers ------------------------------------------------------------------

def attachments_dir() -> Path:
    return config.var_dir() / "hub" / "attachments"


def message_out(row: sqlite3.Row) -> dict:
    msg = dict(row)
    msg["attachments"] = json.loads(msg.pop("attachments_json"))
    return msg


def mailbox(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mailboxes WHERE address = ?", (address,)).fetchone()


def new_id(prefix: str) -> str:
    # Random, not MAX(seq)+1: concurrent sends must never collide. Order by `seq`.
    return f"{prefix}_{secrets.token_hex(6)}"


def _insert_message(conn: sqlite3.Connection, **fields) -> str:
    msg_id = new_id("msg")
    fields.setdefault("thread_id", f"thr_{secrets.token_hex(4)}")
    fields.setdefault("attachments_json", "[]")
    fields.setdefault("sent_at", clock.now().isoformat())
    fields["id"] = msg_id
    cols = ",".join(fields)
    conn.execute(f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
    return msg_id


def _decode_attachments(attachments: list[AttachmentIn]) -> list[tuple[str, str, bytes]]:
    """Validate all attachments before anything is stored. Returns (name, content_type, data)."""
    decoded = []
    for att in attachments:
        name = Path(att.filename).name  # strip any path components
        if not name or name in {".", ".."}:
            raise HTTPException(400, f"bad attachment filename {att.filename!r}")
        try:
            data = base64.b64decode(att.content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, f"attachment {name} is not valid base64")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"attachment {name} exceeds 5 MB")
        decoded.append((name, att.content_type, data))
    return decoded


def _store_attachments(msg_id: str, decoded: list[tuple[str, str, bytes]]) -> list[dict]:
    folder = attachments_dir() / msg_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, _, data in decoded:
        (folder / name).write_bytes(data)
    return [{"filename": n, "content_type": ct, "size": len(d)} for n, ct, d in decoded]


async def _deliver_webhooks(message_id: str, to_addr: str) -> None:
    with db.session(DB_NAME) as conn:
        hooks = [dict(r) for r in conn.execute("SELECT * FROM webhooks WHERE mailbox = ?", (to_addr,))]
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not hooks or row is None:
        return
    payload = {"event": "message.received", "message": message_out(row)}
    async with httpx.AsyncClient(timeout=5) as client:
        for hook in hooks:
            status, error, attempt = "failed", None, 0
            for attempt in range(1, WEBHOOK_ATTEMPTS + 1):
                try:
                    resp = await client.post(hook["url"], json=payload)
                    if resp.status_code < 300:
                        status, error = "delivered", None
                        break
                    error = f"HTTP {resp.status_code}"
                except httpx.HTTPError as exc:
                    error = repr(exc)
                await asyncio.sleep(0.2 * 2 ** attempt)
            with db.session(DB_NAME) as conn:
                conn.execute(
                    "INSERT INTO webhook_deliveries (webhook_id, message_id, status, attempts, last_error) "
                    "VALUES (?,?,?,?,?)", (hook["id"], message_id, status, attempt, error))


# --- API ----------------------------------------------------------------------

@app.post("/api/v1/messages", status_code=201)
def send(body: SendIn, background: BackgroundTasks, who: Caller = Depends(caller)):
    from_addr = normalise(body.channel, body.from_addr)
    to_addr = normalise(body.channel, body.to)
    if not who.may_use(from_addr):
        raise HTTPException(403, f"token '{who.label}' may not send from {from_addr}")
    if body.channel == "email" and who.allowed != "*" and chaos.get("hub.email_outage"):
        raise HTTPException(503, "email service temporarily unavailable")

    with db.session(DB_NAME) as conn:
        if body.idempotency_key:
            dup = conn.execute("SELECT * FROM messages WHERE idempotency_key = ?", (body.idempotency_key,)).fetchone()
            if dup is not None:
                return {**message_out(dup), "duplicate": True}
        sender = mailbox(conn, from_addr)
        if sender is None or sender["channel"] != body.channel:
            raise HTTPException(400, f"{from_addr} is not a {body.channel} mailbox on this hub")

        thread_id = None
        if body.in_reply_to:
            parent = conn.execute("SELECT thread_id FROM messages WHERE id = ?", (body.in_reply_to,)).fetchone()
            if parent is None:
                raise HTTPException(400, f"in_reply_to {body.in_reply_to} not found")
            thread_id = parent["thread_id"]

        recipient = mailbox(conn, to_addr)
        if recipient is None or recipient["channel"] != body.channel:
            status, detail = ("bounced", "550 mailbox not found") if body.channel == "email" \
                else ("failed", "number is not on WhatsApp")
        elif recipient["bounces"]:
            status, detail = "bounced", "550 5.1.1 mailbox unavailable"
        else:
            status, detail = "delivered", None

        decoded = _decode_attachments(body.attachments)
        fields = dict(channel=body.channel, from_addr=from_addr, to_addr=to_addr, subject=body.subject,
                      body=body.body, in_reply_to=body.in_reply_to, status=status, status_detail=detail,
                      idempotency_key=body.idempotency_key, sent_by=who.label)
        if thread_id:
            fields["thread_id"] = thread_id
        msg_id = _insert_message(conn, **fields)
        if decoded:
            conn.execute("UPDATE messages SET attachments_json = ? WHERE id = ?",
                         (json.dumps(_store_attachments(msg_id, decoded)), msg_id))

        if status == "bounced":
            orig = conn.execute("SELECT thread_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
            _insert_message(conn, thread_id=orig["thread_id"], channel="email", from_addr=MAILER_DAEMON,
                            to_addr=from_addr, subject=f"Undeliverable: {body.subject or ''}".strip(),
                            body=f"Delivery to {to_addr} failed: {detail}", in_reply_to=msg_id,
                            status="delivered", sent_by="hub")
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()

    if status == "delivered":
        background.add_task(_deliver_webhooks, msg_id, to_addr)
    return message_out(row)


@app.get("/api/v1/mailboxes/{address}/messages")
def list_messages(address: str, direction: Literal["in", "out", "all"] = "in", since_seq: int = 0,
                  who: Caller = Depends(caller)):
    addr = normalise("whatsapp", address) if address.startswith("+") else address.lower()
    if not who.may_use(addr):
        raise HTTPException(403, f"token '{who.label}' may not read {addr}")
    where = {"in": "to_addr = ?", "out": "from_addr = ?", "all": "(to_addr = ? OR from_addr = ?)"}[direction]
    args = [addr, addr] if direction == "all" else [addr]
    with db.session(DB_NAME) as conn:
        rows = conn.execute(f"SELECT * FROM messages WHERE {where} AND seq > ? ORDER BY seq", [*args, since_seq])
        return [message_out(r) for r in rows]


def _visible(who: Caller, row: sqlite3.Row) -> bool:
    return who.may_use(row["from_addr"]) or who.may_use(row["to_addr"])


@app.get("/api/v1/messages/{message_id}")
def get_message(message_id: str, who: Caller = Depends(caller)):
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None or not _visible(who, row):
        raise HTTPException(404, "message not found")
    return message_out(row)


@app.get("/api/v1/messages/{message_id}/attachments/{filename}")
def get_attachment(message_id: str, filename: str, who: Caller = Depends(caller)):
    get_message(message_id, who)
    path = attachments_dir() / message_id / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "attachment not found")
    return FileResponse(path, filename=path.name)


@app.get("/api/v1/threads/{thread_id}")
def get_thread(thread_id: str, who: Caller = Depends(caller)):
    with db.session(DB_NAME) as conn:
        rows = [r for r in conn.execute("SELECT * FROM messages WHERE thread_id = ? ORDER BY seq", (thread_id,))]
    rows = [r for r in rows if _visible(who, r)]
    if not rows:
        raise HTTPException(404, "thread not found")
    return {"thread_id": thread_id, "messages": [message_out(r) for r in rows]}


@app.post("/api/v1/webhooks", status_code=201)
def add_webhook(body: WebhookIn, who: Caller = Depends(caller)):
    addr = normalise("whatsapp", body.mailbox) if body.mailbox.startswith("+") else body.mailbox.lower()
    if not who.may_use(addr):
        raise HTTPException(403, f"token '{who.label}' may not subscribe to {addr}")
    with db.session(DB_NAME) as conn:
        if mailbox(conn, addr) is None:
            raise HTTPException(404, "mailbox not found")
        conn.execute("INSERT OR IGNORE INTO webhooks (mailbox, url) VALUES (?, ?)", (addr, body.url))
        row = conn.execute("SELECT * FROM webhooks WHERE mailbox = ? AND url = ?", (addr, body.url)).fetchone()
    return dict(row)


@app.post("/api/v1/calls", status_code=201)
def request_call(body: CallIn, who: Caller = Depends(caller)):
    from_addr, to_addr = normalise("whatsapp", body.from_addr), normalise("whatsapp", body.to)
    if not who.may_use(from_addr):
        raise HTTPException(403, f"token '{who.label}' may not call from {from_addr}")
    with db.session(DB_NAME) as conn:
        if mailbox(conn, to_addr) is None:
            raise HTTPException(404, "number not found")
        call_id = new_id("call")
        conn.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                     (call_id, from_addr, to_addr, body.purpose, "requested", None, clock.now().isoformat(), None))
        return dict(conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone())


@app.get("/api/v1/calls/{call_id}")
def get_call(call_id: str, who: Caller = Depends(caller)):
    with db.session(DB_NAME) as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if row is None or not (who.may_use(row["from_addr"]) or who.may_use(row["to_addr"])):
        raise HTTPException(404, "call not found")
    return dict(row)


@app.post("/api/v1/calls/{call_id}/transcript")
def complete_call(call_id: str, body: TranscriptIn, who: Caller = Depends(caller)):
    if who.allowed != "*":
        raise HTTPException(403, "only the customer simulator may complete calls")
    with db.session(DB_NAME) as conn:
        cur = conn.execute("UPDATE calls SET status = ?, transcript = ?, completed_at = ? WHERE id = ?",
                           (body.status, body.transcript, clock.now().isoformat(), call_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "call not found")
        return dict(conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone())


# --- observer UI (sandbox inspection for humans, not used by the agent) ---------

@app.get("/", response_class=HTMLResponse)
def observer(request: Request):
    with db.session(DB_NAME) as conn:
        rows = [message_out(r) for r in conn.execute("SELECT * FROM messages ORDER BY seq DESC LIMIT 200")]
        calls = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY id DESC")]
    return tpl.TemplateResponse(request, "observer.html", {"messages": rows, "calls": calls})


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "message_hub", "now": clock.now().isoformat()}
