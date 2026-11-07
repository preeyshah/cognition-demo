import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import deny
from .config import get_settings
from .db import get_db
from .models import Role, UsedSsoCode, User
from .tracing import set_actor

bearer = HTTPBearer(auto_error=False)


def create_token(user: User, typ: str = "access", minutes: int | None = None) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=minutes if minutes is not None else s.access_token_minutes)
    payload = {"sub": str(user.id), "role": user.role.value, "typ": typ, "iat": now, "exp": exp,
               "jti": secrets.token_urlsafe(12)}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str, typ: str = "access") -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    if payload.get("typ") != typ:
        raise HTTPException(status_code=401, detail="Wrong token type")
    return payload


def consume_code(db: Session, payload: dict) -> bool:
    """Mark a one-time SSO code as used; False if it was already redeemed (by any API process)."""
    jti = payload.get("jti")
    if not jti:
        return False
    now = datetime.now(timezone.utc)
    db.execute(delete(UsedSsoCode).where(UsedSsoCode.expires_at < now))
    db.add(UsedSsoCode(jti=jti, expires_at=datetime.fromtimestamp(payload["exp"], timezone.utc)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(creds.credentials)
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or not found")
    set_actor(user.id, user.email)
    return user


def require_roles(*roles: Role):
    def dep(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if user.role not in roles:
            deny(
                db,
                request,
                user,
                "authz.role_denied",
                f"Requires role: {', '.join(r.value for r in roles)}",
                path=request.url.path,
            )
        return user

    return dep
