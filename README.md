# Refunds Console — an internal tool built with Devin

A working prototype of a fintech **refunds dashboard**, built end-to-end with [Devin](https://devin.ai) to show that
an engineering team can replace a low-code platform such as Microsoft Power Apps (≈ $250K/yr in licences) with
owned, version-controlled code — without giving up the things that make low-code attractive: speed, role-based
screens, approvals, connectors and analytics.

| Power Apps capability | How it's done here |
| --- | --- |
| Role-based screens | One React app; navigation, data and buttons change per role (admin / reviewer / viewer) |
| Approval workflows | Approve / reject / escalate with notes, per-refund + daily limits, admin sign-off for escalations |
| Entra ID / SSO | Mock OIDC authorization-code flow (`/auth/sso/*`, dev only) → swap in Okta / Entra / Google with config |
| Dataverse tables | PostgreSQL schema via SQLAlchemy models, constraints and indexes |
| Connectors | Authenticated HTTP client to a third-party "PayCore" customer-history API (mocked, with API key) |
| Moving money through a provider | Approved refunds are paid back through PayCore's refunds API: idempotency keys, retries with backoff, HMAC-signed webhooks, reconciliation ([below](#payouts-through-a-payments-provider)) |
| Power BI dashboards | Analytics page (Recharts), CSV export, Postgres BI views (`v_refund_facts`, …) |
| Environment governance / auditing | Append-only audit log (DB trigger), every allowed **and denied** action is recorded |
| Hosting | `docker compose up` — Postgres, API, mock provider and static frontend behind nginx |

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

**Viewer** (`vik.auditor`) — read-only analytics and refunds list, customer PII masked, no actions, no history calls.

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
- **Risk**: risk score ≥ threshold cannot be approved by a reviewer — must be escalated.
- **Customer cap**: reviewers can't push a customer over the 30-day refunded cap.
- **Four-eyes**: nobody can decide a refund they requested; can't be assigned one either.
- **State machine**: only `pending`/`escalated` can be decided; escalated → admin only.
- **Notes**: reject/escalate require a justification (min length policy).
- **Admin safety**: admins can't demote/deactivate themselves; viewers can't be assignees.
- **PII**: viewer responses mask customer name/email.
- **Audit**: every decision, denial, assignment, history lookup, policy/user change and login is written to
  `audit_log`, which a Postgres trigger makes append-only.
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
traceability), and `support_requests` (support/error tickets for remediation). Postgres BI views
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
`ENVIRONMENT`, `DATABASE_URL`, `JWT_SECRET` (required in prod), `PAYMENTS_WEBHOOK_SECRET` (required in prod; see Payouts), `HISTORY_PROVIDER_URL`, `HISTORY_PROVIDER_API_KEY`, `ALLOWED_REDIRECT_ORIGINS`, `SEED_ON_STARTUP` (off by default in prod), `MOCK_MFA_CODE` (dev SSO second factor), `GUARDRAIL_ALERT_THRESHOLD` / `GUARDRAIL_ALERT_WINDOW_MINUTES` (auto-ticket on denied-action spikes).
Frontend: `VITE_API_URL` (defaults to `/api`).

### Tests

