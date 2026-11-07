import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .tracing import current_trace_id

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    reviewer = "reviewer"
    viewer = "viewer"


class RefundStatus(str, enum.Enum):
    pending = "pending"
    escalated = "escalated"
    approved = "approved"
    rejected = "rejected"


class RefundReason(str, enum.Enum):
    duplicate_charge = "duplicate_charge"
    service_not_received = "service_not_received"
    fraud_suspected = "fraud_suspected"
    product_defect = "product_defect"
    billing_error = "billing_error"
    goodwill = "goodwill"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), index=True)
    # Max amount this user may approve on a single refund.
    approval_limit_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Max total amount this user may approve per UTC day.
    daily_approval_limit_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("approval_limit_cents >= 0", name="ck_users_limit_nonneg"),
        CheckConstraint("daily_approval_limit_cents >= 0", name="ck_users_daily_nonneg"),
    )


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefundRequest(Base):
    __tablename__ = "refund_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_trace_id: Mapped[str | None] = mapped_column(String(64), index=True, default=current_trace_id)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    reason: Mapped[RefundReason] = mapped_column(Enum(RefundReason, name="refund_reason"))
    description: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="refund_status"), default=RefundStatus.pending, index=True
    )
    requested_by_email: Mapped[str] = mapped_column(String(255))
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    escalated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer] = relationship(lazy="joined")
    assigned_to: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id], lazy="joined")
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id], lazy="joined")
    escalated_by: Mapped[User | None] = relationship(foreign_keys=[escalated_by_id], lazy="joined")
    payout: Mapped["Payout | None"] = relationship(back_populates="refund", lazy="selectin", uselist=False)

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refund_amount_positive"),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="ck_refund_risk_range"),
        Index("ix_refund_status_assignee", "status", "assigned_to_id"),
    )


class RefundEvent(Base):
    """Business lifecycle of a refund (one row per state change / note)."""

    __tablename__ = "refund_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    refund_id: Mapped[int] = mapped_column(ForeignKey("refund_requests.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, default=current_trace_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    actor: Mapped[User | None] = relationship(lazy="joined")


class AuditLog(Base):
    """Append-only security log: every privileged read, write and denial."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, default=current_trace_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PolicySetting(Base):
    """Org-wide guardrails, editable by admins (every change is audited)."""

    __tablename__ = "policy_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RequestTrace(Base):
    """One row per API request, keyed by the trace id returned in X-Trace-Id."""

    __tablename__ = "request_traces"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(255), index=True)
    query: Mapped[str | None] = mapped_column(String(1000))
    status_code: Mapped[int] = mapped_column(Integer, index=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    actor_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SupportRequest(Base):
    """User-reported problems and captured server errors; each can be handed to Devin to fix."""

    __tablename__ = "support_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # "support" | "error"
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(255), index=True)
    reporter_email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    devin_session_id: Mapped[str | None] = mapped_column(String(64))
    devin_session_url: Mapped[str | None] = mapped_column(String(255))
    remediation_error: Mapped[str | None] = mapped_column(Text)
    created_trace_id: Mapped[str | None] = mapped_column(String(64), default=current_trace_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PayoutStatus(str, enum.Enum):
    queued = "queued"  # approved; waiting for the worker to call the provider
    retry_scheduled = "retry_scheduled"  # transient provider error; retried with backoff
    processing = "processing"  # accepted by the provider, waiting for the settlement webhook
    succeeded = "succeeded"
    failed = "failed"


class Payout(Base):
    """The money movement for an approved refund at the payments provider (one per refund).

    Written in the same transaction as the approval (transactional outbox), then driven by the
    payout worker, provider webhooks and reconciliation."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    refund_id: Mapped[int] = mapped_column(ForeignKey("refund_requests.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="paycore")
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status"), default=PayoutStatus.queued, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    manual_retries: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_trace_id: Mapped[str | None] = mapped_column(String(64), default=current_trace_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    refund: Mapped[RefundRequest] = relationship(back_populates="payout", lazy="joined")

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payout_amount_positive"),
        CheckConstraint("attempts >= 0", name="ck_payout_attempts_nonneg"),
    )


class ProviderWebhookEvent(Base):
    """Every signature-verified provider webhook, keyed by event id so redeliveries are no-ops."""

    __tablename__ = "provider_webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="paycore")
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32))  # applied | ignored_stale | unknown_refund
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=current_trace_id)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
