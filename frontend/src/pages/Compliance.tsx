import { useCallback, useEffect, useState } from 'react'

import { api, download } from '../api'
import { Badge, Drawer, ErrorBox, Modal, PageHead, RoleBadge, Segmented, Spinner } from '../components/ui'
import { dateTime, money } from '../format'

type CheckStatus = 'pass' | 'warn' | 'fail' | 'documented'
interface Control {
  id: string
  title: string
  objective: string
  frameworks: Record<string, string[]>
  automated: boolean
  status: CheckStatus
  summary: string
  evidence: Record<string, unknown>
  remediation: string
}
interface FrameworkSummary { framework: string; controls: number; pass: number; warn: number; fail: number; documented: number }
interface Report { generated_at: string; environment: string; frameworks: FrameworkSummary[]; controls: Control[] }
interface Ticket { id: number; control_id: string; title: string; status: string; devin_session_url: string | null; remediation_error: string | null; created_at: string }

const STATUS_KIND: Record<CheckStatus, string> = { pass: 'success', warn: 'warning', fail: 'danger', documented: 'muted' }
const StatusPill = ({ s }: { s: CheckStatus }) => <Badge kind={STATUS_KIND[s]}>{s}</Badge>
const TABS = ['controls', 'access', 'dsr'] as const

