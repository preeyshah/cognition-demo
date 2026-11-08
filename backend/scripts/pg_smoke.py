"""CI smoke test against real Postgres: create schema + BI views, seed, and run a traced request."""

import os

os.environ.setdefault("REMEDIATION_WORKER_ENABLED", "false")  # drained explicitly below

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import remediation  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SupportRequest  # noqa: E402

with TestClient(app) as client:  # lifespan runs init_db (create_all, analytics SQL, seed)
    res = client.get("/health")
    assert res.status_code == 200 and res.headers["X-Trace-Id"], res.text
    # Mock SSO is authorize -> MFA page -> mfa (code) -> 303 redirect carrying the auth code.
    res = client.post("/auth/sso/authorize", data={"email": "priya.admin@acmepay.com",
                      "redirect_uri": "http://localhost:8080/"}, follow_redirects=False)
    assert res.status_code == 200, res.text
    res = client.post("/auth/sso/mfa", data={"email": "priya.admin@acmepay.com",
                      "redirect_uri": "http://localhost:8080/", "code": get_settings().mock_mfa_code},
                      follow_redirects=False)
    assert res.status_code == 303, res.text
    code = res.headers["location"].split("code=")[1]
    res = client.post("/auth/token", json={"code": code})
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    assert client.post("/auth/token", json={"code": code}).status_code == 401  # single-use, DB-backed
    res = client.get("/analytics/overview", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    trace = client.get(f"/admin/traces/{res.headers['X-Trace-Id']}", headers={"Authorization": f"Bearer {token}"})
    assert trace.status_code == 200 and trace.json()["request"]["actor_email"] == "priya.admin@acmepay.com", trace.text
    # Compliance Center: audit hash chain verifies on Postgres (JSONB round-trip) and the append-only trigger exists.
    res = client.get("/compliance/controls", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    au02 = next(c for c in res.json()["controls"] if c["id"] == "AU-02")
    assert au02["status"] == "pass" and au02["evidence"]["append_only_trigger"], au02
    # Partial refund: admin approves less than requested; the payout and the BI view carry the approved amount.
    admin_h = {"Authorization": f"Bearer {token}"}
    esc = client.get("/refunds?status=escalated&limit=500", headers=admin_h).json()["items"]
    target = next(r for r in esc if r["permissions"]["can_approve_partial"])
    res = client.post(f"/refunds/{target['id']}/approve", headers=admin_h,
                      json={"amount_cents": target["amount_cents"] // 2, "note": "partial smoke"})
    assert res.status_code == 200 and res.json()["payout"]["amount_cents"] == target["amount_cents"] // 2, res.text

# Queue: a ticket locked by another worker is skipped (SKIP LOCKED); the free one is processed.
with SessionLocal() as db:
    a, b = SupportRequest(kind="support", title="smoke a", status="queued"), SupportRequest(kind="support", title="smoke b", status="queued")
    db.add_all([a, b])
    db.commit()
    a_id, b_id = a.id, b.id
with SessionLocal() as other_worker:
    other_worker.scalar(select(SupportRequest).where(SupportRequest.id == a_id).with_for_update())
    while remediation.process_queue():  # Devin key unset in CI -> tickets go back to "open"
        pass
    other_worker.rollback()
with SessionLocal() as db:
    assert db.get(SupportRequest, a_id).status == "queued"
    assert db.get(SupportRequest, b_id).status == "open"

with engine.connect() as conn:
    n = conn.execute(text("select count(*) from refund_requests")).scalar_one()
    assert n > 100, n
    refunded, partial = conn.execute(text("select refunded_cents, is_partial from v_refund_facts where id = :i"),
                                     {"i": target["id"]}).one()
    assert partial and refunded == target["amount_cents"] // 2, (refunded, partial)
print("postgres smoke ok:", n, "refunds")
