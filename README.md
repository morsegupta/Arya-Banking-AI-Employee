# Arya — an autonomous Accounts Receivable employee

Arya is an AI employee for a mid-size B2B distributor. Give it an overdue invoice and it emails the
customer, understands the reply, records the promise, watches the bank, posts the receipt in the ERP
and marks the invoice paid only when the money is really there.

It runs on **Operator**, a role-agnostic AI-employee runtime: collections is just a Role Pack (data),
so the same runtime can run other jobs. Design and decisions: [architecture.md](architecture.md).

## Status

**Day 1 complete.** One invoice goes `OVERDUE → CONTACTED → PROMISED → PAID` end to end from the CLI,
verified live on Groq models and covered by an automated end-to-end test.

| Block | Delivered |
|---|---|
| Day 1 morning | Sandbox company: ERP, bank portal, message hub, shared drive, seeded world |
| Day 1 midday | Runtime foundations: event store, scheduler, tool registry, browser driver, channel and file tools |
| Day 1 afternoon | LLM client, pipeline + guards, Operator and Conversation agents, customer simulator + payment engine |
| Day 1 evening | Reply Classifier, Matcher, settlement (receipt + Verifier), orchestrator, end-to-end demo |

Next: Day 2 — Mission Agent (brief → plan → batches), Policy Gate, approvals, injection guard, workspace UI.

