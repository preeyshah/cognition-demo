import hashlib
from datetime import timedelta

from sqlalchemy import update

from app import compliance, remediation
from app.audit import audit, verify_chain
from app.models import (
    AccessReview,
    AuditLog,
    Customer,
    DataSubjectRequest,
    RefundEvent,
    RefundRequest,
    RefundStatus,
    SupportRequest,
    User,
)
from app.seed import DORMANT_USER
from app.services import now_utc

from .conftest import auth, make_refund, user

ADMIN, VIEWER, REVIEWER = "priya.admin@acmepay.com", "vik.auditor@acmepay.com", "jordan.lee@acmepay.com"


def _controls(client, db, email=ADMIN):
    res = client.get("/compliance/controls", headers=auth(user(db, email)))
    assert res.status_code == 200
    return {c["id"]: c for c in res.json()["controls"]}


def test_controls_visible_to_admin_and_auditor_only(client, db):
    assert client.get("/compliance/controls", headers=auth(user(db, VIEWER))).status_code == 200
    assert client.get("/compliance/controls", headers=auth(user(db, REVIEWER))).status_code == 403
    assert client.post("/compliance/access-review", json={"decisions": []}, headers=auth(user(db, VIEWER))).status_code == 403


def test_seeded_findings_fail_and_core_controls_pass(client, db):
    c = _controls(client, db)
    for cid in ("AC-05", "AC-06", "DP-02", "IA-02"):
        assert c[cid]["status"] == "fail", (cid, c[cid]["summary"])
    for cid in ("AC-01", "AC-02", "AC-03", "AC-04", "AU-02", "AU-03", "DP-01"):
        assert c[cid]["status"] == "pass", (cid, c[cid]["summary"])
    assert c["AC-06"]["evidence"]["dormant_active_accounts"][0]["email"] == DORMANT_USER[0]
    assert c["CM-01"]["status"] == "documented"


def test_four_eyes_control_detects_self_approval(client, db):
    reviewer = user(db, REVIEWER)
    r = make_refund(db, reviewer, requested_by=reviewer.email)
    r.status, r.decided_by_id = RefundStatus.approved, reviewer.id
    db.commit()
    c = _controls(client, db)["AC-02"]
    assert c["status"] == "fail" and r.reference in c["evidence"]["self_decided"]


def test_access_review_revokes_dormant_account_and_passes_controls(client, db):
    admin = user(db, ADMIN)
    state = client.get("/compliance/access-review", headers=auth(admin)).json()
    dormant = next(u for u in state["users"] if u["dormant"])
    partial = {"decisions": [{"user_id": admin.id, "decision": "certify"}]}
    assert client.post("/compliance/access-review", json=partial, headers=auth(admin)).status_code == 422
    self_revoke = {"decisions": [{"user_id": u["id"], "decision": "revoke" if u["id"] == admin.id else "certify"}
                                 for u in state["users"]]}
    assert client.post("/compliance/access-review", json=self_revoke, headers=auth(admin)).status_code == 422

    body = {"decisions": [{"user_id": u["id"], "decision": "revoke" if u["dormant"] else "certify"} for u in state["users"]]}
    res = client.post("/compliance/access-review", json=body, headers=auth(admin))
    assert res.status_code == 201 and res.json()["revoked_count"] == 1
    db.expire_all()
    assert db.get(User, dormant["id"]).is_active is False
    assert db.query(AccessReview).count() == 1
    assert db.query(AuditLog).filter_by(action="user.revoke", entity_id=str(dormant["id"])).count() == 1
    c = _controls(client, db)
    assert c["AC-05"]["status"] == "pass" and c["AC-06"]["status"] == "pass"


