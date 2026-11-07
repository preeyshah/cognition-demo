"""CI smoke test against real Postgres: create schema + BI views, seed, and run a traced request."""

import os

os.environ.setdefault("REMEDIATION_WORKER_ENABLED", "false")  # drained explicitly below

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import remediation  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SupportRequest  # noqa: E402

with TestClient(app) as client:  # lifespan runs init_db (create_all, analytics SQL, seed)
    res = client.get("/health")
    assert res.status_code == 200 and res.headers["X-Trace-Id"], res.text
    res = client.post("/auth/sso/authorize", data={"email": "priya.admin@acmepay.com",
                      "redirect_uri": "http://localhost:8080/"}, follow_redirects=False)
    code = res.headers["location"].split("code=")[1]
    res = client.post("/auth/token", json={"code": code})
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    assert client.post("/auth/token", json={"code": code}).status_code == 401  # single-use, DB-backed
    res = client.get("/analytics/overview", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    trace = client.get(f"/admin/traces/{res.headers['X-Trace-Id']}", headers={"Authorization": f"Bearer {token}"})
    assert trace.status_code == 200 and trace.json()["request"]["actor_email"] == "priya.admin@acmepay.com", trace.text

# Queue: a ticket locked by another worker is skipped (SKIP LOCKED); the free one is processed.
with SessionLocal() as db:
    a, b = SupportRequest(kind="support", title="smoke a", status="queued"), SupportRequest(kind="support", title="smoke b", status="queued")
    db.add_all([a, b])
    db.commit()
    a_id, b_id = a.id, b.id
with SessionLocal() as other_worker:
    other_worker.scalar(select(SupportRequest).where(SupportRequest.id == a_id).with_for_update())
    assert remediation.process_queue() == 1  # Devin key unset in CI -> ticket goes back to "open"
    other_worker.rollback()
with SessionLocal() as db:
    assert db.get(SupportRequest, a_id).status == "queued"
    assert db.get(SupportRequest, b_id).status == "open"

with engine.connect() as conn:
    n = conn.execute(text("select count(*) from refund_requests")).scalar_one()
    assert n > 100, n
print("postgres smoke ok:", n, "refunds")
