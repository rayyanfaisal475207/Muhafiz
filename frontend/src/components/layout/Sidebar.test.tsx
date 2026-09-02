import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { useAuthStore } from '../../store/authStore'
import { useProjectStore } from '../../store/projectStore'
import { useCaseStore } from '../../store/caseStore'
import { useSessionStore } from '../../store/sessionStore'
import { SIDEBAR_COLLAPSED_KEY } from '../../lib/constants'

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
      expect(screen.getByRole('combobox', { name: 'Case' })).toHaveValue('All Cases')
    },
  )

  it('shows "No Case" for investigator', () => {
    useAuthStore.setState({ isAuthenticated: false, user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
    renderSidebar()
    expect(screen.getByRole('combobox', { name: 'Case' })).toHaveValue('No Case')
  })
})

describe('Sidebar — collapse/expand (Module 3, FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md)', () => {
  beforeEach(() => {
    localStorage.removeItem(SIDEBAR_COLLAPSED_KEY)
    useAuthStore.setState({ isAuthenticated: false, user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
    useProjectStore.setState({ projects: [], activeProjectId: null, isLoading: false, error: null })
    useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
    useSessionStore.setState({ sessions: [], isLoading: false, error: null })
  })

  it('starts expanded by default (no stored preference) with the Workspace/Case/history sections visible', () => {
    renderSidebar()
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Case' })).toBeInTheDocument()
    expect(screen.getByText('Chat History')).toBeInTheDocument()
    expect(screen.getByText('New Chat')).toBeInTheDocument()
  })

  it('collapsing hides the Workspace/Case/history sections but keeps New Chat and Sign Out reachable as tooltip-labeled icons', () => {
    renderSidebar()
    fireEvent.click(screen.getByLabelText('Collapse sidebar'))

    expect(screen.queryByRole('combobox', { name: 'Workspace' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Case' })).not.toBeInTheDocument()
    expect(screen.queryByText('Chat History')).not.toBeInTheDocument()
    // The label text is gone, but the control is still there with a tooltip.
    expect(screen.queryByText('New Chat')).not.toBeInTheDocument()
    expect(screen.getByLabelText('New Chat')).toBeInTheDocument()
    expect(screen.getByLabelText('New Chat')).toHaveAttribute('title', 'New Chat')
    expect(screen.getByLabelText('Sign Out')).toBeInTheDocument()
    expect(screen.getByLabelText('Sign Out')).toHaveAttribute('title', 'Sign Out')
    expect(screen.getByLabelText('Profile & Settings')).toBeInTheDocument()
  })

  it('re-expands on a second click, restoring the label-dependent sections', () => {
    renderSidebar()
    const toggle = () => screen.getByLabelText(/Collapse sidebar|Expand sidebar/)
    fireEvent.click(toggle())
    expect(screen.queryByText('Chat History')).not.toBeInTheDocument()

    fireEvent.click(toggle())
    expect(screen.getByText('Chat History')).toBeInTheDocument()
    expect(screen.getByText('New Chat')).toBeInTheDocument()
  })

  it('persists the collapsed preference to localStorage on toggle', () => {
    renderSidebar()
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).not.toBe('true')

    fireEvent.click(screen.getByLabelText('Collapse sidebar'))
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('true')

    fireEvent.click(screen.getByLabelText('Expand sidebar'))
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('false')
  })

  it('reads a previously-persisted collapsed preference on a fresh mount', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'true')
    renderSidebar()

    expect(screen.getByLabelText('Expand sidebar')).toBeInTheDocument()
    expect(screen.queryByText('Chat History')).not.toBeInTheDocument()
    expect(screen.getByLabelText('New Chat')).toBeInTheDocument()
  })
})

// Module 8 (FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md): a first-ever
// visit (nothing in SIDEBAR_COLLAPSED_KEY yet) defaults to collapsed below
// the documented 768px minimum-usability breakpoint, via a matchMedia
// check. Any stored preference — from either a manual toggle or a prior
// visit, in either direction — always wins over that viewport check.
describe('Sidebar — first-visit viewport default (Module 8)', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    localStorage.removeItem(SIDEBAR_COLLAPSED_KEY)
    useAuthStore.setState({ isAuthenticated: false, user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
    useProjectStore.setState({ projects: [], activeProjectId: null, isLoading: false, error: null })
    useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
    useSessionStore.setState({ sessions: [], isLoading: false, error: null })
    originalMatchMedia = window.matchMedia
  })

  function mockViewport(matchesNarrow: boolean) {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === '(max-width: 767px)' ? matchesNarrow : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as any
  }

  afterEach(() => {
    window.matchMedia = originalMatchMedia
  })

  it('loads collapsed by default on a first-ever visit narrower than 768px', () => {
    mockViewport(true)
    renderSidebar()
    expect(screen.getByLabelText('Expand sidebar')).toBeInTheDocument()
    expect(screen.queryByText('Chat History')).not.toBeInTheDocument()
  })

  it('loads expanded by default on a first-ever visit at or above 768px', () => {
    mockViewport(false)
    renderSidebar()
    expect(screen.getByLabelText('Collapse sidebar')).toBeInTheDocument()
    expect(screen.getByText('Chat History')).toBeInTheDocument()
  })

  it('a stored "expanded" preference wins over a narrow viewport', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'false')
    mockViewport(true)
    renderSidebar()
    expect(screen.getByLabelText('Collapse sidebar')).toBeInTheDocument()
    expect(screen.getByText('Chat History')).toBeInTheDocument()
  })

  it('a stored "collapsed" preference wins over a wide viewport', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'true')
    mockViewport(false)
    renderSidebar()
    expect(screen.getByLabelText('Expand sidebar')).toBeInTheDocument()
    expect(screen.queryByText('Chat History')).not.toBeInTheDocument()
  })
})
