export const API_URL: string = import.meta.env.VITE_API_URL ?? '/api'

export type Role = 'admin' | 'reviewer' | 'viewer'
export type Status = 'pending' | 'escalated' | 'approved' | 'rejected'

export interface User {
  id: number
  email: string
  name: string
  role: Role
  approval_limit_cents: number
  daily_approval_limit_cents: number
  is_active: boolean
  last_login_at: string | null
}

export interface UserRef {
  id: number
  name: string
}

export interface Permissions {
  can_approve: boolean
  can_reject: boolean
  can_escalate: boolean
  can_view_history: boolean
  can_assign: boolean
  can_approve_partial: boolean
  approve_blocked_reasons: string[]
  escalate_blocked_reasons: string[]
}

export type PayoutStatus = 'queued' | 'retry_scheduled' | 'processing' | 'succeeded' | 'failed'

export interface Payout {
  id: number
  provider: string
  status: PayoutStatus
  amount_cents: number
  currency: string
  provider_refund_id: string | null
  idempotency_key: string
  attempts: number
  manual_retries: number
  next_attempt_at: string | null
  last_http_status: number | null
  last_error: string | null
  failure_code: string | null
  succeeded_at: string | null
  created_at: string
  updated_at: string
}

export const isPartial = (r: { amount_cents: number; approved_amount_cents: number | null }) =>
  r.approved_amount_cents != null && r.approved_amount_cents < r.amount_cents

export const payoutInFlight = (p: Payout | null | undefined) => !!p && p.status !== 'succeeded' && p.status !== 'failed'

export interface Refund {
  id: number
  reference: string
  order_id: string
  amount_cents: number
  approved_amount_cents: number | null
  currency: string
  reason: string
  description: string
  risk_score: number
  status: Status
  requested_by_email: string
  customer: { id: number; external_id: string; name: string; email: string; country: string; pii_masked: boolean }
  assigned_to: UserRef | null
  escalated_by: UserRef | null
  escalation_reason: string | null
  decided_by: UserRef | null
  decision_note: string | null
  decided_at: string | null
  sla_due_at: string
  overdue: boolean
  created_at: string
  permissions: Permissions
  payout: Payout | null
}

export interface RefundEvent {
  id: number
  event_type: string
  actor: UserRef | null
  from_status: string | null
  to_status: string | null
  note: string | null
  created_at: string
}

export interface StatCardData {
  key: string
  label: string
  value: number | null
  format: 'number' | 'money' | 'percent' | 'hours'
  tone: 'default' | 'primary' | 'success' | 'warning' | 'danger'
}

export interface Summary {
  role: Role
  cards: StatCardData[]
  limits?: { approval_limit_cents: number; daily_approval_limit_cents: number; approved_today_cents: number }
}

const KEY = 'refunds.token'
export const getToken = () => localStorage.getItem(KEY)
export const setToken = (t: string | null) => (t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY))

export class ApiError extends Error {
  status: number
  traceId: string | null
  constructor(status: number, message: string, traceId: string | null = null) {
    // The trace id lets support/admins pull up exactly what happened (GET /admin/traces/{id}).
    super(traceId ? `${message} (trace ${traceId})` : message)
    this.status = status
    this.traceId = traceId
  }
}

let onUnauthorized: (() => void) | null = null
export const setUnauthorizedHandler = (fn: () => void) => {
  onUnauthorized = fn
}

async function request(path: string, method = 'GET', body?: unknown): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const t = getToken()
  if (t) headers.Authorization = `Bearer ${t}`
  const res = await fetch(`${API_URL}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  if (res.status === 401 && t) {
    setToken(null)
    onUnauthorized?.()
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg, res.headers.get('X-Trace-Id'))
  }
  return res
}

export async function api<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  return (await request(path, method, body)).json() as Promise<T>
}

export async function download(path: string, filename: string): Promise<Headers> {
  const res = await request(path)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
  return res.headers
}

export const ssoLoginUrl = (redirectUri: string) => `${API_URL}/auth/sso/login?redirect_uri=${encodeURIComponent(redirectUri)}`

export interface AuthConfig {
  environment: string
  mock_sso: boolean
}

export const getAuthConfig = () => api<AuthConfig>('/auth/config')

export interface SupportTicket {
  id: number
  kind: string
  title: string
  description: string
  trace_id: string | null
  reporter_email: string | null
  status: string
  devin_session_url: string | null
  remediation_error: string | null
  created_at: string
}

export const createSupportRequest = (body: { kind: 'feature' | 'support'; title: string; description?: string }) =>
  api<SupportTicket>('/support/requests', 'POST', body)

export const listSupportTickets = (status?: string) =>
  api<SupportTicket[]>(`/admin/support/requests${status ? `?status=${status}` : ''}`)

export const remediateTicket = (id: number) => api<SupportTicket>(`/admin/support/requests/${id}/remediate`, 'POST')
