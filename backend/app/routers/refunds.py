from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import policy
from ..audit import audit, deny
from ..db import get_db
from ..history_client import fetch_customer_history
from ..models import (
    Customer,
    RefundEvent,
    RefundRequest,
    RefundStatus,
    Role,
    User,
)
from ..schemas import (
    AssignIn,
    DecisionIn,
    RefundCreateIn,
    RefundDetailOut,
    RefundList,
    RefundOut,
)
from ..security import get_current_user, require_roles
from ..services import (
    get_policies,
    max_reviewer_limit,
    now_utc,
    pick_assignee,
    policy_context,
    serialize_event,
    serialize_refund,
    visible_refunds_query,
)

router = APIRouter(prefix="/refunds", tags=["refunds"])


def _load_visible(db: Session, request: Request, user: User, refund_id: int, lock: bool = False) -> RefundRequest:
    q = select(RefundRequest).where(RefundRequest.id == refund_id)
    if lock:
        q = q.with_for_update(of=RefundRequest)
    refund = db.scalars(q).unique().first()
    if refund is None:
        raise HTTPException(status_code=404, detail="Refund not found")
    if not policy.can_view(user, refund):
        # 404 rather than 403 so reviewers can't probe for other people's tickets.
        deny(db, request, user, "refund.view", "Refund not found", 404, "refund", refund_id)
    return refund


def _event(db: Session, refund: RefundRequest, actor: User | None, event_type: str, from_status, note=None, **data):
    db.add(
        RefundEvent(
            refund_id=refund.id,
            actor_id=actor.id if actor else None,
            event_type=event_type,
            from_status=from_status.value if from_status else None,
            to_status=refund.status.value,
            note=note,
            data=data,
        )
    )


