"""Payout state machine: moves the money for approved refunds at the payments provider.

    approve ──(same DB txn)──> queued ──worker──> processing ──webhook / reconcile──> succeeded
                                 │  ▲                 │                                 failed
                     5xx/timeout │  │ backoff         └─ 4xx decline ──> failed
                                 ▼  │
                           retry_scheduled ──(max attempts)──> failed (retries_exhausted)

Safety properties:
- exactly-once money movement: every attempt for a payout reuses one Idempotency-Key, so a retry
  after a timeout (where the provider may already have refunded) replays instead of paying twice;
- webhooks are HMAC-verified with a timestamp tolerance (no forgery, no replay) and deduped by event id;
- a terminal state never regresses (out-of-order webhooks are ignored);
- reconciliation polls the provider for anything still in flight (covers lost webhooks).
"""

import hashlib
import hmac
import json
import logging
import random
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import payments_client
from .audit import audit
from .config import get_settings
from .models import Payout, PayoutStatus, ProviderWebhookEvent, RefundEvent, RefundRequest, User
from .services import now_utc

log = logging.getLogger("refunds.payouts")
TERMINAL = {PayoutStatus.succeeded, PayoutStatus.failed}
DUE = (PayoutStatus.queued, PayoutStatus.retry_scheduled)


class WebhookSignatureError(Exception):
    pass


def _event(db: Session, payout: Payout, event_type: str, note: str, actor: User | None = None, **data) -> None:
    refund = payout.refund
    db.add(RefundEvent(refund_id=refund.id, actor_id=actor.id if actor else None, event_type=event_type,
                       from_status=refund.status.value, to_status=refund.status.value, note=note,
                       data={"payout_status": payout.status.value, **data}))


def base_key(refund: RefundRequest) -> str:
    return f"refund-{refund.reference}"


def enqueue(db: Session, refund: RefundRequest, actor: User) -> Payout:
    """Called inside the approval transaction: the approval and the payout intent commit together."""
    payout = Payout(refund=refund, idempotency_key=base_key(refund), amount_cents=refund.amount_cents,
                    currency=refund.currency, status=PayoutStatus.queued, next_attempt_at=now_utc())
    db.add(payout)
    _event(db, payout, "payout_queued", f"Refund of {refund.amount_cents / 100:,.2f} {refund.currency} queued at PayCore",
           actor, idempotency_key=payout.idempotency_key)
    return payout


def backoff_seconds(attempt: int) -> float:
    s = get_settings()
    delay = min(s.payout_retry_base_s * 2 ** (attempt - 1), s.payout_retry_max_s)
    return round(delay * random.uniform(1.0, 1.2), 1)  # jitter so a recovering provider isn't stampeded


def _mark_succeeded(db: Session, payout: Payout, source: str, **data) -> None:
    payout.status = PayoutStatus.succeeded
    payout.succeeded_at = now_utc()
    payout.next_attempt_at = None
    _event(db, payout, "payout_succeeded", f"Money returned to customer (confirmed by {source})",
           source=source, provider_refund_id=payout.provider_refund_id, **data)
    audit(db, None, None, "payout.succeeded", "refund", payout.refund_id, source=source,
          provider_refund_id=payout.provider_refund_id, amount_cents=payout.amount_cents)


def _mark_failed(db: Session, payout: Payout, code: str, message: str, source: str, **data) -> None:
    payout.status = PayoutStatus.failed
    payout.failure_code = code
    payout.last_error = message
    payout.next_attempt_at = None
    _event(db, payout, "payout_failed", f"Payout failed: {code} — {message}", source=source, failure_code=code, **data)
    audit(db, None, None, "payout.failed", "refund", payout.refund_id, success=False, source=source,
          failure_code=code, amount_cents=payout.amount_cents)


