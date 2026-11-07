import { useCallback, useEffect, useState } from 'react'

import { api, type Refund, type Role, type Summary, type User } from '../api'
import { RefundTable } from '../components/RefundTable'
import { Badge, ErrorBox, Modal, PageHead, RoleBadge, Segmented, Spinner, StatGrid } from '../components/ui'
import { dateTime, dollars, humanize, money, toCents } from '../format'

const REASONS = ['duplicate_charge', 'service_not_received', 'fraud_suspected', 'product_defect', 'billing_error', 'goodwill']

export function Escalations() {
  return (
    <>
      <PageHead title="Escalation queue" subtitle="Refunds escalated by reviewers or auto-escalated by policy (high risk / above every reviewer's limit). Admin-only." />
      <RefundTable statuses={['escalated']} initialStatus="escalated" />
    </>
  )
}

export function AllRefunds({ canCreate, viewer }: { canCreate: boolean; viewer?: boolean }) {
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState(0)
  return (
    <>
      <PageHead title="All refunds" subtitle={viewer ? 'Read-only. Customer PII is masked for your role.' : 'Every refund in the system. Click a row for details.'}>
        {canCreate ? <button className="btn primary" onClick={() => setOpen(true)}>+ New refund</button> : null}
      </PageHead>
      <RefundTable refreshKey={key} />
      {open ? <NewRefundModal onClose={() => setOpen(false)} onCreated={() => { setOpen(false); setKey((k) => k + 1) }} /> : null}
    </>
  )
}

function NewRefundModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [f, setF] = useState({ customer_external_id: 'cus_1000', order_id: 'ord_100200', amount: '120', reason: 'duplicate_charge', risk_score: '20', requested_by_email: 'support.amy@acmepay.com', description: '' })
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<Refund | null>(null)
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  const submit = async () => {
    setError(null)
    try {
      setResult(await api<Refund>('/refunds', 'POST', { ...f, amount_cents: toCents(f.amount), risk_score: parseInt(f.risk_score || '0', 10) }))
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <Modal title="New refund request" onClose={result ? onCreated : onClose}>
      {result ? (
        <>
          <p><b>{result.reference}</b> created.</p>
          <p>
            {result.status === 'escalated'
              ? `Auto-escalated to the admin queue — ${result.escalation_reason}.`
              : `Auto-assigned to ${result.assigned_to?.name ?? 'nobody'} (least-loaded active reviewer, never the requester).`}
          </p>
          <button className="btn primary" onClick={onCreated}>Done</button>
        </>
      ) : (
        <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <label className="field">Customer ID<input value={f.customer_external_id} onChange={set('customer_external_id')} placeholder="cus_1000 … cus_1029" /></label>
          <label className="field">Order ID<input value={f.order_id} onChange={set('order_id')} /></label>
          <label className="field">Amount (USD)<input value={f.amount} onChange={set('amount')} inputMode="decimal" /></label>
          <label className="field">Risk score (0-100)<input value={f.risk_score} onChange={set('risk_score')} inputMode="numeric" /></label>
          <label className="field">Reason
            <select value={f.reason} onChange={set('reason')}>{REASONS.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}</select>
          </label>
          <label className="field">Requested by<input value={f.requested_by_email} onChange={set('requested_by_email')} /></label>
          <label className="field" style={{ gridColumn: '1 / 3' }}>Description<textarea value={f.description} onChange={set('description')} /></label>
          <div style={{ gridColumn: '1 / 3' }}>
            <ErrorBox message={error} />
            <button className="btn primary" onClick={submit}>Create & route</button>
          </div>
        </div>
      )}
    </Modal>
  )
}

