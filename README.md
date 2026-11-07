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
