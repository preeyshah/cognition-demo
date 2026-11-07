"""Compliance Center API: live control checks, evidence pack, access reviews and GDPR requests."""

import hashlib
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import compliance, remediation
from ..audit import audit, deny, verify_chain
from ..config import get_settings
from ..db import get_db
from ..models import AccessReview, Customer, DataSubjectRequest, RefundRequest, Role, SupportRequest, User
from ..security import require_roles
from ..services import aware, now_utc

router = APIRouter(prefix="/compliance", tags=["compliance"])
admin_only = require_roles(Role.admin)
# Viewers are the auditor role: they can read controls and download evidence, but change nothing.
auditors = require_roles(Role.admin, Role.viewer)


@router.get("/controls")
def controls(_: User = Depends(auditors), db: Session = Depends(get_db)):
    return compliance.run_all(db)


@router.get("/audit-chain")
def audit_chain(_: User = Depends(auditors), db: Session = Depends(get_db)):
    return verify_chain(db)


def _review_out(r: AccessReview) -> dict:
    return {"id": r.id, "reviewer_email": r.reviewer_email, "completed_at": r.completed_at,
            "decisions": r.decisions, "revoked_count": r.revoked_count}


def _dsr_out(r: DataSubjectRequest) -> dict:
    due = aware(r.due_at)
    return {"id": r.id, "customer_external_id": r.customer.external_id, "kind": r.kind, "status": r.status,
            "note": r.note, "requested_by_email": r.requested_by_email, "created_at": r.created_at, "due_at": r.due_at,
            "overdue": r.status == "open" and due < now_utc(), "completed_at": r.completed_at,
            "result": {k: v for k, v in (r.result or {}).items() if k != "export"}}


@router.get("/evidence-pack")
def evidence_pack(request: Request, actor: User = Depends(auditors), db: Session = Depends(get_db)):
    """Point-in-time JSON for auditors. The SHA-256 of the body is returned in a header and audited."""
    s = get_settings()
    reviews = db.scalars(select(AccessReview).order_by(AccessReview.completed_at.desc()).limit(8)).all()
    dsrs = db.scalars(select(DataSubjectRequest).order_by(DataSubjectRequest.created_at.desc()).limit(100)).unique().all()
    pack = {
        "system": "AcmePay Refunds Console", "repo": s.devin_repo, "generated_by": actor.email,
        "scope_note": "Application-layer controls. Infrastructure inherits AWS attestations; the Devin platform "
                      "used to build and operate it is covered by Cognition's own SOC 2 Type II report.",
        "results": compliance.run_all(db), "audit_chain": verify_chain(db),
        "access_reviews": [_review_out(r) for r in reviews], "data_subject_requests": [_dsr_out(r) for r in dsrs],
        "config": {k: getattr(s, k) for k in ("environment", "access_token_minutes", "session_idle_limit_minutes",
                                             "audit_retention_days", "access_review_interval_days",
                                             "dormant_account_days", "dsar_due_days")},
    }
    body = json.dumps(pack, default=str, indent=2, sort_keys=True).encode()
    digest = hashlib.sha256(body).hexdigest()
    audit(db, request, actor, "compliance.evidence_export", "evidence_pack", digest[:16], sha256=digest)
    db.commit()
    name = f"evidence-pack-{now_utc():%Y%m%dT%H%M%SZ}.json"
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Evidence-SHA256": digest,
        "Access-Control-Expose-Headers": "X-Evidence-SHA256",
    })


# --- Fix with Devin -----------------------------------------------------------------------------

