"""Payouts at the payments provider: admin operations plus the provider's webhook receiver."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import payments_client, payouts
from ..audit import audit, deny
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import Payout, PayoutStatus, ProviderWebhookEvent, Role, User
from ..schemas import PayoutOut, SandboxIn
from ..security import require_roles
from ..services import now_utc

router = APIRouter(prefix="/admin/payouts", tags=["payouts"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])
admin_only = require_roles(Role.admin)


def _row(p: Payout) -> dict:
    r = p.refund
    return {**PayoutOut.model_validate(p).model_dump(), "refund_id": r.id, "reference": r.reference,
            "order_id": r.order_id, "customer_external_id": r.customer.external_id,
            "approved_by": r.decided_by.name if r.decided_by else None}


@router.get("")
def list_payouts(
    status: PayoutStatus | None = None,
    limit: int = Query(default=100, le=500),
    _: User = Depends(admin_only),
    db: Session = Depends(get_db),
):
    q = select(Payout).order_by(Payout.updated_at.desc()).limit(limit)
    if status:
        q = q.where(Payout.status == status)
    counts = dict(db.execute(select(Payout.status, func.count()).group_by(Payout.status)).all())
    paid_24h = db.scalar(
        select(func.coalesce(func.sum(Payout.amount_cents), 0)).where(
            Payout.status == PayoutStatus.succeeded, Payout.succeeded_at >= now_utc() - timedelta(days=1))
    ) or 0
    return {
        "items": [_row(p) for p in db.scalars(q).unique()],
        "counts": {s.value: counts.get(s, 0) for s in PayoutStatus},
        "paid_24h_cents": paid_24h,
        "sandbox_enabled": get_settings().payments_sandbox_controls,
    }


@router.get("/webhooks")
def list_webhook_events(limit: int = Query(default=50, le=500), _: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(ProviderWebhookEvent).order_by(ProviderWebhookEvent.received_at.desc()).limit(limit))
    return [{"event_id": e.event_id, "event_type": e.event_type, "provider_refund_id": e.provider_refund_id,
             "outcome": e.outcome, "received_at": e.received_at} for e in rows]


@router.post("/{payout_id}/retry")
def retry_payout(payout_id: int, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    payout = db.scalars(select(Payout).where(Payout.id == payout_id).with_for_update(of=Payout)).unique().first()
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout not found")
    if payout.status != PayoutStatus.failed:
        deny(db, request, actor, "payout.retry", "Only failed payouts can be retried", 409, "refund", payout.refund_id)
    previous = payout.failure_code
    payouts.manual_retry(db, payout, actor)
    audit(db, request, actor, "payout.retry", "refund", payout.refund_id, previous_failure=previous,
          idempotency_key=payout.idempotency_key)
    db.commit()
    return _row(payout)


@router.post("/reconcile")
def reconcile(actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    return payouts.reconcile(db, actor)


@router.post("/run-due")
def run_due(_: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Drive due payouts now instead of waiting for the worker's next tick."""
    return {"attempted": payouts.run_due(db)}


def _sandbox_guard():
    if not get_settings().payments_sandbox_controls:
        raise HTTPException(status_code=404, detail="Sandbox controls are disabled")


@router.get("/sandbox", dependencies=[Depends(_sandbox_guard)])
def sandbox_state(_: User = Depends(admin_only)):
    res = payments_client.get_sandbox()
    if not res.ok:
        raise HTTPException(status_code=502, detail=f"Provider sandbox unavailable ({res.error})")
    return res.body


@router.post("/sandbox", dependencies=[Depends(_sandbox_guard)])
def set_sandbox(body: SandboxIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)):
    res = payments_client.set_sandbox(body.mode, body.count)
    if not res.ok:
        raise HTTPException(status_code=502, detail=f"Provider sandbox unavailable ({res.error})")
    audit(db, request, actor, "payout.sandbox", mode=body.mode, count=body.count)
    db.commit()
    return res.body


def _process_webhook(payload: bytes, signature: str | None, request: Request) -> dict:
    with SessionLocal() as db:
        try:
            return payouts.handle_webhook(db, payload, signature)
        except (payouts.WebhookSignatureError, ValueError) as exc:
            # Recorded, then rejected: forged or replayed callbacks never touch a payout.
            deny(db, request, None, "webhook.paycore", f"Rejected webhook: {exc}", 400)


@webhooks.post("/paycore", include_in_schema=True)
async def paycore_webhook(request: Request):
    """Unauthenticated by design; trust comes from the HMAC signature (shared secret + timestamp)."""
    payload = await request.body()
    return await run_in_threadpool(_process_webhook, payload, request.headers.get("PayCore-Signature"), request)