export function Team({ me }: { me: User }) {
  const [users, setUsers] = useState<User[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const load = useCallback(() => {
    api<User[]>('/admin/users').then(setUsers).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  return (
    <>
      <PageHead title="Team, roles & limits" subtitle="In production users are provisioned via SCIM and roles map from IdP groups. Every change is audited with a before/after diff.">
        <button className="btn primary" onClick={() => setAdding(true)}>+ Add user</button>
      </PageHead>
      <ErrorBox message={error} />
      {!users ? <Spinner /> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>User</th><th>Role</th><th>Per-refund limit ($)</th><th>Daily limit ($)</th><th>Active</th><th>Last login</th><th /></tr>
            </thead>
            <tbody>{users.map((u) => <UserRow key={u.id} user={u} isMe={u.id === me.id} onSaved={load} />)}</tbody>
          </table>
        </div>
      )}
      {adding ? <AddUserModal onClose={() => setAdding(false)} onDone={() => { setAdding(false); load() }} /> : null}
    </>
  )
}

function UserRow({ user, isMe, onSaved }: { user: User; isMe: boolean; onSaved: () => void }) {
  const [role, setRole] = useState<Role>(user.role)
  const [limit, setLimit] = useState(dollars(user.approval_limit_cents))
  const [daily, setDaily] = useState(dollars(user.daily_approval_limit_cents))
  const [active, setActive] = useState(user.is_active)
  const [error, setError] = useState<string | null>(null)
  const dirty = role !== user.role || toCents(limit) !== user.approval_limit_cents || toCents(daily) !== user.daily_approval_limit_cents || active !== user.is_active
  const save = async () => {
    setError(null)
    const body: Record<string, unknown> = { approval_limit_cents: toCents(limit), daily_approval_limit_cents: toCents(daily) }
    if (role !== user.role) body.role = role
    if (active !== user.is_active) body.is_active = active
    try {
      await api(`/admin/users/${user.id}`, 'PATCH', body)
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <tr style={{ opacity: user.is_active ? 1 : 0.6 }}>
      <td>
        <b>{user.name}</b> {isMe ? <Badge kind="muted">you</Badge> : null}
        <div className="sub">{user.email}</div>
        {error ? <div className="tone-danger" style={{ fontSize: 12 }}>{error}</div> : null}
      </td>
      <td>
        <select value={role} disabled={isMe} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="admin">admin</option><option value="reviewer">reviewer</option><option value="viewer">viewer</option>
        </select>
      </td>
      <td><input style={{ width: 110 }} value={limit} disabled={role === 'viewer'} onChange={(e) => setLimit(e.target.value)} /></td>
      <td><input style={{ width: 110 }} value={daily} disabled={role === 'viewer'} onChange={(e) => setDaily(e.target.value)} /></td>
      <td><input type="checkbox" checked={active} disabled={isMe} onChange={(e) => setActive(e.target.checked)} /></td>
      <td className="sub">{user.last_login_at ? dateTime(user.last_login_at) : 'never'}</td>
      <td className="num"><button className="btn sm primary" disabled={!dirty} onClick={save}>Save</button></td>
    </tr>
  )
}

function AddUserModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: '', email: '', role: 'reviewer' as Role, limit: '250', daily: '2500' })
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    try {
      await api('/admin/users', 'POST', { name: f.name, email: f.email, role: f.role, approval_limit_cents: toCents(f.limit), daily_approval_limit_cents: toCents(f.daily) })
      onDone()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <Modal title="Add user" onClose={onClose} width={480}>
      <div className="grid">
        <label className="field">Name<input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label className="field">Email<input value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
        <Segmented options={['admin', 'reviewer', 'viewer'] as const} value={f.role} onChange={(role) => setF({ ...f, role })} />
        <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <label className="field">Per-refund limit $<input value={f.limit} onChange={(e) => setF({ ...f, limit: e.target.value })} /></label>
          <label className="field">Daily limit $<input value={f.daily} onChange={(e) => setF({ ...f, daily: e.target.value })} /></label>
        </div>
        <ErrorBox message={error} />
        <button className="btn primary" onClick={submit}>Create user</button>
      </div>
    </Modal>
  )
}

interface Policy { key: string; value: number; description: string; updated_at: string | null }
const isMoney = (k: string) => k.endsWith('_cents')

export function Policies() {
  const [items, setItems] = useState<Policy[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    api<Policy[]>('/admin/policies').then(setItems).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  return (
    <>
      <PageHead title="Guardrails & policies" subtitle="Org-wide rules enforced server-side on every request. Changes apply immediately and are audited." />
      <ErrorBox message={error} />
      {!items ? <Spinner /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Policy</th><th>Current</th><th>New value</th><th>Last updated</th><th /></tr></thead>
            <tbody>{items.map((p) => <PolicyRow key={p.key} p={p} onSaved={load} />)}</tbody>
          </table>
        </div>
      )}
    </>
  )
}

