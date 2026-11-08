"""Stand-in for a third-party payments / core-banking API (Stripe/Adyen-style).

GET  /v1/customers/{external_id}/history  customer history (deterministic fake data)
POST /v1/refunds                          issue a refund; `Idempotency-Key` header required
GET  /v1/refunds/{id}                     refund status (used for reconciliation)
POST /v1/test-helpers/sandbox             inject failures: outage, decline, async_fail, lost_response, drop_webhook

Refunds settle asynchronously: after PAYMENTS_SETTLE_DELAY_S a signed `refund.succeeded` /
`refund.failed` webhook is POSTed to PAYMENTS_WEBHOOK_URL with header
`PayCore-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`.
All calls require the X-API-Key header.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

API_KEY = os.getenv("PROVIDER_API_KEY", "demo-provider-key")
WEBHOOK_URL = os.getenv("PAYMENTS_WEBHOOK_URL", "http://localhost:8000/webhooks/paycore")
WEBHOOK_SECRET = os.getenv("PAYMENTS_WEBHOOK_SECRET", "whsec_demo_only")
SETTLE_DELAY_S = float(os.getenv("PAYMENTS_SETTLE_DELAY_S", "4"))
app = FastAPI(title="PayCore (mock third-party provider)")
log = logging.getLogger("paycore")

MERCHANTS = ["Amazon", "Uber", "Netflix", "Shell", "Whole Foods", "Apple", "Delta", "Airbnb", "Spotify", "Target"]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/customers/{external_id}/history")
def history(external_id: str, x_api_key: str | None = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")
    if external_id == "cus_unknown":
        raise HTTPException(status_code=404, detail="not found")
    seed = int(hashlib.sha256(external_id.encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    time.sleep(rng.uniform(0.15, 0.4))
    now = datetime.now(timezone.utc)
    txns = []
    for i in range(rng.randint(6, 12)):
        ts = now - timedelta(days=rng.randint(0, 120), hours=rng.randint(0, 23))
        txns.append(
            {
                "id": f"txn_{seed % 10000:04d}{i:02d}",
                "date": ts.isoformat(),
                "merchant": rng.choice(MERCHANTS),
                "amount_cents": rng.randint(500, 60000),
                "status": rng.choices(["settled", "refunded", "disputed"], [85, 10, 5])[0],
            }
        )
    txns.sort(key=lambda t: t["date"], reverse=True)
    chargebacks = sum(1 for t in txns if t["status"] == "disputed")
    flags = []
    if chargebacks:
        flags.append("prior_chargeback")
    if rng.random() < 0.2:
        flags.append("new_device_login")
    if rng.random() < 0.1:
        flags.append("address_mismatch")
    return {
        "provider": "PayCore",
        "customer_id": external_id,
        "account_opened": (now - timedelta(days=rng.randint(30, 2000))).date().isoformat(),
        "kyc_status": rng.choices(["verified", "pending", "enhanced_due_diligence"], [80, 10, 10])[0],
        "lifetime_spend_cents": sum(t["amount_cents"] for t in txns) * rng.randint(3, 12),
        "lifetime_refunds": sum(1 for t in txns if t["status"] == "refunded") + rng.randint(0, 3),
        "chargebacks_12m": chargebacks,
        "risk_flags": flags,
        "recent_transactions": txns,
    }


# ---------------------------------------------------------------- payments API

SandboxMode = Literal["outage", "decline", "async_fail", "lost_response", "drop_webhook", "flaky_first"]
REFUNDS: dict[str, dict] = {}
IDEMPOTENCY: dict[str, tuple[str, int, dict]] = {}  # key -> (request hash, status code, body)
ATTEMPTS: dict[str, int] = {}
SANDBOX: dict[str, int] = {"outage": 0, "decline": 0, "async_fail": 0, "lost_response": 0, "drop_webhook": 0,
                  # armed high by default: every refund fails its first attempt once, then succeeds on retry
                  "flaky_first": 1_000_000}
_tasks: set[asyncio.Task] = set()


def _auth(x_api_key: str | None) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _take(mode: str) -> bool:
    if SANDBOX[mode] > 0:
        SANDBOX[mode] -= 1
        return True
    return False


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


class RefundIn(BaseModel):
    charge_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    metadata: dict[str, str] = {}


async def _deliver(event: dict) -> None:
    body = json.dumps(event).encode()
    # Like real PSPs, retry delivery with backoff until the receiver acknowledges with a 2xx.
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.post(WEBHOOK_URL, content=body, headers={
                    "Content-Type": "application/json", "PayCore-Signature": sign(body, WEBHOOK_SECRET)})
            if res.status_code < 300:
                return
            log.warning("webhook %s rejected: %s", event["id"], res.status_code)
        except httpx.HTTPError as exc:
            log.warning("webhook %s delivery failed: %s", event["id"], exc)
        await asyncio.sleep(2 ** attempt)


async def _settle(refund_id: str, fail: bool, drop_webhook: bool) -> None:
    await asyncio.sleep(SETTLE_DELAY_S)
    refund = REFUNDS[refund_id]
    refund["status"] = "failed" if fail else "succeeded"
    if fail:
        refund["failure_code"] = "account_closed"
    refund["updated"] = int(time.time())
    if drop_webhook:
        return  # the receiver must notice via reconciliation (GET /v1/refunds/{id})
    await _deliver({"id": f"evt_{uuid.uuid4().hex[:24]}", "type": f"refund.{refund['status']}",
                    "created": int(time.time()), "data": dict(refund)})


@app.post("/v1/refunds")
async def create_refund(
    body: RefundIn,
    x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    _auth(x_api_key)
    if not idempotency_key:
        raise HTTPException(status_code=400, detail={"code": "idempotency_key_required"})
    if _take("outage"):
        return JSONResponse({"error": {"code": "service_unavailable", "message": "PayCore is temporarily unavailable"}},
                            status_code=503)
    # Demo: first attempt per Idempotency-Key always fails once; the client retry (same key) succeeds.
    if ATTEMPTS.get(idempotency_key, 0) == 0 and _take("flaky_first"):
        ATTEMPTS[idempotency_key] = 1
        return JSONResponse({"error": {"code": "service_unavailable", "message": "PayCore is temporarily unavailable"}},
                            status_code=503)
    ATTEMPTS[idempotency_key] = ATTEMPTS.get(idempotency_key, 0) + 1
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    if idempotency_key in IDEMPOTENCY:
        seen_hash, code, stored = IDEMPOTENCY[idempotency_key]
        if seen_hash != fingerprint:
            return JSONResponse({"error": {"code": "idempotency_conflict",
                                           "message": "Key reused with different parameters"}}, status_code=409)
        current = REFUNDS.get(stored.get("id", ""), stored)
        return JSONResponse(current, status_code=code, headers={"Idempotent-Replayed": "true"})
    if _take("decline"):
        err = {"error": {"code": "insufficient_merchant_balance", "message": "Merchant balance cannot cover this refund"}}
        IDEMPOTENCY[idempotency_key] = (fingerprint, 402, err)
        return JSONResponse(err, status_code=402)
    refund = {
        "id": f"re_{uuid.uuid4().hex[:20]}", "object": "refund", "status": "pending", "charge_id": body.charge_id,
        "amount_cents": body.amount_cents, "currency": body.currency.upper(), "metadata": body.metadata,
        "failure_code": None, "created": int(time.time()), "updated": int(time.time()),
    }
    REFUNDS[refund["id"]] = refund
    IDEMPOTENCY[idempotency_key] = (fingerprint, 201, refund)
    task = asyncio.create_task(_settle(refund["id"], _take("async_fail"), _take("drop_webhook")))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    if _take("lost_response"):
        # The refund was created, but the caller never sees the answer (e.g. a gateway timeout).
        return JSONResponse({"error": {"code": "gateway_timeout"}}, status_code=504)
    return JSONResponse(refund, status_code=201)


@app.get("/v1/refunds/{refund_id}")
def get_refund(refund_id: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    if refund_id not in REFUNDS:
        raise HTTPException(status_code=404, detail="not found")
    return REFUNDS[refund_id]


class SandboxIn(BaseModel):
    mode: SandboxMode
    count: int = Field(default=1, ge=0, le=20)


@app.get("/v1/test-helpers/sandbox")
def sandbox_state(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return SANDBOX


@app.post("/v1/test-helpers/sandbox")
def set_sandbox(body: SandboxIn, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    SANDBOX[body.mode] = body.count
    return SANDBOX
