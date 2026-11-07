import { useEffect, useState } from 'react'

import { getAuthConfig, ssoLoginUrl, type AuthConfig } from '../api'
import { useAuth } from '../auth'
import { ErrorBox } from '../components/ui'

export function Login() {
  const { error } = useAuth()
  const [cfg, setCfg] = useState<AuthConfig | null>(null)
  const [cfgError, setCfgError] = useState<string | null>(null)
  useEffect(() => {
    getAuthConfig().then(setCfg).catch((e) => setCfgError((e as Error).message))
  }, [])
  const signIn = () => {
    window.location.href = ssoLoginUrl(`${window.location.origin}/`)
  }
  return (
    <div className="login">
      <div className="card">
        <div className="eyebrow">ACMEPAY INTERNAL</div>
        <h1>Refunds Console</h1>
        <p className="muted" style={{ fontSize: 14 }}>
          Review, approve and audit customer refunds. Access is role-based and every action is logged.
        </p>
        <ErrorBox message={error ?? cfgError} />
        {cfg?.mock_sso ? (
          <>
            <button className="btn primary" onClick={signIn} data-testid="sso-login">Sign in with Acme SSO</button>
            <p className="muted" style={{ textAlign: 'center', marginTop: 16 }}>OIDC authorization-code flow · demo identity provider</p>
          </>
        ) : cfg ? (
          <p className="muted" style={{ fontSize: 14 }}>
            This deployment ({cfg.environment}) uses your corporate single sign-on. Contact your administrator if you
            cannot sign in.
          </p>
        ) : null}
      </div>
    </div>
  )
}