def attempt(db: Session, payout_id: int) -> Payout | None:
    """Make one provider call for a due payout. Safe to run concurrently (row lock + idempotency key)."""
    q = select(Payout).where(Payout.id == payout_id).with_for_update(skip_locked=True, of=Payout)
    payout = db.scalars(q).unique().first()
    if payout is None or payout.status not in DUE:
        db.rollback()
        return payout
    s = get_settings()
    refund = payout.refund
    payout.attempts += 1
    res = payments_client.create_refund(
        payout.idempotency_key, refund.order_id, payout.amount_cents, payout.currency,
        {"refund_reference": refund.reference, "customer_id": refund.customer.external_id},
    )
    payout.last_http_status = res.status_code
    if res.ok:
        payout.provider_refund_id = res.body.get("id")
        payout.last_error = None
        payout.next_attempt_at = None
        replay = " (idempotent replay — no double refund)" if res.replayed else ""
        if res.body.get("status") == "succeeded":
            _mark_succeeded(db, payout, "provider response", replayed=res.replayed)
        elif res.body.get("status") == "failed":
            _mark_failed(db, payout, res.body.get("failure_code") or "provider_failed", "Refund failed at provider",
                         "provider response")
        else:
            payout.status = PayoutStatus.processing
            _event(db, payout, "payout_submitted",
                   f"PayCore accepted refund {payout.provider_refund_id} on attempt {payout.attempts}{replay}",
                   provider_refund_id=payout.provider_refund_id, attempt=payout.attempts, replayed=res.replayed)
    elif res.transient and payout.attempts < s.payout_max_attempts:
        delay = backoff_seconds(payout.attempts)
        payout.status = PayoutStatus.retry_scheduled
        payout.last_error = res.error_code or res.error
        payout.next_attempt_at = now_utc() + timedelta(seconds=delay)
        _event(db, payout, "payout_retry_scheduled",
               f"PayCore {res.error or res.status_code} on attempt {payout.attempts}/{s.payout_max_attempts}; "
               f"retrying in {delay:g}s with the same idempotency key",
               attempt=payout.attempts, http_status=res.status_code, retry_in_s=delay)
    elif res.transient:
        _mark_failed(db, payout, "retries_exhausted",
                     f"Gave up after {payout.attempts} attempts ({res.error_code or res.error})", "worker",
                     http_status=res.status_code)
    else:
        _mark_failed(db, payout, res.error_code or f"http_{res.status_code}",
                     (res.body.get("error") or {}).get("message") or res.error or "Declined by provider", "provider response",
                     http_status=res.status_code)
    db.commit()
    return payout


def run_due(db: Session, limit: int = 25) -> int:
    ids = db.scalars(
        select(Payout.id).where(Payout.status.in_(DUE), Payout.next_attempt_at <= now_utc())
        .order_by(Payout.next_attempt_at).limit(limit)
    ).all()
    db.rollback()
    for pid in ids:
        try:
            attempt(db, pid)
        except Exception:  # noqa: BLE001 - one bad payout must not stall the queue
            db.rollback()
            log.exception("payout attempt failed", extra={"fields": {"payout_id": pid}})
    return len(ids)


def manual_retry(db: Session, payout: Payout, actor: User) -> None:
    """Admin re-drive of a failed payout.

    After a definitive decline the provider has the old key bound to that decline, so a new key is
    used. After `retries_exhausted` the provider may have processed a request whose response was
    lost, so the original key is kept and a duplicate refund is impossible."""
    payout.manual_retries += 1
    if payout.failure_code != "retries_exhausted":
        payout.idempotency_key = f"{base_key(payout.refund)}-r{payout.manual_retries}"
    previous = payout.failure_code
    payout.status = PayoutStatus.queued
    payout.attempts = 0
    payout.failure_code = None
    payout.last_error = None
    payout.next_attempt_at = now_utc()
    _event(db, payout, "payout_retry_requested", f"Re-queued by {actor.name} after {previous}", actor,
           idempotency_key=payout.idempotency_key, previous_failure=previous)


# ---------------------------------------------------------------- webhooks

