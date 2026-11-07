const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const usdShort = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })

export const money = (cents: number | null | undefined) => (cents == null ? '—' : usd.format(cents / 100))
export const moneyShort = (cents: number | null | undefined) => (cents == null ? '—' : usdShort.format(cents / 100))
export const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)

export function formatValue(value: number | null, format: string) {
  if (value == null) return '—'
  if (format === 'money') return moneyShort(value)
  if (format === 'percent') return pct(value)
  if (format === 'hours') return `${value}h`
  return value.toLocaleString()
}

export const humanize = (s: string) => s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())

export function timeAgo(iso: string) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  const abs = Math.abs(diff)
  const [n, u] = abs < 3600 ? [Math.round(abs / 60), 'm'] : abs < 86400 ? [Math.round(abs / 3600), 'h'] : [Math.round(abs / 86400), 'd']
  return diff >= 0 ? `${n}${u} ago` : `in ${n}${u}`
}

export const dateTime = (iso: string) =>
  new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

export const dollars = (cents: number) => String(cents / 100)
export const toCents = (v: string) => Math.round(parseFloat(v || '0') * 100)
