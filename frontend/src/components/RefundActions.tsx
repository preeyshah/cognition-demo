import { useEffect, useState } from 'react'

import { api, type Refund, type User } from '../api'
import { dateTime, dollars, humanize, money, toCents } from '../format'
import { Badge, ErrorBox, Modal, RoleBadge, Spinner, StatusBadge } from './ui'

export type Action = 'approve' | 'reject' | 'escalate'

const copy: Record<Action, { title: string; cta: string; cls: string; placeholder: string }> = {
  approve: { title: 'Approve refund', cta: 'Approve', cls: 'success', placeholder: 'Optional note (e.g. verified against ledger)' },
  reject: { title: 'Reject refund', cta: 'Reject', cls: 'primary', placeholder: 'Reason for rejection (required, min 10 chars)' },
  escalate: { title: 'Escalate to admin', cta: 'Escalate', cls: 'primary', placeholder: 'Why does this need admin review? (required, min 10 chars)' },
}

export function DecisionModal({ refund, action, onClose, onDone }: { refund: Refund; action: Action; onClose: () => void; onDone: (r: Refund) => void }) {
  const [note, setNote] = useState('')
  const [amount, setAmount] = useState(dollars(refund.amount_cents))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const c = copy[action]
  const partialAllowed = action === 'approve' && refund.permissions.can_approve_partial
  const amountCents = toCents(amount)
  const amountError = !partialAllowed
    ? null
    : !/^\d+(\.\d{1,2})?$/.test(amount.trim()) || amountCents <= 0
      ? 'Enter a positive amount in dollars, e.g. 125.50'
      : amountCents > refund.amount_cents
        ? 'Cannot exceed the requested amount'
        : null
  const partial = partialAllowed && !amountError && amountCents !== refund.amount_cents
  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const body = partial ? { note, amount_cents: amountCents } : { note }
      onDone(await api<Refund>(`/refunds/${refund.id}/${action}`, 'POST', body))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title={`${c.title} · ${refund.reference}`} onClose={onClose} width={520}>
      <div style={{ fontSize: 28, fontWeight: 700 }}>{money(refund.amount_cents)}</div>
      <div className="muted" style={{ marginBottom: 14 }}>
        {refund.customer.name} · {humanize(refund.reason)} · risk {refund.risk_score}
      </div>
      {partialAllowed ? (
        <label className="field" style={{ marginBottom: 12 }}>
          Refund amount (USD) · admins can approve a partial refund
          <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" data-testid="decision-amount" />
          {amountError ? <span className="error" data-testid="decision-amount-error">{amountError}</span> : null}
          {partial ? <span className="muted">Partial refund: {money(amountCents)} of {money(refund.amount_cents)} requested</span> : null}
        </label>
      ) : null}
      <textarea autoFocus style={{ width: '100%' }} placeholder={c.placeholder} value={note} onChange={(e) => setNote(e.target.value)} data-testid="decision-note" />
      <ErrorBox message={error} />
      <p className="muted">Recorded in the refund timeline and the append-only audit log.</p>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className={`btn ${c.cls}`} disabled={busy || !!amountError} onClick={submit} data-testid="decision-confirm">
          {busy ? 'Saving…' : partial ? `${c.cta} ${money(amountCents)}` : c.cta}
        </button>
      </div>
    </Modal>
  )
}

interface HistoryData {
  provider: {
    provider: string
    account_opened: string
    kyc_status: string
    lifetime_spend_cents: number
    lifetime_refunds: number
    chargebacks_12m: number
    risk_flags: string[]
    recent_transactions: { id: string; date: string; merchant: string; amount_cents: number; status: string }[]
  }
  internal: {
    by_status: Record<string, { count: number; amount_cents: number }>
    previous_refunds: { reference: string; amount_cents: number; status: string; reason: string; created_at: string }[]
  }
}

