from app import payouts
from app.models import AuditLog, Payout, RefundEvent, RefundStatus
from app.services import approved_today_cents

from .conftest import auth, make_refund, user

ADMIN = "priya.admin@acmepay.com"
JORDAN = "jordan.lee@acmepay.com"
NOTE = "Only the damaged item is refundable"


def test_admin_can_approve_a_partial_refund_and_only_that_amount_is_paid(client, db, paycore):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 4_000, "note": NOTE})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "approved"
    assert body["amount_cents"] == 10_000
    assert body["approved_amount_cents"] == 4_000
    assert body["payout"]["amount_cents"] == 4_000

    db.expire_all()
    payout = db.query(Payout).filter_by(refund_id=r.id).one()
    payouts.attempt(db, payout.id)
    assert next(iter(paycore.refunds.values()))["amount_cents"] == 4_000

    event = db.query(RefundEvent).filter_by(refund_id=r.id, event_type="approved").one()
    assert event.data["partial"] is True and event.data["approved_amount_cents"] == 4_000
    entry = db.query(AuditLog).filter_by(action="refund.approve", success=True, entity_id=str(r.id)).one()
    assert entry.detail["approved_amount_cents"] == 4_000 and entry.detail["partial"] is True
    assert approved_today_cents(db, a.id) == 4_000


def test_full_approval_records_the_requested_amount(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=10_000)
    body = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={}).json()
    assert body["approved_amount_cents"] == 10_000
    assert body["payout"]["amount_cents"] == 10_000


def test_reviewer_cannot_approve_a_partial_refund(client, db, paycore):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=10_000)
    assert client.get(f"/refunds/{r.id}", headers=auth(j)).json()["permissions"]["can_partial_refund"] is False
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={"amount_cents": 5_000, "note": NOTE})
    assert res.status_code == 403
    assert "Only admins can approve a partial refund" in res.json()["detail"]
    db.expire_all()
    assert db.get(type(r), r.id).status == RefundStatus.pending
    assert db.query(Payout).filter_by(refund_id=r.id).count() == 0
    denied = db.query(AuditLog).filter_by(action="refund.approve", success=False).one()
    assert denied.detail["approved_amount_cents"] == 5_000


def test_partial_refund_requires_a_note(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 4_000, "note": "short"})
    assert res.status_code == 403
    assert "partial refund" in res.json()["detail"]


def test_partial_amount_cannot_exceed_requested_or_be_zero(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 10_001, "note": NOTE})
    assert res.status_code == 403
    assert "cannot exceed the requested amount" in res.json()["detail"]
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 0, "note": NOTE}).status_code == 422


def test_limits_still_apply_to_the_full_requested_amount(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=a.approval_limit_cents + 100, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 100, "note": NOTE})
    assert res.status_code == 403
    assert "per-refund approval limit" in res.json()["detail"]


def test_four_eyes_still_applies_to_partial_refunds(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated, requested_by=ADMIN)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 4_000, "note": NOTE})
    assert res.status_code == 403


def test_amount_only_accepted_on_approve(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/reject", headers=auth(a), json={"amount_cents": 4_000, "note": NOTE})
    assert res.status_code == 422
    db.expire_all()
    assert db.get(type(r), r.id).status == RefundStatus.escalated


def test_admin_sees_partial_permission_and_analytics_count_refunded_amount(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=10_000, status=RefundStatus.escalated)
    assert client.get(f"/refunds/{r.id}", headers=auth(a)).json()["permissions"]["can_partial_refund"] is True
    before = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 4_000, "note": NOTE})
    after = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    assert after - before == 4_000
