import { useEffect, useState } from 'react'

import { api, type Refund, type RefundEvent } from '../api'
import { dateTime, humanize, money, timeAgo } from '../format'
import { CustomerHistory, type Action } from './RefundActions'
import { Badge, Drawer, ErrorBox, RiskBadge, Spinner, StatusBadge } from './ui'

export function RefundDrawer({
  refund,
  onClose,
  onAction,
  onAssign,
  readOnly,
}: {
  refund: Refund
  onClose: () => void
  onAction: (a: Action) => void
  onAssign: () => void
  readOnly?: boolean
}) {
  const [tab, setTab] = useState<'details' | 'timeline' | 'history'>('details')
  const [events, setEvents] = useState<RefundEvent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const p = refund.permissions
  const open = refund.status === 'pending' || refund.status === 'escalated'

  useEffect(() => {
    if (readOnly) return
    api<{ events: RefundEvent[] }>(`/refunds/${refund.id}`)
      .then((d) => setEvents(d.events))
      .catch((e) => setError(e.message))
  }, [refund.id, refund.status, readOnly])

  return (
    <Drawer
      title={
        <span className="row">
          {refund.reference} <StatusBadge status={refund.status} /> <RiskBadge score={refund.risk_score} />
          {refund.overdue ? <Badge kind="danger">past SLA</Badge> : null}
        </span>
      }
      onClose={onClose}
    >
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: 30, fontWeight: 700 }}>{money(refund.amount_cents)}</div>
          <div className="muted">{humanize(refund.reason)} · requested {timeAgo(refund.created_at)}</div>
        </div>
        {!readOnly && open && p.can_reject ? (
          <div className="row">
            <button className="btn success" disabled={!p.can_approve} onClick={() => onAction('approve')}>Approve</button>
            <button className="btn danger" disabled={!p.can_reject} onClick={() => onAction('reject')}>Reject</button>
            {p.can_escalate ? <button className="btn warning" onClick={() => onAction('escalate')}>Escalate</button> : null}
            {p.can_assign ? <button className="btn" onClick={onAssign}>Assign</button> : null}
          </div>
        ) : null}
      </div>
      {open && p.can_reject && !p.can_approve ? <div className="notice">Approval blocked: {p.approve_blocked_reasons.join(' · ')}</div> : null}

      <div className="tabs">
        <button className={tab === 'details' ? 'on' : ''} onClick={() => setTab('details')}>Details</button>
        {!readOnly ? <button className={tab === 'timeline' ? 'on' : ''} onClick={() => setTab('timeline')}>Timeline</button> : null}
        {!readOnly && p.can_view_history ? (
          <button className={tab === 'history' ? 'on' : ''} onClick={() => setTab('history')} data-testid="tab-history">Customer history</button>
        ) : null}
      </div>

      {tab === 'details' ? (
        <dl className="kv">
          <dt>Customer</dt>
          <dd>
            {refund.customer.name} · {refund.customer.email} {refund.customer.pii_masked ? <Badge kind="muted">PII masked</Badge> : null}
          </dd>
          <dt>Customer ID</dt><dd>{refund.customer.external_id} · {refund.customer.country}</dd>
          <dt>Order</dt><dd>{refund.order_id}</dd>
          <dt>Description</dt><dd>{refund.description || '—'}</dd>
          <dt>Requested by</dt><dd>{refund.requested_by_email}</dd>
          <dt>Assigned to</dt><dd>{refund.assigned_to?.name ?? (refund.status === 'escalated' ? 'Admin queue' : '—')}</dd>
          <dt>SLA due</dt><dd>{dateTime(refund.sla_due_at)} ({timeAgo(refund.sla_due_at)})</dd>
          {refund.escalation_reason ? (<><dt>Escalation</dt><dd>{refund.escalated_by ? `${refund.escalated_by.name}: ` : ''}{refund.escalation_reason}</dd></>) : null}
          {refund.decided_by ? (<><dt>Decision</dt><dd>{humanize(refund.status)} by {refund.decided_by.name} · {refund.decided_at ? dateTime(refund.decided_at) : ''}</dd></>) : null}
          {refund.decision_note ? (<><dt>Decision note</dt><dd>{refund.decision_note}</dd></>) : null}
        </dl>
      ) : null}

      {tab === 'timeline' ? (
        <>
          <ErrorBox message={error} />
          {!events && !error ? <Spinner /> : null}
          <ul className="timeline">
            {events?.map((e) => (
              <li key={e.id}>
                <div>
                  <b>{humanize(e.event_type)}</b> <span className="muted">by {e.actor?.name ?? 'system'}</span>
                  <div className="muted">
                    {dateTime(e.created_at)}
                    {e.from_status && e.to_status && e.from_status !== e.to_status ? ` · ${e.from_status} → ${e.to_status}` : ''}
                  </div>
                  {e.note ? <div>{e.note}</div> : null}
                </div>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {tab === 'history' ? <CustomerHistory refund={refund} /> : null}
    </Drawer>
  )
}
