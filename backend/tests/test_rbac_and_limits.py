from app.models import AuditLog, RefundStatus

from .conftest import auth, make_refund, user

JORDAN = "jordan.lee@acmepay.com"   # reviewer, $500 limit, $5,000/day
SAM = "sam.patel@acmepay.com"       # reviewer
ADMIN = "priya.admin@acmepay.com"
VIEWER = "vik.auditor@acmepay.com"


def test_reviewer_can_approve_within_limit(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 200
    assert res.json()["status"] == "approved"


def test_reviewer_cannot_see_or_act_on_other_reviewers_ticket(client, db):
    r = make_refund(db, user(db, SAM))
    j = user(db, JORDAN)
    assert client.get(f"/refunds/{r.id}", headers=auth(j)).status_code == 404
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).status_code == 404
    ids = {i["id"] for i in client.get("/refunds?limit=500", headers=auth(j)).json()["items"]}
    assert r.id not in ids


def test_per_refund_limit_blocks_approval_but_allows_escalation(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=60_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 403
    assert "approval limit" in res.json()["detail"]
    res = client.post(f"/refunds/{r.id}/escalate", headers=auth(j), json={"note": "Above my approval limit"})
    assert res.status_code == 200
    assert res.json()["status"] == "escalated"
    assert res.json()["assigned_to"] is None


def test_daily_limit(client, db):
    j = user(db, JORDAN)
    for _ in range(10):
        r = make_refund(db, j, amount=50_000)
        assert client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).status_code == 200
    r = make_refund(db, j, amount=1_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 403
    assert "daily" in res.json()["detail"]


def test_high_risk_requires_escalation(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=1_000, risk=85)
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).status_code == 403


def test_four_eyes_rule(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, status=RefundStatus.escalated, requested_by=ADMIN)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={})
    assert res.status_code == 403
    assert "Four-eyes" in res.json()["detail"]


def test_reject_requires_note(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j)
    assert client.post(f"/refunds/{r.id}/reject", headers=auth(j), json={"note": "no"}).status_code == 403
    assert client.post(f"/refunds/{r.id}/reject", headers=auth(j), json={"note": "Duplicate not found in ledger"}).status_code == 200


def test_reviewer_cannot_decide_escalated_admin_can(client, db):
    j, a = user(db, JORDAN), user(db, ADMIN)
    r = make_refund(db, j, amount=60_000)
    client.post(f"/refunds/{r.id}/escalate", headers=auth(j), json={"note": "Above my approval limit"})
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).status_code == 403
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={}).json()["status"] == "approved"


def test_hard_cap_applies_to_admins(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=2_000_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={})
    assert res.status_code == 403
    assert "hard cap" in res.json()["detail"]


def test_viewer_is_read_only_and_pii_masked(client, db):
    v = user(db, VIEWER)
    r = make_refund(db, user(db, JORDAN))
    detail = client.get(f"/refunds/{r.id}", headers=auth(v)).json()
    assert detail["customer"]["pii_masked"] is True
    assert detail["customer"]["email"].startswith("t***@")
    assert not any(detail["permissions"][k] for k in ("can_approve", "can_reject", "can_escalate", "can_view_history"))
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(v), json={}).status_code == 403
    assert client.get(f"/refunds/{r.id}/customer-history", headers=auth(v)).status_code == 403
    assert client.get("/admin/users", headers=auth(v)).status_code == 403
    assert client.get("/analytics/overview", headers=auth(v)).status_code == 200


def test_reviewer_cannot_use_admin_endpoints(client, db):
    j = user(db, JORDAN)
    for path in ("/admin/users", "/admin/audit-log", "/admin/policies", "/analytics/overview"):
        assert client.get(path, headers=auth(j)).status_code == 403


def test_denials_are_audited(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=60_000)
    client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    entry = db.query(AuditLog).filter_by(action="refund.approve", success=False).one()
    assert entry.actor_email == JORDAN
    assert "approval limit" in entry.detail["reason"]


def test_admin_cannot_set_limit_above_hard_cap_or_demote_self(client, db):
    a, j = user(db, ADMIN), user(db, JORDAN)
    res = client.patch(f"/admin/users/{j.id}", headers=auth(a), json={"approval_limit_cents": 5_000_000})
    assert res.status_code == 422
    assert client.patch(f"/admin/users/{a.id}", headers=auth(a), json={"role": "viewer"}).status_code == 422
    res = client.patch(f"/admin/users/{j.id}", headers=auth(a), json={"approval_limit_cents": 80_000, "daily_approval_limit_cents": 800_000})
    assert res.status_code == 200 and res.json()["approval_limit_cents"] == 80_000


def test_deactivated_user_loses_access(client, db):
    a, j = user(db, ADMIN), user(db, JORDAN)
    client.patch(f"/admin/users/{j.id}", headers=auth(a), json={"is_active": False})
    assert client.get("/auth/me", headers=auth(j)).status_code == 401


def test_escalated_refund_only_assignable_to_admin(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, status=RefundStatus.escalated)
    assert client.post(f"/refunds/{r.id}/assign", headers=auth(a), json={"assignee_id": user(db, SAM).id}).status_code == 422
    assert client.post(f"/refunds/{r.id}/assign", headers=auth(a), json={"assignee_id": a.id}).status_code == 200


def test_sso_flow_rejects_foreign_redirect(client, db):
    res = client.get("/auth/sso/login", params={"redirect_uri": "https://evil.example.com/"})
    assert res.status_code == 400
    res = client.post("/auth/sso/authorize", data={"email": JORDAN, "redirect_uri": "http://localhost:8080/"}, follow_redirects=False)
    assert res.status_code == 303
    code = res.headers["location"].split("code=")[1]
    tok = client.post("/auth/token", json={"code": code})
    assert tok.status_code == 200 and tok.json()["user"]["role"] == "reviewer"
    # An SSO code is not an access token.
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {code}"}).status_code == 401
    # Codes are single-use, and forwarded headers can't widen the redirect allow-list.
    assert client.post("/auth/token", json={"code": code}).status_code == 401
    res = client.post("/auth/sso/authorize", data={"email": JORDAN, "redirect_uri": "https://evil.example.com/"},
                      headers={"X-Forwarded-Host": "evil.example.com", "X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert res.status_code == 400
