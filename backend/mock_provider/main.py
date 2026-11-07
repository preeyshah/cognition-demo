"""Stand-in for a third-party payments / core-banking API.

GET /v1/customers/{external_id}/history  (header X-API-Key required)
Returns deterministic fake data per customer so demos are repeatable.
"""

import hashlib
import os
import random
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException

API_KEY = os.getenv("PROVIDER_API_KEY", "demo-provider-key")
app = FastAPI(title="PayCore (mock third-party provider)")

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
