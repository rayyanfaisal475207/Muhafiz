import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Sidebar from './Sidebar'
import { AuthProvider } from '../AuthContext'

function renderAs(role: string) {
  localStorage.setItem('muhafiz_admin_role', role)
  localStorage.setItem('muhafiz_admin_auth', 'true')
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Sidebar />
      </AuthProvider>
    </MemoryRouter>
  )
}

describe('Sidebar Audit Logs nav visibility', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('shows the Audit Logs link for platform-admin', () => {
    renderAs('platform-admin')
    expect(screen.getByText('Audit Logs')).toBeInTheDocument()
  })

  it('hides the Audit Logs link for supervisor, matching the backend platform-admin-only gate', () => {
    renderAs('supervisor')
    expect(screen.queryByText('Audit Logs')).not.toBeInTheDocument()
  })

  it('hides the Audit Logs link for station-admin', () => {
    renderAs('station-admin')
    expect(screen.queryByText('Audit Logs')).not.toBeInTheDocument()
  })
})
