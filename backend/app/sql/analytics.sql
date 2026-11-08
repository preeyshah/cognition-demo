-- Postgres-only objects applied on startup (idempotent).

-- Partial refunds: columns for databases created before admins could approve less than requested.
ALTER TABLE refund_requests ADD COLUMN IF NOT EXISTS approved_amount_cents integer;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_refund_approved_amount_range') THEN
        ALTER TABLE refund_requests ADD CONSTRAINT ck_refund_approved_amount_range CHECK (
            approved_amount_cents IS NULL OR (approved_amount_cents > 0 AND approved_amount_cents <= amount_cents));
    END IF;
END $$;

-- One row per refund with derived metrics; point Metabase / Looker / dbt here.
CREATE OR REPLACE VIEW v_refund_facts AS
SELECT
    r.id, r.reference, r.status, r.reason, r.amount_cents, r.currency, r.risk_score,
    r.created_at, r.decided_at, r.sla_due_at,
    c.external_id AS customer_external_id, c.country AS customer_country,
    a.email AS assigned_to, d.email AS decided_by, d.role AS decided_by_role, e.email AS escalated_by,
    (r.escalation_reason IS NOT NULL) AS was_escalated,
    EXTRACT(EPOCH FROM (r.decided_at - r.created_at)) / 3600.0 AS hours_to_decision,
    (COALESCE(r.decided_at, now()) > r.sla_due_at) AS sla_breached,
    date_trunc('day', r.created_at) AS created_day,
    CASE WHEN r.status = 'approved' THEN COALESCE(r.approved_amount_cents, r.amount_cents) END AS refunded_cents,
    COALESCE(r.approved_amount_cents < r.amount_cents, false) AS is_partial
FROM refund_requests r
JOIN customers c ON c.id = r.customer_id
LEFT JOIN users a ON a.id = r.assigned_to_id
LEFT JOIN users d ON d.id = r.decided_by_id
LEFT JOIN users e ON e.id = r.escalated_by_id;

CREATE OR REPLACE VIEW v_reviewer_daily AS
SELECT
    u.id AS user_id, u.email, u.role,
    date_trunc('day', r.decided_at) AS day,
    COUNT(*) FILTER (WHERE r.status = 'approved') AS approved,
    COUNT(*) FILTER (WHERE r.status = 'rejected') AS rejected,
    COALESCE(SUM(COALESCE(r.approved_amount_cents, r.amount_cents)) FILTER (WHERE r.status = 'approved'), 0) AS approved_cents,
    u.daily_approval_limit_cents,
    AVG(EXTRACT(EPOCH FROM (r.decided_at - r.created_at)) / 3600.0) AS avg_hours_to_decision
FROM refund_requests r
JOIN users u ON u.id = r.decided_by_id
WHERE r.decided_at IS NOT NULL
GROUP BY u.id, u.email, u.role, u.daily_approval_limit_cents, date_trunc('day', r.decided_at);

CREATE OR REPLACE VIEW v_denied_actions AS
SELECT created_at, actor_email, action, entity_type, entity_id, detail->>'reason' AS reason, ip_address
FROM audit_log WHERE success = false;

-- Hash-chain columns for databases created before the Compliance Center (create_all does not alter tables).
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash varchar(64);
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS row_hash varchar(64);

-- The audit log is append-only, enforced in the database itself.
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;
CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
