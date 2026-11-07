"""Idempotent demo data: users for every role, customers, ~150 refunds with a
realistic lifecycle (events included) spread over the last 30 days."""

import random
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Customer,
    Payout,
    PayoutStatus,
    PolicySetting,
    RefundEvent,
    RefundReason,
    RefundRequest,
    RefundStatus,
    Role,
    User,
)
from .policy import DEFAULT_POLICIES
from .services import now_utc

USERS = [
    # email, name, role, per-refund limit ($), daily limit ($)
    ("priya.admin@acmepay.com", "Priya Shah", Role.admin, 10_000, 100_000),
    ("alex.admin@acmepay.com", "Alex Rivera", Role.admin, 10_000, 100_000),
    ("jordan.lee@acmepay.com", "Jordan Lee", Role.reviewer, 500, 5_000),
    ("sam.patel@acmepay.com", "Sam Patel", Role.reviewer, 1_000, 10_000),
    ("maria.gomez@acmepay.com", "Maria Gomez", Role.reviewer, 2_500, 20_000),
    ("vik.auditor@acmepay.com", "Vik Mehta", Role.viewer, 0, 0),
]

FIRST = ["Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Sophia", "Mason", "Isabella", "Lucas",
         "Mia", "Arjun", "Chen", "Fatima", "Diego", "Hana", "Kwame", "Lena", "Omar", "Zoe"]
LAST = ["Johnson", "Nguyen", "Garcia", "Kim", "Okafor", "Müller", "Rossi", "Silva", "Cohen", "Tanaka",
        "Brown", "Singh", "Ali", "Dubois", "Novak"]
COUNTRIES = ["US", "US", "US", "GB", "CA", "DE", "IN", "BR", "SG"]
AGENTS = ["support.amy@acmepay.com", "support.ben@acmepay.com", "support.cara@acmepay.com", "chargeback-bot@acmepay.com"]
NOTES = {
    RefundReason.duplicate_charge: "Customer charged twice for the same order.",
    RefundReason.service_not_received: "Customer reports transfer never arrived.",
    RefundReason.fraud_suspected: "Customer disputes transaction as unauthorised.",
    RefundReason.product_defect: "Card replacement fee for a defective card.",
    RefundReason.billing_error: "FX fee applied incorrectly on international payment.",
    RefundReason.goodwill: "Goodwill credit after a long support wait.",
}


def _amount(rng: random.Random) -> int:
    r = rng.random()
    if r < 0.55:
        return rng.randint(1_000, 30_000)
    if r < 0.85:
        return rng.randint(30_000, 120_000)
    if r < 0.97:
        return rng.randint(120_000, 240_000)
    return rng.randint(260_000, 650_000)


