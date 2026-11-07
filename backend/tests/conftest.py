import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ON_STARTUP"] = "false"
os.environ["DEVIN_API_KEY"] = ""  # never call the real Devin API from tests
os.environ["REMEDIATION_WORKER_ENABLED"] = "false"  # tests drain the queue explicitly
os.environ["PAYOUT_WORKER_ENABLED"] = "false"  # tests drive payouts explicitly
os.environ["PAYMENTS_WEBHOOK_SECRET"] = "whsec_test"

import pytest
from fastapi.testclient import TestClient

from app import payments_client
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Customer, RefundReason, RefundRequest, RefundStatus, User
from app.security import create_token
from app.seed import seed
from app.services import now_utc


class FakePayCore:
    """In-process stand-in for the provider: idempotent, scriptable failures, no network."""

    def __init__(self):
        self.refunds: dict[str, dict] = {}
        self.keys: dict[str, payments_client.ProviderResponse] = {}
        self.script: list[int | None] = []  # status codes to return before succeeding; None => timeout
        self.calls: list[str] = []

    def create_refund(self, idempotency_key, charge_id, amount_cents, currency, metadata):
        self.calls.append(idempotency_key)
        if idempotency_key in self.keys:
            prev = self.keys[idempotency_key]
            body = self.refunds.get(prev.body.get("id"), prev.body)
            return payments_client.ProviderResponse(prev.status_code, body, prev.error, replayed=True)
        if self.script:
            code = self.script.pop(0)
            if code == 402:
                res = payments_client.ProviderResponse(402, {"error": {"code": "insufficient_merchant_balance",
                                                                        "message": "Merchant balance too low"}}, "HTTP 402")
                self.keys[idempotency_key] = res
                return res
            if code == "lost":  # created at the provider, response lost
                self._create(idempotency_key, charge_id, amount_cents, currency, metadata)
                return payments_client.ProviderResponse(504, {}, "HTTP 504")
            return payments_client.ProviderResponse(code, {}, f"HTTP {code}" if code else "ConnectTimeout")
        return self._create(idempotency_key, charge_id, amount_cents, currency, metadata)

    def _create(self, key, charge_id, amount_cents, currency, metadata):
        rid = f"re_test{len(self.refunds) + 1}"
        self.refunds[rid] = {"id": rid, "status": "pending", "charge_id": charge_id, "amount_cents": amount_cents,
                             "currency": currency, "metadata": metadata, "failure_code": None}
        res = payments_client.ProviderResponse(201, dict(self.refunds[rid]))
        self.keys[key] = res
        return res

    def get_refund(self, rid):
        if rid not in self.refunds:
            return payments_client.ProviderResponse(404, {}, "HTTP 404")
        return payments_client.ProviderResponse(200, dict(self.refunds[rid]))


@pytest.fixture(autouse=True)
def paycore(monkeypatch):
    fake = FakePayCore()
    monkeypatch.setattr(payments_client, "create_refund", fake.create_refund)
    monkeypatch.setattr(payments_client, "get_refund", fake.get_refund)
    return fake


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    seed(s)
    yield s
    s.close()


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def user(db, email) -> User:
    return db.query(User).filter_by(email=email).one()


def auth(u: User) -> dict:
    return {"Authorization": f"Bearer {create_token(u)}"}


def make_refund(db, assignee: User | None, amount=10_000, risk=10, status=RefundStatus.pending,
                requested_by="support.amy@acmepay.com", customer=None):
    from datetime import timedelta
    customer = customer or Customer(external_id=f"cus_t{now_utc().timestamp()}", name="Test Person",
                                    email="t@example.com", country="US")
    db.add(customer)
    db.flush()
    r = RefundRequest(reference=f"RF-T{now_utc().timestamp()}", customer_id=customer.id, order_id="ord_1",
                      amount_cents=amount, reason=RefundReason.billing_error, risk_score=risk, status=status,
                      requested_by_email=requested_by, assigned_to_id=assignee.id if assignee else None,
                      sla_due_at=now_utc() + timedelta(hours=48))
    db.add(r)
    db.commit()
    return r
