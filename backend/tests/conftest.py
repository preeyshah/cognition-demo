import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_ON_STARTUP"] = "false"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Customer, RefundReason, RefundRequest, RefundStatus, User
from app.security import create_token
from app.seed import seed
from app.services import now_utc


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
