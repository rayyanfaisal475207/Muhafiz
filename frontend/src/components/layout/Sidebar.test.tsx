import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { useAuthStore } from '../../store/authStore'
import { useProjectStore } from '../../store/projectStore'
import { useCaseStore } from '../../store/caseStore'
import { useSessionStore } from '../../store/sessionStore'

// Sidebar's mount effects call fetchProjects/fetchCases/fetchSessions when
// authenticated. Keeping isAuthenticated false in tests that pre-seed store
// state avoids those live network calls overwriting the seeded fields.
function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>,
  )
}

describe('Sidebar — store errors and swallowed action failures are now visible', () => {
  beforeEach(() => {
    useAuthStore.setState({ isAuthenticated: false, user: null })
    useProjectStore.setState({ projects: [], activeProjectId: null, isLoading: false, error: null })
    useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
    useSessionStore.setState({ sessions: [], isLoading: false, error: null })
  })

  it('renders projectStore/caseStore/sessionStore fetch errors, previously never shown anywhere', () => {
    useProjectStore.setState({ error: 'Failed to fetch projects' })
    useCaseStore.setState({ error: 'Failed to fetch cases' })
    useSessionStore.setState({ error: 'Failed to fetch sessions' })

    renderSidebar()

    expect(screen.getByText(/Failed to load workspaces: Failed to fetch projects/)).toBeInTheDocument()
    expect(screen.getByText(/Failed to load cases: Failed to fetch cases/)).toBeInTheDocument()
    expect(screen.getByText(/Failed to load chat history: Failed to fetch sessions/)).toBeInTheDocument()
  })

  it('surfaces a session delete failure instead of only console.error', async () => {
    useSessionStore.setState({
      sessions: [{ session_id: 's1', title: 'Test session', created_at: new Date().toISOString(), updated_at: new Date().toISOString() }],
    })
    vi.spyOn(useSessionStore.getState(), 'deleteSession').mockRejectedValue(new Error('boom'))
    vi.spyOn(console, 'error').mockImplementation(() => {})

    renderSidebar()

    // The row's three action icons are Export/Rename/Delete, in that order;
    // Delete is the only one with no `title` attribute.
    const row = screen.getByText('Test session').closest('.group') as HTMLElement
    const trashButton = row.querySelectorAll('button')[2]
    fireEvent.click(trashButton)
    fireEvent.click(screen.getByText('Confirm'))

    await waitFor(() => {
      expect(screen.getByText('Failed to delete session. Please try again.')).toBeInTheDocument()
    })
  })
})

describe('Sidebar — "All Cases" vs "No Case" label is role-gated', () => {
  // The backend only widens retrieval to every case's evidence for
  // supervisor/station-admin/platform-admin (orchestrator.py's
  // _build_retrieval_where(), same floor as graph_retriever.CROSS_CASE_ROLES).
  // The label must not promise an investigator something the backend
  // never actually grants them.
  beforeEach(() => {
    useProjectStore.setState({ projects: [], activeProjectId: null, isLoading: false, error: null })
    useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
    useSessionStore.setState({ sessions: [], isLoading: false, error: null })
  })

  it.each(['supervisor', 'station-admin', 'platform-admin'])(
    'shows "All Cases" for %s',
    (role) => {
      useAuthStore.setState({ isAuthenticated: false, user: { id: 'u1', email: 'a@b.com', role, is_admin: false } as any })
      renderSidebar()
      expect(screen.getByRole('option', { name: 'All Cases' })).toBeInTheDocument()
    },
  )

  it('shows "No Case" for investigator', () => {
    useAuthStore.setState({ isAuthenticated: false, user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
    renderSidebar()
    expect(screen.getByRole('option', { name: 'No Case' })).toBeInTheDocument()
  })
})
