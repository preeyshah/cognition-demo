"""Partial refunds (support request #2): only admins may approve less than the requested amount."""

from app.models import AuditLog, Payout, RefundEvent, RefundStatus

from .conftest import auth, make_refund, user

JORDAN = "jordan.lee@acmepay.com"   # reviewer, $500 limit, $5,000/day
ADMIN = "priya.admin@acmepay.com"
NOTE = "Customer kept part of the order"


def test_admin_can_approve_partial_refund_and_payout_uses_approved_amount(client, db, paycore):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=60_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 25_000, "note": NOTE})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "approved"
    assert body["amount_cents"] == 60_000
    assert body["approved_amount_cents"] == 25_000
    assert body["payout"]["amount_cents"] == 25_000
    db.expire_all()
    assert db.query(Payout).filter_by(refund_id=r.id).one().amount_cents == 25_000
    event = db.query(RefundEvent).filter_by(refund_id=r.id, event_type="approved").one()
    assert event.data["approved_amount_cents"] == 25_000 and event.data["partial"] is True
    entry = db.query(AuditLog).filter_by(action="refund.approve", success=True, entity_id=str(r.id)).one()
    assert entry.detail["approved_amount_cents"] == 25_000 and entry.detail["partial"] is True


def test_full_approval_records_requested_amount(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 200
    assert res.json()["approved_amount_cents"] == 20_000
    assert res.json()["payout"]["amount_cents"] == 20_000


def test_reviewer_cannot_approve_partial_refund_and_denial_is_audited(client, db, paycore):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=20_000)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={"amount_cents": 10_000, "note": NOTE})
    assert res.status_code == 403
    assert "Only admins can approve partial refunds" in res.json()["detail"]
    db.expire_all()
    assert db.get(type(r), r.id).status == RefundStatus.pending
    assert db.query(Payout).filter_by(refund_id=r.id).count() == 0
    entry = db.query(AuditLog).filter_by(action="refund.approve", success=False).one()
    assert entry.actor_email == JORDAN
    assert "Only admins can approve partial refunds" in entry.detail["reason"]


def test_partial_amount_must_be_below_requested_and_positive(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=60_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 60_001, "note": NOTE})
    assert res.status_code == 403
    assert "less than the requested amount" in res.json()["detail"]
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 0, "note": NOTE}).status_code == 422
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": -5, "note": NOTE}).status_code == 422


def test_partial_approval_requires_note(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=60_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 30_000, "note": "short"})
    assert res.status_code == 403
    assert "partial" in res.json()["detail"].lower()


def test_partial_approval_keeps_limits_on_requested_amount(client, db):
    a = user(db, ADMIN)
    r = make_refund(db, None, amount=2_000_000, status=RefundStatus.escalated)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 100_000, "note": NOTE})
    assert res.status_code == 403
    assert "hard cap" in res.json()["detail"]


def test_partial_permission_only_exposed_to_admins(client, db):
    a, j = user(db, ADMIN), user(db, JORDAN)
    mine = make_refund(db, j, amount=20_000)
    escalated = make_refund(db, None, amount=60_000, status=RefundStatus.escalated)
    assert client.get(f"/refunds/{mine.id}", headers=auth(j)).json()["permissions"]["can_approve_partial"] is False
    assert client.get(f"/refunds/{escalated.id}", headers=auth(a)).json()["permissions"]["can_approve_partial"] is True


def test_analytics_count_approved_amount_for_partial_refunds(client, db):
    a = user(db, ADMIN)
    before = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    r = make_refund(db, None, amount=60_000, status=RefundStatus.escalated)
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(a), json={"amount_cents": 25_000, "note": NOTE}).status_code == 200
    after = client.get("/analytics/overview", headers=auth(a)).json()["totals"]["approved_amount_cents"]
    assert after - before == 25_000
