# Operator — AI-employee runtime (Arya, collections)

See [architecture.md](architecture.md) for the design. This README grows as the build progresses.

## Status

Day 1 morning: the sandbox company **Sharma Distributors** is up.

| Service | Port | What it is |
|---|---|---|
| Mini ERP | 8101 | Customers, invoices, receipts. Web UI (login) + read-only REST (`X-API-Key`). Receipts can be posted **only** through the web UI. |
| Bank portal | 8102 | Bharat Commerce Bank net-banking. Web only, no API, view-only role, 5-minute sessions, CSV/PDF statements. Fund transfer is refused. |
| Message hub | 8103 | Fake email + WhatsApp. Per-sender tokens, threads, bounces, attachments, idempotency keys, webhooks, call requests. Observer UI at `/`. |
| Shared drive | `var/drive/` | Invoice PDFs, signed delivery notes, supply agreements, credit policy. |

All four are seeded from one world file: [evals/worlds/sharma_default.yaml](evals/worlds/sharma_default.yaml)
(15 customers, 25 invoices, 10 prior bank transactions, sandbox date Monday 2026-10-19).
Customer personas in that file are ground truth for the simulator and evals; they are never written into any system.

## Run

```bash
uv sync
```

```bash
make sandbox      # reseed var/ and start ERP :8101, bank :8102, hub :8103
```

```bash
make test
```

With Docker: `cp .env.example .env && docker compose up --build`.

Sandbox logins (test values, see `.env.example`): ERP `ar.clerk`, bank `SHARMA001`.

## Sandbox-only admin hooks

`/_sandbox/*` on the bank portal (credit a payment, change chaos settings) needs `X-Sandbox-Token`.
Only the payment engine and eval harness hold this token; the agent runtime never does.