export function CustomerHistory({ refund }: { refund: Refund }) {
  const [data, setData] = useState<HistoryData | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api<HistoryData>(`/refunds/${refund.id}/customer-history`).then(setData).catch((e) => setError(e.message))
  }, [refund.id])
  if (error) return <ErrorBox message={error} />
  if (!data) return <Spinner />
  const p = data.provider
  return (
    <div>
      <p className="muted">
        Fetched live from third-party provider <b>{p.provider}</b> · this access is recorded in the audit log.
      </p>
      <div className="row" style={{ marginBottom: 12 }}>
        <Badge kind={p.kyc_status === 'verified' ? 'success' : 'warning'}>KYC {humanize(p.kyc_status)}</Badge>
        {p.risk_flags.map((f) => (
          <Badge key={f} kind="danger">{humanize(f)}</Badge>
        ))}
      </div>
      <div className="grid stats">
        {[
          ['Account opened', p.account_opened],
          ['Lifetime spend', money(p.lifetime_spend_cents)],
          ['Lifetime refunds', p.lifetime_refunds],
          ['Chargebacks (12m)', p.chargebacks_12m],
        ].map(([k, v]) => (
          <div key={k} className="card stat">
            <div className="label">{k}</div>
            <div className="value" style={{ fontSize: 18 }}>{v}</div>
          </div>
        ))}
      </div>
      <div className="section-title">Recent transactions</div>
      <div className="table-wrap">
        <table>
          <tbody>
            {p.recent_transactions.map((t) => (
              <tr key={t.id}>
                <td className="sub">{dateTime(t.date)}</td>
                <td>{t.merchant}</td>
                <td className="num">{money(t.amount_cents)}</td>
                <td className="num"><Badge kind={t.status === 'settled' ? 'muted' : 'danger'}>{t.status}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="section-title">Previous refunds with us</div>
      {data.internal.previous_refunds.length === 0 ? (
        <div className="empty">No previous refunds</div>
      ) : (
        <div className="table-wrap">
          <table>
            <tbody>
              {data.internal.previous_refunds.map((r) => (
                <tr key={r.reference}>
                  <td className="ref">{r.reference}</td>
                  <td>{humanize(r.reason)}</td>
                  <td className="sub">{dateTime(r.created_at)}</td>
                  <td className="num">{money(r.amount_cents)}</td>
                  <td className="num"><StatusBadge status={r.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function HistoryModal({ refund, onClose }: { refund: Refund; onClose: () => void }) {
  return (
    <Modal title={`Customer history · ${refund.customer.name}`} onClose={onClose} width={820}>
      <CustomerHistory refund={refund} />
    </Modal>
  )
}

export function AssignModal({ refund, onClose, onDone }: { refund: Refund; onClose: () => void; onDone: (r: Refund) => void }) {
  const [users, setUsers] = useState<User[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api<User[]>('/admin/users').then(setUsers).catch((e) => setError(e.message))
  }, [])
  const eligible = (users ?? []).filter((u) => u.is_active && (refund.status === 'escalated' ? u.role === 'admin' : u.role !== 'viewer'))
  const assign = async (id: number) => {
    try {
      onDone(await api<Refund>(`/refunds/${refund.id}/assign`, 'POST', { assignee_id: id }))
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <Modal title={`Assign ${refund.reference} (${money(refund.amount_cents)})`} onClose={onClose} width={520}>
      <ErrorBox message={error} />
      {!users ? <Spinner /> : null}
      <div className="table-wrap">
        <table>
          <tbody>
            {eligible.map((u) => {
              const over = refund.amount_cents > u.approval_limit_cents
              return (
                <tr key={u.id}>
                  <td>
                    <b>{u.name}</b> <RoleBadge role={u.role} /> {refund.assigned_to?.id === u.id ? <span className="muted">(current)</span> : null}
                  </td>
                  <td className="num">
                    <span className={over ? 'tone-danger' : 'muted'}>limit {money(u.approval_limit_cents)}</span>
                  </td>
                  <td className="num">
                    <button className="btn sm" onClick={() => assign(u.id)} disabled={refund.assigned_to?.id === u.id}>Assign</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="muted">Escalated refunds can only go to admins; refunds are never assigned to their requester.</p>
    </Modal>
  )
}