## Quick start

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), a [Groq](https://console.groq.com) API key.

```bash
uv sync
```

```bash
uv run playwright install chromium
```

```bash
cp .env.example .env
```

Put your key in `.env` as `GROQ_API_KEY=...`. Never put real keys in `.env.example` (it is committed).

Start the sandbox company (terminal 1):

```bash
make sandbox
```

Run one invoice end to end (terminal 2):

```bash
uv run python -m apps.cli demo INV-2297 --sim-llm
```

Expected output (abridged):

```
== Mon 19 Oct 2026 09:00: start INV-2297
  outreach INV-2297: email: delivered
  INV-2297: OVERDUE -> CONTACTED ok
  [world] C11 replied (promise)
  INV-2297: reply classified promise_to_pay (0.97), amount 5400000, date 2026-10-20
  INV-2297: CONTACTED -> PROMISED ok
  bank sync: no new matching payments
== Tue 20 Oct 2026 09:00
  [world] C11 paid ₹54,000.00 UTR N497403142
  INV-2297: matched N497403142 (reference) receipt RCPT-0001; verifier pass; PROMISED -> PAID ok
```

Then inspect what happened and why:

```bash
uv run python -m apps.cli status INV-2297
```

To start again from a clean world, stop `make sandbox` and run it again (it re-seeds).

## What happens in the demo

| Sim day | Step | Who |
|---|---|---|
| Mon | Load the invoice from the ERP API, draft an email from allowed facts only, attach the invoice PDF, send | Conversation Agent |
| Mon | Customer reads it and replies with a promise (persona decides in code, LLM writes the words) | Customer simulator |
| Mon | Classify the reply; code checks the quote is verbatim and parses amount/date; guard allows `PROMISED` | Reply Classifier + pipeline |
| Tue | Customer's payment lands in the bank | Payment engine |
| Tue | Log into the bank portal, download the statement CSV | Operator (browser) |
| Tue | Match the bank line to the invoice by payer, reference and amount; lock it | Matcher (code) |
| Tue | Post the receipt through the ERP's web form | Operator (browser) |
| Tue | Re-check in a fresh bank session, read the receipt and balance from the ERP | Verifier (code) |
| Tue | Guard allows `PAID` only with match + receipt + verifier pass | Pipeline |

The sandbox clock is simulated: days pass in seconds. Every step is an event in a hash-chained log.

## Safety properties (enforced in code, not prompts)

- **"Paid" means money in the bank.** `PAID` needs a matched bank line, an ERP receipt with that
  reference, and a passing Verifier. A customer saying "already paid" can never satisfy it.
- **No money can move.** Money-moving tools cannot be registered; the bank is read-only at the network
  layer (only GET plus login/logout POSTs); `/transfer` and unknown hosts are blocked.
- **Credentials never reach a model.** Logins are typed by the driver from `vault://` references;
  password fields cannot be filled by the model; secrets are scrubbed from every result and event.
- **No duplicate side effects.** Sends and form posts need idempotency keys; a retried email is
  deduped by the runtime and again by the provider.
- **No invented data.** Operator results must cite real tool calls; messages may only state provided
  facts; classifier quotes must appear verbatim in the reply.
- **Look-alike payments don't count.** "SUNRISE TRADING CO" never matches *Sunrise Hardware*.
- **Untrusted content stays data.** Pages, PDFs and messages are wrapped as `<untrusted_data>`.
- **One writer at a time.** A run lock stops a demo and a re-seed from corrupting each other.

## Repository layout

```
core/            role-agnostic runtime: models, events, scheduler, pipeline, guards, LLM client
agents/          primary agents: Operator, Conversation (domain-free)
subagents/       Reply Classifier, Matcher (domain-free)
tools/           tool registry, browser driver, channels, files, ERP connector, vault, app maps
apps/            runtime wiring, flows, settlement, orchestrator, CLI
packs/roles/collections/            the job: pipeline, guards, timers, task types, prompts
packs/companies/sharma_distributors/ the company: app maps, channels, facts it may state
sandbox/         the fake company: ERP, bank portal, message hub, drive, customer simulator
evals/worlds/    seeded worlds with hidden ground truth
config/          model per role
tests/           unit, integration, live-browser and end-to-end tests
```

`tests/test_domain_free.py` fails the build if `core/`, `agents/` or `subagents/` mention the
collections domain: all domain knowledge lives in packs.

## The sandbox company: Sharma Distributors

| Service | Port | What it is |
|---|---|---|
| Mini ERP | 8101 | Customers, invoices, receipts. Web UI (login) + read-only REST. Receipts can be posted **only** through the web UI. |
| Bank portal | 8102 | Bharat Commerce Bank net-banking. Web only, no API, view-only role, 5-minute sessions, CSV/PDF statements. Fund transfer is refused. |
| Message hub | 8103 | Fake email + WhatsApp: per-sender tokens, threads, bounces, attachments, idempotency keys, webhooks, call requests. Observer UI at `http://127.0.0.1:8103/`. |
| Shared drive | `var/drive/` | Invoice PDFs, signed delivery notes, supply agreements, credit policy. |

Everything is seeded from [evals/worlds/sharma_default.yaml](evals/worlds/sharma_default.yaml):
15 customers, 25 invoices, 10 prior bank transactions, sandbox date Monday 2026-10-19.
Each customer has a hidden persona (prompt payer, late payer, partial payer, disputer, "already paid"
true or false, discount haggler, ghost, prompt-injection attacker, bank-detail fraudster). Personas
are ground truth for the simulator and evals and are never written into any system the agent can read.

Sandbox logins (test values in `.env.example`): ERP `ar.clerk`, bank `SHARMA001`.
`/_sandbox/*` bank endpoints (payment engine, chaos settings) need `X-Sandbox-Token`, which the agent
runtime never receives.

## Models

All served by Groq's OpenAI-compatible API; change any role in [config/models.yaml](config/models.yaml).

| Role | Model |
|---|---|
| Operator, Conversation | `openai/gpt-oss-120b` (reasoning: medium) |
| Mission, Verifier (Day 2) | `openai/gpt-oss-120b` (reasoning: high) |
| Reply Classifier, light sub-agents | `qwen/qwen3.8-27b` |
| Customer simulator | `qwen/qwen3.8-27b` (a different family from the agents, so evals stay honest) |
| Injection guard (Day 2) | `meta-llama/llama-prompt-guard-2-86m` |

The LLM client throttles from Groq's rate-limit headers, retries 429/5xx, re-samples malformed
gpt-oss output, enforces token budgets and prints when it is waiting.

## CLI reference

```bash
uv run python -m apps.cli demo INV-2297 [--days 5] [--sim-llm]   # one invoice end to end
uv run python -m apps.cli fetch [--min-days 45]                  # Operator: overdue invoices (ERP API)
uv run python -m apps.cli statement [--from 2026-10-01]          # Operator: bank CSV (browser)
uv run python -m apps.cli outreach INV-2291                      # Conversation Agent: one reminder
uv run python -m apps.cli step                                   # one pass: inbox, bank sync, timers
uv run python -m apps.cli status INV-2297                        # an invoice's timeline and evidence
uv run python -m apps.cli entities                               # invoices in the mission + timers
uv run python -m apps.cli events [-n 30]                         # event log + hash-chain check
uv run python -m sandbox.customer_sim tick [--llm]               # simulated customers act
uv run python -m sandbox.customer_sim advance 1d                 # move simulated time
```

## Tests

```bash
make test
```

216 tests: unit tests, in-process integration over the sandbox, live Chromium against real ERP and
bank servers, and an end-to-end acceptance test (`tests/apps/test_end_to_end.py`) that runs the real
Operator loop, browser, systems, simulator, matcher, verifier and guards with scripted LLM decisions.
No network or API key is needed for the test suite.

## Known limitations (so far)

- **Groq free-tier limits.** About 8,000 tokens/minute per model and a daily token cap. One full demo
  uses about 20 LLM calls (~46k tokens) and takes several minutes; repeated runs in a day can hit the
  daily cap and pause for minutes. A paid tier, or `openai/gpt-oss-20b` for the Operator, speeds this up.
- **Docker.** `docker-compose.yml` starts the sandbox services; it has not been tested yet, and the
  image does not include Chromium for the agent runtime. Use the uv commands above.
- **Not built yet (Day 2+).** Mission Agent and batch planning, Policy Gate and approvals, injection
  guard screening, follow-up cadence, chaos/recovery scenarios, workspace UI, eval harness.
- Sandbox only: real bank portals have MFA and device binding; production should use bank APIs.
