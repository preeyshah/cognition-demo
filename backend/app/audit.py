from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditLog, SupportRequest, User


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # Only trust X-Forwarded-For entries appended by our own proxies (nginx locally; CloudFront + ALB on AWS):
    # the client IP is the entry `trusted_proxy_hops` from the right, anything left of it is client-controlled.
    hops = get_settings().trusted_proxy_hops
    fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if hops and len(fwd) >= hops:
        return fwd[-hops]
    return request.client.host if request.client else None


def audit(
    db: Session,
    request: Request | None,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: object = None,
    success: bool = True,
    **detail,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        success=success,
        ip_address=client_ip(request),
        detail=detail,
    )
    db.add(entry)
    return entry


def deny(
    db: Session,
    request: Request | None,
    actor: User | None,
    action: str,
    message: str,
    status_code: int = 403,
    entity_type: str | None = None,
    entity_id: object = None,
    **detail,
):
    """Record a denied attempt (committed even though the request fails) and raise."""
    db.rollback()
    audit(db, request, actor, action, entity_type, entity_id, success=False, reason=message, **detail)
    db.commit()
    _maybe_guardrail_alert(db, actor)
    raise HTTPException(status_code=status_code, detail=message)


def _maybe_guardrail_alert(db: Session, actor: User | None) -> None:
    """A spike in denied actions means either an attack or a broken guardrail — either way
    a human (and Devin) should look. Deduped by fingerprint while a ticket is open."""
    s = get_settings()
    if s.guardrail_alert_threshold <= 0:
        return
    since = datetime.now(timezone.utc) - timedelta(minutes=s.guardrail_alert_window_minutes)
    recent = db.scalars(
        select(AuditLog.action, AuditLog.actor_email).where(AuditLog.success.is_(False), AuditLog.created_at >= since)
    ).all()
    if len(recent) < s.guardrail_alert_threshold:
        return
    fp = "guardrail:denied-actions"
    open_ticket = db.scalar(
        select(SupportRequest).where(
            SupportRequest.fingerprint == fp,
            SupportRequest.status.in_(["open", "queued", "devin_working"]),
        )
    )
    if open_ticket is not None:
        return
    top = Counter(a for a, _ in recent).most_common(5)
    sr = SupportRequest(
        kind="guardrail",
        title=f"Guardrail alert: {len(recent)} denied actions in {s.guardrail_alert_window_minutes}m",
        description=(
            "Denied-action spike detected — possible attack, credential abuse, or a guardrail "
            "misfiring and blocking legitimate work.\n\n"
            f"Top denied actions: {top}\n\n"
            "Investigate the audit log for this window; if a policy is wrong fix it, if it is abuse "
            "recommend containment. Do not weaken authorization checks."
        ),
        fingerprint=fp,
        reporter_email=actor.email if actor else "system",
        status="queued",
    )
    db.add(sr)
    db.flush()
    audit(db, None, actor, "guardrail.alert", "support_request", sr.id,
          denied=len(recent), window_minutes=s.guardrail_alert_window_minutes, top_actions=top)
    db.commit()
    from .remediation import remediate_async  # lazy: remediation imports this module's siblings

    remediate_async(sr.id)
