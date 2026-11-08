# Refunds Console — an internal tool built with Devin

A working prototype of a fintech **refunds dashboard**, built end-to-end with [Devin](https://devin.ai) to show that
an engineering team can replace a low-code platform such as Microsoft Power Apps (≈ $250K/yr in licences) with
owned, version-controlled code — without giving up the things that make low-code attractive: speed, role-based
screens, approvals, connectors and analytics.

## Live environments (already deployed — no setup required)

| Environment | URL | Notes |
| --- | --- | --- |
| **dev** | https://d2eah532twinmd.cloudfront.net | Seeded demo data, mock SSO, mock PayCore with retries |
| **prod** | https://d2r4c05e3dgv73.cloudfront.net | Empty DB (`ENVIRONMENT=prod` disables seed); real OIDC provider still to be wired |

Both are Terraform-managed AWS stacks (CloudFront + S3 + ECS Fargate + RDS Postgres) deployed by GitHub Actions —
see [AWS infrastructure](#aws-infrastructure-infra-terraform) below. Log in on dev with any seeded user via the mock SSO picker.

| Power Apps capability | How it's done here |
| --- | --- |
| Role-based screens | One React app; navigation, data and buttons change per role (admin / reviewer / viewer) |
| Approval workflows | Approve / reject / escalate with notes, per-refund + daily limits, admin sign-off for escalations, admin-only partial refunds |
| Entra ID / SSO | Mock OIDC authorization-code flow (`/auth/sso/*`, dev only) → swap in Okta / Entra / Google with config |
| Dataverse tables | PostgreSQL schema via SQLAlchemy models, constraints and indexes |
| Connectors | Authenticated HTTP client to a third-party "PayCore" customer-history API (mocked, with API key) |
| Moving money through a provider | Approved refunds are paid back through PayCore's refunds API: idempotency keys, retries with backoff, HMAC-signed webhooks, reconciliation ([below](#payouts-through-a-payments-provider)) |
| Power BI dashboards | Analytics page (Recharts), CSV export, Postgres BI views (`v_refund_facts`, …) |
| Environment governance / auditing | Append-only audit log (DB trigger), every allowed **and denied** action is recorded |
| Inherited compliance (Microsoft trust portal) | *Compliance Center*: controls-as-code mapped to SOC 2 / PCI DSS 4.0 / SOX ITGC / GDPR, checked live, exportable evidence pack, failing controls handed to Devin |
| Hosting | `docker compose up` — Postgres, API, mock provider and static frontend behind nginx |

## Why owned code instead of low-code

| Advantage | What it means here |
| --- | --- |
| **Low cost** | No per-user or premium-connector licences: the stack is open source (React, FastAPI, PostgreSQL). The AWS deployment defaults to small, autoscaled resources — a `db.t4g.micro` Postgres and 0.5 vCPU / 1 GB Fargate tasks scaling between `api_min_count` (2) and `api_max_count` (8) — all Terraform variables. Locally it runs on any Postgres with `docker compose up` or `./setup.sh`. |
| **Low maintenance** | Every change goes through CI (ruff, pytest, a Postgres smoke test, oxlint, `tsc`, Docker builds, `terraform validate`) and the `dev` → `main` promotion flow; deploys are automated with ECS auto-rollback. The [self-healing loop](#self-healing-loop-devin) turns production errors, guardrail alerts and failing compliance controls into Devin sessions that open fix PRs, and the [Compliance Center](#compliance-center) checks controls continuously instead of in an annual audit scramble. |
| **High iteration speed** | Support tickets (`POST /support/requests`), production errors and failing compliance controls are queued and handed to Devin, which reproduces the issue with a test, fixes it and opens a PR against `dev`. Merging to `dev` deploys the dev environment, so a request can go from idea to running software in one review cycle. |
| **High flexibility** | Plain code has no delegation limits, formula language or connector ceilings: policy logic lives in [`backend/app/policy.py`](backend/app/policy.py), data in a normal Postgres schema with BI views, and infrastructure in Terraform. Swap the identity provider, payment provider or database host with configuration rather than a platform migration. |
| **Easy third-party connections** | An integration is an authenticated HTTP client plus config: the customer-history API ([`history_client.py`](backend/app/history_client.py), `HISTORY_PROVIDER_URL`/`HISTORY_PROVIDER_API_KEY`), the PayCore payments API with signed webhooks ([`payments_client.py`](backend/app/payments_client.py)), the Devin API ([`remediation.py`](backend/app/remediation.py)) and OIDC SSO. Secrets come from Secrets Manager on AWS; the BI views and CSV export plug into any reporting tool. |

## What it does

**Reviewer** (`jordan.lee`, `sam.patel`, `maria.gomez`)
- *My queue*: only refunds assigned to them, with headline numbers (assigned, pending $, past SLA, approved today,
  daily limit left, 7-day approval rate, time to decide) and an approval-limit progress bar.
- Per row: **Approve**, **Reject**, **Escalate** to admin, and **History** (live call to the third-party provider:
  KYC status, risk flags, recent transactions, chargebacks, previous refunds).
- Buttons are disabled with the exact reason when policy blocks an action (e.g. "Amount exceeds your per-refund approval limit").
- Click a row for a drawer with details, full timeline and customer history. *My history* shows past decisions.

**Admin** (`priya.admin`, `alex.admin`)
- *Analytics*: KPIs, daily volume vs refunded $, status mix, reason breakdown, approval rate by risk band, top
  customers, team performance; 7/30/90-day windows; CSV export.
- *Escalations*: queue of reviewer- and policy-escalated refunds that only admins can decide.
- *All refunds*: search/filter/sort everything, create refunds, (re)assign to reviewers.
- *Team & limits*: add users, change roles, per-refund and daily approval limits, deactivate.
- *Policies*: org hard cap, high-risk threshold, customer 30-day cap, min note length, SLA hours.
- *Audit log*: filterable, including denied attempts. *View as*: read-only preview of exactly what another user sees.
- *Compliance*: live control checks with evidence, evidence-pack download, quarterly access review, GDPR
  data-subject requests and "Fix with Devin" on failing controls (see [Compliance Center](#compliance-center)).

**Viewer** (`vik.auditor`) — read-only analytics and refunds list, customer PII masked, no actions, no history calls.
Also the auditor role for the *Compliance* page: can read every control's evidence and download the evidence pack, but change nothing.

## Payouts through a payments provider

Approving a refund now actually returns the money through the payments provider (PayCore, a mock with the same shape
as Stripe/Adyen refunds). Admins get a **Payouts** page; everyone sees a payout badge on approved refunds and a live
payout section in the refund drawer. The code is in [`backend/app/payouts.py`](backend/app/payouts.py) (state machine),
[`backend/app/payments_client.py`](backend/app/payments_client.py) (provider client) and
[`backend/app/routers/payouts.py`](backend/app/routers/payouts.py) (admin API + webhook receiver).

```
approve ──(same DB transaction)──> queued ──worker──> processing ──signed webhook / reconcile──> succeeded | failed
                                    │  ▲                  └── 4xx decline ──> failed (admin can re-drive)
                         5xx/timeout│  │ exponential backoff + jitter, same Idempotency-Key
                                    ▼  │
                              retry_scheduled ──(PAYOUT_MAX_ATTEMPTS)──> failed (retries_exhausted)
```

- **Exactly-once money movement**: the payout row is written in the approval's transaction (outbox), and every attempt
  for that payout sends one `Idempotency-Key`. A retry after a timeout where the provider *did* refund comes back as a
  replay, not a second refund.
- **Transient vs definitive errors**: 5xx / 429 / timeouts are retried by a background worker with backoff; a 402
  decline fails immediately. Re-driving a declined payout uses a new key; re-driving `retries_exhausted` keeps the old
  key, because the provider may already have processed it.
- **Webhooks**: `POST /webhooks/paycore` checks `PayCore-Signature` (HMAC-SHA256 over `timestamp.body`, constant-time
  compare, 5-minute window so old webhooks can't be replayed), skips redeliveries by event id, and never moves a payout
  out of a final state when events arrive out of order. Rejected webhooks are written to the audit log.
- **Reconciliation**: *Reconcile with PayCore* asks the provider for the status of every payout still in flight and
  fixes any mismatch (for example, a lost webhook).
- **Sandbox**: the Payouts page can make the provider fail on purpose (`outage`, `lost_response`, `decline`,
  `async_fail`, `drop_webhook`) so you can show each case live. Turn it off with `PAYMENTS_SANDBOX_CONTROLS=false` (off by default when `ENVIRONMENT=prod`).
- Every step is a refund timeline event and an audit row on the approval's trace id. `pytest` covers each case against
  a fake provider running inside the test process.

Config: `PAYMENTS_PROVIDER_URL`, `PAYMENTS_PROVIDER_API_KEY`, `PAYMENTS_WEBHOOK_SECRET` (shared with the provider;
from Secrets Manager on AWS), `PAYOUT_MAX_ATTEMPTS`, `PAYOUT_RETRY_BASE_S`, `PAYOUT_RETRY_MAX_S`,
`PAYOUT_WORKER_ENABLED`. Provider side: `PAYMENTS_WEBHOOK_URL`, `PAYMENTS_WEBHOOK_SECRET`, `PAYMENTS_SETTLE_DELAY_S`.
To use a real PSP, swap the three functions in `payments_client.py` and the signature format in `verify_signature`.

### Why this is hard to do well in Power Apps

| Need | Power Apps / Power Automate | Here |
| --- | --- | --- |
| Call the PSP's refund API | Custom connector + premium licensing for every user who runs it | `httpx` call, ~60 lines |
| Idempotency key that is kept across retries *and* manual re-drives | Has to be built by hand in the flow and stored in Dataverse; the connector's built-in retry policy doesn't distinguish a decline from an outage | `payouts.attempt` / `manual_retry`, unit-tested |
| Approval and payout intent committed atomically | A canvas app `Patch` plus a flow run are separate operations; no shared transaction | One DB transaction (outbox row) |
| Receive signed webhooks | "When an HTTP request is received" trigger (premium); flow expressions have no HMAC function, so signature checks usually need an Azure Function | HMAC + timestamp check in `verify_signature` |
| Out-of-order / duplicate events, reconciliation | Concurrency control and dedup tables built by hand; delegation limits when querying large tables from the app | Row locks, unique event ids, terminal-state guard, `reconcile()` |
| Test it | Manual testing in the maker portal | `pytest` simulates outages, lost responses, declines, forged and replayed webhooks |

 in [`backend/app/policy.py`](backend/app/policy.py))

- **Visibility**: reviewers only see refunds assigned to / escalated by / decided by them; others return `404` (no probing).
- **Approval limits**: per-refund and per-UTC-day limits per user; org-wide hard cap applies to admins too.
- **Partial refunds**: only admins can approve less than the requested amount (`POST /refunds/{id}/approve`
  `{amount_cents}`; never more than requested). Limits are still checked against the full requested amount, so a
  partial approval can't sidestep them; the payout, daily/customer usage and analytics use the approved amount
  (`approved_amount_cents`).
- **Risk**: risk score ≥ threshold cannot be approved by a reviewer — must be escalated.
- **Customer cap**: reviewers can't push a customer over the 30-day refunded cap.
- **Four-eyes**: nobody can decide a refund they requested; can't be assigned one either.
- **State machine**: only `pending`/`escalated` can be decided; escalated → admin only.
- **Notes**: reject/escalate require a justification (min length policy).
- **Admin safety**: admins can't demote/deactivate themselves; viewers can't be assignees.
- **PII**: viewer responses mask customer name/email.
- **Audit**: every decision, denial, assignment, history lookup, policy/user change and login is written to
  `audit_log`, which a Postgres trigger makes append-only. Each row is also SHA-256 hash-chained to the previous
  one (`prev_hash` / `row_hash`, see [`backend/app/audit.py`](backend/app/audit.py)), so an edit or deletion made
  by someone who bypasses the trigger is still detected.
- The API also returns computed per-refund `permissions` so the UI can explain *why* a button is disabled.

## Architecture

```
frontend/  React 19 + Vite + TypeScript, react-router, recharts       :5173 (dev) / :8080 (docker)
   │ /api/* (proxy)
backend/   FastAPI + SQLAlchemy 2 + PostgreSQL 16, JWT auth            :8000  (OpenAPI docs at /docs)
   │ X-API-Key
backend/mock_provider/  stand-in third-party customer-history API      :9000
```

Tables: `users`, `customers`, `refund_requests` (status, reason, risk, amounts, SLA, assignee/escalator/decider),
`refund_events` (lifecycle timeline), `audit_log` (append-only), `policy_settings`, `request_traces` (per-request
traceability), `support_requests` (support/error/guardrail/compliance tickets for remediation), `access_reviews`
(completed user-access recertifications) and `data_subject_requests` (GDPR export/erasure requests). Postgres BI views
(`v_refund_facts`, `v_reviewer_daily`, `v_denied_actions`) and the audit-log immutability trigger live in
[`backend/app/sql/analytics.sql`](backend/app/sql/analytics.sql); the SQLAlchemy models are in
[`backend/app/models.py`](backend/app/models.py).
Demo data (≈170 refunds over 30 days) is seeded deterministically on first start in `dev` only — in `prod` seeding is off unless `SEED_ON_STARTUP=true` is set explicitly.

## Run it

### Option A — Docker (one command)

```bash
docker compose up --build
```
Open http://localhost:8080, click **Sign in with Acme SSO**, pick an identity, then enter the mock second-factor
code **123456** (configurable via `MOCK_MFA_CODE`; the whole mock IdP only exists when `ENVIRONMENT=dev`).
API docs: http://localhost:8000/docs.
Reset demo data with `docker compose down -v`.

`ENVIRONMENT` defaults to `dev` everywhere (local dev, Compose, tests). Set `ENVIRONMENT=prod` for a hardened
deployment: the mock IdP endpoints are not mounted, demo seeding is disabled unless `SEED_ON_STARTUP=true`, the payouts
sandbox failure controls are off unless `PAYMENTS_SANDBOX_CONTROLS=true`, and a missing `JWT_SECRET` or `PAYMENTS_WEBHOOK_SECRET` fails startup. The SPA learns the mode from the public `GET /api/auth/config` endpoint.

### Option B — local dev

Requires Python 3.11+, Node 20+ and a Postgres (e.g. `docker run -d --name refunds-pg -e POSTGRES_USER=refunds -e POSTGRES_PASSWORD=refunds -e POSTGRES_DB=refunds -p 5432:5432 postgres:16-alpine`).

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn mock_provider.main:app --port 9000 &     # third-party API
uvicorn app.main:app --port 8000 --reload        # API (creates tables + seeds)

# frontend (new terminal)
cd frontend
npm install
npm run dev                                       # http://localhost:5173
```

Config is via env vars (see [`backend/app/config.py`](backend/app/config.py) and [`.env.example`](.env.example)):
`ENVIRONMENT`, `DATABASE_URL`, `JWT_SECRET` (required in prod), `PAYMENTS_WEBHOOK_SECRET` (required in prod; see Payouts), `HISTORY_PROVIDER_URL`, `HISTORY_PROVIDER_API_KEY`, `ALLOWED_REDIRECT_ORIGINS`, `SEED_ON_STARTUP` (off by default in prod), `MOCK_MFA_CODE` (dev SSO second factor), `GUARDRAIL_ALERT_THRESHOLD` / `GUARDRAIL_ALERT_WINDOW_MINUTES` (auto-ticket on denied-action spikes), and the Compliance Center thresholds `SESSION_IDLE_LIMIT_MINUTES` (15), `AUDIT_RETENTION_DAYS` (400), `ACCESS_REVIEW_INTERVAL_DAYS` (90), `DORMANT_ACCOUNT_DAYS` (90), `DSAR_DUE_DAYS` (30).
Frontend: `VITE_API_URL` (defaults to `/api`).

### Tests

```bash
cd backend && pytest -q        # RBAC, limits, escalation, four-eyes, PII masking, audit, SSO + MFA, compliance controls
cd frontend && npm run build   # type-check + production build
cd frontend && npm run test:e2e # Playwright end-to-end (see below)
```

**End-to-end tests** ([`frontend/e2e/`](frontend/e2e/)): Playwright specs covering the full mock-SSO + 2FA login,
the reviewer approve/reject/history workflow, admin surfaces and viewer restrictions. They run against a live
stack — bring it up first (`docker compose up -d --build`, or `npm run dev` + backend on :8000), then:

```bash
cd frontend
npx playwright install chromium   # one-time browser download
npm run test:e2e                  # E2E_BASE_URL overrides the default http://localhost:8080
```

## Demo users

| User | Role | Per refund | Daily |
| --- | --- | --- | --- |
| priya.admin@acmepay.com / alex.admin@acmepay.com | admin | $10,000 | $100,000 |
| maria.gomez@acmepay.com | reviewer | $2,500 | $20,000 |
| sam.patel@acmepay.com | reviewer | $1,000 | $10,000 |
| jordan.lee@acmepay.com | reviewer | $500 | $5,000 |
| vik.auditor@acmepay.com | viewer | — | — |
| dana.brooks@contractor.acmepay.com | viewer | — | — |

`dana.brooks` is a seeded compliance finding: a contractor who has not signed in for 137 days and was never
offboarded. The seed also adds an overdue GDPR erasure request for `cus_1007`.

## Production notes

- Run with `ENVIRONMENT=prod` (the AWS Terraform deployment does): this unmounts the mock SSO IdP, disables demo seeding and requires a strong `JWT_SECRET` and `PAYMENTS_WEBHOOK_SECRET`. Then replace the mock SSO with a real OIDC provider — the frontend already uses the redirect + code-exchange flow.
- Replace `mock_provider` with the real vendor URL/key via `HISTORY_PROVIDER_URL` / `HISTORY_PROVIDER_API_KEY` (store as secrets).
- Use Alembic migrations instead of `create_all` once the schema stabilises.
- The same pattern (policy module + audit log + role-aware React screens) is a template for the KYC review queue and feature-flag admin panel.

## One-command setup (any Postgres)

```bash
./setup.sh local                               # Docker: Postgres 16 + API + provider + UI on this machine
./setup.sh external 'postgres://u:p@host/db'   # any hosted Postgres: Neon, Supabase, RDS/Aurora, Cloud SQL, Azure
./setup.sh dev [url]                           # no app containers: venv + uvicorn + Vite
./setup.sh aws                                 # Terraform: VPC, WAF, RDS, ECS, CloudFront (needs AWS creds)
./setup.sh inspect [url]                       # list tables, row counts and BI views in the database
```

The app only needs a `DATABASE_URL` (or `DB_HOST`/`DB_USER`/`DB_PASSWORD`). It creates the schema, BI views and audit trigger, and seeds demo data on first start.

## Monitoring & tracing

Every API request gets a unique, server-generated trace id (`tr_<uuid>`; a client-sent `X-Trace-Id` / `X-Request-ID` is only kept as `client_ref` so callers cannot merge traces). It is:

- returned on every response as `X-Trace-Id` (also in the body of 500s, and shown in UI error messages),
- stored in `request_traces` (method, path, status, latency, actor, error + stack),
- stamped on every `audit_log` row, every `refund_events` row and `refund_requests.created_trace_id`,
- included in every structured JSON log line (stdout → CloudWatch Logs on AWS; a saved Logs Insights query `refunds-console/trace-lookup` is provisioned).

Look a trace up (admin only):

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8000/admin/traces/tr_…        # request + audit + refund events + created rows
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/admin/traces?errors_only=true"
```

## Compliance Center

An owned app does not inherit a vendor's certifications the way a Power Apps tenant inherits Microsoft's. The
equivalent here is three layers: AWS attestations for the infrastructure, Cognition's own SOC 2 Type II report for
the Devin platform used to build and operate the app, and **application controls implemented in this repo, checked
continuously and evidenced on demand**. The last layer is the Compliance Center (admin and viewer/auditor nav →
*Compliance*), defined in [`backend/app/compliance.py`](backend/app/compliance.py) and served by
[`backend/app/routers/compliance.py`](backend/app/routers/compliance.py).

- **Control catalog**: 18 controls, each mapped to SOC 2, PCI DSS 4.0, SOX ITGC and/or GDPR references. Automated
  controls run live against the database and config on every request (`GET /compliance/controls`) and return
  `pass` / `warn` / `fail` with the evidence they looked at. Examples: segregation of duties (no self-decided
  refunds), delegated limits within the hard cap, admin sign-off on high-risk approvals, quarterly access review,
  dormant accounts (90 days), MFA, 15-minute session idle timeout (PCI DSS 8.2.8), security-event logging (successful sign-in,
  refund-decision and PII-access events must all be present), audit
  hash chain + append-only trigger, 12-month log retention, live PII-masking probe, GDPR request deadlines, and
  encryption in transit, and continuous monitoring (MN-01 warns while any failing control has no open ticket).
  High-risk sign-off is judged by the approver's role recorded on the approval event, not their current role.
  Infra/process controls (change management, WAF, backups) are `documented` and link to
  the files that implement them.
- **Evidence pack** (`GET /compliance/evidence-pack`): JSON snapshot of every control result, the audit-chain
  verification (including the chain `head_hash`), recent access reviews, data-subject requests (without their
  free-text notes, since viewers can download the pack) and the relevant config. The SHA-256 of the file is
  returned in `X-Evidence-SHA256` and written to the audit log, so the exported file can be matched to the export later.
- **Tamper-evident audit log** (`GET /compliance/audit-chain`): recomputes the hash chain and reports the first
  broken entry. Rows written before the chain existed are counted as legacy, not verified.
- **Access review** (`/compliance/access-review`, admin): certify or revoke every active user in one signed-off
  review. Revoking deactivates the account. The review and each revocation are audited, and admins cannot revoke themselves.
- **Data-subject requests** (`/compliance/dsr`, admin): log GDPR access/erasure requests with a 30-day due date.
  Export returns the subject's data; calling fulfil again on a completed export re-delivers it (audited as
  `compliance.dsr_export_redeliver`). Erasure pseudonymises the customer's name and email, redacts them from refund
  descriptions, and keeps the refund records,
  which must be retained for financial record-keeping (GDPR Art. 17(3)(b)).
- **Fix with Devin** (`POST /compliance/controls/{id}/remediate`, admin): opens a `compliance` ticket that contains the
  control, its framework references, the failing evidence and the expected remediation (deduped while open). With
  the Devin API configured, the ticket goes through the [self-healing loop](#self-healing-loop-devin), and Devin is
  told to fix the root cause with a regression test and never to weaken the check.

Out of the box (seeded demo data) four controls fail: no access review on record (AC-05), the dormant contractor
(AC-06) and the overdue erasure request (DP-02) can all be fixed in the UI. The 8-hour session vs the 15-minute
idle limit (IA-02) is a code change, so it is the one to hand to Devin.

## Self-healing loop (Devin)

- **Feature requests & problem reports**: the **Request a feature** button in the top bar (all roles) opens a
  modal — pick *Feature request* or *Report a problem*, add a title/details, submit. With `DEVIN_API_KEY` +
  `DEVIN_ORG_ID` configured (`AUTO_REMEDIATE_REQUESTS`, default on) the ticket is queued and a Devin session
  starts immediately: it investigates and opens a PR against `dev` when the request is actionable. Without the
  key the ticket is still recorded (`status: open`) for manual triage. Admins watch progress on the
  **Support tickets** page (status, Devin session link, re-send failed tickets to Devin).
- Any user can also file `POST /support/requests {kind: "feature"|"support", title, description, trace_id}`.
- Every unhandled exception automatically opens an `error` ticket (deduped by exception fingerprint for `REMEDIATION_COOLDOWN_MINUTES`).
- With `DEVIN_API_KEY` set (a legacy `apk_` key uses `POST /v1/sessions`; an org key also needs `DEVIN_ORG_ID` and uses `POST /v3/organizations/{org}/sessions`), error tickets start a Devin session automatically (`AUTO_REMEDIATE_ERRORS`). UI feature/problem requests are queued automatically (`AUTO_REMEDIATE_REQUESTS`); any other ticket is sent with `POST /admin/support/requests/{id}/remediate` or the **Send to Devin** button. The prompt contains the full trace bundle and instructs Devin to reproduce with a failing test, fix, keep CI green, and open a PR against `DEVIN_BASE_BRANCH` (default `dev`). Each session is capped at `DEVIN_MAX_ACU` (default 10).
- Admins see every ticket, its status and Devin session link on the **Support tickets** page (`GET /admin/support/requests`, `GET /admin/support/config`).
- Without the key, tickets are still recorded; nothing is sent anywhere.
- Hand-off is durable: tickets are queued in Postgres (`status=queued`) and a worker in each API process sends them (`SELECT … FOR UPDATE SKIP LOCKED`), so a ticket survives restarts/deploys and is sent once even with several API tasks. Tune with `REMEDIATION_POLL_SECONDS` / `REMEDIATION_WORKER_ENABLED`.
- **Compliance findings**: "Fix with Devin" on a failing Compliance Center control opens a `compliance` ticket through the same loop (see [Compliance Center](#compliance-center)).
- **Guardrail alerts**: every policy/RBAC denial is audited; a spike of `GUARDRAIL_ALERT_THRESHOLD` (default 50)
  denied actions within `GUARDRAIL_ALERT_WINDOW_MINUTES` (default 10) auto-opens a `guardrail` support ticket —
  deduped while one is open — which goes through the same Devin remediation loop, so a misfiring policy or an
  attack probe results in a investigated PR rather than a silent log line. Set the threshold to `0` to disable.

### Demo (`scripts/demo.sh`)

A repeatable demo on a throwaway `demo` branch, so `dev` and `main` are never touched:

```bash
scripts/demo.sh reset     # rebuild `demo` from dev (DEMO_BASE) + a planted bug (ZeroDivisionError in per-reviewer approval rate), close stale demo PRs
scripts/demo.sh up        # isolated stack (compose project refunds-demo, fresh DB) with DEVIN_BASE_BRANCH=demo
scripts/demo.sh trigger   # optional: does steps 2-3 below from the CLI
scripts/demo.sh status    # tickets + Devin session links
scripts/demo.sh down
```

1. Open http://localhost:8080 and sign in as `priya.admin@acmepay.com` (MFA code `MOCK_MFA_CODE`, default `123456`).
2. **Team & limits**: add a reviewer (they have no decisions yet).
3. **Analytics**: the page fails with a 500; the error shows the trace id.
4. **Support tickets**: an `error` ticket for the `ZeroDivisionError` is `devin_working`, with a link to the Devin session.
5. Follow the session to a PR into `demo` with a failing test, the fix and green CI. Run `reset` again to repeat.

To rehearse without spending ACUs, point the backend at a stub: `DEVIN_API_KEY=apk_fake DEVIN_API_URL=http://172.17.0.1:9999 scripts/demo.sh up`.

## Secrets (`scripts/secrets.sh`)

`setup.sh` and `scripts/db-ops.sh` load `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `JWT_SECRET`, `PROVIDER_API_KEY`, ... in this order,
never overriding something already exported:

1. the current environment
2. **AWS Secrets Manager** (`refunds-console/devin-api-key` holds the Devin key + org id, `refunds-console/app`) when AWS credentials work
3. `~/.config/refunds-console/secrets.env.gpg`: GPG/AES-256, passphrase-protected (outside the repo)
4. `~/.config/refunds-console/secrets.env`: plaintext fallback; refused unless mode `600` and owned by you

```bash
scripts/secrets.sh init     # prompts (no echo) and writes the encrypted local store
scripts/secrets.sh status   # where each secret came from (values never printed)
scripts/secrets.sh push     # copy the local store into Secrets Manager, then delete the local file
```

## Database monitoring & redundancy

On AWS (Terraform, `infra/modules/app/data.tf` + `infra/modules/app/db_resilience.tf`):
- **Multi-AZ standby** with automatic failover, and an optional read replica (`-var db_read_replica=true`).
- **Backups:** 14-day point-in-time recovery, plus an AWS Backup plan (daily kept 35 days, weekly kept 180 days) that copies every backup to a vault in `dr_region`.
- **Monitoring:** Enhanced Monitoring (60 s), Performance Insights, and a CloudWatch dashboard.
- **Alarms:** CPU, free storage, memory, connections, read/write latency and replica lag. These and the RDS failover/failure/storage events go to the alerts SNS topic (`-var alert_email=...`).

Anywhere (`scripts/db-ops.sh`):
```bash
scripts/db-ops.sh check [url]            # health: connections, size, cache hit, replication, long queries, vacuum
scripts/db-ops.sh backup [url]           # verified pg_dump into ./backups (rotates, mode 600)
scripts/db-ops.sh restore-test <dump>    # restore into a throwaway Postgres and count rows
scripts/db-ops.sh aws-harden <rds-id> [--replica]   # apply all of the above to any existing RDS instance
scripts/db-ops.sh aws-status <rds-id>
```

## CI/CD & branch flow

- **Branch model**: all work lands on `dev` via PR. Merging to `dev` runs CI, the *Dev deploy* docker-compose
  smoke test, and (when `AWS_DEPLOY_ENABLED=true`) an AWS deploy of `infra/dev`. Production only moves via a
  `dev` → `main` PR, which deploys `infra/prod`; a CI check
  (`branch-flow.yml`) rejects any other PR targeting `main`. (Hotfix exception: temporarily allow the PR in
  GitHub branch protection / merge it as an admin, then back-merge to `dev`.)
- `.github/workflows/ci.yml` (every PR and push to `main`/`dev`): ruff, pytest, a Postgres 16 smoke test (`backend/scripts/pg_smoke.py`), frontend oxlint + `tsc` + Vite build, Docker builds, `terraform fmt`/`validate`.
- `.github/workflows/dev.yml` (push to `dev`): CI, then the docker-compose dev deploy + smoke test above, plus an AWS deploy of the `dev` environment when `AWS_DEPLOY_ENABLED=true`.
- `.github/workflows/infra-plan.yml` (PRs touching `infra/`): `terraform plan` for both `infra/dev` and `infra/prod` against real AWS state, posted to the job summary. Enable by setting the repo variable `AWS_DEPLOY_ENABLED=true` (see below).
- `.github/workflows/deploy-env.yml` (reusable): builds/pushes the image to ECR (tag = commit SHA, passed through each env root's `image_tag` variable), `terraform apply`s the given environment root, waits for a stable ECS rollout (auto-rollback on failure), publishes the SPA to S3, invalidates CloudFront and smoke-tests `/api/health`. Apply errors are warnings, not failures — CloudFront-dependent resources are skipped if distribution creation is blocked (e.g. pending AWS account verification); frontend publish + smoke test run only when a distribution exists.
- `.github/workflows/deploy.yml` (**manual only** — Actions → Deploy → Run workflow): reruns CI, then deploys `infra/prod` (`ENVIRONMENT=prod`: mock SSO and demo seed disabled). Skipped until the repo variable `AWS_DEPLOY_ENABLED=true` is set.

## AWS infrastructure (`infra/`, Terraform)

Two environments share one module — `infra/modules/app` — instantiated by two thin roots:

- `infra/dev` → state key `refunds-console-dev/terraform.tfstate`, name `refunds-dev`: **minimal** — subnets in 2 AZs
  (ALB/RDS requirement) but no NAT (tasks run in public subnets), no WAF, no autoscaling, no AWS Backup,
  `db.t4g.micro` single-AZ, 1×256/1024 api + 1×256/512 provider Fargate tasks, `ENVIRONMENT=dev` (mock SSO + demo
  seed on, like docker compose). Deployed at https://d2eah532twinmd.cloudfront.net.
- `infra/prod` → state key `refunds-console/terraform.tfstate`, name `refunds-console`: the full stack below.
  `moved.tf` maps the pre-module state addresses so the first apply updates in place (no recreates).
  Currently sized for the AWS free tier (`db_multi_az=false`, `db_backup_retention_days=1`) — flip those back
  to the module defaults after upgrading the account plan. Deployed at https://d2r4c05e3dgv73.cloudfront.net
  (empty DB — `ENVIRONMENT=prod` disables seed; run a one-off ECS task with `SEED_ON_STARTUP=true` to load demo data).

```text
Browser ──HTTPS──> CloudFront (+ AWS WAF on prod: IP reputation, OWASP common, bad inputs, SQLi, rate limits)
                    ├── /*     -> S3 (private, OAC only)                      React SPA
                    └── /api/* -> ALB (SG: CloudFront prefix list only, secret origin header, else 403)
                                 └── ECS Fargate "api" (private subnets on prod, non-root, read-only FS)
                                      ├── autoscaling (prod): target tracking on CPU (60%), memory (75%),
                                      │   requests/target (1000/min); bounds api_min_count..api_max_count
                                      ├── RDS Postgres 16 (DB subnets, no public IP, KMS, force SSL,
                                      │   storage autoscaling, backups + DR copy, deletion protection)
                                      └── ECS "provider" (mock PayCore, reachable only from api via Cloud Map)
Secrets Manager (KMS): RDS-managed DB password, JWT secret, provider key, Devin API key. VPC flow logs, CloudWatch alarms -> SNS.
```

Same-origin SPA + API means the browser never makes cross-origin calls; CORS and the SSO redirect allow-list are pinned to allowed origins. Both are module variables (`allowed_redirect_origins`, `cors_origins`) supporting `*` wildcards — envs currently allow `https://*.cloudfront.net` plus `localhost:8080`/`localhost:5173` for local frontend dev. `TRUSTED_PROXY_HOPS=2` makes audit IPs come from the CloudFront/ALB-appended `X-Forwarded-For` entry, not client-supplied values.

Escape hatch: `allow_public_alb` (module var, default off) opens the ALB to `0.0.0.0/0` without the origin-verify header — for dev only while CloudFront is unavailable; keep it off in prod.

One-time setup (admin credentials, dedicated AWS account):

```bash
cd infra/bootstrap && terraform init && terraform apply          # state bucket, lock table, GitHub OIDC roles (optional)
gh secret set AWS_ACCESS_KEY_ID     --body "<key id>"           # IAM user with rights to build/apply this stack
gh secret set AWS_SECRET_ACCESS_KEY --body "<secret>"
gh variable set AWS_DEPLOY_ENABLED  --body true                 # turns on deploy.yml, dev.yml AWS deploy, infra-plan
gh variable set TF_STATE_BUCKET     --body "$(terraform output -raw state_bucket)"
gh variable set AWS_REGION          --body us-east-1
# optional: gh variable set DEVIN_ORG_ID --body "<org id>"
# optional self-healing: after the first deploy, `scripts/secrets.sh push` stores the Devin key in Secrets Manager.
# ECS reads secrets at task start, so roll the API tasks to pick the key up:
# aws ecs update-service --cluster refunds-console --service api --force-new-deployment
```

Deploys authenticate with the `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` repo secrets. `infra/bootstrap` can also
mint GitHub OIDC roles (`deploy_role_arn`/`plan_role_arn`) if you later want keyless CI — swap the
`aws-access-key-id`/`aws-secret-access-key` inputs in `deploy-env.yml`/`infra-plan.yml` for `role-to-assume` then.

Every infra setting is a Terraform variable on the module — change it in a PR (see
[`infra/modules/app/terraform.tfvars.example`](infra/modules/app/terraform.tfvars.example) for the full list):
instance class and storage for the database, Postgres parameters (`db_parameters`, e.g. `work_mem`), task
cpu/memory, autoscaling bounds, VPC CIDR, WAF rate limits, image tag (`image_tag`, fed by `TF_VAR_image_tag` =
commit SHA in deploys), allowed origins (`allowed_redirect_origins`, `cors_origins`), the `allow_public_alb`
escape hatch, and the environment sizing toggles (`az_count`, `enable_nat_gateway`, `enable_waf`,
`enable_autoscaling`, `enable_backup`, `db_multi_az`, ...). Env roots live in `infra/dev/main.tf` and
`infra/prod/main.tf`. On the PR, the **Infra plan** workflow shows the exact diff for both
environments; merging to `main` applies prod, merging to `dev` applies dev. Parameter-group changes that need a
reboot show as pending-reboot — reboot the instance from the RDS console or `aws rds reboot-db-instance` after merge.

Then merge to `main` (or run the Deploy workflow); the app URL is the `app_url` Terraform output of the matching
environment root.

Not yet production-grade: tables are created with `create_all` (use Alembic migrations before real data) and prod still needs a real OIDC provider wired into `/auth/token` (the mock IdP is dev-only). Redeemed SSO codes are tracked in Postgres (`used_sso_codes`), so single-use holds across API tasks.
