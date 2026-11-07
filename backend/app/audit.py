from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .models import AuditLog, User


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
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
    raise HTTPException(status_code=status_code, detail=message)
