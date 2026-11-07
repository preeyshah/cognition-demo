from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .audit import deny
from .config import get_settings
from .db import get_db
from .models import Role, User

bearer = HTTPBearer(auto_error=False)


def create_token(user: User, typ: str = "access", minutes: int | None = None) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=minutes if minutes is not None else s.access_token_minutes)
    payload = {"sub": str(user.id), "role": user.role.value, "typ": typ, "iat": now, "exp": exp}
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
