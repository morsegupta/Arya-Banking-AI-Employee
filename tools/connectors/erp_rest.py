"""ERP REST connector (read-only): the API tier, preferred over the browser (ADR-7).

Outputs are trimmed to the fields an agent needs, to keep prompts small.
Writes (posting receipts) are not possible here: the ERP only accepts them
through its web UI, which the Operator drives with the erp_web browser tools.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from core.models import Evidence, ToolResult
from tools.base import PermanentToolError, RunContext, Tool, ToolDenied, ToolRegistry, TransientToolError
from tools.vault import Vault

INVOICE_FIELDS = ("number", "customer_id", "customer_name", "issue_date", "due_date", "days_overdue",
                  "total_paise", "balance_paise", "status", "dispute_note")


class ErpConfig(BaseModel):
    base_url: str
    api_key: str        # vault reference


class ErpClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=15, transport=transport,
                                       headers={"X-API-Key": api_key})

    async def get(self, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._http.get(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientToolError(f"ERP unreachable: {exc!r}") from exc
        if resp.status_code >= 500:
            raise TransientToolError(f"ERP error {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ToolDenied("ERP refused the API key")
        if resp.status_code == 404:
            raise PermanentToolError(f"not found: {path}")
        if resp.status_code >= 400:
            raise PermanentToolError(f"ERP rejected {path}: {resp.text[:200]}")
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()


class ListInvoicesArgs(BaseModel):
    status: str | None = Field(None, description="Comma separated: OPEN, PARTIALLY_PAID, PAID, DISPUTED")
    customer_id: str | None = None
    min_days_overdue: int | None = None
    max_days_overdue: int | None = None


class InvoiceArgs(BaseModel):
    number: str


class CustomerArgs(BaseModel):
    customer_id: str


class ReceiptsArgs(BaseModel):
    invoice: str | None = None


class NoArgs(BaseModel):
    pass


class _ErpTool(Tool):
    app: ClassVar[str] = "erp_api"
    risk: ClassVar[str] = "read"
    kind: ClassVar[str] = "api"

    def __init__(self, client: ErpClient):
        self.client = client


def _trim(inv: dict) -> dict:
    return {k: inv.get(k) for k in INVOICE_FIELDS}


class ListInvoices(_ErpTool):
    name = "erp.list_invoices"
    description = "List invoices with filters. Amounts are integer paise. days_overdue is as of the ERP's today."
    input_schema = ListInvoicesArgs

    async def run(self, args: ListInvoicesArgs, ctx: RunContext) -> ToolResult:
        data = await self.client.get("/api/v1/invoices", **args.model_dump())
        rows = [_trim(i) for i in data["invoices"]]
        return ToolResult(ok=True, output={"as_of": data["as_of"], "count": len(rows), "invoices": rows},
                          evidence=[Evidence(kind="record", ref="erp:/api/v1/invoices",
                                             summary=f"{len(rows)} invoices as of {data['as_of']}",
                                             data={"filters": args.model_dump(exclude_none=True)})])


class GetInvoice(_ErpTool):
    name = "erp.get_invoice"
    description = "One invoice with line items and posted receipts."
    input_schema = InvoiceArgs

    async def run(self, args: InvoiceArgs, ctx: RunContext) -> ToolResult:
        inv = await self.client.get(f"/api/v1/invoices/{args.number}")
        out = {**_trim(inv), "lines": inv["lines"], "receipts": inv["receipts"], "pdf_path": inv["pdf_path"]}
        return ToolResult(ok=True, output=out, evidence=[Evidence(kind="record", ref=f"erp:{args.number}",
                                                                  summary=f"{args.number} {inv['status']}")])


class ListCustomers(_ErpTool):
    name = "erp.list_customers"
    description = "All customers with payment terms, AP contact and flags (payment_plan, legal_hold)."
    input_schema = NoArgs

    async def run(self, args: NoArgs, ctx: RunContext) -> ToolResult:
        return ToolResult(ok=True, output=await self.client.get("/api/v1/customers"))


class GetCustomer(_ErpTool):
    name = "erp.get_customer"
    description = "One customer with payment terms, AP contact and flags."
    input_schema = CustomerArgs

    async def run(self, args: CustomerArgs, ctx: RunContext) -> ToolResult:
        return ToolResult(ok=True, output=await self.client.get(f"/api/v1/customers/{args.customer_id}"))


class ListReceipts(_ErpTool):
    name = "erp.list_receipts"
    description = "Posted receipts, optionally for one invoice."
    input_schema = ReceiptsArgs

    async def run(self, args: ReceiptsArgs, ctx: RunContext) -> ToolResult:
        return ToolResult(ok=True, output=await self.client.get("/api/v1/receipts", invoice=args.invoice))


def register_erp_tools(registry: ToolRegistry, config: ErpConfig, vault: Vault, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> ErpClient:
    key = vault.resolve(config.api_key) if vault.is_ref(config.api_key) else config.api_key
    client = ErpClient(config.base_url, key, transport=transport)
    for cls in (ListInvoices, GetInvoice, ListCustomers, GetCustomer, ListReceipts):
        registry.register(cls(client))
    return client
