import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditLog, SupportRequest, User, utcnow
from .tracing import current_trace_id

# Serialises chain appends across API processes/tasks (Postgres transaction-scoped advisory lock).
_CHAIN_LOCK_KEY = 0x41554449


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def chain_hash(entry: AuditLog, prev_hash: str | None) -> str:
    """SHA-256 over the previous link and a canonical encoding of every audited field."""
    payload = json.dumps(
        {
            "prev": prev_hash, "actor_id": entry.actor_id, "actor_email": entry.actor_email,
            "action": entry.action, "entity_type": entry.entity_type, "entity_id": entry.entity_id,
            "success": bool(entry.success), "ip_address": entry.ip_address, "detail": entry.detail or {},
            "trace_id": entry.trace_id, "created_at": _utc_iso(entry.created_at),
        },
        sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@event.listens_for(Session, "before_flush")
def _link_audit_entries(session: Session, _ctx, _instances) -> None:
    new = [o for o in session.new if isinstance(o, AuditLog)]
    if not new:
        return
    new.sort(key=lambda o: o._sa_instance_state.insert_order)
    with session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHAIN_LOCK_KEY})
        prev = session.scalar(
            select(AuditLog.row_hash).where(AuditLog.row_hash.is_not(None)).order_by(AuditLog.id.desc()).limit(1)
        )
    for entry in new:
        # Column defaults are applied at INSERT time, after this hook; resolve them now so the hash covers them.
        entry.created_at = entry.created_at or utcnow()
        entry.trace_id = entry.trace_id or current_trace_id()
        entry.success = True if entry.success is None else entry.success
        entry.detail = json.loads(json.dumps(entry.detail or {}, default=str))
        entry.prev_hash = prev
        entry.row_hash = prev = chain_hash(entry, prev)


def verify_chain(db: Session) -> dict:
    """Recompute every link. Rows written before the chain existed are counted, not verified."""
    checked = legacy = 0
    prev: str | None = None
    broken: dict | None = None
    for a in db.scalars(select(AuditLog).order_by(AuditLog.id).execution_options(yield_per=1000)):
        if a.row_hash is None and checked == 0:
            legacy += 1
            continue
        if a.row_hash is None:
            broken = {"id": a.id, "reason": "entry is missing its hash"}
        elif a.prev_hash != prev:
            broken = {"id": a.id, "reason": "link to the previous entry is broken (an entry was deleted or reordered)"}
        elif chain_hash(a, prev) != a.row_hash:
            broken = {"id": a.id, "reason": "entry contents do not match its hash (modified after it was written)"}
        if broken:
            break
        checked += 1
        prev = a.row_hash
    return {"ok": broken is None, "verified_entries": checked, "legacy_unhashed_entries": legacy,
            "head_hash": prev, "first_broken": broken}


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
    recent = db.execute(
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
    from .remediation import notify  # lazy: remediation imports this module's siblings

    notify()
