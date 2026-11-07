"""CI smoke test against real Postgres: create schema + BI views, seed, and run a traced request."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.main import app

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
    res = client.get("/analytics/overview", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    trace = client.get(f"/admin/traces/{res.headers['X-Trace-Id']}", headers={"Authorization": f"Bearer {token}"})
    assert trace.status_code == 200 and trace.json()["request"]["actor_email"] == "priya.admin@acmepay.com", trace.text
    # Compliance Center: audit hash chain verifies on Postgres (JSONB round-trip) and the append-only trigger exists.
    res = client.get("/compliance/controls", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    au02 = next(c for c in res.json()["controls"] if c["id"] == "AU-02")
    assert au02["status"] == "pass" and au02["evidence"]["append_only_trigger"], au02

with engine.connect() as conn:
    n = conn.execute(text("select count(*) from refund_requests")).scalar_one()
    assert n > 100, n
print("postgres smoke ok:", n, "refunds")
