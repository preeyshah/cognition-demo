"""Client for the payments provider's refund API (PayCore; same shape as Stripe/Adyen refunds).

Every call returns a ProviderResponse instead of raising, so the payout state machine can tell
transient failures (retry with the same idempotency key) from definitive ones."""

from dataclasses import dataclass

import httpx

from .config import get_settings


@dataclass
class ProviderResponse:
    status_code: int | None  # None => network error / timeout (the request may or may not have landed)
    body: dict
    error: str | None = None
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def transient(self) -> bool:
        return self.status_code is None or self.status_code in (408, 425, 429) or self.status_code >= 500

    @property
    def error_code(self) -> str | None:
        err = self.body.get("error") if isinstance(self.body, dict) else None
        if isinstance(err, dict):
            return err.get("code")
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        return detail.get("code") if isinstance(detail, dict) else None


def _call(method: str, path: str, **kwargs) -> ProviderResponse:
    s = get_settings()
    headers = {"X-API-Key": s.payments_provider_api_key, **kwargs.pop("headers", {})}
    try:
        res = httpx.request(method, f"{s.payments_provider_url}{path}", headers=headers,
                            timeout=s.payments_timeout_s, **kwargs)
    except httpx.HTTPError as exc:
        return ProviderResponse(None, {}, f"{exc.__class__.__name__}: provider unreachable")
    try:
        body = res.json()
    except ValueError:
        body = {}
    return ProviderResponse(res.status_code, body if isinstance(body, dict) else {"data": body},
                            None if res.is_success else f"HTTP {res.status_code}",
                            res.headers.get("Idempotent-Replayed") == "true")


def create_refund(idempotency_key: str, charge_id: str, amount_cents: int, currency: str,
                  metadata: dict[str, str]) -> ProviderResponse:
    return _call("POST", "/v1/refunds", headers={"Idempotency-Key": idempotency_key}, json={
        "charge_id": charge_id, "amount_cents": amount_cents, "currency": currency, "metadata": metadata})


def get_refund(provider_refund_id: str) -> ProviderResponse:
    return _call("GET", f"/v1/refunds/{provider_refund_id}")


def set_sandbox(mode: str, count: int) -> ProviderResponse:
    return _call("POST", "/v1/test-helpers/sandbox", json={"mode": mode, "count": count})


def get_sandbox() -> ProviderResponse:
    return _call("GET", "/v1/test-helpers/sandbox")
