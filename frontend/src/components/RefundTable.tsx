import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { api, type Refund, type Status } from '../api'
import { humanize, money, timeAgo } from '../format'
import { AssignModal, DecisionModal, HistoryModal, type Action } from './RefundActions'
import { RefundDrawer } from './RefundDrawer'
import { Badge, ErrorBox, PayoutBadge, RiskBadge, Segmented, Spinner, StatusBadge } from './ui'

type Filter = 'all' | Status
type SortKey = 'created_at' | 'amount_cents' | 'risk_score' | 'sla_due_at'
const REASONS = ['duplicate_charge', 'service_not_received', 'fraud_suspected', 'product_defect', 'billing_error', 'goodwill']
const PAGE = 25

export function RefundTable({
  mine,
  statuses = ['all', 'pending', 'escalated', 'approved', 'rejected'],
  initialStatus = 'all',
  rows,
  readOnly,
  toolbar,
  refreshKey = 0,
  onChanged,
}: {
  mine?: boolean
  statuses?: Filter[]
  initialStatus?: Filter
  rows?: Refund[]
  readOnly?: boolean
  toolbar?: ReactNode
  refreshKey?: number
  onChanged?: () => void
}) {
  const [status, setStatus] = useState<Filter>(initialStatus)
  const [q, setQ] = useState('')
  const [reason, setReason] = useState('')
  const [minAmount, setMinAmount] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'created_at', dir: -1 })
  const [page, setPage] = useState(0)
  const [fetched, setFetched] = useState<Refund[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<Refund | null>(null)
  const [modal, setModal] = useState<{ kind: Action | 'history' | 'assign'; refund: Refund } | null>(null)

  const load = useCallback(async () => {
    if (rows) return
    setError(null)
    const params = new URLSearchParams({ limit: '500' })
    if (status !== 'all') params.set('status', status)
    if (mine) params.set('mine', 'true')
    if (q.trim()) params.set('q', q.trim())
    try {
      setFetched((await api<{ items: Refund[] }>(`/refunds?${params}`)).items)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [rows, status, mine, q])

  useEffect(() => {
    const t = setTimeout(load, q ? 250 : 0)
    return () => clearTimeout(t)
  }, [load, refreshKey])
  useEffect(() => setPage(0), [status, q, reason, minAmount])

  const data = useMemo(() => {
    let list = rows ?? fetched ?? []
    if (rows && status !== 'all') list = list.filter((r) => r.status === status)
    if (reason) list = list.filter((r) => r.reason === reason)
    if (minAmount) list = list.filter((r) => r.amount_cents >= parseFloat(minAmount) * 100)
    return [...list].sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key]
      return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir
    })
  }, [rows, fetched, status, reason, minAmount, sort])

  const pageRows = data.slice(page * PAGE, page * PAGE + PAGE)
  const total = data.reduce((s, r) => s + r.amount_cents, 0)

  const updated = (r: Refund) => {
    setModal(null)
    setDrawer((d) => (d && d.id === r.id ? r : d))
    setFetched((prev) => prev?.map((x) => (x.id === r.id ? r : x)) ?? prev)
    onChanged?.()
    load()
  }

  const th = (key: SortKey, label: string, num = false) => (
    <th className={`sortable ${num ? 'num' : ''}`} onClick={() => setSort((s) => ({ key, dir: s.key === key ? (-s.dir as 1 | -1) : -1 }))}>
      {label} {sort.key === key ? (sort.dir === 1 ? '▲' : '▼') : ''}
    </th>
  )

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        {statuses.length > 1 ? <Segmented options={statuses} value={status} onChange={setStatus} /> : null}
        {!rows ? <input placeholder="Search reference, order, customer…" value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 240 }} /> : null}
        <select value={reason} onChange={(e) => setReason(e.target.value)}>
          <option value="">All reasons</option>
          {REASONS.map((r) => (
            <option key={r} value={r}>{humanize(r)}</option>
          ))}
        </select>
        <input placeholder="Min $" value={minAmount} onChange={(e) => setMinAmount(e.target.value)} style={{ width: 90 }} inputMode="decimal" />
        <span className="spacer" />
        <span className="muted">{data.length} refunds · {money(total)}</span>
        {!rows ? <button className="btn sm" onClick={load}>Refresh</button> : null}
        {toolbar}
      </div>
      <ErrorBox message={error} />
      {!rows && !fetched ? (
        <Spinner />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Refund</th>
                <th>Customer</th>
                <th>Reason</th>
                {th('amount_cents', 'Amount', true)}
                {th('risk_score', 'Risk')}
                <th>Status</th>
                <th>Owner</th>
                {th('created_at', 'Created')}
                {th('sla_due_at', 'SLA')}
                <th className="num">Actions</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
                <tr><td colSpan={10} className="empty">Nothing here — queue is clear.</td></tr>
              ) : null}
              {pageRows.map((r) => {
                const p = r.permissions
                const open = r.status === 'pending' || r.status === 'escalated'
                const canAct = !readOnly && open && p.can_reject
                return (
                  <tr key={r.id} className="clickable" onClick={() => setDrawer(r)} data-testid={`row-${r.reference}`}>
                    <td><div className="ref">{r.reference}</div><div className="sub">{r.order_id}</div></td>
                    <td><div>{r.customer.name}</div><div className="sub">{r.customer.external_id} · {r.customer.country}</div></td>
                    <td>{humanize(r.reason)}</td>
                    <td className="num"><b>{money(r.amount_cents)}</b></td>
                    <td><RiskBadge score={r.risk_score} /></td>
                    <td><StatusBadge status={r.status} />{r.payout ? <div style={{ marginTop: 4 }}><PayoutBadge payout={r.payout} /></div> : null}</td>
                    <td className="sub">{r.decided_by?.name ?? r.assigned_to?.name ?? (r.status === 'escalated' ? 'Admin queue' : '—')}</td>
                    <td className="sub">{timeAgo(r.created_at)}</td>
                    <td>{open ? (r.overdue ? <Badge kind="danger">overdue</Badge> : <span className="sub">{timeAgo(r.sla_due_at)}</span>) : <span className="sub">—</span>}</td>
                    <td className="num" onClick={(e) => e.stopPropagation()}>
                      <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'nowrap' }}>
                        {canAct ? (
                          <>
                            <button className="btn sm success" disabled={!p.can_approve} title={p.approve_blocked_reasons.join('\n')} onClick={() => setModal({ kind: 'approve', refund: r })} data-testid={`approve-${r.reference}`}>Approve</button>
                            <button className="btn sm danger" onClick={() => setModal({ kind: 'reject', refund: r })} data-testid={`reject-${r.reference}`}>Reject</button>
                            {p.can_escalate ? <button className="btn sm warning" onClick={() => setModal({ kind: 'escalate', refund: r })}>Escalate</button> : null}
                          </>
                        ) : null}
                        {readOnly && open && p.can_reject ? (p.can_approve ? <Badge kind="success">can approve</Badge> : <Badge kind="warning">must escalate</Badge>) : null}
                        {!readOnly && p.can_assign ? <button className="btn sm" onClick={() => setModal({ kind: 'assign', refund: r })}>Assign</button> : null}
                        {!readOnly && p.can_view_history ? <button className="btn sm" onClick={() => setModal({ kind: 'history', refund: r })} data-testid={`history-${r.reference}`}>History</button> : null}
                      </div>
                      {canAct && !p.can_approve ? <div className="blocked">{p.approve_blocked_reasons[0]}</div> : null}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {data.length > PAGE ? (
            <div className="pager">
              <span className="muted">Page {page + 1} of {Math.ceil(data.length / PAGE)}</span>
              <div className="row">
                <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</button>
                <button className="btn sm" disabled={(page + 1) * PAGE >= data.length} onClick={() => setPage(page + 1)}>Next</button>
              </div>
            </div>
          ) : null}
        </div>
      )}

      {drawer ? (
        <RefundDrawer
          key={drawer.id}
          refund={drawer}
          readOnly={readOnly}
          onClose={() => setDrawer(null)}
          onAction={(a) => setModal({ kind: a, refund: drawer })}
          onAssign={() => setModal({ kind: 'assign', refund: drawer })}
        />
      ) : null}
      {modal && (modal.kind === 'approve' || modal.kind === 'reject' || modal.kind === 'escalate') ? (
        <DecisionModal refund={modal.refund} action={modal.kind} onClose={() => setModal(null)} onDone={updated} />
      ) : null}
      {modal?.kind === 'history' ? <HistoryModal refund={modal.refund} onClose={() => setModal(null)} /> : null}
      {modal?.kind === 'assign' ? <AssignModal refund={modal.refund} onClose={() => setModal(null)} onDone={updated} /> : null}
    </div>
  )
}
