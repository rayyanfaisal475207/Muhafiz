import React, { useState } from 'react'
import { useAuth } from '../AuthContext'

const LoginPage: React.FC = () => {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    const ok = await login(username, password)
    if (!ok) setError('Invalid credentials. Check email and password.')
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">M</div>
          <div>
            <div className="login-logo-text">Muhafiz</div>
            <div className="login-logo-sub">Admin Console</div>
          </div>
        </div>

        <div className="login-title">Sign in</div>
        <div className="login-sub">Restricted access — authorized personnel only.</div>

        {error && <div className="login-error">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label className="form-label" htmlFor="login-email">Email</label>
            <input
              id="login-email"
              className="form-input"
              type="text"
              autoComplete="username"
              value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="admin@example.com"
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="login-password">Password</label>
            <input
              id="login-password"
              className="form-input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="••••••••••••"
            />
          </div>
          <button type="submit" className="login-btn">Sign in →</button>
        </form>
      </div>
    </div>
  )
}

export default LoginPage
