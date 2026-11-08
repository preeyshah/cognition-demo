# Review guidelines

This is a fintech refunds console (see `AGENTS.md`). Reviews should prioritise correctness of money movement,
authorization and auditability over style.

## Critical areas
- `backend/app/policy.py`, `backend/app/security.py`, `backend/app/routers/`: any change to who can see or decide a
  refund, approval limits, four-eyes rules, PII masking or the escalation state machine. Flag loosened checks,
  missing `admin_only`/role dependencies and endpoints that leak existence (should return `404` to reviewers).
- `backend/app/audit.py` and `backend/app/sql/analytics.sql`: the audit log must stay append-only and hash-chained;
  every allowed **and** denied action must be audited.
- `backend/app/payouts.py`, `backend/app/payments_client.py`: idempotency keys reused across retries, approval and
  payout intent in one transaction, HMAC + timestamp webhook verification, duplicate/out-of-order event handling.
- `backend/app/compliance.py`: controls must not be weakened, skipped or special-cased to pass.
- `backend/app/remediation.py`: tickets go through the Postgres queue (`FOR UPDATE SKIP LOCKED`); flag direct Devin
  API calls from request handlers, secrets in prompts or responses, and missing `DEVIN_MAX_ACU` caps.
- `infra/` and `.github/workflows/`: no public database access, no long-lived AWS keys (GitHub OIDC only), WAF and
  CloudFront origin protection kept, `branch-flow.yml` not bypassed.

## Conventions
- Behaviour changes need tests in `backend/tests/` (or `frontend/e2e/` for UI flows) and must keep CI green:
  ruff, pytest, the Postgres smoke test, oxlint, `tsc` and the Vite build.
- `README.md` must be updated in the same PR when documented behaviour, setup, env vars, tables/views, scripts,
  CI/CD or infrastructure change. Check the "Why owned code instead of low-code" claims still hold.
- New settings go in `backend/app/config.py` with safe defaults; prod (`ENVIRONMENT=prod`) must not enable mock SSO
  or demo seeding.
- Third-party integrations are thin authenticated HTTP clients configured by env vars; keys come from secrets,
  never hard-coded.
- Feature PRs target `dev`, not `main`.

## Performance
- Flag queries inside loops / N+1 patterns in routers and analytics, and unbounded list endpoints (lists are capped,
  e.g. `limit(200)`).

## Ignore
- `frontend/package-lock.json` unless dependencies changed; generated `dist/`, `node_modules/`, `.venv/`.
