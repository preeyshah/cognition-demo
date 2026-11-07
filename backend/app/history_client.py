"""Client for the third-party customer-history provider (e.g. the payments
processor / core-banking API). Kept behind one function so it can be swapped
for Stripe, Adyen, Marqeta, etc."""

import httpx
from fastapi import HTTPException

from .config import get_settings


def fetch_customer_history(external_id: str) -> dict:
    s = get_settings()
    try:
        resp = httpx.get(
            f"{s.history_provider_url}/v1/customers/{external_id}/history",
            headers={"X-API-Key": s.history_provider_api_key},
            timeout=s.history_provider_timeout_s,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"History provider unreachable: {exc.__class__.__name__}") from exc
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Customer not found at provider")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"History provider error ({resp.status_code})")
    return resp.json()
