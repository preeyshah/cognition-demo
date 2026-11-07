"""Headline numbers and analytics. Computed in Python over a bounded window so
the same code runs on Postgres and SQLite (tests); Postgres also exposes the
SQL views in sql/analytics.sql for BI tools."""

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import policy
from .models import Customer, RefundRequest, RefundStatus, Role, User
from .services import aware, get_policies, now_utc, start_of_day


def _hours(r: RefundRequest) -> float | None:
    if r.decided_at is None:
        return None
    return (aware(r.decided_at) - aware(r.created_at)).total_seconds() / 3600


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _rate(num, den):
    return round(num / den, 3) if den else None


def _card(key, label, value, fmt="number", tone="default"):
    return {"key": key, "label": label, "value": value, "format": fmt, "tone": tone}


def summary_for(db: Session, user: User) -> dict:
    now, sod = now_utc(), start_of_day()
    open_statuses = (RefundStatus.pending, RefundStatus.escalated)
    if user.role == Role.reviewer:
        mine = db.scalars(
            select(RefundRequest).where(
                (RefundRequest.assigned_to_id == user.id)
                | (RefundRequest.decided_by_id == user.id)
                | (RefundRequest.escalated_by_id == user.id)
            )
        ).unique().all()
        pending = [r for r in mine if r.status == RefundStatus.pending and r.assigned_to_id == user.id]
        approved_today = [r for r in mine if r.decided_by_id == user.id and r.status == RefundStatus.approved and aware(r.decided_at) >= sod]
        week = [r for r in mine if r.decided_by_id == user.id and aware(r.decided_at) >= now - timedelta(days=7)]
        approved_today_cents = sum(r.amount_cents for r in approved_today)
        remaining = max(user.daily_approval_limit_cents - approved_today_cents, 0)
        overdue = [r for r in pending if aware(r.sla_due_at) < now]
        escalated_open = [r for r in mine if r.escalated_by_id == user.id and r.status == RefundStatus.escalated]
        return {
            "role": user.role.value,
            "cards": [
                _card("pending", "Assigned to me", len(pending), tone="primary"),
                _card("pending_amount", "Pending amount", sum(r.amount_cents for r in pending), "money"),
                _card("overdue", "Past SLA", len(overdue), tone="danger" if overdue else "default"),
                _card("approved_today", "Approved today", approved_today_cents, "money", "success"),
                _card("daily_remaining", "Daily limit left", remaining, "money", "warning" if remaining < user.daily_approval_limit_cents * 0.2 else "default"),
                _card("approval_rate_7d", "Approval rate (7d)", _rate(sum(r.status == RefundStatus.approved for r in week), len(week)), "percent"),
                _card("avg_hours_7d", "Avg time to decide (7d)", _avg(_hours(r) for r in week), "hours"),
                _card("escalated_open", "Escalated, awaiting admin", len(escalated_open)),
            ],
            "limits": {
                "approval_limit_cents": user.approval_limit_cents,
                "daily_approval_limit_cents": user.daily_approval_limit_cents,
                "approved_today_cents": approved_today_cents,
            },
        }

    rows = db.scalars(select(RefundRequest).where(RefundRequest.created_at >= now - timedelta(days=90))).unique().all()
    open_rows = db.scalars(select(RefundRequest).where(RefundRequest.status.in_(open_statuses))).unique().all()
    pending = [r for r in open_rows if r.status == RefundStatus.pending]
    escalated = [r for r in open_rows if r.status == RefundStatus.escalated]
    overdue = [r for r in open_rows if aware(r.sla_due_at) < now]
    decided_30 = db.scalars(select(RefundRequest).where(RefundRequest.decided_at >= now - timedelta(days=30))).unique().all()
    approved_30 = [r for r in decided_30 if r.status == RefundStatus.approved]
    approved_today = [r for r in approved_30 if aware(r.decided_at) >= sod]
    return {
        "role": user.role.value,
        "cards": [
            _card("pending", "Open (pending)", len(pending), tone="primary"),
            _card("escalated", "Escalated to admin", len(escalated), tone="warning" if escalated else "default"),
            _card("open_amount", "Open exposure", sum(r.amount_cents for r in open_rows), "money"),
            _card("overdue", "Past SLA", len(overdue), tone="danger" if overdue else "default"),
            _card("approved_today", "Refunded today", sum(r.amount_cents for r in approved_today), "money", "success"),
            _card("approved_30d", "Refunded (30d)", sum(r.amount_cents for r in approved_30), "money"),
            _card("approval_rate_30d", "Approval rate (30d)", _rate(len(approved_30), len(decided_30)), "percent"),
            _card("avg_hours_30d", "Avg time to decide (30d)", _avg(_hours(r) for r in decided_30), "hours"),
        ],
    }


