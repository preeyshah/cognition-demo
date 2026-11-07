"""Pure authorization + guardrail rules for refunds.

Everything that decides *who may see or do what* lives here so it can be unit
tested in isolation and reused by every endpoint (and by the UI, which receives
the computed `permissions` for each refund).
"""

from dataclasses import dataclass, field

from .models import RefundRequest, RefundStatus, Role, User

DEFAULT_POLICIES: dict[str, tuple[int, str]] = {
    "max_refund_cents": (1_000_000, "Hard cap: no single refund above this can be approved by anyone."),
    "high_risk_threshold": (70, "Risk score at/above which reviewers must escalate to an admin."),
    "customer_30d_cap_cents": (500_000, "Max approved refunds per customer in 30 days before admin sign-off is required."),
    "min_note_length": (10, "Minimum characters required in a rejection / escalation note."),
    "sla_hours": (48, "Hours a refund may wait before it is considered overdue."),
}


@dataclass
class PolicyContext:
    policies: dict[str, int]
    approved_today_cents: int = 0
    customer_30d_approved_cents: int = 0

    def get(self, key: str) -> int:
        return self.policies.get(key, DEFAULT_POLICIES[key][0])


@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        return "; ".join(self.reasons)


TERMINAL = {RefundStatus.approved, RefundStatus.rejected}


def can_view(user: User, refund: RefundRequest) -> bool:
    if not user.is_active:
        return False
    if user.role in (Role.admin, Role.viewer):
        return True
    return user.id in (refund.assigned_to_id, refund.escalated_by_id, refund.decided_by_id)


def can_view_pii(user: User) -> bool:
    return user.role in (Role.admin, Role.reviewer)


def _base_action_checks(user: User, refund: RefundRequest) -> list[str]:
    reasons: list[str] = []
    if not user.is_active:
        return ["User is deactivated"]
    if user.role == Role.viewer:
        return ["Viewers have read-only access"]
    if refund.status in TERMINAL:
        return [f"Refund is already {refund.status.value}"]
    if user.role == Role.reviewer:
        if refund.assigned_to_id != user.id:
            reasons.append("Refund is not assigned to you")
        if refund.status != RefundStatus.pending:
            reasons.append("Escalated refunds can only be decided by an admin")
    if refund.requested_by_email.lower() == user.email.lower():
        reasons.append("Four-eyes rule: you cannot decide a refund you requested")
    return reasons


def check_approve(user: User, refund: RefundRequest, ctx: PolicyContext) -> Decision:
    reasons = _base_action_checks(user, refund)
    if reasons:
        return Decision(False, reasons)
    if refund.amount_cents > ctx.get("max_refund_cents"):
        reasons.append("Amount exceeds the organisation hard cap")
    if refund.amount_cents > user.approval_limit_cents:
        reasons.append("Amount exceeds your per-refund approval limit")
    if ctx.approved_today_cents + refund.amount_cents > user.daily_approval_limit_cents:
        reasons.append("Approval would exceed your daily approval limit")
    if user.role == Role.reviewer:
        if refund.risk_score >= ctx.get("high_risk_threshold"):
            reasons.append("High-risk refund: must be escalated to an admin")
        if ctx.customer_30d_approved_cents + refund.amount_cents > ctx.get("customer_30d_cap_cents"):
            reasons.append("Customer 30-day refund cap reached: admin sign-off required")
    return Decision(not reasons, reasons)


def check_reject(user: User, refund: RefundRequest, ctx: PolicyContext, note: str | None) -> Decision:
    reasons = _base_action_checks(user, refund)
    if not reasons and len((note or "").strip()) < ctx.get("min_note_length"):
        reasons.append(f"A rejection note of at least {ctx.get('min_note_length')} characters is required")
    return Decision(not reasons, reasons)


def check_escalate(user: User, refund: RefundRequest, ctx: PolicyContext, note: str | None) -> Decision:
    if user.role != Role.reviewer:
        return Decision(False, ["Only reviewers can escalate"])
    reasons = _base_action_checks(user, refund)
    if not reasons and len((note or "").strip()) < ctx.get("min_note_length"):
        reasons.append(f"An escalation reason of at least {ctx.get('min_note_length')} characters is required")
    return Decision(not reasons, reasons)


def check_view_history(user: User, refund: RefundRequest) -> Decision:
    if user.role == Role.viewer:
        return Decision(False, ["Viewers cannot access customer PII / history"])
    if not can_view(user, refund):
        return Decision(False, ["You do not have access to this refund"])
    if user.role == Role.reviewer and refund.status in TERMINAL and refund.assigned_to_id != user.id:
        return Decision(False, ["History is only available on refunds you are working"])
    return Decision(True)


def needs_auto_escalation(amount_cents: int, risk_score: int, policies: dict[str, int], max_reviewer_limit: int) -> str | None:
    if risk_score >= policies.get("high_risk_threshold", DEFAULT_POLICIES["high_risk_threshold"][0]):
        return f"Auto-escalated: risk score {risk_score}"
    if amount_cents > max_reviewer_limit:
        return "Auto-escalated: amount above every reviewer's approval limit"
    return None


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def mask_name(name: str) -> str:
    parts = name.split()
    if not parts:
        return "***"
    return " ".join([parts[0]] + [p[:1] + "." for p in parts[1:]])