def test_dsr_erasure_pseudonymises_but_keeps_financial_records(client, db):
    admin = user(db, ADMIN)
    dsr = db.query(DataSubjectRequest).one()
    customer = db.get(Customer, dsr.customer_id)
    original_email = customer.email
    refunds = db.query(RefundRequest).filter_by(customer_id=customer.id).count()
    res = client.post(f"/compliance/dsr/{dsr.id}/fulfil", headers=auth(admin))
    assert res.status_code == 200 and res.json()["status"] == "completed"
    db.expire_all()
    customer = db.get(Customer, dsr.customer_id)
    assert customer.email != original_email and customer.email.endswith("@redacted.invalid")
    assert res.json()["result"]["refunds_retained"] == refunds
    assert db.query(RefundRequest).filter_by(customer_id=customer.id).count() == refunds
    assert client.post(f"/compliance/dsr/{dsr.id}/fulfil", headers=auth(admin)).status_code == 409
    assert _controls(client, db)["DP-02"]["status"] == "pass"


def test_dsr_export_returns_subject_data(client, db):
    admin = user(db, ADMIN)
    res = client.post("/compliance/dsr", json={"customer_external_id": "cus_1001", "kind": "export"}, headers=auth(admin))
    assert res.status_code == 201
    out = client.post(f"/compliance/dsr/{res.json()['id']}/fulfil", headers=auth(admin)).json()
    assert out["export"]["customer"]["external_id"] == "cus_1001"
    assert "export" not in client.get("/compliance/dsr", headers=auth(admin)).json()[0]["result"]


def test_audit_hash_chain_detects_tampering(client, db):
    admin = user(db, ADMIN)
    client.get("/compliance/evidence-pack", headers=auth(admin))  # API-written entries carry a trace id
    assert db.query(AuditLog).filter(AuditLog.trace_id.is_not(None)).count() >= 1
    for i in range(3):
        audit(db, None, admin, "test.event", "thing", i, n=i)
    db.commit()
    assert verify_chain(db)["ok"] and verify_chain(db)["verified_entries"] >= 3
    target = db.query(AuditLog).order_by(AuditLog.id).all()[1]
    # Simulates someone with direct DB access bypassing the append-only trigger (SQLite has none).
    db.execute(update(AuditLog).where(AuditLog.id == target.id).values(detail={"n": 999}))
    db.commit()
    chain = verify_chain(db)
    assert not chain["ok"] and chain["first_broken"]["id"] == target.id
    assert _controls(client, db)["AU-02"]["status"] == "fail"


def test_evidence_pack_hash_and_audit(client, db):
    res = client.get("/compliance/evidence-pack", headers=auth(user(db, VIEWER)))
    assert res.status_code == 200
    assert hashlib.sha256(res.content).hexdigest() == res.headers["X-Evidence-SHA256"]
    assert {"results", "audit_chain", "access_reviews", "data_subject_requests"} <= set(res.json())
    assert db.query(AuditLog).filter_by(action="compliance.evidence_export").count() == 1


def test_fix_with_devin_opens_compliance_ticket(client, db):
    admin = user(db, ADMIN)
    res = client.post("/compliance/controls/IA-02/remediate", headers=auth(admin))
    assert res.status_code == 200 and res.json()["devin_enabled"] is False
    again = client.post("/compliance/controls/IA-02/remediate", headers=auth(admin))
    assert again.json()["id"] == res.json()["id"]  # deduped while open
    sr = db.get(SupportRequest, res.json()["id"])
    assert sr.kind == "compliance" and "PCI DSS 4.0 8.2.8" in sr.description
    assert client.post("/compliance/controls/AC-02/remediate", headers=auth(admin)).status_code == 409
    assert client.post("/compliance/controls/IA-02/remediate", headers=auth(user(db, VIEWER))).status_code == 403


def test_access_review_expires_after_interval_not_whole_days(client, db):
    admin = user(db, ADMIN)
    db.add(AccessReview(reviewer_id=admin.id, reviewer_email=admin.email, decisions=[], revoked_count=0,
                        completed_at=now_utc() - timedelta(days=90, hours=1)))
    db.commit()
    assert _controls(client, db)["AC-05"]["status"] == "fail"