```bash
cd backend && pytest -q        # RBAC, limits, escalation, four-eyes, PII masking, audit, SSO + MFA
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

## Self-healing loop (Devin)

- Any user can file `POST /support/requests {title, description, trace_id}`.
- Every unhandled exception automatically opens an `error` ticket (deduped by exception fingerprint for `REMEDIATION_COOLDOWN_MINUTES`).
- With `DEVIN_API_KEY` set (a legacy `apk_` key uses `POST /v1/sessions`; an org key also needs `DEVIN_ORG_ID` and uses `POST /v3/organizations/{org}/sessions`), error tickets start a Devin session automatically (`AUTO_REMEDIATE_ERRORS`). Support tickets are sent with `POST /admin/support/requests/{id}/remediate` or the **Send to Devin** button. The prompt contains the full trace bundle and instructs Devin to reproduce with a failing test, fix, keep CI green, and open a PR against `DEVIN_BASE_BRANCH` (default `dev`). Each session is capped at `DEVIN_MAX_ACU` (default 10).
- Admins see every ticket, its status and Devin session link on the **Support tickets** page (`GET /admin/support/requests`, `GET /admin/support/config`).
- Without the key, tickets are still recorded; nothing is sent anywhere.
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

On AWS (Terraform, `infra/data.tf` + `infra/db_resilience.tf`):
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

- **Branch model**: all work lands on `dev` via PR. Merging to `dev` runs CI and then the *Dev deploy* workflow,
  which brings the whole stack up with `docker compose` and smoke-tests `/health`, `/auth/config` and the mock
  SSO page — that merge **is** the dev deployment. Production only moves via a `dev` → `main` PR; a CI check
  (`branch-flow.yml`) rejects any other PR targeting `main`. (Hotfix exception: temporarily allow the PR in
  GitHub branch protection / merge it as an admin, then back-merge to `dev`.)
- `.github/workflows/ci.yml` (every PR and push to `main`/`dev`): ruff, pytest, a Postgres 16 smoke test (`backend/scripts/pg_smoke.py`), frontend oxlint + `tsc` + Vite build, Docker builds, `terraform fmt`/`validate`.
- `.github/workflows/dev.yml` (push to `dev`): CI, then the docker-compose dev deploy + smoke test above.
- `.github/workflows/infra-plan.yml` (PRs touching `infra/`): `terraform plan` against real AWS state via a read-only OIDC role, posted to the job summary. Enable by setting the repo variable `AWS_PLAN_ROLE_ARN` (see below).
- `.github/workflows/deploy.yml` (push to `main` only): reruns CI, then via GitHub OIDC (no stored AWS keys) builds/pushes the image to ECR, `terraform apply`s with the new image tag, waits for a stable ECS rollout (auto-rollback on failure), publishes the SPA to S3, invalidates CloudFront and smoke-tests `/api/health`. The AWS deployment runs with `ENVIRONMENT=prod` (mock SSO and demo seed disabled). Skipped until the repo variable `AWS_DEPLOY_ROLE_ARN` is set.

## AWS infrastructure (`infra/`, Terraform)

```text
Browser ──HTTPS──> CloudFront (+ AWS WAF: IP reputation, OWASP common, bad inputs, SQLi, per-IP + SSO rate limits)
                    ├── /*     -> S3 (private, OAC only)                      React SPA
                    └── /api/* -> ALB (SG: CloudFront prefix list only, secret origin header, else 403)
                                 └── ECS Fargate "api" (private subnets, non-root, read-only FS)
                                      ├── autoscaling: target tracking on CPU (60%), memory (75%),
                                      │   requests/target (1000/min); bounds api_min_count..api_max_count
                                      ├── RDS Postgres 16 (DB subnets, no public IP, KMS, force SSL,
                                      │   storage autoscaling, backups, deletion protection)
                                      └── ECS "provider" (mock PayCore, reachable only from api via Cloud Map)
Secrets Manager (KMS): RDS-managed DB password, JWT secret, provider key, Devin API key. VPC flow logs, CloudWatch alarms -> SNS.
```

Same-origin SPA + API means the browser never makes cross-origin calls; CORS and the SSO redirect allow-list are pinned to the CloudFront origin. `TRUSTED_PROXY_HOPS=2` makes audit IPs come from the CloudFront/ALB-appended `X-Forwarded-For` entry, not client-supplied values.

One-time setup (admin credentials, dedicated AWS account):

```bash
cd infra/bootstrap && terraform init && terraform apply          # state bucket, lock table, GitHub OIDC deploy + plan roles
gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform output -raw deploy_role_arn)"
gh variable set AWS_PLAN_ROLE_ARN  --body "$(terraform output -raw plan_role_arn)"   # enables infra-plan on PRs
gh variable set TF_STATE_BUCKET   --body "$(terraform output -raw state_bucket)"
gh variable set AWS_REGION        --body us-east-1
# optional self-healing: after the first deploy, `scripts/secrets.sh push` stores the Devin key in Secrets Manager.
# ECS reads secrets at task start, so roll the API tasks to pick the key up:
# aws ecs update-service --cluster refunds-console --service api --force-new-deployment
```

Every infra setting is a Terraform variable — change it in a PR (see [`infra/terraform.tfvars.example`](infra/terraform.tfvars.example) for the full list): instance class and storage for the database, Postgres parameters (`db_parameters`, e.g. `work_mem`), task cpu/memory, autoscaling bounds, VPC CIDR, WAF rate limits. On the PR, the **Infra plan** workflow shows the exact diff; merging to `main` applies it. Parameter-group changes that need a reboot show as pending-reboot — reboot the instance from the RDS console or `aws rds reboot-db-instance` after merge.

Then merge to `main` (or run the Deploy workflow); the app URL is the `app_url` Terraform output.

Not yet production-grade: tables are created with `create_all` (use Alembic migrations before real data), prod still needs a real OIDC provider wired into `/auth/token` (the mock IdP is dev-only), and the single-use SSO code store is per-process.
