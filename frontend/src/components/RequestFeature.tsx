import { useState } from 'react'

import { createSupportRequest, type SupportTicket } from '../api'
import { ErrorBox, Modal, Segmented } from './ui'

const KIND_LABELS = { feature: 'Feature request', support: 'Report a problem' } as const
type Kind = keyof typeof KIND_LABELS

/** Topbar button → modal → ticket → Devin session → PR. Available to every role. */
export function RequestFeature() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button className="btn sm primary" onClick={() => setOpen(true)} data-testid="request-feature">
        Request a feature
      </button>
      {open ? <RequestModal onClose={() => setOpen(false)} /> : null}
    </>
  )
}

function RequestModal({ onClose }: { onClose: () => void }) {
  const [kind, setKind] = useState<Kind>('feature')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ticket, setTicket] = useState<SupportTicket | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      setTicket(await createSupportRequest({ kind, title, description }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Request a feature or report a problem" onClose={ticket ? onClose : onClose} width={560}>
      {ticket ? (
        <div data-testid="request-submitted">
          <p>
            <b>Ticket #{ticket.id}</b> submitted — <b>{ticket.title}</b>
          </p>
          <p className="muted" style={{ fontSize: 14 }}>
            {ticket.status === 'queued'
              ? 'Devin is investigating now. If it\u2019s actionable, it will reproduce the request, implement it and open a pull request — you can follow progress on the ticket.'
              : 'Recorded. The team will triage it shortly.'}
          </p>
          {ticket.remediation_error ? <ErrorBox message={ticket.remediation_error} /> : null}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn primary" onClick={onClose}>Done</button>
          </div>
        </div>
      ) : (
        <div className="grid">
          <Segmented options={['feature', 'support'] as const} value={kind} onChange={setKind} labels={KIND_LABELS} />
          <label className="field">Title
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={kind === 'feature' ? 'e.g. Bulk-approve refunds under $50' : 'e.g. Export button fails on Analytics'} data-testid="request-title" />
          </label>
          <label className="field">Details
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What should it do / what went wrong? The more context, the better the PR." data-testid="request-description" />
          </label>
          <ErrorBox message={error} />
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            Submissions are sent to Devin, which investigates and opens a pull request when it can — every request is audited.
          </p>
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || title.trim().length < 5} onClick={submit} data-testid="request-submit">
              {busy ? 'Submitting…' : 'Submit & auto-fix'}
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}
