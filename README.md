# Refunds Console — an internal tool built with Devin

A working prototype of a fintech **refunds dashboard**, built end-to-end with [Devin](https://devin.ai) to show that
an engineering team can replace a low-code platform such as Microsoft Power Apps (≈ $250K/yr in licences) with
owned, version-controlled code — without giving up the things that make low-code attractive: speed, role-based
screens, approvals, connectors and analytics.

| Power Apps capability | How it's done here |
| --- | --- |
| Role-based screens | One React app; navigation, data and buttons change per role (admin / reviewer / viewer) |
| Approval workflows | Approve / reject / escalate with notes, per-refund + daily limits, admin sign-off for escalations |
| Entra ID / SSO | Mock OIDC authorization-code flow (`/auth/sso/*`) → swap in Okta / Entra / Google with config |
| Dataverse tables | PostgreSQL schema via SQLAlchemy models, constraints and indexes |
| Connectors | Authenticated HTTP client to a third-party "PayCore" customer-history API (mocked, with API key) |
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

## Controls (enforced server-side in [`backend/app/policy.py`](backend/app/policy.py))

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

Tables: `users`, `customers`, `refund_requests`, `refund_events` (lifecycle timeline), `audit_log`,
`policy_settings`. BI views and the audit trigger live in [`backend/app/sql/analytics.sql`](backend/app/sql/analytics.sql).
Demo data (≈170 refunds over 30 days) is seeded deterministically on first start.

## Run it

### Option A — Docker (one command)

```bash
docker compose up --build
```
Open http://localhost:8080, click **Sign in with Acme SSO** and pick an identity. API docs: http://localhost:8000/docs.
Reset demo data with `docker compose down -v`.

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
`DATABASE_URL`, `JWT_SECRET`, `HISTORY_PROVIDER_URL`, `HISTORY_PROVIDER_API_KEY`, `ALLOWED_REDIRECT_ORIGINS`, `SEED_ON_STARTUP`.
Frontend: `VITE_API_URL` (defaults to `/api`).

### Tests

```bash
cd backend && pytest -q        # RBAC, limits, escalation, four-eyes, PII masking, audit, SSO
cd frontend && npm run build   # type-check + production build
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

- Replace the mock SSO with a real OIDC provider (the frontend already uses the redirect + code-exchange flow) and set a strong `JWT_SECRET`.
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
- With `DEVIN_API_KEY` + `DEVIN_ORG_ID` set, error tickets (and support tickets via `POST /admin/support/requests/{id}/remediate`) start a Devin session through the Devin API (`POST /v3/organizations/{org}/sessions`). The prompt contains the full trace bundle and instructs Devin to reproduce with a failing test, fix, keep CI green, and open a PR against `main`. The session URL is stored on the ticket (`GET /admin/support/requests`).
- Without the key, tickets are still recorded; nothing is sent anywhere.

## Secrets (`scripts/secrets.sh`)

`setup.sh` and `scripts/db-ops.sh` load `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `JWT_SECRET`, `PROVIDER_API_KEY`, ... in this order,
never overriding something already exported:

1. the current environment
2. **AWS Secrets Manager** (`refunds-console/devin-api-key`, `refunds-console/app`, `refunds-console/local`) when AWS credentials work
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

## CI/CD

- `.github/workflows/ci.yml` (every PR and push): ruff, pytest, a Postgres 16 smoke test (`backend/scripts/pg_smoke.py`), frontend oxlint + `tsc` + Vite build, Docker builds, `terraform fmt`/`validate`.
- `.github/workflows/deploy.yml` (push to `main`): reruns CI, then via GitHub OIDC (no stored AWS keys) builds/pushes the image to ECR, `terraform apply`s with the new image tag, waits for a stable ECS rollout (auto-rollback on failure), publishes the SPA to S3, invalidates CloudFront and smoke-tests `/api/health`. Skipped until the repo variable `AWS_DEPLOY_ROLE_ARN` is set.

## AWS infrastructure (`infra/`, Terraform)

```text
Browser ──HTTPS──> CloudFront (+ AWS WAF: IP reputation, OWASP common, bad inputs, SQLi, per-IP + SSO rate limits)
                    ├── /*     -> S3 (private, OAC only)                      React SPA
                    └── /api/* -> ALB (SG: CloudFront prefix list only, secret origin header, else 403)
                                 └── ECS Fargate "api" (private subnets, non-root, read-only FS)
                                      ├── RDS Postgres 16 (DB subnets, no public IP, KMS, force SSL, backups, deletion protection)
                                      └── ECS "provider" (mock PayCore, reachable only from api via Cloud Map)
Secrets Manager (KMS): RDS-managed DB password, JWT secret, provider key, Devin API key. VPC flow logs, CloudWatch alarms -> SNS.
```

Same-origin SPA + API means the browser never makes cross-origin calls; CORS and the SSO redirect allow-list are pinned to the CloudFront origin. `TRUSTED_PROXY_HOPS=2` makes audit IPs come from the CloudFront/ALB-appended `X-Forwarded-For` entry, not client-supplied values.

One-time setup (admin credentials, dedicated AWS account):

```bash
cd infra/bootstrap && terraform init && terraform apply          # state bucket, lock table, GitHub OIDC deploy role
gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform output -raw deploy_role_arn)"
gh variable set TF_STATE_BUCKET   --body "$(terraform output -raw state_bucket)"
gh variable set AWS_REGION        --body us-east-1
# optional self-healing: after the first deploy, `scripts/secrets.sh push` stores the Devin key in Secrets Manager.
# ECS reads secrets at task start, so roll the API tasks to pick the key up:
# aws ecs update-service --cluster refunds-console --service api --force-new-deployment
```

Then merge to `main` (or run the Deploy workflow); the app URL is the `app_url` Terraform output.

Not yet production-grade: tables are created with `create_all` (use Alembic migrations before real data), the SSO is a mock (swap for Okta/Entra OIDC), and the single-use SSO code store is per-process.