@router.get("", response_model=RefundList)
def list_refunds(
    status: RefundStatus | None = None,
    q: str | None = Query(default=None, max_length=100),
    assigned_to_id: int | None = None,
    mine: bool = False,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = visible_refunds_query(user)
    if status:
        query = query.where(RefundRequest.status == status)
    if mine:
        query = query.where(RefundRequest.assigned_to_id == user.id)
    if assigned_to_id is not None and user.role != Role.reviewer:
        query = query.where(RefundRequest.assigned_to_id == assigned_to_id)
    if q:
        like = f"%{q.lower()}%"
        query = query.join(Customer).where(
            or_(
                func.lower(RefundRequest.reference).like(like),
                func.lower(RefundRequest.order_id).like(like),
                func.lower(Customer.external_id).like(like),
                *([func.lower(Customer.name).like(like)] if policy.can_view_pii(user) else []),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(RefundRequest.created_at.desc()).limit(limit).offset(offset)
    ).unique().all()
    policies = get_policies(db)
    return RefundList(items=[serialize_refund(db, user, r, policies) for r in rows], total=total or 0)


@router.post("", response_model=RefundOut, status_code=201)
def create_refund(
    body: RefundCreateIn,
    request: Request,
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    customer = db.scalar(select(Customer).where(Customer.external_id == body.customer_external_id))
    if customer is None:
        raise HTTPException(status_code=404, detail="Unknown customer")
    policies = get_policies(db)
    if body.amount_cents > policies["max_refund_cents"]:
        deny(db, request, user, "refund.create", "Amount exceeds the organisation hard cap", 422)
    refund = RefundRequest(
        reference=f"tmp-{uuid4().hex}",
        customer_id=customer.id,
        order_id=body.order_id,
        amount_cents=body.amount_cents,
        currency=body.currency.upper(),
        reason=body.reason,
        description=body.description,
        risk_score=body.risk_score,
        requested_by_email=body.requested_by_email,
        sla_due_at=now_utc() + timedelta(hours=policies["sla_hours"]),
    )
    escalation = policy.needs_auto_escalation(body.amount_cents, body.risk_score, policies, max_reviewer_limit(db))
    if escalation:
        refund.status = RefundStatus.escalated
        refund.escalation_reason = escalation
    else:
        assignee = pick_assignee(db, exclude_email=body.requested_by_email)
        refund.assigned_to_id = assignee.id if assignee else None
    db.add(refund)
    db.flush()
    refund.reference = f"RF-{1000 + refund.id}"
    _event(db, refund, user, "created", None, escalation, assigned_to_id=refund.assigned_to_id)
    audit(db, request, user, "refund.create", "refund", refund.id, amount_cents=refund.amount_cents)
    db.commit()
    db.refresh(refund)
    return serialize_refund(db, user, refund)


@router.get("/{refund_id}", response_model=RefundDetailOut)
def get_refund(refund_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    refund = _load_visible(db, request, user, refund_id)
    events = db.scalars(
        select(RefundEvent).where(RefundEvent.refund_id == refund.id).order_by(RefundEvent.created_at)
    ).unique().all()
    base = serialize_refund(db, user, refund)
    return RefundDetailOut(**base.model_dump(), events=[serialize_event(e) for e in events])


def _decide(action: str, refund_id: int, body: DecisionIn, request: Request, user: User, db: Session) -> RefundOut:
    refund = _load_visible(db, request, user, refund_id, lock=True)
    ctx = policy_context(db, user, refund)
    if action == "approve":
        decision = policy.check_approve(user, refund, ctx)
    elif action == "reject":
        decision = policy.check_reject(user, refund, ctx, body.note)
    else:
        decision = policy.check_escalate(user, refund, ctx, body.note)
    if not decision.allowed:
        deny(
            db, request, user, f"refund.{action}", decision.message, 403, "refund", refund.id,
            amount_cents=refund.amount_cents, reasons=decision.reasons,
        )
    previous = refund.status
    note = (body.note or "").strip() or None
    if action == "escalate":
        refund.status = RefundStatus.escalated
        refund.escalated_by_id = user.id
        refund.escalation_reason = note
        refund.assigned_to_id = None
    else:
        refund.status = RefundStatus.approved if action == "approve" else RefundStatus.rejected
        refund.decided_by_id = user.id
        refund.decided_at = now_utc()
        refund.decision_note = note
    _event(db, refund, user, action + ("d" if action.endswith("e") else "ed"), previous, note,
           amount_cents=refund.amount_cents)
    audit(db, request, user, f"refund.{action}", "refund", refund.id, amount_cents=refund.amount_cents,
          from_status=previous.value, to_status=refund.status.value)
    db.commit()
    db.refresh(refund)
    return serialize_refund(db, user, refund)


@router.post("/{refund_id}/approve", response_model=RefundOut)
def approve(refund_id: int, body: DecisionIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _decide("approve", refund_id, body, request, user, db)


@router.post("/{refund_id}/reject", response_model=RefundOut)
def reject(refund_id: int, body: DecisionIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _decide("reject", refund_id, body, request, user, db)


@router.post("/{refund_id}/escalate", response_model=RefundOut)
def escalate(refund_id: int, body: DecisionIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _decide("escalate", refund_id, body, request, user, db)


@router.post("/{refund_id}/assign", response_model=RefundOut)
def assign(
    refund_id: int,
    body: AssignIn,
    request: Request,
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    refund = _load_visible(db, request, user, refund_id, lock=True)
    if refund.status in policy.TERMINAL:
        deny(db, request, user, "refund.assign", "Cannot reassign a decided refund", 409, "refund", refund.id)
    assignee = db.get(User, body.assignee_id)
    if assignee is None or not assignee.is_active or assignee.role == Role.viewer:
        deny(db, request, user, "refund.assign", "Assignee must be an active reviewer or admin", 422, "refund", refund.id)
    if refund.status == RefundStatus.escalated and assignee.role != Role.admin:
        deny(db, request, user, "refund.assign", "Escalated refunds can only be assigned to admins", 422, "refund", refund.id)
    if assignee.email.lower() == refund.requested_by_email.lower():
        deny(db, request, user, "refund.assign", "Cannot assign a refund to its requester", 422, "refund", refund.id)
    previous_assignee = refund.assigned_to_id
    refund.assigned_to_id = assignee.id
    _event(db, refund, user, "assigned", refund.status, None, from_user_id=previous_assignee, to_user_id=assignee.id)
    audit(db, request, user, "refund.assign", "refund", refund.id, from_user_id=previous_assignee, to_user_id=assignee.id)
    db.commit()
    db.refresh(refund)
    return serialize_refund(db, user, refund)


@router.get("/{refund_id}/customer-history")
def customer_history(refund_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    refund = _load_visible(db, request, user, refund_id)
    decision = policy.check_view_history(user, refund)
    if not decision.allowed:
        deny(db, request, user, "customer.history", decision.message, 403, "customer", refund.customer.external_id)
    external = fetch_customer_history(refund.customer.external_id)
    internal_rows = db.execute(
        select(RefundRequest.status, func.count(), func.coalesce(func.sum(RefundRequest.amount_cents), 0))
        .where(RefundRequest.customer_id == refund.customer_id)
        .group_by(RefundRequest.status)
    ).all()
    previous = db.scalars(
        select(RefundRequest)
        .where(RefundRequest.customer_id == refund.customer_id, RefundRequest.id != refund.id)
        .order_by(RefundRequest.created_at.desc())
        .limit(10)
    ).unique().all()
    audit(db, request, user, "customer.history", "customer", refund.customer.external_id,
          refund_id=refund.id, provider=external.get("provider"))
    _event(db, refund, user, "history_viewed", refund.status, None, provider=external.get("provider"))
    db.commit()
    return {
        "customer": {"external_id": refund.customer.external_id, "name": refund.customer.name, "email": refund.customer.email},
        "provider": external,
        "internal": {
            "by_status": {s.value: {"count": n, "amount_cents": amt} for s, n, amt in internal_rows},
            "previous_refunds": [
                {"reference": r.reference, "amount_cents": r.amount_cents, "status": r.status.value,
                 "reason": r.reason.value, "created_at": r.created_at.isoformat()}
                for r in previous
            ],
        },
    }
