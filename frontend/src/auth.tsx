import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

import { api, getToken, setToken, setUnauthorizedHandler, type User } from './api'

interface AuthState {
  user: User | null
  loading: boolean
  error: string | null
  logout: () => void
}

const AuthContext = createContext<AuthState>({ user: null, loading: true, error: null, logout: () => {} })

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null))
    const url = new URL(window.location.href)
    const code = url.searchParams.get('code')
    const run = async () => {
      try {
        if (code) {
          url.searchParams.delete('code')
          window.history.replaceState({}, '', url.pathname + url.search)
          const res = await api<{ access_token: string; user: User }>('/auth/token', 'POST', { code })
          setToken(res.access_token)
          setUser(res.user)
        } else if (getToken()) {
          setUser(await api<User>('/auth/me'))
        }
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setLoading(false)
      }
    }
    run()
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  return <AuthContext.Provider value={{ user, loading, error, logout }}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => useContext(AuthContext)