function PolicyRow({ p, onSaved }: { p: Policy; onSaved: () => void }) {
  const [v, setV] = useState(isMoney(p.key) ? dollars(p.value) : String(p.value))
  const [error, setError] = useState<string | null>(null)
  const value = isMoney(p.key) ? toCents(v) : parseInt(v || '0', 10)
  const save = async () => {
    setError(null)
    try {
      await api(`/admin/policies/${p.key}`, 'PUT', { value })
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <tr>
      <td>
        <b>{humanize(p.key.replace(/_cents$/, ''))}</b>
        <div className="sub">{p.description}</div>
        {error ? <div className="tone-danger" style={{ fontSize: 12 }}>{error}</div> : null}
      </td>
      <td>{isMoney(p.key) ? money(p.value) : p.value}</td>
      <td><input style={{ width: 120 }} value={v} onChange={(e) => setV(e.target.value)} />{isMoney(p.key) ? <span className="muted"> USD</span> : null}</td>
      <td className="sub">{p.updated_at ? dateTime(p.updated_at) : '—'}</td>
      <td className="num"><button className="btn sm primary" disabled={value === p.value} onClick={save}>Save</button></td>
    </tr>
  )
}

interface AuditRow { id: number; created_at: string; actor_email: string | null; action: string; entity_type: string | null; entity_id: string | null; success: boolean; ip_address: string | null; detail: Record<string, unknown> }
const AUDIT_FILTERS = ['all', 'denied', 'refund.', 'customer.', 'user.', 'policy.', 'auth.', 'admin.'] as const

export function AuditLog() {
  const [filter, setFilter] = useState<(typeof AUDIT_FILTERS)[number]>('all')
  const [rows, setRows] = useState<AuditRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: '500' })
    if (filter === 'denied') params.set('success', 'false')
    else if (filter !== 'all') params.set('action', filter)
    api<AuditRow[]>(`/admin/audit-log?${params}`).then(setRows).catch((e) => setError(e.message))
  }, [filter])
  useEffect(load, [load])
  return (
    <>
      <PageHead title="Audit log" subtitle="Append-only (enforced by a Postgres trigger): logins, decisions, PII/history access, admin changes and every denied attempt.">
        <button className="btn sm" onClick={load}>Refresh</button>
      </PageHead>
      <Segmented options={AUDIT_FILTERS} value={filter} onChange={setFilter}
        labels={{ all: 'All', denied: 'Denied only', 'refund.': 'Refund actions', 'customer.': 'History access', 'user.': 'User changes', 'policy.': 'Policy changes', 'auth.': 'Logins', 'admin.': 'Admin' }} />
      <ErrorBox message={error} />
      <div className="table-wrap" style={{ marginTop: 12 }}>
        {!rows ? <Spinner /> : (
          <table>
            <thead><tr><th>When</th><th>Result</th><th>Action</th><th>Actor</th><th>Entity</th><th>Detail</th><th>IP</th></tr></thead>
            <tbody>
              {rows.length === 0 ? <tr><td colSpan={7} className="empty">No entries</td></tr> : null}
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="sub">{dateTime(r.created_at)}</td>
                  <td><Badge kind={r.success ? 'success' : 'danger'}>{r.success ? 'ok' : 'denied'}</Badge></td>
                  <td><b>{r.action}</b></td>
                  <td>{r.actor_email ?? 'system'}</td>
                  <td className="sub">{r.entity_type ? `${r.entity_type} ${r.entity_id}` : ''}</td>
                  <td className="sub" style={{ maxWidth: 380 }}>{typeof r.detail?.reason === 'string' ? r.detail.reason : JSON.stringify(r.detail)}</td>
                  <td className="sub">{r.ip_address}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}

export function ViewAs({ me }: { me: User }) {
  const [users, setUsers] = useState<User[]>([])
  const [target, setTarget] = useState<number | null>(null)
  const [data, setData] = useState<{ user: User; summary: Summary; refunds: Refund[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api<User[]>('/admin/users').then((u) => setUsers(u.filter((x) => x.id !== me.id))).catch((e) => setError(e.message))
  }, [me.id])
  useEffect(() => {
    if (target == null) return
    setData(null)
    api<{ user: User; summary: Summary; refunds: Refund[] }>(`/admin/view-as/${target}`).then(setData).catch((e) => setError(e.message))
  }, [target])
  return (
    <>
      <PageHead title="View as another user" subtitle="Read-only preview of exactly what a user sees — their queue, headline numbers and which actions their limits allow. Each preview is audited." />
      <div className="row">
        {users.map((u) => (
          <button key={u.id} className={`btn ${target === u.id ? 'primary' : ''}`} onClick={() => setTarget(u.id)}>
            {u.name} <RoleBadge role={u.role} />
          </button>
        ))}
      </div>
      <ErrorBox message={error} />
      {target != null && !data ? <Spinner /> : null}
      {data ? (
        <div style={{ marginTop: 16 }}>
          <div className="notice">
            Viewing as <b>{data.user.name}</b> ({data.user.role}) · per-refund limit {money(data.user.approval_limit_cents)} · daily {money(data.user.daily_approval_limit_cents)} · read-only
          </div>
          <StatGrid cards={data.summary.cards} />
          <div className="section-title">Refunds visible to {data.user.name} ({data.refunds.length})</div>
          <RefundTable rows={data.refunds} readOnly statuses={['all', 'pending', 'escalated', 'approved', 'rejected']} />
        </div>
      ) : null}
    </>
  )
}

interface SupportTicket { id: number; kind: 'support' | 'error'; title: string; description: string; trace_id: string | null; reporter_email: string | null; status: string; devin_session_url: string | null; remediation_error: string | null; created_at: string }
interface SupportConfig { devin_enabled: boolean; auto_remediate_errors: boolean; repo: string; base_branch: string; max_acu: number }
const TICKET_TONE: Record<string, string> = { open: 'muted', queued: 'warning', devin_working: 'pending', remediation_failed: 'danger' }

export function SupportTickets() {
  const [rows, setRows] = useState<SupportTicket[] | null>(null)
  const [config, setConfig] = useState<SupportConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    api<SupportTicket[]>('/admin/support/requests').then(setRows).catch((e) => setError(e.message))
  }, [])
  useEffect(() => {
    api<SupportConfig>('/admin/support/config').then(setConfig).catch((e) => setError(e.message))
    load()
    const t = setInterval(load, 5000)  // tickets change in the background as Devin picks them up
    return () => clearInterval(t)
  }, [load])
  const remediate = async (id: number) => {
    setError(null)
    try {
      await api(`/admin/support/requests/${id}/remediate`, 'POST')
      load()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <>
      <PageHead title="Support tickets" subtitle="Support requests and server errors captured by monitoring. Each one can be handed to Devin, which investigates the trace and opens a fix PR.">
        <button className="btn sm" onClick={load}>Refresh</button>
      </PageHead>
      {config ? (
        <div className="notice">
          {config.devin_enabled
            ? <>Devin is connected: {config.auto_remediate_errors ? 'server errors are sent automatically' : 'tickets are sent manually'}; PRs target <b>{config.repo}</b> @ <b>{config.base_branch}</b> (max {config.max_acu} ACU per session).</>
            : <>Devin is not configured (set <code>DEVIN_API_KEY</code>). Tickets are recorded but not sent.</>}
        </div>
      ) : null}
      <ErrorBox message={error} />
      <div className="table-wrap" style={{ marginTop: 12 }}>
        {!rows ? <Spinner /> : (
          <table>
            <thead><tr><th>#</th><th>When</th><th>Kind</th><th>Ticket</th><th>Trace</th><th>Status</th><th>Devin</th><th></th></tr></thead>
            <tbody>
              {rows.length === 0 ? <tr><td colSpan={8} className="empty">No tickets</td></tr> : null}
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="sub">{r.id}</td>
                  <td className="sub">{dateTime(r.created_at)}</td>
                  <td><Badge kind={r.kind === 'error' ? 'danger' : 'muted'}>{r.kind}</Badge></td>
                  <td style={{ maxWidth: 420 }}>
                    <b>{r.title}</b>
                    <div className="sub">{r.reporter_email}{r.description ? ` · ${r.description.split('\n')[0]}` : ''}</div>
                    {r.remediation_error ? <div className="tone-danger" style={{ fontSize: 12 }}>{r.remediation_error}</div> : null}
                  </td>
                  <td className="sub"><code>{r.trace_id ?? '—'}</code></td>
                  <td><Badge kind={TICKET_TONE[r.status] ?? 'muted'}>{humanize(r.status)}</Badge></td>
                  <td>{r.devin_session_url ? <a href={r.devin_session_url} target="_blank" rel="noreferrer">Open session</a> : <span className="sub">—</span>}</td>
                  <td className="num">
                    {['open', 'remediation_failed'].includes(r.status) ? (
                      <button className="btn sm primary" disabled={!config?.devin_enabled} title={config?.devin_enabled ? '' : 'Devin is not configured'} onClick={() => remediate(r.id)}>
                        {r.status === 'remediation_failed' ? 'Retry' : 'Send to Devin'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