export function ComplianceCenter({ admin }: { admin: boolean }) {
  const [tab, setTab] = useState<(typeof TABS)[number]>('controls')
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hash, setHash] = useState<string | null>(null)
  const load = useCallback(() => {
    api<Report>('/compliance/controls').then(setReport).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  const exportPack = async () => {
    setError(null)
    try {
      const h = await download('/compliance/evidence-pack', `evidence-pack-${new Date().toISOString().slice(0, 10)}.json`)
      setHash(h.get('X-Evidence-SHA256'))
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <>
      <PageHead title="Compliance Center"
        subtitle="Controls-as-code mapped to SOC 2, PCI DSS 4.0, SOX ITGC and GDPR. Automated controls run live against the database and running config every time this page loads.">
        <button className="btn" onClick={load}>Re-run checks</button>
        <button className="btn primary" onClick={exportPack}>Download evidence pack</button>
      </PageHead>
      {hash ? <div className="notice">Evidence pack exported and audited. SHA-256 <span className="hash">{hash}</span></div> : null}
      <ErrorBox message={error} />
      {report ? <FrameworkCards report={report} /> : null}
      {admin ? (
        <div className="tabs">
          {TABS.map((t) => (
            <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
              {{ controls: 'Controls', access: 'Access review', dsr: 'Data subject requests' }[t]}
            </button>
          ))}
        </div>
      ) : <div className="notice">Auditor view: read-only. You can inspect every control's evidence and download the evidence pack.</div>}
      {tab === 'controls' ? (report ? <Controls report={report} admin={admin} /> : <Spinner />) : null}
      {tab === 'access' && admin ? <AccessReview onDone={load} /> : null}
      {tab === 'dsr' && admin ? <DataSubjectRequests onDone={load} /> : null}
      <p className="muted" style={{ marginTop: 24 }}>
        Scope: these are the application-layer controls this team owns. Infrastructure controls inherit AWS's attestations (see infra/), and the Devin platform used to build and run the self-healing loop is covered by Cognition's own SOC 2 Type II report.
        Environment: {report?.environment ?? '…'} · last run {report ? dateTime(report.generated_at) : '…'}
      </p>
    </>
  )
}

function FrameworkCards({ report }: { report: Report }) {
  return (
    <div className="grid stats" style={{ marginBottom: 8 }}>
      {report.frameworks.map((f) => {
        const automated = f.controls - f.documented
        return (
          <div key={f.framework} className="card stat">
            <div className="label">{f.framework}</div>
            <div className={`value tone-${f.fail ? 'danger' : f.warn ? 'warning' : 'success'}`}>{f.pass}/{automated} passing</div>
            <div className="muted">{f.fail} failing · {f.warn} warning · {f.documented} documented</div>
          </div>
        )
      })}
    </div>
  )
}

function Controls({ report, admin }: { report: Report; admin: boolean }) {
  const [open, setOpen] = useState<Control | null>(null)
  const [filter, setFilter] = useState<'all' | 'fail' | 'warn' | 'pass' | 'documented'>('all')
  const [tickets, setTickets] = useState<Ticket[]>([])
  const loadTickets = useCallback(() => {
    if (admin) api<Ticket[]>('/compliance/tickets').then(setTickets).catch(() => setTickets([]))
  }, [admin])
  useEffect(loadTickets, [loadTickets])
  const rows = report.controls.filter((c) => filter === 'all' || c.status === filter)
  const ticketFor = (id: string) => tickets.find((t) => t.control_id === id)
  return (
    <>
      <Segmented options={['all', 'fail', 'warn', 'pass', 'documented'] as const} value={filter} onChange={setFilter}
        labels={{ all: `All (${report.controls.length})`, fail: `Failing (${report.controls.filter((c) => c.status === 'fail').length})` }} />
      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead><tr><th>Control</th><th>Frameworks</th><th>Status</th><th>Result</th><th /></tr></thead>
          <tbody>
            {rows.map((c) => {
              const t = ticketFor(c.id)
              return (
                <tr key={c.id} className="clickable" onClick={() => setOpen(c)} data-testid={`control-${c.id}`}>
                  <td style={{ minWidth: 220 }}><b>{c.id}</b> {c.title}<div className="sub">{c.automated ? 'Automated check' : 'Documented control'}</div></td>
                  <td style={{ maxWidth: 260 }}>{Object.entries(c.frameworks).map(([fw, refs]) => <span key={fw} className="fw-chip">{fw} {refs.join(', ')}</span>)}</td>
                  <td><StatusPill s={c.status} /></td>
                  <td className="sub" style={{ maxWidth: 380 }}>{c.summary}</td>
                  <td className="num">{t ? <Badge kind="pending">{t.status === 'devin_working' ? 'Devin working' : `ticket #${t.id}`}</Badge> : null}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {admin && tickets.length ? (
        <>
          <div className="section-title">Compliance remediation tickets</div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>#</th><th>Finding</th><th>Status</th><th>Devin session</th><th>Opened</th></tr></thead>
              <tbody>
                {tickets.map((t) => (
                  <tr key={t.id}>
                    <td>{t.id}</td>
                    <td><b>{t.title}</b>{t.remediation_error ? <div className="tone-danger" style={{ fontSize: 12 }}>{t.remediation_error}</div> : null}</td>
                    <td><Badge kind={t.status === 'open' ? 'warning' : 'pending'}>{t.status.replace('_', ' ')}</Badge></td>
                    <td>{t.devin_session_url ? <a href={t.devin_session_url} target="_blank" rel="noreferrer">Open session ↗</a> : <span className="sub">—</span>}</td>
                    <td className="sub">{dateTime(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
      {open ? <ControlDrawer c={open} admin={admin} ticket={ticketFor(open.id)} onClose={() => setOpen(null)} onTicket={loadTickets} /> : null}
    </>
  )
}

function ControlDrawer({ c, admin, ticket, onClose, onTicket }: { c: Control; admin: boolean; ticket?: Ticket; onClose: () => void; onTicket: () => void }) {
  const [msg, setMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fix = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api<{ id: number; status: string; devin_enabled: boolean }>(`/compliance/controls/${c.id}/remediate`, 'POST')
      setMsg(r.devin_enabled
        ? `Ticket #${r.id} handed to Devin. It gets the control's evidence, fixes the root cause with a regression test, and opens a PR against dev.`
        : `Ticket #${r.id} opened. Set DEVIN_API_KEY / DEVIN_ORG_ID on the API to have Devin pick it up automatically.`)
      onTicket()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const failing = c.status === 'fail' || c.status === 'warn'
  return (
    <Drawer title={<>{c.id} · {c.title} <StatusPill s={c.status} /></>} onClose={onClose}>
      <dl className="kv">
        <dt>Objective</dt><dd>{c.objective}</dd>
        <dt>Frameworks</dt><dd>{Object.entries(c.frameworks).map(([fw, refs]) => <span key={fw} className="fw-chip">{fw} {refs.join(', ')}</span>)}</dd>
        <dt>Type</dt><dd>{c.automated ? 'Automated — evaluated live on every load and in every evidence pack' : 'Documented — implemented in infrastructure / process'}</dd>
        <dt>Result</dt><dd><b>{c.summary}</b></dd>
        {failing ? <><dt>Remediation</dt><dd>{c.remediation}</dd></> : null}
      </dl>
      <div className="section-title">Evidence</div>
      <pre className="evidence">{JSON.stringify(c.evidence, null, 2)}</pre>
      {admin && failing ? (
        <div style={{ marginTop: 16 }}>
          {ticket?.devin_session_url ? <p><a href={ticket.devin_session_url} target="_blank" rel="noreferrer">Devin is on it — open session ↗</a></p> : null}
          <button className="btn primary" disabled={busy} onClick={fix}>Fix with Devin</button>
          {msg ? <div className="notice">{msg}</div> : null}
          <ErrorBox message={error} />
        </div>
      ) : null}
    </Drawer>
  )
}

interface ReviewUser { id: number; name: string; email: string; role: string; approval_limit_cents: number; daily_approval_limit_cents: number; last_login_at: string | null; dormant: boolean }
interface ReviewRecord { id: number; reviewer_email: string; completed_at: string; revoked_count: number; decisions: { email: string; decision: string }[] }

function AccessReview({ onDone }: { onDone: () => void }) {
  const [state, setState] = useState<{ interval_days: number; users: ReviewUser[]; history: ReviewRecord[] } | null>(null)
  const [decisions, setDecisions] = useState<Record<number, 'certify' | 'revoke'>>({})
  const [error, setError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  const load = useCallback(() => {
    api<{ interval_days: number; users: ReviewUser[]; history: ReviewRecord[] }>('/compliance/access-review')
      .then((s) => {
        setState(s)
        setDecisions(Object.fromEntries(s.users.map((u) => [u.id, u.dormant ? 'revoke' : 'certify'])))
      })
      .catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  if (!state) return error ? <ErrorBox message={error} /> : <Spinner />
  const revoking = state.users.filter((u) => decisions[u.id] === 'revoke')
  const submit = async () => {
    setError(null)
    try {
      await api('/compliance/access-review', 'POST', { decisions: state.users.map((u) => ({ user_id: u.id, decision: decisions[u.id] })) })
      setConfirm(false)
      load()
      onDone()
    } catch (e) {
      setConfirm(false)
      setError((e as Error).message)
    }
  }
  return (
    <>
      <p className="muted">Recertify every active user's role and limits (required every {state.interval_days} days). Revoking deactivates the account immediately. The review and every revocation are written to the audit log.</p>
      <ErrorBox message={error} />
      <div className="table-wrap">
        <table>
          <thead><tr><th>User</th><th>Role</th><th>Per-refund / daily limit</th><th>Last login</th><th>Decision</th></tr></thead>
          <tbody>
            {state.users.map((u) => (
              <tr key={u.id}>
                <td><b>{u.name}</b> {u.dormant ? <Badge kind="danger">dormant</Badge> : null}<div className="sub">{u.email}</div></td>
                <td><RoleBadge role={u.role} /></td>
                <td className="sub">{u.role === 'viewer' ? 'read-only' : `${money(u.approval_limit_cents)} / ${money(u.daily_approval_limit_cents)}`}</td>
                <td className="sub">{u.last_login_at ? dateTime(u.last_login_at) : 'never'}</td>
                <td><Segmented options={['certify', 'revoke'] as const} value={decisions[u.id]} onChange={(d) => setDecisions({ ...decisions, [u.id]: d })} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn primary" onClick={() => setConfirm(true)}>Complete review ({state.users.length - revoking.length} certify · {revoking.length} revoke)</button>
      </div>
      <div className="section-title">Review history</div>
      {state.history.length === 0 ? <div className="empty">No access review on record.</div> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Completed</th><th>Reviewer</th><th>Users reviewed</th><th>Revoked</th></tr></thead>
            <tbody>{state.history.map((h) => (
              <tr key={h.id}><td>{dateTime(h.completed_at)}</td><td>{h.reviewer_email}</td><td>{h.decisions.length}</td><td>{h.revoked_count}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {confirm ? (
        <Modal title="Complete access review" onClose={() => setConfirm(false)} width={480}>
          <p>You are attesting that every listed user's access is appropriate.</p>
          {revoking.length ? <p>These accounts will be <b>deactivated</b>: {revoking.map((u) => u.email).join(', ')}</p> : null}
          <button className="btn primary" onClick={submit}>Sign off review</button>
        </Modal>
      ) : null}
    </>
  )
}

interface Dsr { id: number; customer_external_id: string; kind: 'export' | 'erasure'; status: string; note: string; requested_by_email: string; created_at: string; due_at: string; overdue: boolean; completed_at: string | null; result: Record<string, unknown>; export?: unknown }

function DataSubjectRequests({ onDone }: { onDone: () => void }) {
  const [rows, setRows] = useState<Dsr[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [f, setF] = useState({ customer_external_id: 'cus_1001', kind: 'export' as 'export' | 'erasure', note: '' })
  const load = useCallback(() => {
    api<Dsr[]>('/compliance/dsr').then(setRows).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])
  const create = async () => {
    setError(null)
    try {
      await api('/compliance/dsr', 'POST', f)
      load()
      onDone()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  const fulfil = async (r: Dsr) => {
    setError(null)
    try {
      const out = await api<Dsr>(`/compliance/dsr/${r.id}/fulfil`, 'POST')
      if (out.export) {
        const a = document.createElement('a')
        a.href = URL.createObjectURL(new Blob([JSON.stringify(out.export, null, 2)], { type: 'application/json' }))
        a.download = `dsar-${r.customer_external_id}.json`
        a.click()
        URL.revokeObjectURL(a.href)
      }
      load()
      onDone()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <>
      <p className="muted">GDPR access (Art. 15) and erasure (Art. 17) requests, due within 30 days. Erasure pseudonymises the customer's identity but keeps refund records, which must be retained for financial record-keeping (Art. 17(3)(b)).</p>
      <div className="card row" style={{ marginBottom: 12 }}>
        <label className="field">Customer ID<input value={f.customer_external_id} onChange={(e) => setF({ ...f, customer_external_id: e.target.value })} /></label>
        <Segmented options={['export', 'erasure'] as const} value={f.kind} onChange={(kind) => setF({ ...f, kind })} />
        <label className="field" style={{ flex: 1 }}>Note<input value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="How the request was received" /></label>
        <button className="btn primary" onClick={create}>Log request</button>
      </div>
      <ErrorBox message={error} />
      {!rows ? <Spinner /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Customer</th><th>Type</th><th>Received</th><th>Due</th><th>Status</th><th>Outcome</th><th /></tr></thead>
            <tbody>
              {rows.length === 0 ? <tr><td colSpan={8} className="empty">No requests</td></tr> : null}
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td><b>{r.customer_external_id}</b><div className="sub">{r.note}</div></td>
                  <td>{r.kind}</td>
                  <td className="sub">{dateTime(r.created_at)}</td>
                  <td className="sub">{dateTime(r.due_at)}</td>
                  <td>{r.status === 'completed' ? <Badge kind="success">completed</Badge> : r.overdue ? <Badge kind="danger">overdue</Badge> : <Badge kind="warning">open</Badge>}</td>
                  <td className="sub" style={{ maxWidth: 280 }}>{r.status === 'completed' ? JSON.stringify(r.result) : ''}</td>
                  <td className="num">{r.status === 'open' ? <button className="btn sm primary" onClick={() => fulfil(r)}>{r.kind === 'export' ? 'Fulfil & download' : 'Erase'}</button> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
