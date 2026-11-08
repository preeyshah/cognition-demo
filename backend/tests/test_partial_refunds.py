from app.models import AuditLog, Payout, RefundStatus

from .conftest import auth, make_refund, user

JORDAN = "jordan.lee@acmepay.com"   # reviewer, $500 limit, $5,000/day
ADMIN = "priya.admin@acmepay.com"
VIEWER = "vik.auditor@acmepay.com"


def test_admin_can_approve_partial_refund_and_payout_moves_partial_amount(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=80_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a),
                      json={"amount_cents": 30_000, "note": "Only the duplicate line item"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "approved"
    assert body["amount_cents"] == 80_000
    assert body["approved_amount_cents"] == 30_000
    assert body["payout"]["amount_cents"] == 30_000
    db.expire_all()
    assert db.query(Payout).filter_by(refund_id=r.id).one().amount_cents == 30_000
    row = db.query(AuditLog).filter_by(action="refund.approve", entity_id=str(r.id), success=True).one()
    assert row.detail["approved_amount_cents"] == 30_000 and row.detail["partial"] is True


def test_full_approval_records_full_amount(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    body = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).json()
    assert body["approved_amount_cents"] == 20_000
    assert body["payout"]["amount_cents"] == 20_000
    r2 = make_refund(db, j, amount=20_000)
    assert client.post(f"/refunds/{r2.id}/approve", headers=auth(j), json={"amount_cents": 20_000}).status_code == 200


def test_reviewer_cannot_approve_partial_refund(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={"amount_cents": 5_000})
    assert res.status_code == 403
    assert "Only admins can approve a partial refund" in res.json()["detail"]
    db.expire_all()
    assert db.get(type(r), r.id).status == RefundStatus.pending
    assert db.query(AuditLog).filter_by(action="refund.approve", entity_id=str(r.id), success=False).count() == 1


def test_viewer_cannot_approve_partial_refund(client, db):
    v = user(db, VIEWER)
    r = make_refund(db, None, amount=20_000, status=RefundStatus.escalated)
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(v), json={"amount_cents": 5_000}).status_code == 403


def test_partial_amount_cannot_exceed_requested(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=20_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 20_001})
    assert res.status_code == 403
    assert "cannot exceed the requested amount" in res.json()["detail"]
    for bad in (0, -500):
        res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": bad})
        assert res.status_code == 403
        assert "must be positive" in res.json()["detail"]
    assert db.query(AuditLog).filter_by(action="refund.approve", entity_id=str(r.id), success=False).count() == 3


def test_partial_approval_does_not_bypass_limits(client, db):
    """Limits are checked against the full requested amount, so a partial approval can't sidestep them."""
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=a.approval_limit_cents + 1, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 1_000})
    assert res.status_code == 403
    assert "per-refund approval limit" in res.json()["detail"]


def test_four_eyes_applies_to_partial_approval(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=20_000, status=RefundStatus.escalated, requested_by=ADMIN)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 1_000})
    assert res.status_code == 403
    assert "Four-eyes" in res.json()["detail"]


def test_amount_only_accepted_on_approve(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    res = client.post(f"/refunds/{r.id}/reject", headers=auth(j), json={"note": "Duplicate not found in ledger", "amount_cents": 1_000})
    assert res.status_code == 422


def test_partial_permission_exposed_to_admins_only(client, db):
    a, j = user(db, ADMIN), user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    assert client.get(f"/refunds/{r.id}", headers=auth(j)).json()["permissions"]["can_approve_partial"] is False
    assert client.get(f"/refunds/{r.id}", headers=auth(a)).json()["permissions"]["can_approve_partial"] is True


def test_daily_usage_and_analytics_count_approved_amount(client, db):
    from app.services import approved_today_cents

    a = user(db, ADMIN)
    r = make_refund(db, None, amount=80_000, status=RefundStatus.escalated)
    used_before = approved_today_cents(db, a.id)
    refunded_before = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 30_000}).status_code == 200
    assert approved_today_cents(db, a.id) - used_before == 30_000
    refunded_after = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    assert refunded_after - refunded_before == 30_000
