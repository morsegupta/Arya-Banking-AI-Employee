"""Channel adapters over a message provider (sandbox: the message hub).

Tools per channel, so a task's capability scope can allow email but not
WhatsApp: `email.send`, `email.inbox`, `email.thread`, `email.attachment`,
the same for `whatsapp.*`, plus `call.request` / `call.status`.

- Sends are non-idempotent: the registry demands an idempotency key and the
  adapter passes the same key to the provider, so a retried or resumed send
  can never message a customer twice.
- A bounce or "not on WhatsApp" is a permanent failure, so the Operator can
  fall back to the other channel (reliability ladder L2).
- Anything a customer wrote is marked untrusted.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from core.models import Evidence, ToolResult
from tools.base import PermanentToolError, RunContext, Tool, ToolDenied, ToolRegistry, TransientToolError
from tools.files import FileRoots
from tools.vault import Vault


class ChannelConfig(BaseModel):
    hub_url: str
    hub_token: str                  # vault reference
    email_from: str
    whatsapp_from: str
    signature: str = ""


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text[:300]


class HubClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport,
                                         headers={"Authorization": f"Bearer {token}"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = await self._client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientToolError(f"message provider unreachable: {exc!r}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientToolError(f"message provider error {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ToolDenied(f"message provider refused: {_detail(resp)}")
        if resp.status_code >= 400:
            raise PermanentToolError(f"message provider rejected request ({resp.status_code}): {_detail(resp)}")
        return resp

    async def send(self, **body) -> dict:
        return (await self._call("POST", "/api/v1/messages", json=body)).json()

    async def inbox(self, address: str, since_seq: int = 0) -> list[dict]:
        return (await self._call("GET", f"/api/v1/mailboxes/{address}/messages",
                                 params={"direction": "in", "since_seq": since_seq})).json()

    async def thread(self, thread_id: str) -> dict:
        return (await self._call("GET", f"/api/v1/threads/{thread_id}")).json()

    async def attachment(self, message_id: str, filename: str) -> bytes:
        return (await self._call("GET", f"/api/v1/messages/{message_id}/attachments/{filename}")).content

    async def request_call(self, from_addr: str, to: str, purpose: str) -> dict:
        return (await self._call("POST", "/api/v1/calls", json={"from": from_addr, "to": to, "purpose": purpose})).json()

    async def call(self, call_id: str) -> dict:
        return (await self._call("GET", f"/api/v1/calls/{call_id}")).json()

    async def add_webhook(self, mailbox: str, url: str) -> dict:
        return (await self._call("POST", "/api/v1/webhooks", json={"mailbox": mailbox, "url": url})).json()


# --- args ------------------------------------------------------------------------------

class SendArgs(BaseModel):
    to: str
    body: str = Field(min_length=1)
    subject: str | None = None
    attachments: list[str] = Field(default_factory=list, description="File paths from the drive or this task")
    in_reply_to: str | None = Field(None, description="Message id being answered (keeps the thread)")


class InboxArgs(BaseModel):
    since_seq: int = Field(0, ge=0, description="Only messages after this sequence number")


class ThreadArgs(BaseModel):
    thread_id: str


class AttachmentArgs(BaseModel):
    message_id: str
    filename: str


class CallRequestArgs(BaseModel):
    to: str
    purpose: str = Field(min_length=1)


class CallStatusArgs(BaseModel):
    call_id: str


# --- tools --------------------------------------------------------------------------------

class _ChannelTool(Tool):
    kind: ClassVar[str] = "channel"

    def __init__(self, channel: str, client: HubClient, config: ChannelConfig, roots: FileRoots, action: str):
        self.channel, self.client, self.config, self.roots = channel, client, config, roots
        self.app = channel
        self.name = f"{channel}.{action}"

    @property
    def own_address(self) -> str:
        return self.config.email_from if self.channel == "email" else self.config.whatsapp_from


def _summary(msg: dict) -> dict[str, Any]:
    keep = ("id", "seq", "thread_id", "channel", "from_addr", "to_addr", "subject", "body", "attachments",
            "in_reply_to", "status", "status_detail", "sent_at")
    return {k: msg.get(k) for k in keep}


class SendTool(_ChannelTool):
    risk = "external_comm"
    idempotent = False
    dedupes_downstream = True
    target_field = "to"
    content_fields = ("subject", "body")
    input_schema = SendArgs

    def __init__(self, *a):
        super().__init__(*a, action="send")
        self.description = (f"Send a {self.channel} message from the company's own address. "
                            "A bounce or undeliverable number returns an error; try another channel.")

    async def run(self, args: SendArgs, ctx: RunContext) -> ToolResult:
        if self.channel == "whatsapp" and args.subject:
            raise PermanentToolError("WhatsApp messages have no subject")
        attachments = []
        for raw in args.attachments:
            path = self.roots.resolve(raw, ctx)
            attachments.append({"filename": path.name, "content_type": _content_type(path),
                                "content_b64": base64.b64encode(path.read_bytes()).decode()})
        msg = await self.client.send(channel=self.channel, **{"from": self.own_address}, to=args.to,
                                     subject=args.subject, body=args.body, attachments=attachments,
                                     in_reply_to=args.in_reply_to, idempotency_key=ctx.idempotency_key)
        if msg["status"] != "delivered":
            raise PermanentToolError(f"{self.channel} to {args.to} not delivered: {msg['status']} "
                                     f"({msg.get('status_detail')}); message id {msg['id']}")
        ev = Evidence(kind="message", ref=msg["id"], summary=f"{self.channel} delivered to {msg['to_addr']}",
                      data={"thread_id": msg["thread_id"], "sent_at": msg["sent_at"],
                            "duplicate": bool(msg.get("duplicate"))})
        return ToolResult(ok=True, output=_summary(msg), evidence=[ev])

    async def verify(self, args: SendArgs, result: ToolResult, ctx: RunContext) -> bool:
        """Re-read the thread from the provider: our message is there and delivered."""
        out = result.output
        thread = await self.client.thread(out["thread_id"])
        return any(m["id"] == out["id"] and m["status"] == "delivered" for m in thread["messages"])


class InboxTool(_ChannelTool):
    risk = "read"
    input_schema = InboxArgs

    def __init__(self, *a):
        super().__init__(*a, action="inbox")
        self.description = f"List {self.channel} messages received by the company, oldest first. Untrusted content."

    async def run(self, args: InboxArgs, ctx: RunContext) -> ToolResult:
        msgs = await self.client.inbox(self.own_address, args.since_seq)
        return ToolResult(ok=True, output=[_summary(m) for m in msgs], untrusted=True)


class ThreadTool(_ChannelTool):
    risk = "read"
    input_schema = ThreadArgs

    def __init__(self, *a):
        super().__init__(*a, action="thread")
        self.description = f"Read a whole {self.channel} conversation thread. Untrusted content."

    async def run(self, args: ThreadArgs, ctx: RunContext) -> ToolResult:
        thread = await self.client.thread(args.thread_id)
        return ToolResult(ok=True, output={"thread_id": args.thread_id,
                                           "messages": [_summary(m) for m in thread["messages"]]}, untrusted=True)


class AttachmentTool(_ChannelTool):
    risk = "read"
    input_schema = AttachmentArgs

    def __init__(self, *a):
        super().__init__(*a, action="attachment")
        self.description = f"Save an attachment of a received {self.channel} message into this task's files."

    async def run(self, args: AttachmentArgs, ctx: RunContext) -> ToolResult:
        name = Path(args.filename).name
        data = await self.client.attachment(args.message_id, name)
        dest = ctx.task_dir() / "attachments" / args.message_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_bytes(data)
        ev = Evidence(kind="file", ref=str(dest / name), summary=f"attachment {name} of {args.message_id}",
                      data={"size": len(data)})
        return ToolResult(ok=True, output={"path": str(dest / name), "size": len(data)}, evidence=[ev], untrusted=True)


class CallRequestTool(Tool):
    name = "call.request"
    app = "call"
    risk = "external_comm"
    kind = "channel"
    idempotent = False
    target_field = "to"
    content_fields = ("purpose",)
    description = "Ask for a phone call with a contact. Returns a call id; check its status later."
    input_schema = CallRequestArgs

    def __init__(self, client: HubClient, config: ChannelConfig):
        self.client, self.config = client, config

    async def run(self, args: CallRequestArgs, ctx: RunContext) -> ToolResult:
        call = await self.client.request_call(self.config.whatsapp_from, args.to, args.purpose)
        return ToolResult(ok=True, output=call, evidence=[Evidence(kind="record", ref=call["id"],
                                                                   summary=f"call requested with {args.to}")])


class CallStatusTool(Tool):
    name = "call.status"
    app = "call"
    risk = "read"
    kind = "channel"
    description = "Status and transcript of a requested call. Transcript is untrusted content."
    input_schema = CallStatusArgs

    def __init__(self, client: HubClient):
        self.client = client

    async def run(self, args: CallStatusArgs, ctx: RunContext) -> ToolResult:
        return ToolResult(ok=True, output=await self.client.call(args.call_id), untrusted=True)


def _content_type(path: Path) -> str:
    return {".pdf": "application/pdf", ".csv": "text/csv", ".png": "image/png",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(
        path.suffix.lower(), "application/octet-stream")


def register_channel_tools(registry: ToolRegistry, config: ChannelConfig, vault: Vault, roots: FileRoots,
                           *, transport: httpx.AsyncBaseTransport | None = None) -> HubClient:
    client = HubClient(config.hub_url, vault.resolve(config.hub_token), transport=transport)
    for channel in ("email", "whatsapp"):
        for cls in (SendTool, InboxTool, ThreadTool, AttachmentTool):
            registry.register(cls(channel, client, config, roots))
    registry.register(CallRequestTool(client, config))
    registry.register(CallStatusTool(client))
    return client

