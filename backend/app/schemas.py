from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import PayoutStatus, RefundReason, RefundStatus, Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: Role
    approval_limit_cents: int
    daily_approval_limit_cents: int
    is_active: bool
    last_login_at: datetime | None = None


class UserRef(BaseModel):
    id: int
    name: str


class CustomerOut(BaseModel):
    id: int
    external_id: str
    name: str
    email: str
    country: str
    pii_masked: bool


class Permissions(BaseModel):
    can_approve: bool
    can_reject: bool
    can_escalate: bool
    can_view_history: bool
    can_assign: bool
    can_approve_partial: bool = False
    approve_blocked_reasons: list[str] = []
    escalate_blocked_reasons: list[str] = []


class PayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    status: PayoutStatus
    amount_cents: int
    currency: str
    provider_refund_id: str | None
    idempotency_key: str
    attempts: int
    manual_retries: int
    next_attempt_at: datetime | None
    last_http_status: int | None
    last_error: str | None
    failure_code: str | None
    succeeded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RefundOut(BaseModel):
    id: int
    reference: str
    order_id: str
    amount_cents: int
    approved_amount_cents: int | None = None
    currency: str
    reason: RefundReason
    description: str
    risk_score: int
    status: RefundStatus
    requested_by_email: str
    customer: CustomerOut
    assigned_to: UserRef | None
    escalated_by: UserRef | None
    escalation_reason: str | None
    decided_by: UserRef | None
    decision_note: str | None
    decided_at: datetime | None
    sla_due_at: datetime
    overdue: bool
    created_at: datetime
    permissions: Permissions
    payout: PayoutOut | None = None


class RefundEventOut(BaseModel):
    id: int
    event_type: str
    actor: UserRef | None
    from_status: str | None
    to_status: str | None
    note: str | None
    data: dict
    created_at: datetime


class RefundDetailOut(RefundOut):
    events: list[RefundEventOut]


class RefundList(BaseModel):
    items: list[RefundOut]
    total: int


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class ApproveIn(DecisionIn):
    amount_cents: int | None = Field(default=None, gt=0)  # omit (or send the requested amount) for a full refund


class AssignIn(BaseModel):
    assignee_id: int


class RefundCreateIn(BaseModel):
    customer_external_id: str
    order_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0)
    currency: Literal["USD"] = "USD"  # limits and analytics are USD-denominated
    reason: RefundReason
    description: str = Field(default="", max_length=2000)
    risk_score: int = Field(default=0, ge=0, le=100)
    requested_by_email: str


class SandboxIn(BaseModel):
    mode: Literal["outage", "decline", "async_fail", "lost_response", "drop_webhook"]
    count: int = Field(default=1, ge=0, le=20)


class UserCreateIn(BaseModel):
    email: str
    name: str
    role: Role
    approval_limit_cents: int = Field(default=0, ge=0)
    daily_approval_limit_cents: int = Field(default=0, ge=0)


class UserUpdateIn(BaseModel):
    role: Role | None = None
    approval_limit_cents: int | None = Field(default=None, ge=0)
    daily_approval_limit_cents: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: int
    description: str
    updated_at: datetime | None


class PolicyUpdateIn(BaseModel):
    value: int = Field(ge=0)


class TokenIn(BaseModel):
    code: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