def verify_signature(payload: bytes, header: str | None, secret: str, tolerance_s: int, now: float | None = None) -> None:
    if not header:
        raise WebhookSignatureError("missing PayCore-Signature header")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise WebhookSignatureError("malformed signature header") from exc
    if abs((now or time.time()) - ts) > tolerance_s:
        raise WebhookSignatureError("signature timestamp outside tolerance (possible replay)")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise WebhookSignatureError("signature mismatch")


def apply_provider_state(db: Session, payout: Payout, provider: dict, source: str) -> bool:
    """Move a payout to the provider's terminal state. Returns False if nothing changed."""
    status = provider.get("status")
    if payout.status in TERMINAL or status not in ("succeeded", "failed"):
        return False
    payout.provider_refund_id = payout.provider_refund_id or provider.get("id")
    if status == "succeeded":
        _mark_succeeded(db, payout, source)
    else:
        _mark_failed(db, payout, provider.get("failure_code") or "provider_failed",
                     "Refund failed at provider after it was accepted", source)
    return True


def handle_webhook(db: Session, payload: bytes, signature: str | None) -> dict:
    s = get_settings()
    verify_signature(payload, signature, s.payments_webhook_secret, s.payments_webhook_tolerance_s)
    event = json.loads(payload)
    event_id, event_type, data = event.get("id"), event.get("type", ""), event.get("data") or {}
    if not event_id:
        raise WebhookSignatureError("event id missing")
    if db.scalar(select(ProviderWebhookEvent.id).where(ProviderWebhookEvent.event_id == event_id)):
        return {"received": True, "duplicate": True}
    payout = db.scalars(
        select(Payout).where(Payout.provider_refund_id == data.get("id")).with_for_update(of=Payout)
    ).unique().first() if data.get("id") else None
    if payout is None and (ref := (data.get("metadata") or {}).get("refund_reference")):
        payout = db.scalars(
            select(Payout).join(RefundRequest).where(RefundRequest.reference == ref).with_for_update(of=Payout)
        ).unique().first()
    if payout is None:
        outcome = "unknown_refund"
    elif event_type in ("refund.succeeded", "refund.failed"):
        outcome = "applied" if apply_provider_state(db, payout, data, "webhook") else "ignored_stale"
    else:
        outcome = "ignored_type"
    db.add(ProviderWebhookEvent(event_id=event_id, event_type=event_type, provider_refund_id=data.get("id"),
                                outcome=outcome, payload=event))
    audit(db, None, None, "webhook.paycore", "refund", payout.refund_id if payout else None,
          event_id=event_id, event_type=event_type, outcome=outcome)
    db.commit()
    return {"received": True, "outcome": outcome}


# ---------------------------------------------------------------- reconciliation

def reconcile(db: Session, actor: User | None = None) -> dict:
    """Compare every in-flight payout with the provider's view and fix drift (e.g. a lost webhook)."""
    in_flight = db.scalars(
        select(Payout).where(Payout.status == PayoutStatus.processing, Payout.provider_refund_id.is_not(None))
    ).unique().all()
    changed, errors = [], []
    for payout in in_flight:
        res = payments_client.get_refund(payout.provider_refund_id)
        if not res.ok:
            errors.append({"reference": payout.refund.reference, "error": res.error})
            continue
        before = payout.status.value
        if apply_provider_state(db, payout, res.body, "reconciliation"):
            changed.append({"reference": payout.refund.reference, "from": before, "to": payout.status.value})
    audit(db, None, actor, "payout.reconcile", checked=len(in_flight), changed=len(changed), errors=len(errors))
    db.commit()
    return {"checked": len(in_flight), "changed": changed, "errors": errors}


# ---------------------------------------------------------------- worker

class PayoutWorker:
    """Background loop that drives due payouts (one per API process; row locks make several safe)."""

    def __init__(self, session_factory, interval_s: float):
        self._session_factory = session_factory
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="payout-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._session_factory() as db:
                    run_due(db)
            except Exception:  # noqa: BLE001 - e.g. DB briefly unavailable; try again next tick
                log.exception("payout worker tick failed")
            self._stop.wait(self._interval_s)
