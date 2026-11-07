# Agent instructions

- Purpose: this codebase is a working prototype of an internal fintech refunds console, built with Devin to demonstrate replacing a low-code platform such as Microsoft Power Apps with owned, version-controlled software. It includes role-based screens, refund approval workflows, mock SSO, PostgreSQL persistence, audit logging, analytics, a mocked third-party customer-history connector, and AWS deployment infrastructure.
- Keep `README.md` synchronized with the code. Whenever a change affects documented behavior, setup steps, architecture, database tables/views, environment variables, scripts, CI/CD, or deployment details, update the README in the same change.
- Prefer updating the existing README section over adding duplicate documentation. Verify commands and file references against the current code before documenting them.
- The README's "Why owned code instead of low-code" section states the project's value proposition (low cost, low maintenance, high iteration speed, high flexibility, easy third-party connections). Keep its concrete claims (defaults, file names, workflows) true when you change them.

## Workflow

- Open feature PRs against `dev`, never `main`: `.github/workflows/branch-flow.yml` fails any PR into `main` whose head isn't `dev`; `dev` auto-deploys and production moves via a `dev` → `main` PR.
- Before pushing, run what CI runs: `cd backend && ruff check app tests mock_provider && pytest -q` (install `requirements.txt` and `requirements-dev.txt`), and `cd frontend && npm ci && npm run lint && npm run build`.
- Every authorization rule lives in `backend/app/policy.py` and every allowed or denied action must be written through `backend/app/audit.py`. Never weaken a limit, permission check or compliance control to make a test or feature pass; fix the code and add a regression test.
- Support tickets reach Devin through the Postgres queue in `backend/app/remediation.py` (`status="queued"` + `notify()`); do not call `start_devin_session` from request handlers.
- Review guidelines for PRs are in `REVIEW.md`.