@router.post("/controls/{control_id}/remediate")
def remediate_control(control_id: str, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Open a `compliance` ticket for a failing control and hand it to Devin (same loop as error tickets)."""
    control = compliance.BY_ID.get(control_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Unknown control")
    result = compliance.evaluate(db, control)
    if result["status"] in ("pass", "documented"):
        deny(db, request, actor, "compliance.remediate", "Control is not failing; nothing to remediate", 409, "control", control_id)
    fingerprint = f"compliance:{control_id}"
    sr = db.scalar(select(SupportRequest).where(
        SupportRequest.fingerprint == fingerprint, SupportRequest.status.in_(["open", "queued", "devin_working"])
    ))
    if sr is None or (sr.status == "open" and remediation.enabled()):
        frameworks = "; ".join(f"{fw} {', '.join(refs)}" for fw, refs in control.frameworks.items())
        if sr is None:
            sr = SupportRequest(kind="compliance", fingerprint=fingerprint, reporter_email=actor.email,
                                title=f"Compliance control {control_id} failing: {control.title}"[:200], description="")
            db.add(sr)
        sr.description = (
            f"Control: {control_id} {control.title}\nFrameworks: {frameworks}\nObjective: {control.objective}\n"
            f"Current result: {result['status'].upper()} - {result['summary']}\n"
            f"Evidence: {json.dumps(result['evidence'], default=str)[:4000]}\n"
            f"Expected remediation: {control.remediation}\n"
            f"The check lives in backend/app/compliance.py ({control.check.__name__ if control.check else 'documented'}); "
            "the fix must make it pass without weakening the check."
        )
        if remediation.enabled():
            sr.status = "queued"
        db.flush()
        audit(db, request, actor, "compliance.remediate", "control", control_id, support_request_id=sr.id,
              devin=remediation.enabled())
        db.commit()
        if remediation.enabled():
            remediation.remediate_async(sr.id)
    return {"id": sr.id, "status": sr.status, "devin_enabled": remediation.enabled(),
            "devin_session_url": sr.devin_session_url, "title": sr.title}


@router.get("/tickets")
def compliance_tickets(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(SupportRequest).where(SupportRequest.kind == "compliance")
                      .order_by(SupportRequest.created_at.desc()).limit(50)).all()
    return [{"id": r.id, "control_id": (r.fingerprint or "").removeprefix("compliance:"), "title": r.title,
             "status": r.status, "devin_session_url": r.devin_session_url, "remediation_error": r.remediation_error,
             "created_at": r.created_at} for r in rows]


# --- Access review ------------------------------------------------------------------------------

@router.get("/access-review")
def access_review_state(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = get_settings()
    cutoff = now_utc() - timedelta(days=s.dormant_account_days)
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.role, User.name)).all()
    reviews = db.scalars(select(AccessReview).order_by(AccessReview.completed_at.desc()).limit(10)).all()
    return {
        "interval_days": s.access_review_interval_days,
        "users": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role.value,
                   "approval_limit_cents": u.approval_limit_cents, "daily_approval_limit_cents": u.daily_approval_limit_cents,
                   "last_login_at": u.last_login_at, "dormant": aware(u.last_login_at or u.created_at) < cutoff}
                  for u in users],
        "history": [_review_out(r) for r in reviews],
    }


class ReviewDecision(BaseModel):
    user_id: int
    decision: Literal["certify", "revoke"]
    note: str = Field(default="", max_length=500)


class ReviewIn(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1)


@router.post("/access-review", status_code=201)
def complete_access_review(body: ReviewIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    active = {u.id: u for u in db.scalars(select(User).where(User.is_active.is_(True)))}
    decided = {d.user_id: d for d in body.decisions}
    if set(decided) != set(active):
        deny(db, request, actor, "compliance.access_review", "Every active user must be certified or revoked", 422)
    if decided[actor.id].decision == "revoke":
        deny(db, request, actor, "compliance.access_review", "Admins cannot revoke their own access", 422, "user", actor.id)
    revoked = 0
    out = []
    for uid, d in decided.items():
        u = active[uid]
        if d.decision == "revoke":
            u.is_active = False
            revoked += 1
            audit(db, request, actor, "user.revoke", "user", u.id, reason="access review", note=d.note)
        out.append({"user_id": u.id, "email": u.email, "role": u.role.value,
                    "approval_limit_cents": u.approval_limit_cents, "decision": d.decision, "note": d.note})
    review = AccessReview(reviewer_id=actor.id, reviewer_email=actor.email, decisions=out, revoked_count=revoked)
    db.add(review)
    db.flush()
    audit(db, request, actor, "compliance.access_review", "access_review", review.id,
          users_reviewed=len(out), revoked=revoked)
    db.commit()
    return _review_out(review)


# --- GDPR data-subject requests -----------------------------------------------------------------

class DsrIn(BaseModel):
    customer_external_id: str = Field(max_length=64)
    kind: Literal["export", "erasure"]
    note: str = Field(default="", max_length=1000)


@router.get("/dsr")
def list_dsr(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(DataSubjectRequest).order_by(DataSubjectRequest.created_at.desc()).limit(200)).unique().all()
    return [_dsr_out(r) for r in rows]


@router.post("/dsr", status_code=201)
def create_dsr(body: DsrIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    customer = db.scalar(select(Customer).where(Customer.external_id == body.customer_external_id))
    if customer is None:
        raise HTTPException(status_code=404, detail="Unknown customer")
    now = now_utc()
    r = DataSubjectRequest(customer_id=customer.id, kind=body.kind, note=body.note, requested_by_email=actor.email,
                           created_at=now, due_at=now + timedelta(days=get_settings().dsar_due_days))
    db.add(r)
    db.flush()
    audit(db, request, actor, "compliance.dsr_create", "data_subject_request", r.id, kind=body.kind,
          customer=customer.external_id)
    db.commit()
    return _dsr_out(r)


@router.post("/dsr/{request_id}/fulfil")
def fulfil_dsr(request_id: int, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    r = db.get(DataSubjectRequest, request_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Unknown request")
    if r.status != "open":
        raise HTTPException(status_code=409, detail="Request already completed")
    c = r.customer
    refunds = db.scalars(select(RefundRequest).where(RefundRequest.customer_id == c.id).order_by(RefundRequest.id)).unique().all()
    export = None
    if r.kind == "export":
        export = {
            "customer": {"external_id": c.external_id, "name": c.name, "email": c.email, "country": c.country,
                         "created_at": c.created_at},
            "refunds": [{"reference": x.reference, "order_id": x.order_id, "amount_cents": x.amount_cents,
                         "currency": x.currency, "reason": x.reason.value, "status": x.status.value,
                         "created_at": x.created_at, "decided_at": x.decided_at} for x in refunds],
        }
        r.result = {"records_exported": 1 + len(refunds)}
    else:
        # Art. 17(3)(b): financial records are retained (AML / tax); identity is pseudonymised.
        c.name = f"Erased subject {c.external_id}"
        c.email = f"erased+{c.external_id}@redacted.invalid"
        r.result = {"pseudonymised_fields": ["customer.name", "customer.email"], "refunds_retained": len(refunds),
                    "legal_basis_for_retention": "GDPR Art. 17(3)(b) - financial record-keeping obligations"}
    r.status = "completed"
    r.completed_at = now_utc()
    r.completed_by_id = actor.id
    audit(db, request, actor, f"compliance.dsr_{r.kind}", "data_subject_request", r.id, customer=c.external_id,
          on_time=aware(r.due_at) >= r.completed_at)
    db.commit()
    out = _dsr_out(r)
    if export is not None:
        out["export"] = json.loads(json.dumps(export, default=str))
    return out
