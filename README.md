# Arya — an autonomous Accounts Receivable employee

Arya is an AI employee for a mid-size B2B distributor. Give it an overdue invoice and it emails the
customer, understands the reply, records the promise, watches the bank, posts the receipt in the ERP
and marks the invoice paid only when the money is really there.

It runs on **Operator**, a role-agnostic AI-employee runtime: collections is just a Role Pack (data),
so the same runtime can run other jobs. Design and decisions: [architecture.md](architecture.md).

## Status

**Day 2 midday complete.** A one-line brief becomes a checked mission spec, a policy-filtered scope,
a ranked plan that waits for human approval, and daily batches of outreach (one message per customer
covering all their overdue invoices). Every action now passes a deterministic Policy Gate; anything
that commits the company waits in an approvals inbox; text that tries to instruct the AI is
quarantined before any agent reads it; and models never see account numbers, emails or phone numbers.

| Block | Delivered |
|---|---|
| Day 1 morning | Sandbox company: ERP, bank portal, message hub, shared drive, seeded world |
| Day 1 midday | Runtime foundations: event store, scheduler, tool registry, browser driver, channel and file tools |
| Day 1 afternoon | LLM client, pipeline + guards, Operator and Conversation agents, customer simulator + payment engine |
| Day 1 evening | Reply Classifier, Matcher, settlement (receipt + Verifier), orchestrator, end-to-end demo |
| Day 2 morning | Mission Agent (brief → spec → plan → daily review), mission state machine, Prioritizer, Matcher for one-payment-many-invoices and partials, multi-invoice Reply Classifier, Document Generator (statement of account) |
| Day 2 midday | Verifier Agent + Evidence Collector, Policy Gate + `policies.yaml`, Approval Broker + `approvals` CLI, Injection Guard (Prompt Guard 2 + patterns, quarantine path), PII tokenization, fraud and concession handling |

Next: Day 2 afternoon — reliability (fallbacks, replan, loop detection, budgets, resume after kill, chaos).

## Trust layer (Day 2 midday)

