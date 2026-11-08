"""Shared query helpers + serialisation used by several routers."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import policy
from .models import (
    PolicySetting,
    RefundEvent,
    RefundRequest,
    RefundStatus,
    Role,
    User,
)
from .schemas import CustomerOut, PayoutOut, Permissions, RefundEventOut, RefundOut, UserRef


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def start_of_day() -> datetime:
    n = now_utc()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def get_policies(db: Session) -> dict[str, int]:
    values = {k: v for k, (v, _) in policy.DEFAULT_POLICIES.items()}
    for row in db.scalars(select(PolicySetting)):
        values[row.key] = row.value
    return values


def refunded_cents_sum():
    """SUM of money actually refunded (partial approvals count their approved amount)."""
    return func.coalesce(func.sum(func.coalesce(RefundRequest.approved_amount_cents, RefundRequest.amount_cents)), 0)


def approved_today_cents(db: Session, user_id: int) -> int:
    return db.scalar(
        select(refunded_cents_sum()).where(
            RefundRequest.decided_by_id == user_id,
            RefundRequest.status == RefundStatus.approved,
            RefundRequest.decided_at >= start_of_day(),
        )
    ) or 0


def customer_30d_approved_cents(db: Session, customer_id: int) -> int:
    return db.scalar(
        select(refunded_cents_sum()).where(
            RefundRequest.customer_id == customer_id,
            RefundRequest.status == RefundStatus.approved,
            RefundRequest.decided_at >= now_utc() - timedelta(days=30),
        )
    ) or 0


def policy_context(db: Session, user: User, refund: RefundRequest, policies: dict[str, int] | None = None) -> policy.PolicyContext:
    return policy.PolicyContext(
        policies=policies or get_policies(db),
        approved_today_cents=approved_today_cents(db, user.id),
        customer_30d_approved_cents=customer_30d_approved_cents(db, refund.customer_id),
    )


def visible_refunds_query(user: User):
    q = select(RefundRequest)
    if user.role == Role.reviewer:
        q = q.where(
            or_(
                RefundRequest.assigned_to_id == user.id,
                RefundRequest.escalated_by_id == user.id,
                RefundRequest.decided_by_id == user.id,
            )
        )
    return q


def _ref(u: User | None) -> UserRef | None:
    return UserRef(id=u.id, name=u.name) if u else None


def is_overdue(refund: RefundRequest) -> bool:
    return refund.status in (RefundStatus.pending, RefundStatus.escalated) and aware(refund.sla_due_at) < now_utc()


def serialize_refund(db: Session, user: User, refund: RefundRequest, policies: dict[str, int] | None = None) -> RefundOut:
    policies = policies or get_policies(db)
    show_pii = policy.can_view_pii(user)
    c = refund.customer
    acting = user.role != Role.viewer and refund.status not in policy.TERMINAL
    if acting:
        ctx = policy_context(db, user, refund, policies)
        approve = policy.check_approve(user, refund, ctx)
        partial = approve.allowed and policy.can_approve_partial(user, refund, ctx)
        # Note length is validated at submit time; here we only check eligibility.
        reject = policy.check_reject(user, refund, ctx, "x" * 1000)
        escalate = policy.check_escalate(user, refund, ctx, "x" * 1000)
    else:
        approve = reject = escalate = policy.Decision(False, ["Read-only"])
        partial = False
    history = policy.check_view_history(user, refund)
    return RefundOut(
        id=refund.id,
        reference=refund.reference,
        order_id=refund.order_id,
        amount_cents=refund.amount_cents,
        currency=refund.currency,
        reason=refund.reason,
        description=refund.description,
        risk_score=refund.risk_score,
        status=refund.status,
        requested_by_email=refund.requested_by_email if show_pii else policy.mask_email(refund.requested_by_email),
        customer=CustomerOut(
            id=c.id,
            external_id=c.external_id,
            name=c.name if show_pii else policy.mask_name(c.name),
            email=c.email if show_pii else policy.mask_email(c.email),
            country=c.country,
            pii_masked=not show_pii,
        ),
        assigned_to=_ref(refund.assigned_to),
        escalated_by=_ref(refund.escalated_by),
        escalation_reason=refund.escalation_reason,
        decided_by=_ref(refund.decided_by),
        decision_note=refund.decision_note,
        decided_at=refund.decided_at,
        approved_amount_cents=refund.refunded_cents if refund.status == RefundStatus.approved else None,
        sla_due_at=refund.sla_due_at,
        overdue=is_overdue(refund),
        created_at=refund.created_at,
        permissions=Permissions(
            can_approve=approve.allowed,
            can_reject=reject.allowed,
            can_escalate=escalate.allowed,
            can_view_history=history.allowed,
            can_assign=user.role == Role.admin and refund.status not in policy.TERMINAL,
            can_approve_partial=partial,
            approve_blocked_reasons=[] if approve.allowed else approve.reasons,
            escalate_blocked_reasons=[] if escalate.allowed else escalate.reasons,
        ),
        payout=PayoutOut.model_validate(refund.payout) if refund.payout else None,
    )


def serialize_event(e: RefundEvent) -> RefundEventOut:
    return RefundEventOut(
        id=e.id,
        event_type=e.event_type,
        actor=_ref(e.actor),
        from_status=e.from_status,
        to_status=e.to_status,
        note=e.note,
        data=e.data or {},
        created_at=e.created_at,
    )


def pick_assignee(db: Session, exclude_email: str | None = None) -> User | None:
    """Least-loaded active reviewer (never the requester)."""
    load = (
        select(RefundRequest.assigned_to_id, func.count().label("n"))
        .where(RefundRequest.status == RefundStatus.pending)
        .group_by(RefundRequest.assigned_to_id)
        .subquery()
    )
    q = (
        select(User)
        .outerjoin(load, load.c.assigned_to_id == User.id)
        .where(User.role == Role.reviewer, User.is_active.is_(True))
        .order_by(func.coalesce(load.c.n, 0), User.id)
    )
    for u in db.scalars(q):
        if not exclude_email or u.email.lower() != exclude_email.lower():
            return u
    return None


def max_reviewer_limit(db: Session) -> int:
    return db.scalar(
        select(func.coalesce(func.max(User.approval_limit_cents), 0)).where(
            User.role == Role.reviewer, User.is_active.is_(True)
        )
    ) or 0
