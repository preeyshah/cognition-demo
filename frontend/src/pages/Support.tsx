import { useCallback, useEffect, useState } from 'react'

import { listSupportTickets, remediateTicket, type SupportTicket } from '../api'
import { Badge, ErrorBox, PageHead, Segmented, Spinner } from '../components/ui'
import { dateTime, humanize } from '../format'

const STATUSES = ['all', 'open', 'queued', 'devin_working', 'remediation_failed'] as const
const KIND_LABELS: Record<string, string> = { feature: 'feature', support: 'problem', error: 'error', guardrail: 'guardrail' }

export function SupportTickets() {
  const [filter, setFilter] = useState<(typeof STATUSES)[number]>('all')
  const [rows, setRows] = useState<SupportTicket[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const load = useCallback(() => {
    listSupportTickets(filter === 'all' ? undefined : filter).then(setRows).catch((e) => setError(e.message))
  }, [filter])
  useEffect(load, [load])
  const remediate = async (id: number) => {
    setBusy(id)
    setError(null)
    try {
      await remediateTicket(id)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }
  return (
    <>
      <PageHead title="Support tickets" subtitle="Feature requests, problem reports, captured errors and guardrail alerts. Each can be handed to Devin, which investigates and opens a PR.">
        <button className="btn sm" onClick={load}>Refresh</button>
      </PageHead>
      <Segmented options={STATUSES} value={filter} onChange={setFilter} labels={{ all: 'All', open: 'Open', queued: 'Queued', devin_working: 'Devin working', remediation_failed: 'Failed' }} />
      <ErrorBox message={error} />
      <div className="table-wrap" style={{ marginTop: 12 }}>
        {!rows ? <Spinner /> : (
          <table>
            <thead><tr><th>#</th><th>Kind</th><th>Title</th><th>Reporter</th><th>Status</th><th>Devin</th><th>Submitted</th><th /></tr></thead>
            <tbody>
              {rows.length === 0 ? <tr><td colSpan={8} className="empty">No tickets</td></tr> : null}
              {rows.map((t) => (
                <tr key={t.id}>
                  <td className="sub">{t.id}</td>
                  <td><Badge kind={t.kind === 'feature' ? 'success' : t.kind === 'error' ? 'danger' : t.kind === 'guardrail' ? 'warning' : 'muted'}>{KIND_LABELS[t.kind] ?? humanize(t.kind)}</Badge></td>
                  <td>
                    <b>{t.title}</b>
                    {t.remediation_error ? <div className="tone-danger" style={{ fontSize: 12 }}>{t.remediation_error}</div> : null}
                  </td>
                  <td className="sub">{t.reporter_email}</td>
                  <td><Badge kind={t.status === 'devin_working' ? 'success' : t.status === 'remediation_failed' ? 'danger' : t.status === 'queued' ? 'pending' : 'muted'}>{humanize(t.status)}</Badge></td>
                  <td>
                    {t.devin_session_url ? <a href={t.devin_session_url} target="_blank" rel="noreferrer">session ↗</a> : '—'}
                  </td>
                  <td className="sub">{dateTime(t.created_at)}</td>
                  <td className="num">
                    {!['devin_working', 'queued'].includes(t.status) ? (
                      <button className="btn sm primary" disabled={busy === t.id} onClick={() => remediate(t.id)}>
                        {busy === t.id ? 'Queueing…' : 'Send to Devin'}
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