def test_high_risk_signoff_uses_role_at_decision_time(client, db):
    admin, reviewer = user(db, ADMIN), user(db, REVIEWER)
    for approver, role_then in ((admin, "reviewer"), (reviewer, "admin")):
        r = make_refund(db, None, risk=95, status=RefundStatus.approved)
        r.decided_by_id = approver.id
        db.add(RefundEvent(refund_id=r.id, actor_id=approver.id, event_type="approved", data={"actor_role": role_then}))
    db.commit()
    bad = _controls(client, db)["AC-04"]["evidence"]["approved_without_admin"]
    refs = {r.decided_by_id: r.reference for r in db.query(RefundRequest).filter(RefundRequest.risk_score == 95)}
    assert refs[admin.id] in bad and refs[reviewer.id] not in bad


def test_security_logging_requires_each_category(client, db):
    db.query(AuditLog).delete()
    audit(db, None, user(db, ADMIN), "compliance.evidence_export", "evidence_pack", "x")
    db.commit()
    res = compliance.evaluate(db, compliance.BY_ID["AU-01"])
    assert res["status"] == "warn" and set(res["evidence"]["missing_categories"]) == {"sign-in", "refund decision", "PII access"}
    admin = user(db, ADMIN)
    audit(db, None, admin, "auth.login", "user", admin.id)
    audit(db, None, admin, "refund.create", "refund", 1)
    audit(db, None, admin, "customer.history", "customer", "cus_1001", success=False)
    db.commit()
    res = compliance.evaluate(db, compliance.BY_ID["AU-01"])
    assert res["status"] == "warn" and set(res["evidence"]["missing_categories"]) == {"refund decision", "PII access"}
    audit(db, None, admin, "refund.approve", "refund", 1)
    audit(db, None, admin, "customer.history", "customer", "cus_1001")
    db.commit()
    assert compliance.evaluate(db, compliance.BY_ID["AU-01"])["status"] == "pass"


def test_monitoring_flags_failing_controls_without_ticket(client, db, monkeypatch):
    monkeypatch.setattr(remediation, "enabled", lambda: True)
    monkeypatch.setattr(remediation, "notify", lambda: None)
    mn = _controls(client, db)["MN-01"]
    assert mn["status"] == "warn" and "IA-02" in mn["evidence"]["failing_without_ticket"]
    admin = user(db, ADMIN)
    for cid in mn["evidence"]["failing_controls"]:
        assert client.post(f"/compliance/controls/{cid}/remediate", headers=auth(admin)).status_code == 200
    assert _controls(client, db)["MN-01"]["status"] == "pass"


def test_evidence_pack_omits_free_text_dsr_notes(client, db):
    db.query(DataSubjectRequest).first().note = "Jane Doe called from jane@example.com"
    db.commit()
    res = client.get("/compliance/evidence-pack", headers=auth(user(db, VIEWER)))
    assert res.json()["data_subject_requests"] and "jane@example.com" not in res.text


def test_erasure_redacts_identity_in_refund_descriptions(client, db):
    dsr = db.query(DataSubjectRequest).one()
    customer = db.get(Customer, dsr.customer_id)
    r = db.query(RefundRequest).filter_by(customer_id=customer.id).first()
    first = customer.name.split()[0]
    r.description = f"Requested by {customer.name} <{customer.email.upper()}> {customer.name}_2024; {first}{first.lower()}x unchanged"
    db.commit()
    rid = r.id
    out = client.post(f"/compliance/dsr/{dsr.id}/fulfil", headers=auth(user(db, ADMIN))).json()
    db.expire_all()
    assert db.get(RefundRequest, rid).description == f"Requested by [redacted] <[redacted]> [redacted]_2024; {first}{first.lower()}x unchanged"
    assert out["result"]["refund_descriptions_redacted"] >= 1


def test_completed_export_can_be_redelivered(client, db):
    admin = user(db, ADMIN)
    dsr_id = client.post("/compliance/dsr", json={"customer_external_id": "cus_1001", "kind": "export"},
                         headers=auth(admin)).json()["id"]
    client.post(f"/compliance/dsr/{dsr_id}/fulfil", headers=auth(admin))
    again = client.post(f"/compliance/dsr/{dsr_id}/fulfil", headers=auth(admin))
    assert again.status_code == 200 and again.json()["export"]["customer"]["external_id"] == "cus_1001"
    assert db.query(AuditLog).filter_by(action="compliance.dsr_export_redeliver").count() == 1
