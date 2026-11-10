# Agent OS × PayPal: Sentinel

Sentinel is a set of AI agents that watch a merchant's PayPal account and prepare the response to
disputes, overdue invoices and failed subscription payments. It runs on **Agent OS**, a runtime that
compiles PayPal's API into typed, governed functions.

Agents decide. Governance decides whether the decision runs now or waits for a human. Every step is
written to an append-only audit log.

Built for the PayPal AI Hackathon.

## Run it (2 minutes, no PayPal account needed)

You need Python 3.10 or newer.

**Windows:** double-click `scripts\run_dev.bat` (or run it from a terminal in the project folder).

**macOS / Linux:** `./scripts/run_dev.sh`

The script creates `.venv`, installs the dependencies, copies `.env.example` to `.env`, registers
the PayPal functions and starts the daemon. Then open:

- Dashboard: <http://127.0.0.1:8000>
- API docs: <http://127.0.0.1:8000/docs>

The default is `PAYPAL_ENV=mock`: a built-in fake PayPal, so everything works with no keys.

To do the same by hand:

```
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
copy .env.example .env            (macOS/Linux: cp .env.example .env)
agentos paypal register
agentos serve
```

## A 3-minute demo

On the dashboard's Desk, use the **Try it** buttons.

1. **Open a test dispute.** A signed webhook arrives and is verified. The dispute agent reads the
   dispute, the payment and the order, finds the tracking number and decides to fight. Its evidence
   upload is held on an approval slip.
2. **See the evidence** on the slip: the reasoning, the proof, every function call in order, and the
   audit trail. Then **Approve**. The evidence is sent to PayPal once.
3. Open the decision again and choose **Settle for the seller** to play PayPal's reviewer. PayPal's
   outcome appears on the decision.
4. **Seed overdue invoices.** Four clients with different payment histories get four different
   treatments: a gentle note, a firm reminder, a discount that waits for approval, and a small
   discount that runs on its own because it is under the auto-approve cap.
5. **Send a forged webhook.** It is rejected and logged. Nothing reaches an agent.
6. Look at **Functions**: the 26 PayPal functions and their risk tiers, compiled from PayPal's own specs.

## How it works

```
PayPal ──webhook──▶ daemon/api/webhooks.py ──▶ paypal_adapter/webhooks.py (verify signature)
                                                      │
                          events table ◀──────────────┤──▶ event bus ──▶ dashboard (/stream)
                                                      ▼
                                              worker/queue.py (job)
                                                      ▼
                         worker/runner.py ──▶ agents/orchestrator.py ──▶ a specialist agent
                                                      │
                             agents/toolbridge.py (this agent's functions only)
                                                      ▼
                    daemon/invocation.py: exists? ─▶ arguments valid? ─▶ governance/policy.py
                                                      │
                          ┌───────────────────────────┼───────────────────────────┐
                        allow                    needs approval                  deny
                          ▼                           ▼
              paypal_adapter/dispatch.py     governance/approvals.py ──approve──▶ dispatch, once
                          ▼
                 paypal_adapter/client.py ──▶ PayPal
```

**Connect time, once.** `agentos paypal register` reads PayPal's official OpenAPI specs
(`paypal_adapter/specs/`, pinned by hash), compiles each allowlisted operation into a typed function
(`compiler.py`) and submits them as one atomic snapshot to the SQLite registry (`register.py`).

**Ask time, every call.** Agents pick from the registry. Nothing is discovered at ask time.

**PayPal attaches in one place.** Only `paypal_adapter/` talks to PayPal.

### Risk tiers

Set in `paypal_adapter/allowlist.py`, enforced in `daemon/governance/policy.py`.

| Tier | Examples | Behaviour |
|---|---|---|
| `read` | `disputes.get`, `orders.get`, `invoices.search`, `transactions.list` | Runs automatically |
| `communicate` | `invoices.send_reminder`, `disputes.send_message` | Runs automatically, logged, rate-limited per client |
| `money_legal` | `disputes.provide_evidence`, `disputes.make_offer`, `disputes.accept_claim`, `payments.refund`, `subscriptions.capture`, `invoices.apply_discount` | Waits for approval, unless the amount is known and at or under `AUTO_APPROVE_CAP` |
| `sandbox_only` | `sandbox.create_dispute`, `sandbox.settle_dispute`, `sandbox.seed_invoices` | Allowed only when `PAYPAL_ENV` is `mock` or `sandbox` |
| `never` | anything not on the allowlist | Never compiled, never registered |

An approved action runs exactly once: the status change is a guarded database update, and the same
idempotency key is sent to PayPal as `PayPal-Request-Id` on every attempt.

### The agents

| Agent | Trigger | Decides | Action it may propose |
|---|---|---|---|
| Dispute resolver | `CUSTOMER.DISPUTE.CREATED` | fight / offer / accept / needs a human | provide evidence, make offer, accept claim |
| Collections | sweep (timer, dashboard or CLI) | gentle note / reminder / discount / wait | send reminder, apply discount |
| Subscription rescue | `BILLING.SUBSCRIPTION.PAYMENT.FAILED` | retry / needs a human / wait | capture outstanding balance |

