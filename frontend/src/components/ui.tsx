import { useEffect, type ReactNode } from 'react'

import type { StatCardData } from '../api'
import { formatValue, humanize } from '../format'

export const Spinner = () => <div className="spinner" />

export const ErrorBox = ({ message }: { message: string | null | undefined }) => (message ? <div className="error">{message}</div> : null)

export function StatGrid({ cards }: { cards: StatCardData[] }) {
  return (
    <div className="grid stats">
      {cards.map((c) => (
        <div key={c.key} className="card stat">
          <div className="label">{c.label}</div>
          <div className={`value tone-${c.tone}`}>{formatValue(c.value, c.format)}</div>
        </div>
      ))}
    </div>
  )
}

export const Badge = ({ kind, children }: { kind: string; children: ReactNode }) => <span className={`badge b-${kind}`}>{children}</span>
export const StatusBadge = ({ status }: { status: string }) => <Badge kind={status}>{status}</Badge>
export const RoleBadge = ({ role }: { role: string }) => <Badge kind={role}>{role}</Badge>
export const RiskBadge = ({ score }: { score: number }) => (
  <Badge kind={score >= 70 ? 'danger' : score >= 40 ? 'warning' : 'success'}>risk {score}</Badge>
)

export function Segmented<T extends string | number>({
  options,
  value,
  onChange,
  labels,
}: {
  options: readonly T[]
  value: T
  onChange: (v: T) => void
  labels?: Partial<Record<T, string>>
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={String(o)} className={o === value ? 'on' : ''} onClick={() => onChange(o)}>
          {labels?.[o] ?? humanize(String(o))}
        </button>
      ))}
    </div>
  )
}

function useEscape(onClose: () => void) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
}

export function Modal({ title, onClose, children, width = 560 }: { title: ReactNode; onClose: () => void; children: ReactNode; width?: number }) {
  useEscape(onClose)
  return (
    <div className="overlay center" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ maxWidth: width }} role="dialog">
        <div className="sheet-head">
          <h2>{title}</h2>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="sheet-body">{children}</div>
      </div>
    </div>
  )
}

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEscape(onClose)
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer" role="dialog">
        <div className="sheet-head">
          <h2>{title}</h2>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="sheet-body">{children}</div>
      </aside>
    </div>
  )
}

export function HBar({ label, value, max, right, color = 'var(--primary)' }: { label: string; value: number; max: number; right: string; color?: string }) {
  return (
    <div className="hbar">
      <div className="top">
        <span>{label}</span>
        <span className="muted">{right}</span>
      </div>
      <div className="bar">
        <div style={{ width: `${max ? (value / max) * 100 : 0}%`, background: color }} />
      </div>
    </div>
  )
}

export function PageHead({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      <div className="row">{children}</div>
    </div>
  )
}
