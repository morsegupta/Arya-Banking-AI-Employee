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

## Runtime foundations (Day 1 midday)

| Module | What it does |
|---|---|
| `core/models.py` | Typed, domain-free contracts: Mission, Entity, Task, Event, ToolResult, reports |
| `core/events.py` | Append-only, hash-chained event store with dedupe keys; in-process bus |
| `core/scheduler.py` | Durable timers in SQLite, exactly-once firing, survive restarts |
| `tools/base.py` | Tool interface + registry: capability scope, policy hook, idempotency ledger, retry/backoff, verify, secret scrubbing. Money-moving tools cannot register |
| `tools/browser.py` | Playwright driver: per-task context, ref-based snapshots, vault logins, network guard (bank is GET-only), session-expiry recovery |
| `tools/channels/hub.py` | email / WhatsApp / call tools; sends deduped end to end via idempotency keys; bounces surface as permanent errors |
| `tools/files.py` | PDF / text / CSV / XLSX readers confined to the drive and the task's own files |
| `packs/companies/sharma_distributors/` | Company pack: channel identity and app maps (no secrets, only `vault://` refs) |

`tests/test_domain_free.py` fails the build if `core/` or `agents/` mention the collections domain.

## Agents and pipeline (Day 1 afternoon)

| Module | What it does |
|---|---|
| `core/llm.py` | One client for every role (Groq, OpenAI-compatible): rate limiting from provider headers, 429/5xx retry, re-sampling of malformed gpt-oss output, token budgets, JSON output with one repair retry, Prompt Guard scoring |
| `core/guards.py` | Safe guard language from role packs (no `eval`), per-clause audit |
| `core/pipeline.py` | Entity state machine from `role.yaml`: guarded transitions, human/system-only moves, timers per state |
| `agents/operator_agent.py` | Bounded ReAct loop over the registry; results must cite real tool calls; lean context |
| `agents/conversation_agent.py` | Drafts messages from allowed facts only; attachments and cited facts checked in code |
| `apps/flows.py`, `apps/cli.py` | Role-pack-driven flows (tasks, entity loading, outreach with channel fallback) and a CLI |
| `sandbox/customer_sim/` | Personas decide in code (ground truth); qwen writes the wording; payment engine credits the bank |
| `packs/roles/collections/` | Pipeline, guards, timers, task types, entity source, prompts |

Models per role: `config/models.yaml`. Put `GROQ_API_KEY` in `.env` (never in `.env.example`).

```bash
make sandbox                                   # terminal 1
uv run python -m apps.cli fetch                # Operator: overdue invoices via ERP API
uv run python -m apps.cli statement            # Operator: bank CSV via the browser
uv run python -m apps.cli outreach INV-2291    # Conversation Agent: email + OVERDUE -> CONTACTED
uv run python -m sandbox.customer_sim tick --llm
uv run python -m sandbox.customer_sim advance 6d && uv run python -m sandbox.customer_sim tick
```

## Run

```bash
uv sync
```

```bash
uv run playwright install chromium
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
