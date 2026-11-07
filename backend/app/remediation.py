"""Self-healing loop: hand a support request or captured server error to Devin,
which investigates using the trace bundle and opens a PR on the repo."""

import logging
import threading
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .models import AuditLog, RefundEvent, RequestTrace, SupportRequest
from .services import now_utc

log = logging.getLogger("refunds.remediation")


def enabled() -> bool:
    s = get_settings()
    return bool(s.devin_api_key and s.devin_org_id)


def trace_bundle(db: Session, trace_id: str) -> dict:
    req = db.get(RequestTrace, trace_id)
    audits = db.scalars(select(AuditLog).where(AuditLog.trace_id == trace_id).order_by(AuditLog.id)).all()
    events = db.scalars(select(RefundEvent).where(RefundEvent.trace_id == trace_id).order_by(RefundEvent.id)).unique().all()
    return {
        "trace_id": trace_id,
        "request": None if req is None else {
            "method": req.method, "path": req.path, "query": req.query, "status_code": req.status_code,
            "duration_ms": req.duration_ms, "actor_email": req.actor_email, "error": req.error,
            "created_at": req.created_at.isoformat(),
        },
        "audit": [
            {"id": a.id, "action": a.action, "success": a.success, "actor_email": a.actor_email,
             "entity": f"{a.entity_type}:{a.entity_id}", "detail": a.detail, "created_at": a.created_at.isoformat()}
            for a in audits
        ],
        "refund_events": [
            {"id": e.id, "refund_id": e.refund_id, "event_type": e.event_type, "from": e.from_status,
             "to": e.to_status, "created_at": e.created_at.isoformat()}
            for e in events
        ],
    }


def build_prompt(sr: SupportRequest, bundle: dict | None) -> str:
    s = get_settings()
    lines = [
        f"Repo: {s.devin_repo}",
        f"A {'production error' if sr.kind == 'error' else 'support request'} was raised in the Refunds Console "
        f"(support request #{sr.id}).",
        "",
        f"Title: {sr.title}",
        f"Description: {sr.description or '-'}",
        f"Trace id: {sr.trace_id or '-'}",
    ]
    if bundle and bundle.get("request"):
        import json

        lines += ["", "Trace bundle (request, audit log and refund events for this trace):",
                  json.dumps(bundle, default=str, indent=2)[:12000]]
    lines += [
        "",
        "Investigate the root cause, reproduce it with a failing test in backend/tests where possible, fix it, "
        "make sure CI (ruff, pytest, frontend lint/build) passes, and open a PR against main that references "
        f"support request #{sr.id} and trace {sr.trace_id or '-'}. Do not weaken any authorization checks or limits. "
        "If it is not a code defect (e.g. a user question or expected policy denial), do not open a PR; "
        "explain why in the session instead.",
    ]
    return "\n".join(lines)


def start_devin_session(prompt: str, title: str) -> dict:
    s = get_settings()
    res = httpx.post(
        f"{s.devin_api_url}/v3/organizations/{s.devin_org_id}/sessions",
        headers={"Authorization": f"Bearer {s.devin_api_key}"},
        json={"prompt": prompt, "title": title, "tags": ["refunds-console", "auto-remediation"]},
        timeout=20,
    )
    res.raise_for_status()
    return res.json()


def remediate(support_request_id: int) -> None:
    """Create a Devin session for a support request and store the link on it."""
    with SessionLocal() as db:
        sr = db.get(SupportRequest, support_request_id)
        if sr is None:
            return
        if not enabled():
            sr.status = "open"
            sr.remediation_error = "Devin API not configured (set DEVIN_API_KEY and DEVIN_ORG_ID)"
            db.commit()
            return
        bundle = trace_bundle(db, sr.trace_id) if sr.trace_id else None
        try:
            data = start_devin_session(build_prompt(sr, bundle), f"[refunds-console] {sr.title}"[:120])
            sr.devin_session_id = data.get("session_id") or data.get("devin_id")
            sr.devin_session_url = data.get("url") or (
                f"https://app.devin.ai/sessions/{sr.devin_session_id.removeprefix('devin-')}" if sr.devin_session_id else None
            )
            sr.status = "devin_working"
            sr.remediation_error = None
        except Exception as exc:  # noqa: BLE001 - recorded on the request for retry
            log.warning("devin remediation failed", extra={"fields": {"support_request_id": sr.id, "error": str(exc)}})
            sr.status = "remediation_failed"
            sr.remediation_error = str(exc)[:1000]
        db.commit()


def remediate_async(support_request_id: int) -> None:
    threading.Thread(target=remediate, args=(support_request_id,), daemon=True).start()


def record_error(entry: dict) -> None:
    """Called by the tracing middleware for unhandled exceptions: open (or dedupe) an error ticket."""
    err = entry.get("error") or {}
    fingerprint = err.get("fingerprint")
    s = get_settings()
    with SessionLocal() as db:
        recent = db.scalar(
            select(SupportRequest).where(
                SupportRequest.kind == "error",
                SupportRequest.fingerprint == fingerprint,
                SupportRequest.created_at >= now_utc() - timedelta(minutes=s.remediation_cooldown_minutes),
            )
        )
        if recent is not None:
            return  # same failure already being handled; its trace is enough
        sr = SupportRequest(
            kind="error",
            title=f"{err.get('type', 'Error')} on {entry['method']} {entry['path']}"[:200],
            description=f"{err.get('message', '')}\n\nFingerprint: {fingerprint}",
            trace_id=entry["trace_id"],
            fingerprint=fingerprint,
            reporter_email="monitoring@system",
        )
        db.add(sr)
        db.commit()
        sr_id = sr.id
    if s.auto_remediate_errors and enabled():
        remediate_async(sr_id)
