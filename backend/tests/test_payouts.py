import hashlib
import hmac
import json
import time
from datetime import timedelta

from app import payouts
from app.models import AuditLog, Payout, PayoutStatus, ProviderWebhookEvent, RefundEvent
from app.services import now_utc

from .conftest import auth, make_refund, user

JORDAN = "jordan.lee@acmepay.com"
ADMIN = "priya.admin@acmepay.com"
SECRET = "whsec_test"


def signed(event: dict, secret: str = SECRET, ts: int | None = None) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    t = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"PayCore-Signature": f"t={t},v1={mac}", "Content-Type": "application/json"}


def webhook(client, event, **kw):
    body, headers = signed(event, **kw)
    return client.post("/webhooks/paycore", content=body, headers=headers)


def approve(client, db, amount=10_000):
    j = user(db, JORDAN)
    r = make_refund(db, j, amount=amount)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 200, res.text
    db.expire_all()
    return r, db.query(Payout).filter_by(refund_id=r.id).one()


def make_due(db, payout):
    payout.next_attempt_at = now_utc() - timedelta(seconds=1)
    db.commit()


def test_approval_queues_payout_and_signed_webhook_settles_it(client, db, paycore):
    r, p = approve(client, db)
    assert p.status == PayoutStatus.processing and p.provider_refund_id == "re_test1"
    assert p.idempotency_key == f"refund-{r.reference}" and p.attempts == 1

    event = {"id": "evt_1", "type": "refund.succeeded", "data": {**paycore.refunds["re_test1"], "status": "succeeded"}}
    assert webhook(client, event).json()["outcome"] == "applied"
    assert webhook(client, event).json()["duplicate"] is True  # redelivery is a no-op
    db.expire_all()
    p = db.get(Payout, p.id)
    assert p.status == PayoutStatus.succeeded and p.succeeded_at is not None
    assert db.query(ProviderWebhookEvent).count() == 1
    types = [e.event_type for e in db.query(RefundEvent).filter_by(refund_id=r.id).order_by(RefundEvent.id)]
    assert types == ["approved", "payout_queued", "payout_submitted", "payout_succeeded"]
    detail = client.get(f"/refunds/{r.id}", headers=auth(user(db, JORDAN))).json()
    assert detail["payout"]["status"] == "succeeded"


def test_transient_errors_retry_with_backoff_and_same_idempotency_key(client, db, paycore):
    paycore.script = [503, None]
    _, p = approve(client, db)
    assert p.status == PayoutStatus.retry_scheduled and p.last_http_status == 503
    assert p.next_attempt_at is not None
    assert payouts.run_due(db) == 0  # not due yet
    make_due(db, p)
    payouts.run_due(db)
    db.refresh(p)
    assert p.status == PayoutStatus.retry_scheduled and p.attempts == 2 and p.last_http_status is None
    make_due(db, p)
    payouts.run_due(db)
    db.refresh(p)
    assert p.status == PayoutStatus.processing and p.attempts == 3
    assert len(set(paycore.calls)) == 1 and len(paycore.calls) == 3


def test_lost_response_retry_replays_instead_of_refunding_twice(client, db, paycore):
    paycore.script = ["lost"]
    _, p = approve(client, db)
    assert p.status == PayoutStatus.retry_scheduled
    make_due(db, p)
    payouts.run_due(db)
    db.refresh(p)
    assert p.status == PayoutStatus.processing and p.provider_refund_id == "re_test1"
    assert len(paycore.refunds) == 1  # money moved exactly once
    submitted = db.query(RefundEvent).filter_by(refund_id=p.refund_id, event_type="payout_submitted").one()
    assert submitted.data["replayed"] is True


