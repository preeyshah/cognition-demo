import { useCallback, useEffect, useState } from 'react'

import { api, type Payout, type PayoutStatus } from '../api'
import { dateTime, humanize, money, timeAgo } from '../format'
import { Badge, ErrorBox, PageHead, PayoutBadge, Segmented, Spinner, StatGrid } from '../components/ui'

interface PayoutRow extends Payout {
  refund_id: number
  reference: string
  order_id: string
  customer_external_id: string
  approved_by: string | null
}
interface PayoutList { items: PayoutRow[]; counts: Record<PayoutStatus, number>; paid_24h_cents: number; sandbox_enabled: boolean }
interface WebhookRow { event_id: string; event_type: string; provider_refund_id: string | null; outcome: string; received_at: string }
interface ReconcileResult { checked: number; changed: { reference: string; from: string; to: string }[]; errors: { reference: string; error: string }[] }

const FILTERS = ['all', 'queued', 'retry_scheduled', 'processing', 'succeeded', 'failed'] as const
const SANDBOX: { mode: string; label: string; help: string }[] = [
  { mode: 'outage', label: 'Outage (next 2 calls → 503)', help: 'Worker retries with exponential backoff + jitter, same idempotency key.' },
  { mode: 'lost_response', label: 'Lost response (504 after refunding)', help: 'Retry is replayed by the provider — the customer is never refunded twice.' },
  { mode: 'decline', label: 'Decline next refund (402)', help: 'Definitive failure: no retries; an admin can re-drive it with a new key.' },
  { mode: 'async_fail', label: 'Fail after acceptance', help: 'Accepted, then a signed refund.failed webhook arrives.' },
  { mode: 'drop_webhook', label: 'Drop next webhook', help: 'Payout stays “processing” until Reconcile asks the provider.' },
]

