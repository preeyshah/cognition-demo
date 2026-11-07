"""Single sign-on.

Ships with a built-in *mock OIDC identity provider* ("Acme SSO") so the flow is
demoable without an Okta / Entra ID tenant: the app redirects to the IdP, the
user picks an identity, the IdP redirects back with a short-lived signed code,
and the app exchanges it for an access token — the same authorization-code
shape as a real OIDC integration. Swapping in a real IdP only replaces
`/auth/sso/login` + `/auth/sso/authorize` with the provider's endpoints.
"""

import html
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import get_db
from ..models import Role, User
from ..schemas import TokenIn, TokenOut, UserOut
from ..security import consume_code, create_token, decode_token, get_current_user
from ..services import now_utc

router = APIRouter(prefix="/auth", tags=["auth"])


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _validate_redirect(request: Request, redirect_uri: str) -> None:
    if _origin(redirect_uri) not in get_settings().redirect_origins:
        raise HTTPException(status_code=400, detail="redirect_uri origin is not allowed")


ROLE_BLURB = {
    Role.admin: "Analytics, escalations, users & limits, audit log",
    Role.reviewer: "Own queue, approve / reject / escalate within limits",
    Role.viewer: "Read-only analytics and refunds, PII masked",
}


@router.get("/sso/login", response_class=HTMLResponse)
def sso_login(request: Request, redirect_uri: str, db: Session = Depends(get_db)):
    _validate_redirect(request, redirect_uri)
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.role, User.name)).all()
    rows = "".join(
        f"""<button name="email" value="{html.escape(u.email)}">
              <span class="n">{html.escape(u.name)}</span>
              <span class="r r-{u.role.value}">{u.role.value}</span>
              <span class="e">{html.escape(u.email)}</span>
              <span class="b">{ROLE_BLURB[u.role]}</span>
            </button>"""
        for u in users
    )
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acme SSO</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;margin:0;display:flex;justify-content:center;padding:48px 16px}}
.card{{background:#fff;border-radius:14px;max-width:460px;width:100%;padding:28px;box-shadow:0 20px 50px rgba(0,0,0,.35)}}
h1{{margin:0 0 4px;font-size:20px}} p{{color:#64748b;margin:0 0 20px;font-size:14px}}
button{{display:grid;grid-template-columns:1fr auto;gap:2px 8px;width:100%;text-align:left;border:1px solid #e2e8f0;background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;cursor:pointer}}
button:hover{{border-color:#6366f1;background:#eef2ff}}
.n{{font-weight:600}} .e{{color:#64748b;font-size:12px}} .b{{color:#94a3b8;font-size:12px;grid-column:1/3}}
.r{{font-size:11px;padding:2px 8px;border-radius:99px;text-transform:uppercase;font-weight:700;align-self:start}}
.r-admin{{background:#fee2e2;color:#b91c1c}} .r-reviewer{{background:#dbeafe;color:#1d4ed8}} .r-viewer{{background:#f1f5f9;color:#475569}}
.badge{{display:inline-block;font-size:11px;background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:6px;margin-bottom:12px}}
</style></head><body><div class="card">
<span class="badge">Mock identity provider (OIDC authorization-code flow)</span>
<h1>Acme SSO</h1><p>Choose a corporate identity to sign in to <b>Refunds Console</b>.</p>
<form method="post" action="authorize"><input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">{rows}</form>
</div></body></html>"""


@router.post("/sso/authorize")
def sso_authorize(
    request: Request,
    email: str = Form(...),
    redirect_uri: str = Form(...),
    db: Session = Depends(get_db),
):
    _validate_redirect(request, redirect_uri)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        audit(db, request, None, "auth.sso_failed", success=False, email=email)
        db.commit()
        raise HTTPException(status_code=403, detail="Unknown or inactive identity")
    code = create_token(user, typ="sso_code", minutes=1)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'code': code})}", status_code=303)


@router.post("/token", response_model=TokenOut)
def exchange_code(body: TokenIn, request: Request, db: Session = Depends(get_db)):
    payload = decode_token(body.code, typ="sso_code")
    if not consume_code(db, payload):
        raise HTTPException(status_code=401, detail="Authorization code already used")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or not found")
    user.last_login_at = now_utc()
    audit(db, request, user, "auth.login", "user", user.id, method="sso")
    db.commit()
    return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