def seed(db: Session) -> None:
    for key, (value, desc) in DEFAULT_POLICIES.items():
        if db.get(PolicySetting, key) is None:
            db.add(PolicySetting(key=key, value=value, description=desc))
    db.flush()
    if db.scalar(select(User.id).limit(1)):
        db.commit()
        return

    rng = random.Random(42)
    now = now_utc()
    users = [
        User(email=e, name=n, role=r, approval_limit_cents=lim * 100, daily_approval_limit_cents=d * 100)
        for e, n, r, lim, d in USERS
    ]
    db.add_all(users)
    customers = []
    for i in range(30):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        customers.append(Customer(
            external_id=f"cus_{1000 + i}", name=f"{first} {last}",
            email=f"{first.lower()}.{last.lower().replace('ü', 'u')}{i}@example.com",
            country=rng.choice(COUNTRIES), created_at=now - timedelta(days=rng.randint(60, 900)),
        ))
    db.add_all(customers)
    db.flush()

    admins = [u for u in users if u.role == Role.admin]
    reviewers = [u for u in users if u.role == Role.reviewer]
    max_reviewer = max(u.approval_limit_cents for u in reviewers)
    sla = timedelta(hours=DEFAULT_POLICIES["sla_hours"][0])
    counter = [1001]

    def make(created, customer, amount, risk, reason, assignee=None, force_open=False, escalate=False):
        r = RefundRequest(
            reference=f"RF-{counter[0]}", customer_id=customer.id, order_id=f"ord_{rng.randint(100000, 999999)}",
            amount_cents=amount, currency="USD", reason=reason, description=NOTES[reason], risk_score=risk,
            requested_by_email=rng.choice(AGENTS), sla_due_at=created + sla, created_at=created, updated_at=created,
        )
        counter[0] += 1
        events = []
        auto = risk >= DEFAULT_POLICIES["high_risk_threshold"][0] or amount > max_reviewer
        if auto:
            r.status = RefundStatus.escalated
            r.escalation_reason = f"Auto-escalated: risk score {risk}" if risk >= 70 else "Auto-escalated: amount above every reviewer's approval limit"
            events.append(("created", None, None, "escalated", r.escalation_reason, created))
        else:
            r.assigned_to_id = (assignee or rng.choice(reviewers)).id
            events.append(("created", None, None, "pending", None, created))
        age_h = (now - created).total_seconds() / 3600
        open_ = force_open or (age_h < 30 and rng.random() < 0.8)
        t = created + timedelta(hours=rng.uniform(0.5, min(40, max(age_h - 0.2, 0.6))))
        if (escalate or not open_) and not auto:
            reviewer = next(u for u in reviewers if u.id == r.assigned_to_id)
            if escalate or amount > reviewer.approval_limit_cents or rng.random() < 0.07:
                r.status = RefundStatus.escalated
                r.escalated_by_id = reviewer.id
                r.escalation_reason = "Above my approval limit, needs admin sign-off." if amount > reviewer.approval_limit_cents else "Customer has prior chargebacks, second opinion please."
                r.assigned_to_id = None
                events.append(("escalated", reviewer.id, "pending", "escalated", r.escalation_reason, t))
                t = min(t + timedelta(hours=rng.uniform(1, 20)), now - timedelta(minutes=5))
            else:
                approve = rng.random() < (0.82 if risk < 40 else 0.6)
                r.status = RefundStatus.approved if approve else RefundStatus.rejected
                r.decided_by_id, r.decided_at = reviewer.id, t
                r.decision_note = "Verified against transaction log." if approve else "No evidence of duplicate charge in ledger."
                events.append((r.status.value, reviewer.id, "pending", r.status.value, r.decision_note, t))
        if r.status == RefundStatus.escalated and not open_ and (now - t).total_seconds() > 3600 * 6:
            admin = rng.choice(admins)
            approve = rng.random() < 0.55
            r.status = RefundStatus.approved if approve else RefundStatus.rejected
            r.decided_by_id, r.decided_at = admin.id, t
            r.decision_note = "Approved after reviewing provider history." if approve else "Rejected: pattern of repeat claims on this account."
            events.append((r.status.value, admin.id, "escalated", r.status.value, r.decision_note, t))
        db.add(r)
        db.flush()
        if r.status == RefundStatus.approved:
            paid_at = min(t + timedelta(minutes=2 + r.id % 25), now - timedelta(minutes=1))  # keep rng sequence stable
            db.add(Payout(refund_id=r.id, idempotency_key=f"refund-{r.reference}", provider_refund_id=f"re_seed{r.id:05d}",
                          amount_cents=amount, currency="USD", status=PayoutStatus.succeeded, attempts=1,
                          last_http_status=201, succeeded_at=paid_at, created_at=t, updated_at=paid_at))
            events.append(("payout_succeeded", None, r.status.value, r.status.value,
                           "Money returned to customer (confirmed by webhook)", paid_at))
        for etype, actor, frm, to, note, ts in events:
            db.add(RefundEvent(refund_id=r.id, actor_id=actor, event_type=etype, from_status=frm, to_status=to,
                               note=note, data={"amount_cents": amount}, created_at=ts))
        return r

    for _ in range(140):
        created = now - timedelta(days=rng.uniform(0, 30), hours=rng.uniform(0, 3))
        make(created, rng.choice(customers), _amount(rng), min(int(rng.betavariate(2, 5) * 100), 99), rng.choice(list(RefundReason)))

    # Guarantee each reviewer an interesting open queue for the demo.
    for reviewer in reviewers:
        for _ in range(6):
            created = now - timedelta(hours=rng.uniform(1, 30))
            amount = rng.randint(2_000, reviewer.approval_limit_cents)
            make(created, rng.choice(customers), amount, rng.randint(5, 60), rng.choice(list(RefundReason)), reviewer, True)
        # Above this reviewer's own limit (must escalate) and an overdue one.
        if reviewer.approval_limit_cents < max_reviewer:
            make(now - timedelta(hours=5), rng.choice(customers), reviewer.approval_limit_cents + 35_000, 25,
                 RefundReason.billing_error, reviewer, True)
        make(now - timedelta(hours=60), rng.choice(customers), rng.randint(2_000, 20_000), 30,
             RefundReason.service_not_received, reviewer, True)
        # Something this reviewer already sent up to an admin.
        make(now - timedelta(hours=rng.uniform(3, 20)), rng.choice(customers), rng.randint(5_000, reviewer.approval_limit_cents),
             rng.randint(20, 60), rng.choice(list(RefundReason)), reviewer, True, escalate=True)
    # Open auto-escalations awaiting an admin: high risk and above every reviewer's limit.
    for hours, amount, risk in [(2, 42_000, 84), (9, 18_500, 77), (14, 380_000, 35), (26, 615_000, 52)]:
        make(now - timedelta(hours=hours), rng.choice(customers), amount, risk, rng.choice(list(RefundReason)), force_open=True)
    # One payout the provider bounced after accepting it, so the Payouts page has something to re-drive.
    db.flush()
    failed = db.scalars(select(Payout).order_by(Payout.succeeded_at.desc()).limit(1)).first()
    if failed:
        failed.status, failed.succeeded_at = PayoutStatus.failed, None
        failed.failure_code, failed.last_error = "account_closed", "Refund failed at provider after it was accepted"
        db.query(RefundEvent).filter_by(refund_id=failed.refund_id, event_type="payout_succeeded").delete()
        db.add(RefundEvent(refund_id=failed.refund_id, event_type="payout_failed", from_status="approved",
                           to_status="approved", note="Payout failed: account_closed — customer's account is closed",
                           data={"failure_code": "account_closed"}, created_at=failed.updated_at))
    db.commit()