def analytics(db: Session, user: User, days: int = 30) -> dict:
    now = now_utc()
    since = now - timedelta(days=days)
    show_pii = policy.can_view_pii(user)
    policies = get_policies(db)
    rows = db.scalars(select(RefundRequest).where(RefundRequest.created_at >= since)).unique().all()
    decided = [r for r in rows if r.decided_at]
    approved = [r for r in rows if r.status == RefundStatus.approved]
    ever_escalated = [r for r in rows if r.escalation_reason]
    breached = [r for r in decided if aware(r.decided_at) > aware(r.sla_due_at)] + [
        r for r in rows if not r.decided_at and aware(r.sla_due_at) < now
    ]

    by_status = defaultdict(lambda: {"count": 0, "amount_cents": 0})
    by_reason = defaultdict(lambda: {"count": 0, "amount_cents": 0, "approved": 0, "decided": 0, "approved_amount_cents": 0})
    daily = {(since + timedelta(days=i + 1)).date().isoformat(): {"created": 0, "approved_amount_cents": 0, "approved": 0, "rejected": 0} for i in range(days)}
    risk = {"low (0-39)": [0, 0, 0], "medium (40-69)": [0, 0, 0], "high (70+)": [0, 0, 0]}
    customers = defaultdict(lambda: {"count": 0, "amount_cents": 0})

    for r in rows:
        by_status[r.status.value]["count"] += 1
        by_status[r.status.value]["amount_cents"] += r.amount_cents
        b = by_reason[r.reason.value]
        b["count"] += 1
        b["amount_cents"] += r.amount_cents
        day = aware(r.created_at).date().isoformat()
        if day in daily:
            daily[day]["created"] += 1
        if r.decided_at:
            b["decided"] += 1
            dday = aware(r.decided_at).date().isoformat()
            if r.status == RefundStatus.approved:
                b["approved"] += 1
                b["approved_amount_cents"] += r.amount_cents
                if dday in daily:
                    daily[dday]["approved"] += 1
                    daily[dday]["approved_amount_cents"] += r.amount_cents
            elif dday in daily:
                daily[dday]["rejected"] += 1
        bucket = "low (0-39)" if r.risk_score < 40 else "medium (40-69)" if r.risk_score < 70 else "high (70+)"
        risk[bucket][0] += 1
        if r.decided_at:
            risk[bucket][1] += 1
            risk[bucket][2] += r.status == RefundStatus.approved
        customers[r.customer_id]["count"] += 1
        customers[r.customer_id]["amount_cents"] += r.amount_cents

    people = db.scalars(select(User).where(User.role.in_([Role.reviewer, Role.admin])).order_by(User.role, User.name)).all()
    open_by_user = defaultdict(int)
    for r in db.scalars(select(RefundRequest).where(RefundRequest.status == RefundStatus.pending)).unique():
        open_by_user[r.assigned_to_id] += 1
    reviewers = []
    for u in people:
        mine = [r for r in decided if r.decided_by_id == u.id]
        ok = [r for r in mine if r.status == RefundStatus.approved]
        reviewers.append({
            "id": u.id, "name": u.name, "role": u.role.value, "is_active": u.is_active,
            "decided": len(mine), "approved": len(ok), "rejected": len(mine) - len(ok),
            "escalated": sum(1 for r in rows if r.escalated_by_id == u.id),
            "approved_amount_cents": sum(r.amount_cents for r in ok),
            "approval_rate": _rate(len(ok), len(mine)),
            "avg_decision_hours": _avg(_hours(r) for r in mine),
            "open_assigned": open_by_user.get(u.id, 0),
            "approval_limit_cents": u.approval_limit_cents,
            "daily_approval_limit_cents": u.daily_approval_limit_cents,
        })

    top = sorted(customers.items(), key=lambda kv: kv[1]["amount_cents"], reverse=True)[:8]
    cust_map = {c.id: c for c in db.scalars(select(Customer).where(Customer.id.in_([k for k, _ in top])))}

    return {
        "window_days": days,
        "totals": {
            "count": len(rows),
            "amount_cents": sum(r.amount_cents for r in rows),
            "approved_count": len(approved),
            "approved_amount_cents": sum(r.amount_cents for r in approved),
            "rejected_count": by_status["rejected"]["count"],
            "open_count": by_status["pending"]["count"] + by_status["escalated"]["count"],
            "approval_rate": _rate(len(approved), len(decided)),
            "escalation_rate": _rate(len(ever_escalated), len(rows)),
            "sla_breach_rate": _rate(len(breached), len(rows)),
            "avg_decision_hours": _avg(_hours(r) for r in decided),
        },
        "by_status": [{"status": k, **v} for k, v in by_status.items()],
        "by_reason": [
            {"reason": k, "count": v["count"], "amount_cents": v["amount_cents"],
             "approved_amount_cents": v["approved_amount_cents"], "approval_rate": _rate(v["approved"], v["decided"])}
            for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["amount_cents"])
        ],
        "daily": [{"date": d, **v} for d, v in daily.items()],
        "risk_buckets": [{"bucket": k, "count": c, "approval_rate": _rate(a, d)} for k, (c, d, a) in risk.items()],
        "reviewers": reviewers,
        "top_customers": [
            {"external_id": cust_map[k].external_id,
             "name": cust_map[k].name if show_pii else policy.mask_name(cust_map[k].name),
             **v}
            for k, v in top if k in cust_map
        ],
    }