| Control | What it does | Where |
|---|---|---|
| Policy Gate | Every tool call: allowed actions per app, forbidden wording (threats) → refused; quiet hours 20:00–09:30 and weekends → deferred; max 3 contacts per person per week → deferred; concessions (discount, payment plan…) or quarantined accounts → held for a human | [core/policy.py](core/policy.py), [policies.yaml](packs/companies/sharma_distributors/policies.yaml) |
| Scoped writes | An ERP write is possible only inside a task granted that exact change. Receipt tasks may submit one receipt, for the matched invoice, amount and bank reference; anything else is blocked at the browser's network layer, and the gate refuses ERP writes in tasks without a grant | [tools/browser.py](tools/browser.py), [apps/settlement.py](apps/settlement.py) |
| Approval Broker | Holds the exact action; approving it runs that action verbatim (same text, same idempotency key); only a named human can decide | [core/approvals.py](core/approvals.py), [apps/approvals.py](apps/approvals.py) |
| Injection Guard | Inbound messages with their PDF/text attachments, the policy documents the Mission Agent reads, and every untrusted Operator observation are scored (Prompt Guard 2 86M, chunked, cached by hash) plus pattern checks. Flagged text is replaced by a redacted note, the account or task is quarantined, and a human reviews it | [core/injection.py](core/injection.py) |
| PII tokenization | Account numbers, IFSC, emails and phones reach models (Conversation, Reply Classifier, Verifier, Mission and the Operator's page and file observations) only as `{{pii:account_1}}`; code fills real values in, including in the Operator's tool arguments. A draft that writes a raw account number or an unknown token is rejected, so a model cannot alter bank details | [core/pii.py](core/pii.py) |
| Verifier Agent | Deterministic checks first; a second, independent LLM opinion only for borderline promises (classifier confidence < 0.9). `PROMISED` now needs the Verifier too. Before `COMPLETED`, the bank is re-read in a fresh session and only lines really there count | [agents/verifier_agent.py](agents/verifier_agent.py) |
| Evidence Collector | Messages, statements, bank lines, receipts and verification results stored per invoice, files with SHA-256 | [subagents/evidence.py](subagents/evidence.py) |
| Fraud | "Our bank details changed" → never acted on: escalated, and the official details are restated | orchestrator |

```bash
uv run python -m apps.cli approvals
```

```bash
uv run python -m apps.cli approvals show apr_...
```

```bash
uv run python -m apps.cli approvals approve apr_... --by "Priya (Finance Manager)" --note "5% ok"
```

## Run a mission from a brief

```bash
uv run python -m apps.cli mission new "Collect the 45+ day overdue invoices — target ₹12L by Friday."
```

Arya reads the brief and the credit policy, picks the invoices, ranks customers and proposes a plan:

```
  scope: 15 entities in 12 groups, value 162100000; excluded 2: INV-2248 (customer on legal hold ...),
         INV-2259 (customer is on an agreed payment plan)
  target: ₹12,00,000.00   deadline: Fri 23 Oct 2026 18:00
  day 0: Day0 Firm Batch [firm_with_statement] C02 Initech Supplies LLP (3), C08 Lotus Foods (2), ...
  day 1: Day1 Soft Batch [soft_reminder] C12 Nilgiri Wholesale (1), C05 Sunrise Hardware (1), ...
```

Nothing is sent until a human approves. Then tick once per (simulated) day:

```bash
uv run python -m apps.cli mission approve
```

```bash
uv run python -m apps.cli mission run --days 5
```

`mission board` shows verified vs target and every customer's invoices by stage.

What is decided by the model and what by code:

| Step | Model proposes | Code checks / decides |
|---|---|---|
| Understand | objective, scope, target as written, deadline, selection parameters | target parsed from the brief's own text; deadline not in the past; only declared selection parameters |
| Select | — | policy exclusions (legal hold, payment plan, disputed, not due) applied on top, always |
| Rank | — | Prioritizer: value × age × risk per customer; approach rules from the role pack |
| Plan | batches, approach, order, rationale | known customers only, each once, ≤ capacity per day, anything forgotten appended |
| Approve | — | human only (`AWAITING_PLAN_APPROVAL → ACTIVE`) |
| Daily review | assessment + levers (follow-up, statement, escalate, payment plan…) | only role-pack levers, only for customers in the states a lever applies to; payment plans → approval request |
| Complete | — | only when **verified** money (matched + receipt + Verifier) ≥ target; promises never count |

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
== Mon 19 Oct 2026 10:00: start INV-2297
  outreach INV-2297: email: delivered
  INV-2297: OVERDUE -> CONTACTED ok
  [world] C11 replied (promise)
  INV-2297: reply classified promise_to_pay (0.97), amount 5400000, date 2026-10-20
  INV-2297: CONTACTED -> PROMISED ok
  bank sync: no new matching payments
== Tue 20 Oct 2026 10:00
  [world] C11 paid ₹54,000.00 UTR N497403142
  INV-2297: matched N497403142 (reference) receipt RCPT-0001; verifier pass; PROMISED -> PAID ok
```

Then inspect what happened and why:

```bash
uv run python -m apps.cli status INV-2297
```

To start again from a clean world, stop `make sandbox` and run it again (it re-seeds the systems and
clears the agent's runtime state and the simulator's memory).

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
- **Untrusted content stays data.** Pages, PDFs and messages are wrapped as `<untrusted_data>`, and
  screened first: text that tries to instruct the AI is quarantined and never shown to an agent.
- **Policy before every action.** Threats are refused, quiet hours and contact caps defer, concessions
  wait for a named human, and an approved action runs exactly as approved.
- **Models never see account numbers or contact details.** They write placeholders; code fills them in.
- **A task can only make the change it was created for.** A receipt task can post one receipt: the
  matched invoice, amount and bank reference, enforced on the wire.
- **One writer at a time.** A run lock stops a demo and a re-seed from corrupting each other.

## Repository layout

```
core/            role-agnostic runtime: models, events, scheduler, pipeline, mission, guards, LLM client
agents/          primary agents: Mission, Operator, Conversation (domain-free)
subagents/       Prioritizer, Reply Classifier, Matcher, Document Generator (domain-free)
tools/           tool registry, browser driver, channels, files, ERP connector, vault, app maps
apps/            runtime wiring, flows, settlement, orchestrator, mission runner, documents, CLI
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
15 customers, 25 invoices, 10 prior bank transactions, sandbox time Monday 2026-10-19 10:00 IST
(after contact hours open at 09:30).
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
| Mission | `openai/gpt-oss-120b` (reasoning: high) |
| Verifier (second opinion on borderline promises) | `openai/gpt-oss-120b` (reasoning: high) |
| Reply Classifier, light sub-agents | `qwen/qwen3.8-27b` |
| Customer simulator | `qwen/qwen3.8-27b` (a different family from the agents, so evals stay honest) |
| Injection guard | `meta-llama/llama-prompt-guard-2-86m` |

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
uv run python -m apps.cli mission new "<brief>"                  # Mission Agent: spec + scope + plan
uv run python -m apps.cli mission show|board|plan|approve|tick   # inspect, (re)plan, approve, one tick
uv run python -m apps.cli mission answer "<text>"                # answer the agent's blocking questions
uv run python -m apps.cli mission run [--days 5] [--sim-llm]     # tick daily with simulated customers
uv run python -m apps.cli approvals [list|show|approve|reject]   # the human decisions inbox
uv run python -m sandbox.customer_sim tick [--llm]               # simulated customers act
uv run python -m sandbox.customer_sim advance 1d                 # move simulated time
```

## Tests

```bash
make test
```

260 tests: unit tests, in-process integration over the sandbox, live Chromium against real ERP and
bank servers, and two offline acceptance tests that run the real Operator loop, browser, systems,
simulator, matcher, verifier and guards with scripted LLM decisions:
`tests/apps/test_end_to_end.py` (one invoice, Day 1) and `tests/apps/test_mission_week.py` (a whole
mission week from the brief: already-paid invoice settled before any chase, one transfer for three
invoices, a partial payment, a look-alike payer never matched, zero false "paid").
No network or API key is needed for the test suite.

## Known limitations (so far)

- **Groq free-tier limits.** About 8,000 tokens/minute per model and a daily token cap. One full demo
  uses about 20 LLM calls (~46k tokens) and takes several minutes; repeated runs in a day can hit the
  daily cap and pause for minutes. A paid tier, or `openai/gpt-oss-20b` for the Operator, speeds this up.
- **Docker.** `docker-compose.yml` starts the sandbox services; it has not been tested yet, and the
  image does not include Chromium for the agent runtime. Use the uv commands above.
- **Not built yet (Day 2 afternoon+).** Chaos/recovery scenarios, workspace UI, eval harness, memory.
- If the injection classifier is unreachable, inbound messages are quarantined (fail closed) and page
  observations fall back to pattern checks only.
- Attachments that are not text (images, scans) cannot be screened; no agent reads them either.
- The forecast uses fixed per-state probabilities from the role pack until memory supplies real history.
- A new promise from a customer who is already `PROMISED` (e.g. a later date after a reminder) is
  classified and logged but does not yet replace the old promise date (Day 2 afternoon: replan).
- A customer whose invoices were settled before their batch still takes a slot in that day's capacity.
- Sandbox only: real bank portals have MFA and device binding; production should use bank APIs.
