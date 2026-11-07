"""Trace lookup and support requests (the entry point of the self-healing loop)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import remediation
from ..audit import audit
from ..config import get_settings
from ..db import get_db
from ..models import RefundRequest, RequestTrace, Role, SupportRequest, User
from ..security import get_current_user, require_roles

router = APIRouter(tags=["monitoring"])
admin_only = require_roles(Role.admin)


class SupportIn(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(default="", max_length=5000)
    trace_id: str | None = Field(default=None, max_length=64)


def _support_out(sr: SupportRequest) -> dict:
    return {
        "id": sr.id, "kind": sr.kind, "title": sr.title, "description": sr.description, "trace_id": sr.trace_id,
        "fingerprint": sr.fingerprint, "reporter_email": sr.reporter_email, "status": sr.status,
        "devin_session_id": sr.devin_session_id, "devin_session_url": sr.devin_session_url,
        "remediation_error": sr.remediation_error, "created_at": sr.created_at,
    }


@router.get("/admin/traces")
def list_traces(
    errors_only: bool = False,
    path: str | None = None,
    actor_email: str | None = None,
    min_status: int | None = None,
    limit: int = Query(default=100, le=500),
    _: User = Depends(admin_only),
    db: Session = Depends(get_db),
):
    q = select(RequestTrace).order_by(RequestTrace.created_at.desc()).limit(limit)
    if errors_only:
        q = q.where(RequestTrace.status_code >= 500)
    if min_status:
        q = q.where(RequestTrace.status_code >= min_status)
    if path:
        q = q.where(RequestTrace.path.like(f"{path}%"))
    if actor_email:
        q = q.where(RequestTrace.actor_email == actor_email)
    return [
        {"trace_id": t.trace_id, "method": t.method, "path": t.path, "status_code": t.status_code,
         "duration_ms": t.duration_ms, "actor_email": t.actor_email, "has_error": t.error is not None,
         "created_at": t.created_at}
        for t in db.scalars(q)
    ]


@router.get("/admin/traces/{trace_id}")
def get_trace(trace_id: str, _: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Everything that happened under one trace id: request, audit entries, refund events, created rows."""
    bundle = remediation.trace_bundle(db, trace_id)
    created = db.scalars(select(RefundRequest).where(RefundRequest.created_trace_id == trace_id)).unique().all()
    support = db.scalars(select(SupportRequest).where(SupportRequest.trace_id == trace_id)).all()
    if bundle["request"] is None and not bundle["audit"] and not bundle["refund_events"] and not created:
        raise HTTPException(status_code=404, detail="Unknown trace id")
    bundle["created_refunds"] = [{"id": r.id, "reference": r.reference} for r in created]
    bundle["support_requests"] = [_support_out(s) for s in support]
    return bundle


@router.post("/support/requests", status_code=201)
def create_support_request(
    body: SupportIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    sr = SupportRequest(kind="support", title=body.title, description=body.description,
                        trace_id=body.trace_id, reporter_email=user.email)
    db.add(sr)
    db.flush()
    audit(db, request, user, "support.create", "support_request", sr.id, trace=body.trace_id)
    db.commit()
    return _support_out(sr)


@router.get("/admin/support/config")
def support_config(_: User = Depends(admin_only)):
    """Whether tickets are handed to Devin, and where its PRs go (no secrets)."""
    s = get_settings()
    return {"devin_enabled": remediation.enabled(), "auto_remediate_errors": s.auto_remediate_errors,
            "repo": s.devin_repo, "base_branch": s.devin_base_branch, "max_acu": s.devin_max_acu}


@router.get("/admin/support/requests")
def list_support_requests(
    status: str | None = None, _: User = Depends(admin_only), db: Session = Depends(get_db)
):
    q = select(SupportRequest).order_by(SupportRequest.created_at.desc()).limit(200)
    if status:
        q = q.where(SupportRequest.status == status)
    return [_support_out(s) for s in db.scalars(q)]


@router.post("/admin/support/requests/{request_id}/remediate")
def remediate_support_request(
    request_id: int, request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)
):
    """Hand a ticket to Devin: it gets the trace bundle and opens a PR with the fix."""
    sr = db.get(SupportRequest, request_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Unknown support request")
    if not remediation.enabled():
        raise HTTPException(status_code=503, detail="Devin API not configured (DEVIN_API_KEY / DEVIN_ORG_ID)")
    sr.status = "queued"
    audit(db, request, user, "support.remediate", "support_request", sr.id)
    db.commit()
    remediation.notify()
    return _support_out(sr)