export function Payouts() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('all')
  const [data, setData] = useState<PayoutList | null>(null)
  const [hooks, setHooks] = useState<WebhookRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    const qs = filter === 'all' ? '' : `&status=${filter}`
    Promise.all([api<PayoutList>(`/admin/payouts?limit=200${qs}`), api<WebhookRow[]>('/admin/payouts/webhooks?limit=15')])
      .then(([d, h]) => {
        setData(d)
        setHooks(h)
        setError(null)
      })
      .catch((e) => setError(e.message))
  }, [filter])
  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [load])

  const run = async (fn: () => Promise<string>) => {
    setBusy(true)
    setError(null)
    try {
      setNotice(await fn())
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const reconcile = () =>
    run(async () => {
      const r = await api<ReconcileResult>('/admin/payouts/reconcile', 'POST')
      const changed = r.changed.map((c) => `${c.reference}: ${c.from} → ${c.to}`).join(', ')
      return `Reconciled ${r.checked} in-flight payout(s) with PayCore. ${changed ? `Fixed drift: ${changed}.` : 'No drift.'}${r.errors.length ? ` ${r.errors.length} lookup error(s).` : ''}`
    })
  const retry = (p: PayoutRow) =>
    run(async () => {
      const r = await api<PayoutRow>(`/admin/payouts/${p.id}/retry`, 'POST')
      await api('/admin/payouts/run-due', 'POST')
      return `${p.reference} re-queued with idempotency key ${r.idempotency_key}.`
    })
  const sandbox = (mode: string, label: string) =>
    run(async () => {
      await api('/admin/payouts/sandbox', 'POST', { mode, count: mode === 'outage' ? 2 : 1 })
      return `Sandbox armed: ${label}. Approve a refund to see it.`
    })

  const c = data?.counts
  return (
    <>
      <PageHead
        title="Payouts"
        subtitle="Approved refunds are paid back through the PayCore payments API: idempotent requests, automatic retries, HMAC-signed webhooks and reconciliation. Admin-only; every action is audited."
      >
        <button className="btn" disabled={busy} onClick={reconcile}>Reconcile with PayCore</button>
      </PageHead>
      {c ? (
        <StatGrid
          cards={[
            { key: 'paid', label: 'Paid out (24h)', value: data.paid_24h_cents, format: 'money', tone: 'success' },
            { key: 'flight', label: 'In flight', value: c.queued + c.processing, format: 'number', tone: 'primary' },
            { key: 'retry', label: 'Retrying', value: c.retry_scheduled, format: 'number', tone: c.retry_scheduled ? 'warning' : 'default' },
            { key: 'failed', label: 'Failed — needs action', value: c.failed, format: 'number', tone: c.failed ? 'danger' : 'default' },
            { key: 'ok', label: 'Succeeded (all time)', value: c.succeeded, format: 'number', tone: 'default' },
          ]}
        />
      ) : null}
      <ErrorBox message={error} />
      {notice ? <div className="notice" data-testid="payout-notice">{notice}</div> : null}

      {data?.sandbox_enabled ? (
        <>
          <div className="section-title">Provider sandbox — inject failures</div>
          <div className="grid stats">
            {SANDBOX.map((s) => (
              <div key={s.mode} className="card">
                <button className="btn sm" disabled={busy} onClick={() => sandbox(s.mode, s.label)}>{s.label}</button>
                <div className="muted" style={{ marginTop: 6 }}>{s.help}</div>
              </div>
            ))}
          </div>
        </>
      ) : null}

      <div className="section-title">
        Payouts
        <Segmented options={FILTERS} value={filter} onChange={setFilter} />
      </div>
      <div className="table-wrap">
        {!data ? <Spinner /> : (
          <table>
            <thead>
              <tr><th>Refund</th><th>Amount</th><th>Status</th><th>Provider ref</th><th>Attempts</th><th>Detail</th><th>Updated</th><th /></tr>
            </thead>
            <tbody>
              {data.items.length === 0 ? <tr><td colSpan={8} className="empty">No payouts</td></tr> : null}
              {data.items.map((p) => (
                <tr key={p.id} data-testid={`payout-${p.reference}`}>
                  <td><div className="ref">{p.reference}</div><div className="sub">{p.customer_external_id} · approved by {p.approved_by ?? '—'}</div></td>
                  <td className="num"><b>{money(p.amount_cents)}</b></td>
                  <td><PayoutBadge payout={p} /></td>
                  <td className="sub">{p.provider_refund_id ?? '—'}<div><code>{p.idempotency_key}</code></div></td>
                  <td className="num">{p.attempts}{p.manual_retries ? <div className="sub">{p.manual_retries} manual</div> : null}</td>
                  <td className="sub" style={{ maxWidth: 320 }}>
                    {p.status === 'retry_scheduled' && p.next_attempt_at ? `${p.last_error ?? 'error'} · next try ${timeAgo(p.next_attempt_at)}` : null}
                    {p.status === 'failed' ? `${p.failure_code}${p.last_error ? ` — ${p.last_error}` : ''}` : null}
                    {p.status === 'succeeded' && p.succeeded_at ? `Paid ${dateTime(p.succeeded_at)}` : null}
                    {p.status === 'processing' ? 'Accepted; awaiting settlement webhook' : null}
                  </td>
                  <td className="sub">{timeAgo(p.updated_at)}</td>
                  <td className="num">
                    {p.status === 'failed' ? <button className="btn sm primary" disabled={busy} onClick={() => retry(p)}>Retry</button> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="section-title">Webhook deliveries (signature-verified)</div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Received</th><th>Event</th><th>Type</th><th>Provider ref</th><th>Outcome</th></tr></thead>
          <tbody>
            {hooks.length === 0 ? <tr><td colSpan={5} className="empty">No webhooks yet — approve a refund.</td></tr> : null}
            {hooks.map((h) => (
              <tr key={h.event_id}>
                <td className="sub">{dateTime(h.received_at)}</td>
                <td className="sub"><code>{h.event_id}</code></td>
                <td><b>{h.event_type}</b></td>
                <td className="sub">{h.provider_refund_id}</td>
                <td><Badge kind={h.outcome === 'applied' ? 'success' : 'muted'}>{humanize(h.outcome)}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
