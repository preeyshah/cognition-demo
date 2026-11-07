from app import remediation
from app.main import app
from app.models import AuditLog, RequestTrace, SupportRequest

from .conftest import auth, make_refund, user

JORDAN = "jordan.lee@acmepay.com"


def test_every_request_gets_a_trace_id_linked_to_audit_and_events(client, db):
    j = user(db, JORDAN)
    r = make_refund(db, j)
    res = client.post(f"/refunds/{r.id}/approve", headers=auth(j), json={})
    assert res.status_code == 200
    trace_id = res.headers["X-Trace-Id"]
    assert trace_id.startswith("tr_")
    db.expire_all()
    assert db.get(RequestTrace, trace_id).actor_email == JORDAN
    assert db.query(AuditLog).filter_by(trace_id=trace_id, action="refund.approve").count() == 1

    admin = user(db, "priya.admin@acmepay.com")
    bundle = client.get(f"/admin/traces/{trace_id}", headers=auth(admin)).json()
    assert bundle["request"]["path"] == f"/refunds/{r.id}/approve"
    assert [e["event_type"] for e in bundle["refund_events"]] == ["approved"]
    # Traces are admin-only.
    assert client.get(f"/admin/traces/{trace_id}", headers=auth(j)).status_code == 403


def test_incoming_trace_id_is_honoured_only_if_well_formed(client, db):
    assert client.get("/health", headers={"X-Trace-Id": "client-abc-123"}).headers["X-Trace-Id"] == "client-abc-123"
    assert client.get("/health", headers={"X-Trace-Id": "bad id\n"}).headers["X-Trace-Id"].startswith("tr_")


def test_unhandled_error_returns_trace_and_opens_ticket_for_devin(client, db, monkeypatch):
    started = []
    monkeypatch.setattr(remediation, "enabled", lambda: True)
    monkeypatch.setattr(remediation, "remediate_async", lambda sr_id: started.append(sr_id))

    @app.get("/__boom")
    def boom():
        raise RuntimeError("kaboom")

    try:
        res = client.get("/__boom")
        res2 = client.get("/__boom")
    finally:
        app.router.routes = [rt for rt in app.router.routes if getattr(rt, "path", None) != "/__boom"]
    assert res.status_code == 500 and res.json()["trace_id"] == res.headers["X-Trace-Id"]
    db.expire_all()
    tickets = db.query(SupportRequest).filter_by(kind="error").all()
    assert len(tickets) == 1 and tickets[0].trace_id == res.headers["X-Trace-Id"]  # repeat is deduped
    assert started == [tickets[0].id]
    assert res2.status_code == 500


def test_support_request_flow(client, db, monkeypatch):
    j = user(db, JORDAN)
    admin = user(db, "priya.admin@acmepay.com")
    res = client.post("/support/requests", headers=auth(j), json={"title": "Approve button greyed out", "trace_id": "tr_x"})
    assert res.status_code == 201
    sr_id = res.json()["id"]
    assert client.get("/admin/support/requests", headers=auth(j)).status_code == 403
    assert client.post(f"/admin/support/requests/{sr_id}/remediate", headers=auth(admin)).status_code == 503

    monkeypatch.setattr(remediation, "enabled", lambda: True)
    sent = {}
    monkeypatch.setattr(remediation, "start_devin_session", lambda prompt, title: sent.update(prompt=prompt) or
                        {"session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc"})
    remediation.remediate(sr_id)
    db.expire_all()
    sr = db.get(SupportRequest, sr_id)
    assert sr.status == "devin_working" and sr.devin_session_url.endswith("/abc")
    assert "open a PR" in sent["prompt"] and "Approve button greyed out" in sent["prompt"]
