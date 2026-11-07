import type { ReactNode } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router-dom'

import type { User } from './api'
import { useAuth } from './auth'
import { RoleBadge, Spinner } from './components/ui'
import { money } from './format'
import { AllRefunds, AuditLog, Escalations, Policies, SupportTickets, Team, ViewAs } from './pages/Admin'
import { Analytics } from './pages/Analytics'
import { Payouts } from './pages/Payouts'
import { Login } from './pages/Login'
import { ReviewerHistory, ReviewerQueue } from './pages/ReviewerQueue'

interface Page {
  path: string
  label: string
  element: ReactNode
}

function pagesFor(user: User): Page[] {
  if (user.role === 'reviewer') {
    return [
      { path: '/queue', label: 'My queue', element: <ReviewerQueue user={user} /> },
      { path: '/history', label: 'My history', element: <ReviewerHistory /> },
    ]
  }
  if (user.role === 'admin') {
    return [
      { path: '/analytics', label: 'Analytics', element: <Analytics user={user} /> },
      { path: '/escalations', label: 'Escalations', element: <Escalations /> },
      { path: '/refunds', label: 'All refunds', element: <AllRefunds canCreate /> },
      { path: '/payouts', label: 'Payouts', element: <Payouts /> },
      { path: '/team', label: 'Team & limits', element: <Team me={user} /> },
      { path: '/policies', label: 'Policies', element: <Policies /> },
      { path: '/audit', label: 'Audit log', element: <AuditLog /> },
      { path: '/support', label: 'Support tickets', element: <SupportTickets /> },
      { path: '/view-as', label: 'View as', element: <ViewAs me={user} /> },
    ]
  }
  return [
    { path: '/analytics', label: 'Analytics', element: <Analytics user={user} /> },
    { path: '/refunds', label: 'All refunds', element: <AllRefunds canCreate={false} viewer /> },
  ]
}

export default function App() {
  const { user, loading, logout } = useAuth()
  if (loading) return <Spinner />
  if (!user) return <Login />
  const pages = pagesFor(user)

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="topbar-row">
            <div className="brand">
              Refunds Console<small>AcmePay internal</small>
            </div>
            <div className="who">
              <div>
                <div className="name">{user.name}</div>
                <div className="email">
                  {user.email}
                  {user.role !== 'viewer' ? ` · limit ${money(user.approval_limit_cents)}` : ''}
                </div>
              </div>
              <RoleBadge role={user.role} />
              <button className="btn sm ghost-dark" onClick={logout}>Sign out</button>
            </div>
          </div>
          <nav className="nav">
            {pages.map((p) => (
              <NavLink key={p.path} to={p.path} className={({ isActive }) => (isActive ? 'active' : '')}>
                {p.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="page">
        <Routes>
          {pages.map((p) => (
            <Route key={p.path} path={p.path} element={p.element} />
          ))}
          {/* Any route not granted to this role falls back to the role's home page. */}
          <Route path="*" element={<Navigate to={pages[0].path} replace />} />
        </Routes>
      </main>
    </>
  )
}
