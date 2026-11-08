import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import summary_for
from ..audit import audit, deny
from ..db import get_db
from ..models import AuditLog, PolicySetting, RefundRequest, RefundStatus, Role, User
from ..policy import DEFAULT_POLICIES
from ..schemas import PolicyOut, PolicyUpdateIn, UserCreateIn, UserOut, UserUpdateIn
from ..security import require_roles
from ..services import get_policies, serialize_refund, visible_refunds_query

router = APIRouter(prefix="/admin", tags=["admin"])
admin_only = require_roles(Role.admin)


@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.role, User.name)).all()


def _check_limits(db: Session, request: Request, actor: User, approval: int | None, daily: int | None, target=None):
    cap = get_policies(db)["max_refund_cents"]
    if approval is not None and approval > cap:
        deny(db, request, actor, "user.update", "Approval limit cannot exceed the organisation hard cap", 422, "user", target)
    if approval is not None and daily is not None and daily < approval:
        deny(db, request, actor, "user.update", "Daily limit must be at least the per-refund limit", 422, "user", target)


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreateIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="Email already exists")
    if body.role == Role.viewer:
        body.approval_limit_cents = body.daily_approval_limit_cents = 0
    _check_limits(db, request, actor, body.approval_limit_cents, body.daily_approval_limit_cents)
    user = User(**body.model_dump())
    db.add(user)
    db.flush()
    audit(db, request, actor, "user.create", "user", user.id, **body.model_dump(mode="json"))
    db.commit()
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdateIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    changes = body.model_dump(exclude_unset=True, mode="json")
    if user.id == actor.id and ("role" in changes or changes.get("is_active") is False):
        deny(db, request, actor, "user.update", "Admins cannot change their own role or deactivate themselves", 422, "user", user.id)
    new_role = Role(changes.get("role", user.role))
    approval = changes.get("approval_limit_cents", user.approval_limit_cents)
    daily = changes.get("daily_approval_limit_cents", user.daily_approval_limit_cents)
    if new_role == Role.viewer:
        approval = daily = 0
    _check_limits(db, request, actor, approval, daily, user.id)
    before = UserOut.model_validate(user).model_dump(mode="json")
    user.role = new_role
    user.approval_limit_cents = approval
    user.daily_approval_limit_cents = daily
    if "is_active" in changes:
        user.is_active = changes["is_active"]
    after = UserOut.model_validate(user).model_dump(mode="json")
    diff = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    audit(db, request, actor, "user.update", "user", user.id, changes=diff)
    db.commit()
    return user


@router.get("/policies", response_model=list[PolicyOut])
def list_policies(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    return db.scalars(select(PolicySetting).order_by(PolicySetting.key)).all()


@router.put("/policies/{key}", response_model=PolicyOut)
def update_policy(key: str, body: PolicyUpdateIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    if key not in DEFAULT_POLICIES:
        raise HTTPException(status_code=404, detail="Unknown policy")
    if key == "high_risk_threshold" and body.value > 100:
        deny(db, request, actor, "policy.update", "Risk threshold must be 0-100", 422, "policy", key)
    if key == "sla_hours" and body.value < 1:
        deny(db, request, actor, "policy.update", "SLA must be at least 1 hour", 422, "policy", key)
    row = db.get(PolicySetting, key)
    old = row.value
    row.value = body.value
    row.updated_by_id = actor.id
    audit(db, request, actor, "policy.update", "policy", key, **{"from": old, "to": body.value})
    db.commit()
    return row


@router.get("/audit-log")
def audit_log(
    action: str | None = None,
    actor_id: int | None = None,
    success: bool | None = None,
    limit: int = Query(default=200, le=1000),
    _: User = Depends(admin_only),
    db: Session = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if actor_id is not None:
        q = q.where(AuditLog.actor_id == actor_id)
    if success is not None:
        q = q.where(AuditLog.success.is_(success))
    return [
        {"id": a.id, "created_at": a.created_at, "actor_email": a.actor_email, "action": a.action,
         "entity_type": a.entity_type, "entity_id": a.entity_id, "success": a.success,
         "ip_address": a.ip_address, "detail": a.detail, "trace_id": a.trace_id}
        for a in db.scalars(q)
    ]


@router.get("/view-as/{user_id}")
def view_as(user_id: int, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Read-only preview of exactly what another user sees (permissions included)."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    refunds = db.scalars(
        visible_refunds_query(target).order_by(RefundRequest.created_at.desc()).limit(100)
    ).unique().all()
    policies = get_policies(db)
    audit(db, request, actor, "admin.view_as", "user", target.id)
    db.commit()
    return {
        "user": UserOut.model_validate(target),
        "summary": summary_for(db, target),
        "refunds": [serialize_refund(db, target, r, policies) for r in refunds],
        "read_only": True,
    }


@router.get("/export/refunds.csv")
def export_refunds(request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(RefundRequest).order_by(RefundRequest.id)).unique().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reference", "created_at", "status", "amount_cents", "currency", "reason", "risk_score",
                "customer_external_id", "assigned_to", "decided_by", "decided_at", "escalated_by", "escalation_reason",
                "approved_amount_cents"])
    for r in rows:
        w.writerow([r.reference, r.created_at.isoformat(), r.status.value, r.amount_cents, r.currency, r.reason.value,
                    r.risk_score, r.customer.external_id, r.assigned_to.email if r.assigned_to else "",
                    r.decided_by.email if r.decided_by else "", r.decided_at.isoformat() if r.decided_at else "",
                    r.escalated_by.email if r.escalated_by else "", (r.escalation_reason or "").strip(),
                    r.refunded_cents if r.status == RefundStatus.approved else ""])
    audit(db, request, actor, "export.refunds", rows=len(rows))
    db.commit()
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=refunds.csv"})
