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
    # The first payout attempt runs after the response but stays on the approval's trace.
    assert [e["event_type"] for e in bundle["refund_events"]] == ["approved", "payout_queued", "payout_submitted"]
    # Traces are admin-only.
    assert client.get(f"/admin/traces/{trace_id}", headers=auth(j)).status_code == 403


def test_trace_ids_are_server_generated_and_unique(client, db):
    a = client.get("/auth/me", headers={"X-Trace-Id": "client-abc-123"}).headers["X-Trace-Id"]
    b = client.get("/auth/me", headers={"X-Trace-Id": "client-abc-123"}).headers["X-Trace-Id"]
    assert a != b and a.startswith("tr_") and b.startswith("tr_")
    db.expire_all()
    assert "client_ref=client-abc-123" in db.get(RequestTrace, a).query


def test_unhandled_error_returns_trace_and_opens_ticket_for_devin(client, db, monkeypatch):
    started = []
    monkeypatch.setattr(remediation, "enabled", lambda: True)
    monkeypatch.setattr(remediation, "start_devin_session", lambda prompt, title: started.append(title) or
                        {"session_id": "devin-err", "url": "https://app.devin.ai/sessions/err"})

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
    assert tickets[0].status == "queued" and started == []  # durable: nothing sent from the request itself
    assert res2.status_code == 500

    assert remediation.process_queue() == 1
    assert remediation.process_queue() == 0
    db.expire_all()
    assert started == [f"[refunds-console] {tickets[0].title}"]
    assert db.get(SupportRequest, tickets[0].id).status == "devin_working"


def test_error_ticket_failure_still_returns_traced_500(client, monkeypatch):
    from app import main

    def broken(entry):
        raise RuntimeError("db down")

    for m in app.user_middleware:
        if m.cls.__name__ == "TracingMiddleware":
            monkeypatch.setitem(m.kwargs, "on_error", broken)
    app.middleware_stack = None  # rebuild with the patched hook

    @app.get("/__boom2")
    def boom2():
        raise RuntimeError("kaboom")

    try:
        res = client.get("/__boom2")
    finally:
        app.router.routes = [rt for rt in app.router.routes if getattr(rt, "path", None) != "/__boom2"]
        for m in app.user_middleware:
            if m.cls.__name__ == "TracingMiddleware":
                m.kwargs["on_error"] = main.record_error
        app.middleware_stack = None
    assert res.status_code == 500 and res.json()["trace_id"] == res.headers["X-Trace-Id"]


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


def test_admin_remediate_queues_ticket_for_worker(client, db, monkeypatch):
    admin = user(db, "priya.admin@acmepay.com")
    sr_id = client.post("/support/requests", headers=auth(admin), json={"title": "Export is empty"}).json()["id"]
    monkeypatch.setattr(remediation, "enabled", lambda: True)
    res = client.post(f"/admin/support/requests/{sr_id}/remediate", headers=auth(admin))
    assert res.status_code == 200 and res.json()["status"] == "queued"

    monkeypatch.setattr(remediation, "start_devin_session", lambda prompt, title: (_ for _ in ()).throw(RuntimeError("503")))
    assert remediation.process_queue() == 1
    db.expire_all()
    sr = db.get(SupportRequest, sr_id)
    assert sr.status == "remediation_failed" and "503" in sr.remediation_error


def test_guardrail_alert_is_queued_for_the_worker(client, db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "guardrail_alert_threshold", 1)
    viewer = user(db, "vik.auditor@acmepay.com")
    r = make_refund(db, user(db, JORDAN))
    assert client.post(f"/refunds/{r.id}/approve", headers=auth(viewer), json={}).status_code == 403
    db.expire_all()
    sr = db.query(SupportRequest).filter_by(kind="guardrail").one()
    assert sr.status == "queued"

    monkeypatch.setattr(remediation, "enabled", lambda: True)
    monkeypatch.setattr(remediation, "start_devin_session", lambda prompt, title: {"session_id": "devin-g"})
    assert remediation.process_queue() == 1
    db.expire_all()
    assert db.get(SupportRequest, sr.id).status == "devin_working"
