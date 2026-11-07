import { useCallback, useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, ComposedChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { api, download, type Summary, type User } from '../api'
import { ErrorBox, HBar, PageHead, RoleBadge, Segmented, Spinner, StatGrid } from '../components/ui'
import { humanize, moneyShort, pct } from '../format'

interface AnalyticsData {
  totals: Record<string, number | null>
  by_status: { status: string; count: number; amount_cents: number }[]
  by_reason: { reason: string; count: number; amount_cents: number; approved_amount_cents: number; approval_rate: number | null }[]
  daily: { date: string; created: number; approved: number; rejected: number; approved_amount_cents: number }[]
  risk_buckets: { bucket: string; count: number; approval_rate: number | null }[]
  reviewers: { id: number; name: string; role: string; decided: number; approved: number; rejected: number; escalated: number; approved_amount_cents: number; approval_rate: number | null; avg_decision_hours: number | null; open_assigned: number; approval_limit_cents: number }[]
  top_customers: { external_id: string; name: string; count: number; amount_cents: number }[]
}

const STATUS_COLORS: Record<string, string> = { pending: '#4f46e5', escalated: '#d97706', approved: '#059669', rejected: '#dc2626' }

export function Analytics({ user }: { user: User }) {
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setError(null)
    Promise.all([api<Summary>('/dashboard/summary'), api<AnalyticsData>(`/analytics/overview?days=${days}`)])
      .then(([s, a]) => {
        setSummary(s)
        setData(a)
      })
      .catch((e) => setError(e.message))
  }, [days])
  useEffect(load, [load])

  return (
    <>
      <PageHead title="Refund analytics" subtitle={user.role === 'viewer' ? 'Read-only view · customer PII is masked for your role.' : 'Live operational picture across all reviewers.'}>
        <Segmented options={[7, 30, 90] as const} value={days} onChange={setDays} labels={{ 7: '7 days', 30: '30 days', 90: '90 days' }} />
        {user.role === 'admin' ? <button className="btn sm" onClick={() => download('/admin/export/refunds.csv', 'refunds.csv')}>Export CSV</button> : null}
        <button className="btn sm" onClick={load}>Refresh</button>
      </PageHead>
      <ErrorBox message={error} />
      {!summary || !data ? (
        <Spinner />
      ) : (
        <>
          <StatGrid cards={summary.cards} />
          <div className="section-title">Last {days} days</div>
          <StatGrid
            cards={[
              { key: 'c', label: 'Refund requests', value: data.totals.count, format: 'number', tone: 'default' },
              { key: 'a', label: 'Requested', value: data.totals.amount_cents, format: 'money', tone: 'default' },
              { key: 'r', label: 'Refunded', value: data.totals.approved_amount_cents, format: 'money', tone: 'success' },
              { key: 'ar', label: 'Approval rate', value: data.totals.approval_rate, format: 'percent', tone: 'default' },
              { key: 'er', label: 'Escalation rate', value: data.totals.escalation_rate, format: 'percent', tone: 'warning' },
              { key: 'sla', label: 'SLA breach rate', value: data.totals.sla_breach_rate, format: 'percent', tone: (data.totals.sla_breach_rate ?? 0) > 0.1 ? 'danger' : 'default' },
            ]}
          />

          <div className="grid cols-3-1" style={{ marginTop: 12 }}>
            <div className="card">
              <h3>Daily volume and refunded amount</h3>
              <ResponsiveContainer width="100%" height={260}>
                <ComposedChart data={data.daily.map((d) => ({ ...d, date: d.date.slice(5), refunded: d.approved_amount_cents / 100 }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef0f5" />
                  <XAxis dataKey="date" fontSize={11} />
                  <YAxis yAxisId="l" fontSize={11} allowDecimals={false} />
                  <YAxis yAxisId="r" orientation="right" fontSize={11} tickFormatter={(v) => `$${v >= 1000 ? `${Math.round(v / 1000)}k` : v}`} />
                  <Tooltip formatter={(v, n) => (n === 'Refunded $' ? `$${Number(v).toLocaleString()}` : v)} />
                  <Legend />
                  <Bar yAxisId="l" dataKey="created" name="Requests" fill="#c7d2fe" radius={[3, 3, 0, 0]} />
                  <Line yAxisId="r" dataKey="refunded" name="Refunded $" stroke="#059669" strokeWidth={2} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
            <div className="card">
              <h3>Status mix</h3>
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie data={data.by_status} dataKey="count" nameKey="status" innerRadius={55} outerRadius={90} paddingAngle={2}>
                    {data.by_status.map((s) => (
                      <Cell key={s.status} fill={STATUS_COLORS[s.status]} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="grid cols-2" style={{ marginTop: 12 }}>
            <div className="card">
              <h3>By reason</h3>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={data.by_reason.map((r) => ({ name: humanize(r.reason), requested: r.amount_cents / 100, refunded: r.approved_amount_cents / 100 }))} layout="vertical" margin={{ left: 40 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef0f5" />
                  <XAxis type="number" fontSize={11} tickFormatter={(v) => `$${Math.round(v / 1000)}k`} />
                  <YAxis type="category" dataKey="name" fontSize={11} width={120} />
                  <Tooltip formatter={(v) => `$${Number(v).toLocaleString()}`} />
                  <Legend />
                  <Bar dataKey="requested" fill="#c7d2fe" name="Requested $" />
                  <Bar dataKey="refunded" fill="#4f46e5" name="Refunded $" />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="card">
              <h3>Approval rate by risk band</h3>
              {data.risk_buckets.map((b) => (
                <HBar key={b.bucket} label={b.bucket} value={b.approval_rate ?? 0} max={1} right={`${pct(b.approval_rate)} of ${b.count}`} color="var(--warning)" />
              ))}
              <h3 style={{ marginTop: 20 }}>Top customers by refund amount</h3>
              {data.top_customers.map((c) => (
                <HBar key={c.external_id} label={`${c.name} (${c.external_id})`} value={c.amount_cents} max={data.top_customers[0]?.amount_cents ?? 1} right={`${c.count} · ${moneyShort(c.amount_cents)}`} color="var(--danger)" />
              ))}
            </div>
          </div>

          <div className="section-title">Team performance</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th><th className="num">Open</th><th className="num">Decided</th><th className="num">Approved</th><th className="num">Rejected</th>
                  <th className="num">Approval rate</th><th className="num">Escalated</th><th className="num">Refunded</th><th className="num">Avg hours</th><th className="num">Per-refund limit</th>
                </tr>
              </thead>
              <tbody>
                {data.reviewers.map((r) => (
                  <tr key={r.id}>
                    <td><b>{r.name}</b> <RoleBadge role={r.role} /></td>
                    <td className="num">{r.open_assigned}</td>
                    <td className="num">{r.decided}</td>
                    <td className="num">{r.approved}</td>
                    <td className="num">{r.rejected}</td>
                    <td className="num">{pct(r.approval_rate)}</td>
                    <td className="num">{r.escalated}</td>
                    <td className="num">{moneyShort(r.approved_amount_cents)}</td>
                    <td className="num">{r.avg_decision_hours ?? '—'}</td>
                    <td className="num">{moneyShort(r.approval_limit_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
