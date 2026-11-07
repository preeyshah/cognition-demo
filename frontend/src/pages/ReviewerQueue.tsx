import { useCallback, useEffect, useState } from 'react'

import { api, type Summary, type User } from '../api'
import { RefundTable } from '../components/RefundTable'
import { ErrorBox, PageHead, Spinner, StatGrid } from '../components/ui'
import { money } from '../format'

export function ReviewerQueue({ user }: { user: User }) {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    api<Summary>('/dashboard/summary').then(setSummary).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  const l = summary?.limits
  const used = l ? Math.min(l.approved_today_cents / Math.max(l.daily_approval_limit_cents, 1), 1) : 0
  return (
    <>
      <PageHead title={`Good to see you, ${user.name.split(' ')[0]}`} subtitle="Refunds assigned to you. Click a row for details, timeline and customer history." />
      <ErrorBox message={error} />
      {summary ? <StatGrid cards={summary.cards} /> : <Spinner />}
      {l ? (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <b>Your approval limits</b>
            <span className="muted">
              Per refund {money(l.approval_limit_cents)} · Daily {money(l.daily_approval_limit_cents)} · Used today {money(l.approved_today_cents)}
            </span>
          </div>
          <div className="bar" style={{ marginTop: 10 }}>
            <div style={{ width: `${used * 100}%`, background: used > 0.8 ? 'var(--danger)' : 'var(--success)' }} />
          </div>
          <p className="muted" style={{ marginBottom: 0 }}>
            Refunds above your per-refund limit, high-risk refunds and customers over their 30-day cap must be escalated to an admin.
          </p>
        </div>
      ) : null}
      <div className="section-title">My queue</div>
      <RefundTable mine statuses={['pending']} initialStatus="pending" onChanged={load} />
    </>
  )
}

export function ReviewerHistory() {
  return (
    <>
      <PageHead title="My history" subtitle="Refunds you decided or escalated." />
      <RefundTable statuses={['all', 'approved', 'rejected', 'escalated']} />
    </>
  )
}
