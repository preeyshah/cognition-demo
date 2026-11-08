"""Compliance Center: controls-as-code mapped to the frameworks a fintech is audited against.

Each control is either *automated* (a live check against the database and running config that
returns pass / warn / fail plus the evidence it looked at) or *documented* (implemented in infra
or process; the evidence is a file in this repo). Failing controls can be handed to Devin through
the same remediation loop used for production errors.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import policy, remediation
from .audit import verify_chain
from .config import get_settings
from .db import engine
from .models import (
    AccessReview,
    AuditLog,
    DataSubjectRequest,
    RefundEvent,
    RefundRequest,
    RefundStatus,
    Role,
    SupportRequest,
    User,
)
from .services import aware, get_policies, now_utc, serialize_refund

ACTIVE_TICKET_STATUSES = ("open", "queued", "devin_working")

SOC2, PCI, SOX, GDPR = "SOC 2", "PCI DSS 4.0", "SOX ITGC", "GDPR"
FRAMEWORKS = [SOC2, PCI, SOX, GDPR]


@dataclass
class Result:
    status: str  # pass | warn | fail | documented
    summary: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Control:
    id: str
    title: str
    objective: str
    frameworks: dict[str, list[str]]
    remediation: str
    check: Callable[[Session], Result] | None = None
    evidence_files: list[str] = field(default_factory=list)


def _decided(db: Session):
    return select(RefundRequest).where(RefundRequest.status.in_([RefundStatus.approved, RefundStatus.rejected]))


def check_least_privilege(db: Session) -> Result:
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    bad = [u.email for u in users if u.role == Role.viewer and (u.approval_limit_cents or u.daily_approval_limit_cents)]
    roles = Counter(u.role.value for u in users)
    ev = {"active_users_by_role": dict(roles), "viewers_with_approval_authority": bad}
    if bad:
        return Result("fail", f"{len(bad)} read-only user(s) hold approval authority", ev)
    return Result("pass", f"{len(users)} active users, each on exactly one role; read-only roles hold no approval authority", ev)


def check_four_eyes(db: Session) -> Result:
    decided = db.scalars(_decided(db)).unique().all()
    bad = [r.reference for r in decided if r.decided_by and r.decided_by.email.lower() == r.requested_by_email.lower()]
    ev = {"decided_refunds": len(decided), "self_decided": bad[:50]}
    if bad:
        return Result("fail", f"{len(bad)} refund(s) were decided by the person who requested them", ev)
    return Result("pass", f"0 of {len(decided)} decided refunds were decided by their requester", ev)


def check_delegated_limits(db: Session) -> Result:
    cap = get_policies(db)["max_refund_cents"]
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    over_cap = [u.email for u in users if u.approval_limit_cents > cap]
    daily_lt = [u.email for u in users if u.daily_approval_limit_cents < u.approval_limit_cents]
    approved = db.scalars(_decided(db).where(RefundRequest.status == RefundStatus.approved)).unique().all()
    over_cap_refunds = [r.reference for r in approved if r.amount_cents > cap]
    ev = {"org_hard_cap_cents": cap, "users_above_hard_cap": over_cap, "daily_below_per_refund": daily_lt,
          "approved_above_hard_cap": over_cap_refunds[:50], "approved_refunds": len(approved)}
    issues = len(over_cap) + len(daily_lt) + len(over_cap_refunds)
    if issues:
        return Result("fail", f"{issues} delegated-authority exception(s)", ev)
    return Result("pass", f"All limits within the {cap / 100:,.0f} USD hard cap; {len(approved)} approvals within it", ev)


def check_high_risk_signoff(db: Session) -> Result:
    threshold = get_policies(db)["high_risk_threshold"]
    approved = db.scalars(
        _decided(db).where(RefundRequest.status == RefundStatus.approved, RefundRequest.risk_score >= threshold)
    ).unique().all()
    # Judge each approval by the approver's role when they decided (recorded on the event), not their role today.
    role_at = {rid: (data or {}).get("actor_role") for rid, data in db.execute(
        select(RefundEvent.refund_id, RefundEvent.data)
        .where(RefundEvent.event_type == "approved", RefundEvent.refund_id.in_([r.id for r in approved]))
    ).all()} if approved else {}

    def signed_by_admin(r: RefundRequest) -> bool:
        role = role_at.get(r.id) or (r.decided_by.role.value if r.decided_by else None)
        return role == Role.admin.value

    bad = [r.reference for r in approved if not signed_by_admin(r)]
    ev = {"high_risk_threshold": threshold, "high_risk_approvals": len(approved), "approved_without_admin": bad[:50]}
    if bad:
        return Result("fail", f"{len(bad)} high-risk refund(s) approved without admin sign-off", ev)
    if not approved:
        return Result("pass", f"No high-risk approvals yet; reviewers are blocked at risk ≥ {threshold}", ev)
    return Result("pass", f"All {len(approved)} high-risk approvals (risk ≥ {threshold}) were signed off by an admin", ev)


def check_access_review(db: Session) -> Result:
    days = get_settings().access_review_interval_days
    last = db.scalar(select(AccessReview).order_by(AccessReview.completed_at.desc()).limit(1))
    ev = {"interval_days": days, "last_review": None}
    if last is None:
        return Result("fail", f"No user access review on record (required every {days} days)", ev)
    elapsed = now_utc() - aware(last.completed_at)
    age = elapsed.days
    ev["last_review"] = {"id": last.id, "completed_at": last.completed_at.isoformat(), "reviewer": last.reviewer_email,
                         "users_reviewed": len(last.decisions or []), "revoked": last.revoked_count, "age_days": age}
    if elapsed > timedelta(days=days):
        return Result("fail", f"Last access review was {age} days ago (limit {days})", ev)
    return Result("pass", f"Access recertified {age} day(s) ago by {last.reviewer_email}", ev)


def check_dormant_accounts(db: Session) -> Result:
    days = get_settings().dormant_account_days
    cutoff = now_utc() - timedelta(days=days)
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    dormant = [
        {"email": u.email, "role": u.role.value, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}
        for u in users if aware(u.last_login_at or u.created_at) < cutoff
    ]
    ev = {"threshold_days": days, "dormant_active_accounts": dormant}
    if dormant:
        return Result("fail", f"{len(dormant)} active account(s) unused for more than {days} days", ev)
    return Result("pass", f"No active account has been idle for more than {days} days", ev)


def check_mfa(_: Session) -> Result:
    s = get_settings()
    if s.mock_sso_enabled:
        return Result("pass", "Every sign-in passes a second factor before a token is issued (mock IdP, dev)",
                      {"environment": s.environment, "flow": "/auth/sso/authorize -> /auth/sso/mfa -> /auth/token"})
    return Result("documented", "Enforced by the corporate IdP's conditional-access policy; attach the IdP policy export",
                  {"environment": s.environment})


def check_session_timeout(_: Session) -> Result:
    s = get_settings()
    ev = {"access_token_minutes": s.access_token_minutes, "required_idle_limit_minutes": s.session_idle_limit_minutes,
          "idle_tracking": False}
    if s.access_token_minutes > s.session_idle_limit_minutes:
        return Result("fail", f"Sessions stay valid for {s.access_token_minutes} min with no idle re-authentication "
                      f"(max {s.session_idle_limit_minutes} min idle)", ev)
    return Result("pass", f"Sessions expire after {s.access_token_minutes} min", ev)


# Successful events of each kind must be in the log; a denied attempt or a refund.create is not a decision.
REQUIRED_LOG_EVENTS = {
    "sign-in": ("auth.login",),
    "refund decision": ("refund.approve", "refund.reject"),  # escalation leaves the refund undecided
    "PII access": ("customer.history",),
}


def check_security_logging(db: Session) -> Result:
    since = now_utc() - timedelta(days=30)
    rows = db.execute(
        select(AuditLog.action, AuditLog.success, func.count()).where(AuditLog.created_at >= since)
        .group_by(AuditLog.action, AuditLog.success)
    ).all()
    by_cat: Counter = Counter()
    denied = 0
    for action, ok, n in rows:
        by_cat[action.split(".")[0]] += n
        denied += 0 if ok else n
    ev = {"window_days": 30, "events_by_category": dict(by_cat), "denied_attempts": denied}
    total = sum(by_cat.values())
    succeeded = {action for action, ok, _ in rows if ok}
    missing = [kind for kind, actions in REQUIRED_LOG_EVENTS.items() if not succeeded.intersection(actions)]
    ev["missing_categories"] = missing
    if missing:
        return Result("warn", f"No {', '.join(missing)} events recorded in the last 30 days", ev)
    return Result("pass", f"{total} security events in 30 days ({denied} denied attempts), incl. logins, decisions, PII access", ev)


def check_audit_integrity(db: Session) -> Result:
    chain = verify_chain(db)
    ev = dict(chain)
    if engine.dialect.name == "postgresql":
        ev["append_only_trigger"] = bool(db.scalar(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_audit_log_immutable'")
        ))
    if not chain["ok"]:
        return Result("fail", f"Audit chain broken at entry #{chain['first_broken']['id']}: {chain['first_broken']['reason']}", ev)
    if ev.get("append_only_trigger") is False:
        return Result("fail", "Append-only trigger on audit_log is missing", ev)
    return Result("pass", f"{chain['verified_entries']} entries verified against the SHA-256 hash chain", ev)


def check_log_retention(db: Session) -> Result:
    days = get_settings().audit_retention_days
    oldest = db.scalar(select(func.min(AuditLog.created_at)))
    ev = {"retention_days": days, "oldest_entry": oldest.isoformat() if oldest else None,
          "deletion": "blocked by append-only trigger; no purge job before retention_days"}
    if days < 365:
        return Result("fail", f"Audit retention is {days} days (minimum 365)", ev)
    return Result("pass", f"Audit entries retained for {days} days and cannot be deleted", ev)


def check_pii_minimisation(db: Session) -> Result:
    sample = db.scalars(select(RefundRequest).limit(1)).unique().first()
    if sample is None:
        return Result("warn", "No refunds to test masking against yet", {})
    probe = User(id=0, email="compliance.probe@system", name="probe", role=Role.viewer, is_active=True,
                 approval_limit_cents=0, daily_approval_limit_cents=0)
    out = serialize_refund(db, probe, sample)
    leaked = [f for f, v in (("name", out.customer.name), ("email", out.customer.email)) if v in (sample.customer.name, sample.customer.email)]
    ev = {"role_tested": "viewer", "refund": sample.reference, "masked_email": out.customer.email,
          "masked_name": out.customer.name, "unmasked_fields": leaked}
    if leaked or not out.customer.pii_masked:
        return Result("fail", f"Read-only role receives unmasked PII: {', '.join(leaked)}", ev)
    return Result("pass", "Live probe: read-only role receives masked customer name and email", ev)


def check_dsar(db: Session) -> Result:
    days = get_settings().dsar_due_days
    open_ = db.scalars(select(DataSubjectRequest).where(DataSubjectRequest.status == "open")).unique().all()
    overdue = [{"id": r.id, "kind": r.kind, "customer": r.customer.external_id, "due_at": r.due_at.isoformat()}
               for r in open_ if aware(r.due_at) < now_utc()]
    done = db.scalar(select(func.count()).where(DataSubjectRequest.status == "completed")) or 0
    ev = {"due_days": days, "open": len(open_), "overdue": overdue, "completed": done}
    if overdue:
        return Result("fail", f"{len(overdue)} data-subject request(s) past the {days}-day deadline", ev)
    if open_:
        return Result("warn", f"{len(open_)} data-subject request(s) open, none overdue", ev)
    return Result("pass", f"No open data-subject requests; {done} completed", ev)


def check_encryption(_: Session) -> Result:
    s = get_settings()
    url = s.database_url
    ev = {"environment": s.environment, "hsts": "max-age=31536000; includeSubDomains",
          "db_tls": "sslmode=require" in url or url.startswith("sqlite"), "infra": "RDS storage_encrypted + KMS, rds.force_ssl=1"}
    if s.environment == "prod" and "sslmode=require" not in url:
        return Result("fail", "Production database connection does not require TLS", ev)
    if s.environment != "prod":
        return Result("warn", "Dev uses a local database without TLS; prod enforces TLS + KMS (infra/data.tf)", ev)
    return Result("pass", "TLS to the database and browser (HSTS); storage encrypted with KMS", ev)


def check_continuous_monitoring(db: Session) -> Result:
    on = remediation.enabled()
    failing = [c.id for c in CONTROLS if c.check and c.check is not check_continuous_monitoring
               and evaluate(db, c)["status"] == "fail"]
    tracked = set(db.scalars(select(SupportRequest.fingerprint).where(
        SupportRequest.kind == "compliance", SupportRequest.status.in_(ACTIVE_TICKET_STATUSES)
    )).all())
    untracked = [cid for cid in failing if f"compliance:{cid}" not in tracked]
    ev = {"devin_remediation_enabled": on, "repo": get_settings().devin_repo, "base_branch": get_settings().devin_base_branch,
          "failing_controls": failing, "failing_without_ticket": untracked}
    if not on:
        return Result("warn", "Failing controls are ticketed, but DEVIN_API_KEY is not set so no fix PR is opened automatically", ev)
    if untracked:
        return Result("warn", f"{len(untracked)} failing control(s) have no open remediation ticket: {', '.join(untracked)}", ev)
    return Result("pass", "Every failing control has an open ticket that Devin turns into a reviewed PR", ev)


CONTROLS: list[Control] = [
    Control("AC-01", "Role-based least privilege",
            "Users get one role; read-only roles cannot approve money movement.",
            {SOC2: ["CC6.1", "CC6.3"], PCI: ["7.2.1", "7.2.2"], SOX: ["Logical access"]},
            "Remove approval authority from read-only users.", check_least_privilege),
    Control("AC-02", "Segregation of duties (four-eyes)",
            "No one can decide a refund they requested.",
            {SOC2: ["CC5.1", "CC6.3"], SOX: ["Segregation of duties"]},
            "Reverse/re-review self-decided refunds; confirm policy.check_* four-eyes rule is enforced.", check_four_eyes),
    Control("AC-03", "Delegated approval authority",
            "Per-user and daily limits sit inside the org hard cap; nothing above the cap is approved.",
            {SOC2: ["CC5.2"], SOX: ["Transaction authorization"]},
            "Lower user limits to the hard cap and review approvals above it.", check_delegated_limits),
    Control("AC-04", "High-risk refunds need admin sign-off",
            "Refunds at/above the risk threshold are approved only by admins.",
            {SOC2: ["CC5.2"], SOX: ["Transaction authorization"], PCI: ["7.2.2"]},
            "Re-review the listed refunds; confirm reviewers are blocked at the risk threshold.", check_high_risk_signoff),
    Control("AC-05", "Periodic user access review",
            "An admin recertifies every user's role and limits at least quarterly.",
            {SOC2: ["CC6.2", "CC6.3"], PCI: ["7.2.4"], SOX: ["Access recertification"]},
            "Complete an access review in Compliance → Access review.", check_access_review),
    Control("AC-06", "Dormant accounts disabled",
            "Active accounts unused for 90 days are disabled.",
            {SOC2: ["CC6.2"], PCI: ["8.2.6"], SOX: ["Access termination"]},
            "Revoke the dormant accounts (an access review does this).", check_dormant_accounts),
    Control("IA-01", "Multi-factor authentication",
            "Every interactive sign-in requires a second factor.",
            {SOC2: ["CC6.1"], PCI: ["8.4.2"], SOX: ["Logical access"]},
            "Enforce MFA in the IdP for the app's client.", check_mfa),
    Control("IA-02", "Session idle timeout",
            "Sessions idle for more than 15 minutes require re-authentication.",
            {SOC2: ["CC6.1"], PCI: ["8.2.8"]},
            "Implement a 15-minute idle timeout: short-lived access tokens refreshed on user activity "
            "(or server-side last-activity tracking), with the SPA returning to sign-in when idle. "
            "Keep ACCESS_TOKEN_MINUTES within SESSION_IDLE_LIMIT_MINUTES and add tests.", check_session_timeout),
    Control("AU-01", "Security event logging",
            "Logins, decisions, PII access, admin changes and every denied attempt are logged with actor, IP and trace id.",
            {SOC2: ["CC7.2"], PCI: ["10.2.1", "10.2.2"], SOX: ["Audit trail"]},
            "Ensure every privileged endpoint calls audit()/deny().", check_security_logging),
    Control("AU-02", "Tamper-evident audit trail",
            "The audit log is append-only and SHA-256 hash-chained so any edit or deletion is detectable.",
            {SOC2: ["CC7.2"], PCI: ["10.3.2", "10.3.4"], SOX: ["Audit trail"]},
            "Investigate the broken entry as a security incident; restore from backup.", check_audit_integrity),
    Control("AU-03", "Audit log retention",
            "Audit history is kept for at least 12 months.",
            {PCI: ["10.5.1"], SOX: ["Record retention"]},
            "Set AUDIT_RETENTION_DAYS ≥ 365.", check_log_retention),
    Control("DP-01", "PII minimisation for read-only roles",
            "Viewers/auditors see masked customer names and emails (live probe of the API serializer).",
            {SOC2: ["C1.1", "CC6.1"], GDPR: ["Art. 25", "Art. 32"]},
            "Mask PII in services.serialize_refund for roles without policy.can_view_pii.", check_pii_minimisation),
    Control("DP-02", "Data-subject requests within 30 days",
            "Access and erasure requests are fulfilled within one month; erasure keeps legally required financial records.",
            {GDPR: ["Art. 12(3)", "Art. 15", "Art. 17"], SOC2: ["P5.1"]},
            "Fulfil the overdue requests in Compliance → Data subject requests.", check_dsar),
    Control("SC-01", "Encryption in transit and at rest",
            "TLS to browser and database; database storage encrypted with KMS.",
            {SOC2: ["CC6.7"], PCI: ["4.2.1"], GDPR: ["Art. 32"]},
            "Require sslmode=require on DATABASE_URL in prod.", check_encryption, ["infra/data.tf", "backend/app/main.py"]),
    Control("CM-01", "Change management",
            "Every change goes through a PR with CI (lint, tests, build, terraform plan); prod only moves via dev → main.",
            {SOC2: ["CC8.1"], PCI: ["6.5.1"], SOX: ["Program change"]},
            "Enable branch protection with required reviews and checks.", None,
            [".github/workflows/ci.yml", ".github/workflows/branch-flow.yml", ".github/workflows/deploy.yml"]),
    Control("SC-02", "Web application firewall",
            "Public traffic passes AWS WAF (OWASP common rules, SQLi, IP reputation, rate limits).",
            {SOC2: ["CC6.6"], PCI: ["6.4.2"]},
            "Keep the WAF web ACL attached to CloudFront.", None, ["infra/waf.tf", "infra/edge.tf"]),
    Control("OP-01", "Backups and recovery",
            "Multi-AZ database, 14-day PITR, cross-region backup copies and a scripted restore test.",
            {SOC2: ["A1.2", "A1.3"], SOX: ["Computer operations"]},
            "Run scripts/db-ops.sh restore-test regularly and keep the output.", None,
            ["infra/db_resilience.tf", "scripts/db-ops.sh"]),
    Control("MN-01", "Continuous control monitoring",
            "Controls run on demand and in the evidence pack; failures are handed to Devin to fix via PR.",
            {SOC2: ["CC4.1", "CC4.2"], SOX: ["Monitoring"]},
            "Set DEVIN_API_KEY / DEVIN_ORG_ID so failing controls start a remediation session.", check_continuous_monitoring),
]
BY_ID = {c.id: c for c in CONTROLS}


def evaluate(db: Session, control: Control) -> dict:
    if control.check is None:
        res = Result("documented", "Implemented in infrastructure / process; evidence in the repo",
                     {"evidence_files": control.evidence_files})
    else:
        try:
            res = control.check(db)
        except Exception as exc:  # noqa: BLE001 - a broken check is itself a finding
            res = Result("fail", f"Check errored: {exc}", {})
        if control.evidence_files:
            res.evidence["evidence_files"] = control.evidence_files
    return {
        "id": control.id, "title": control.title, "objective": control.objective, "frameworks": control.frameworks,
        "automated": control.check is not None, "status": res.status, "summary": res.summary,
        "evidence": res.evidence, "remediation": control.remediation,
    }


def run_all(db: Session) -> dict:
    results = [evaluate(db, c) for c in CONTROLS]
    summary = []
    for fw in FRAMEWORKS:
        in_fw = [r for r in results if fw in r["frameworks"]]
        counts = Counter(r["status"] for r in in_fw)
        summary.append({"framework": fw, "controls": len(in_fw), "pass": counts["pass"], "warn": counts["warn"],
                        "fail": counts["fail"], "documented": counts["documented"]})
    return {"generated_at": now_utc().isoformat(), "environment": get_settings().environment,
            "frameworks": summary, "controls": results}


__all__ = ["BY_ID", "CONTROLS", "FRAMEWORKS", "evaluate", "policy", "run_all"]