With `ANTHROPIC_API_KEY` set, each agent runs a Claude tool-use loop: Claude gets the agent's **read**
functions as tools plus `submit_decision`, whose schema is a Pydantic model. Claude never gets write
functions; code turns the validated decision into one write call, and governance handles that call.
Without a key, a built-in rules engine makes the same decisions through the same functions.

## Connect the real PayPal sandbox

1. Create a sandbox app at <https://developer.paypal.com> (Apps & Credentials).
2. In `.env` set `PAYPAL_ENV=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`.
3. Start the daemon. Check the connection: `agentos invoke disputes.list`
4. For webhooks PayPal needs a public URL. Run `scripts\tunnel.bat` (ngrok), add
   `<https url>/webhooks/paypal` as a webhook on your sandbox app, subscribe to the dispute, invoice
   and subscription events, and put the Webhook ID in `.env` as `PAYPAL_WEBHOOK_ID`. Restart.
5. File a dispute from your sandbox **buyer** account (Resolution Center). PayPal sends the webhook.
   No tunnel yet? `agentos demo dispute --dispute-id <id>` replays that real dispute locally.

`render.yaml` deploys the same daemon to Render, which gives a permanent webhook URL. Set
`DASHBOARD_TOKEN` on any public deployment: the dashboard can approve money actions.

## CLI

```
agentos serve                         start the daemon and dashboard
agentos status                        what the daemon is connected to
agentos functions                     list registered functions
agentos invoke disputes.list          call one function through governance
agentos paypal register               compile the specs into the registry
agentos paypal functions              PayPal functions with their tiers
agentos approvals list|approve|reject
agentos decisions list|show
agentos audit
agentos demo dispute|invoices|subscription|settle
```

## API

| Method and path | Returns / does |
|---|---|
| `GET /decisions` | Latest agent decisions |
| `GET /decisions/{id}` | One decision: reasoning, evidence, tool calls, audit trail |
| `GET /approvals` | Pending money/legal actions (`?status=all` for history) |
| `POST /approvals/{id}/approve` | Executes the action once |
| `POST /approvals/{id}/reject` | Cancels it, logged |
| `GET /audit` | Audit log, filterable |
| `GET /clients` · `GET /clients/{email}/profile` | Payment profiles |
| `GET /stream` | Server-sent events for the dashboard |
| `GET /functions` | All registered functions and tiers |
| `POST /invoke/{name}` | Run a function through validation and governance |
| `POST /webhooks/paypal` | PayPal's webhook endpoint |
| `POST /demo/...` | Demo triggers (mock and sandbox only) |

## Tests

```
pytest            89 tests
ruff check .
```

PayPal is mocked with `respx` and with the built-in fake PayPal; Claude is replaced by a scripted
fake. `tests/test_dispute_flow.py` runs one dispute end to end.

## File structure

```
daemon/            FastAPI runtime: config, registry, invocation, events, ingest, runtime
  api/             HTTP routes
  governance/      policy, approvals, limits, audit
  storage/         SQLite wrapper, migrations, table stores
converters/mock/   four demo functions that prove the registry path
paypal_adapter/    auth, client, specs, allowlist, compiler, overrides, register, dispatch,
                   webhooks, sandbox helpers, mock_server
agents/            llm, toolbridge, orchestrator, three specialists, profiles, schemas, prompts
worker/            queue, runner, scheduler
cli/               the agentos command
frontend/          the dashboard (one HTML file, no build step)
scripts/           run_dev, tunnel, download_specs
tests/
```

## What is verified, and what is not

Verified here: the whole system against the built-in mock PayPal (89 automated tests, plus a
click-through of the dashboard in a browser).

Not yet run against the real services, because that needs your keys:

- **Real PayPal sandbox calls.** The client, auth and webhook verification follow PayPal's documented
  behaviour and are tested against mocks, but have not made a real request.
- **Real Claude calls.** The tool-use loop is tested with a scripted fake client.
- **`sandbox.seed_invoices` on the real sandbox.** It backdates invoices and payments; PayPal may refuse
  past dates.

Known limits:

- PayPal's spec does not describe the bodies of `provide-evidence`, `accept-claim` and `send-message`,
  so those three are hand-written in `paypal_adapter/overrides/` from PayPal's documentation.
- PayPal's reminder endpoint replaces a custom subject and note with its own wording. The agent's note
  is kept with the decision.
- PayPal has no API for a merchant to open a dispute, so `sandbox.create_dispute` creates one only in
  mock mode; in sandbox mode it replays a dispute you filed as the buyer.
- The collections sweep reads the first 100 open invoices.
- `mcp_server/` (the same registry over MCP) is not built yet.

## Hackathon note: what is new

The Agent OS idea and its core design (a registry of typed functions, an invocation engine, an event
bus, a mock converter, a CLI) predate the hackathon. New for this submission: the PayPal adapter, the
governance layer (tiers, approvals, limits, audit), the Sentinel agents, the worker, the webhook
pipeline and the dashboard. Edit this paragraph so it matches what you tell Devpost.

## Licence

MIT. See `LICENSE`. PayPal's OpenAPI specs in `paypal_adapter/specs/` are Apache-2.0.
