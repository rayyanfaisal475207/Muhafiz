import React, { createContext, useContext, useState } from 'react'

// Any role at or above 'supervisor' may use the admin app at all; individual
// pages/nav items further restrict to 'station-admin' or 'platform-admin'
// where the backend itself requires it (case assignments, platform-wide
// metrics). 'investigator' is excluded — that role uses the main chat app.
export const ADMIN_ROLES = ['supervisor', 'station-admin', 'platform-admin'] as const
export type AdminRole = typeof ADMIN_ROLES[number]

interface AuthContextType {
  isAuthenticated: boolean
  role: AdminRole | null
  email: string | null
  login: (user: string, pass: string) => Promise<boolean>
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

function readStoredRole(): AdminRole | null {
  const stored = localStorage.getItem('muhafiz_admin_role')
  return (ADMIN_ROLES as readonly string[]).includes(stored || '') ? (stored as AdminRole) : null
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [role, setRole] = useState<AdminRole | null>(readStoredRole)
  const [email, setEmail] = useState<string | null>(() => localStorage.getItem('muhafiz_admin_email'))
  // isAuthenticated requires a VALID role, not just the old auth flag — a
  // session created before role-based auth existed has `muhafiz_admin_auth
  // = true` with no role ever stored. Treating that as authenticated made
  // ProtectedLayout render routes that RequireRole immediately bounced out
  // of (role is null) back to /login, which then bounced straight back to
  // "/" (the stale flag still said authenticated) — an infinite redirect
  // loop ("Maximum update depth exceeded"), not a rendering bug.
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    const flag = localStorage.getItem('muhafiz_admin_auth') === 'true'
    const validRole = readStoredRole() !== null
    if (flag && !validRole) {
      localStorage.removeItem('muhafiz_admin_auth')
      localStorage.removeItem('muhafiz_admin_email')
      return false
    }
    return flag
  })

  const login = async (user: string, pass: string) => {
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email: user, password: pass })
      });

      if (!res.ok) return false;

      // The login endpoint accepts ANY valid user — verify the account is
      // at least 'supervisor' before opening the admin UI (investigator-role
      // accounts use the main chat app instead), otherwise a regular user
      // sees the whole shell with every request failing 403.
      const meRes = await fetch('/api/auth/me', { credentials: 'include' });
      if (!meRes.ok) return false;
      const me = await meRes.json();
      if (!ADMIN_ROLES.includes(me.role)) {
        // Not admin-app-eligible — drop the session we just created.
        await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
        return false;
      }

      setIsAuthenticated(true);
      setRole(me.role);
      setEmail(me.email);
      localStorage.setItem('muhafiz_admin_auth', 'true');
      localStorage.setItem('muhafiz_admin_role', me.role);
      localStorage.setItem('muhafiz_admin_email', me.email || '');
      return true;
    } catch (err) {
      console.error(err);
    }
    return false;
  }

  const logout = () => {
    // Invalidate the HttpOnly cookie server-side — clearing localStorage
    // alone left a valid 7-day session behind.
    const match = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'))
    fetch('/api/auth/logout', {
      method: 'POST',
      credentials: 'include',
      headers: match ? { 'X-CSRF-Token': match[2] } : undefined,
    }).catch(() => {})
    setIsAuthenticated(false)
    setRole(null)
    setEmail(null)
    localStorage.removeItem('muhafiz_admin_auth')
    localStorage.removeItem('muhafiz_admin_role')
    localStorage.removeItem('muhafiz_admin_email')
  }

  return (
    <AuthContext.Provider value={{ isAuthenticated, role, email, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be inside AuthProvider')
  return ctx
}
