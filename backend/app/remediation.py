"""Self-healing loop: hand a support request or captured server error to Devin,
which investigates using the trace bundle and opens a PR on the repo.

Tickets are queued in the database (status "queued") and a worker in each API process drains them,
so a ticket survives restarts and deploys and is sent once even with several API tasks running.
"""

import logging
import threading
from collections.abc import Callable
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
    # Org-scoped service-user keys (cog_...) need the org id (v3 API); legacy personal keys (apk_...) use v1.
    return bool(s.devin_api_key and (s.devin_org_id or s.devin_api_key.startswith("apk_")))


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


_KIND_LABEL = {
    "error": "production error",
    "feature": "feature request",
    "compliance": "failing compliance control",
    "guardrail": "guardrail alert",
}


def build_prompt(sr: SupportRequest, bundle: dict | None) -> str:
    s = get_settings()
    lines = [
        f"Repo: {s.devin_repo} (base branch: {s.devin_base_branch})",
        f"A {_KIND_LABEL.get(sr.kind, 'support request')} was raised in the Refunds Console "
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
    if sr.kind == "compliance":
        lines += ["", "This is an audit finding: fix the system so the control passes. Never weaken, skip or special-case "
                  "the check in backend/app/compliance.py; add a regression test proving the control now holds. "
                  "If the gap is a process/data issue an admin can resolve in the Compliance Center (e.g. a "
                  "missing access review), say so in the session and do not open a PR."]
    lines += [
        "",
        "Investigate the root cause, reproduce it with a failing test in backend/tests where possible, fix it, "
        "make sure CI (ruff, pytest, frontend lint/build) passes, and open a PR against "
        f"{s.devin_base_branch} that references "
        f"request #{sr.id} and trace {sr.trace_id or '-'}. Do not weaken any authorization checks or limits. "
        + (
            "This is a feature request: implement the requested change end-to-end (backend, frontend, tests, "
            "README if behaviour changes) following existing conventions. "
            if sr.kind == "feature"
            else ""
        )
        + "If it is not a code defect or a reasonable feature (e.g. a user question or expected policy denial), "
        "do not open a PR; explain why in the session instead.",
    ]
    return "\n".join(lines)


def start_devin_session(prompt: str, title: str) -> dict:
    s = get_settings()
    body = {"prompt": prompt, "title": title, "tags": ["refunds-console", "auto-remediation"],
            "max_acu_limit": s.devin_max_acu}
    if s.devin_api_key.startswith("apk_") or not s.devin_org_id:
        url = f"{s.devin_api_url}/v1/sessions"
    else:
        url = f"{s.devin_api_url}/v3/organizations/{s.devin_org_id}/sessions"
    res = httpx.post(url, headers={"Authorization": f"Bearer {s.devin_api_key}"}, json=body, timeout=20)
    res.raise_for_status()
    return res.json()


QUEUED = "queued"
_wake = threading.Event()


def _remediate(db: Session, sr: SupportRequest) -> None:
    """Create a Devin session for a support request and store the link on it."""
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


def remediate(support_request_id: int) -> None:
    with SessionLocal() as db:
        sr = db.get(SupportRequest, support_request_id)
        if sr is not None:
            _remediate(db, sr)


def process_queue(limit: int = 20) -> int:
    """Send queued tickets to Devin. Each row stays locked (SKIP LOCKED) until its status is committed, so
    concurrent workers never pick the same ticket and a crash mid-send leaves it queued for retry."""
    done = 0
    while done < limit:
        with SessionLocal() as db:
            sr = db.scalar(
                select(SupportRequest)
                .where(SupportRequest.status == QUEUED)
                .order_by(SupportRequest.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if sr is None:
                break
            _remediate(db, sr)
        done += 1
    return done


def notify() -> None:
    """Wake this process's worker now instead of at the next poll."""
    _wake.set()


def _run_worker(stop: threading.Event) -> None:
    interval = get_settings().remediation_poll_seconds
    while not stop.is_set():
        _wake.clear()
        try:
            process_queue()
        except Exception:  # noqa: BLE001 - keep draining on the next tick
            log.exception("remediation worker failed")
        _wake.wait(interval)


def start_worker() -> Callable[[], None]:
    """Start the queue worker; returns a function that stops it."""
    stop = threading.Event()
    thread = threading.Thread(target=_run_worker, args=(stop,), name="remediation-worker", daemon=True)
    thread.start()

    def shutdown() -> None:
        stop.set()
        _wake.set()
        thread.join(timeout=30)

    return shutdown


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
            status=QUEUED if s.auto_remediate_errors and enabled() else "open",
        )
        db.add(sr)
        db.commit()
        queued = sr.status == QUEUED
    if queued:
        notify()