def test_decline_fails_and_admin_retry_uses_a_new_key(client, db, paycore):
    paycore.script = [402]
    r, p = approve(client, db)
    assert p.status == PayoutStatus.failed and p.failure_code == "insufficient_merchant_balance"
    assert client.post(f"/admin/payouts/{p.id}/retry", headers=auth(user(db, JORDAN))).status_code == 403
    res = client.post(f"/admin/payouts/{p.id}/retry", headers=auth(user(db, ADMIN)))
    assert res.status_code == 200 and res.json()["idempotency_key"] == f"refund-{r.reference}-r1"
    payouts.run_due(db)
    db.expire_all()
    assert db.get(Payout, p.id).status == PayoutStatus.processing
    assert db.query(AuditLog).filter_by(action="payout.retry").count() == 1
    # Retrying something that isn't failed is refused (and audited).
    assert client.post(f"/admin/payouts/{p.id}/retry", headers=auth(user(db, ADMIN))).status_code == 409


def test_retries_exhausted_keeps_key_on_manual_retry(client, db, paycore, monkeypatch):
    monkeypatch.setattr(payouts.get_settings(), "payout_max_attempts", 2)
    paycore.script = [503, 503]
    _, p = approve(client, db)
    make_due(db, p)
    payouts.run_due(db)
    db.refresh(p)
    assert p.status == PayoutStatus.failed and p.failure_code == "retries_exhausted"
    key = p.idempotency_key
    client.post(f"/admin/payouts/{p.id}/retry", headers=auth(user(db, ADMIN)))
    db.expire_all()
    assert db.get(Payout, p.id).idempotency_key == key  # provider may have processed it: never risk a duplicate


def test_forged_or_replayed_webhooks_are_rejected(client, db, paycore):
    _, p = approve(client, db)
    event = {"id": "evt_x", "type": "refund.succeeded", "data": {"id": p.provider_refund_id, "status": "succeeded"}}
    assert webhook(client, event, secret="wrong").status_code == 400
    assert webhook(client, event, ts=int(time.time()) - 3600).status_code == 400
    assert client.post("/webhooks/paycore", content=json.dumps(event)).status_code == 400
    db.expire_all()
    assert db.get(Payout, p.id).status == PayoutStatus.processing
    assert db.query(AuditLog).filter_by(action="webhook.paycore", success=False).count() == 3


def test_out_of_order_webhook_cannot_regress_terminal_state(client, db, paycore):
    _, p = approve(client, db)
    data = {"id": p.provider_refund_id}
    webhook(client, {"id": "evt_a", "type": "refund.succeeded", "data": {**data, "status": "succeeded"}})
    res = webhook(client, {"id": "evt_b", "type": "refund.failed", "data": {**data, "status": "failed"}})
    assert res.json()["outcome"] == "ignored_stale"
    db.expire_all()
    assert db.get(Payout, p.id).status == PayoutStatus.succeeded


def test_reconciliation_catches_lost_webhook(client, db, paycore):
    _, p = approve(client, db)
    paycore.refunds[p.provider_refund_id]["status"] = "failed"
    paycore.refunds[p.provider_refund_id]["failure_code"] = "account_closed"
    assert client.post("/admin/payouts/reconcile", headers=auth(user(db, JORDAN))).status_code == 403
    res = client.post("/admin/payouts/reconcile", headers=auth(user(db, ADMIN))).json()
    assert res["checked"] == 1 and res["changed"][0]["to"] == "failed"
    db.expire_all()
    assert db.get(Payout, p.id).failure_code == "account_closed"


def test_rejection_and_escalation_never_move_money(client, db, paycore):
    j = user(db, JORDAN)
    r = make_refund(db, j)
    assert client.post(f"/refunds/{r.id}/reject", headers=auth(j), json={"note": "Duplicate claim, see ledger"}).status_code == 200
    assert db.query(Payout).filter_by(refund_id=r.id).count() == 0 and paycore.calls == []


def test_signature_helper_matches_mock_provider():
    from mock_provider.main import sign

    body = b'{"id":"evt"}'
    payouts.verify_signature(body, sign(body, SECRET), SECRET, 300)
