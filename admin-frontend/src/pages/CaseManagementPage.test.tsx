import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CaseManagementPage from './CaseManagementPage'
import { AuthProvider } from '../AuthContext'
import casesApi from '../casesApi'

vi.mock('../casesApi', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

const mockedCasesApi = vi.mocked(casesApi, true)

const CASE = {
  case_id: 'case-1',
  fir_number: 'FIR-1',
  crime_category: 'Theft',
  investigation_officer: 'IO Ahmed',
  police_station: 'Station A',
  incident_date: null,
  investigation_status: 'Open',
  location: null,
  description: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}
const ASSIGNMENT = { user_id: 'user-1', email: 'investigator@example.com', role: 'investigator' }

function renderAsPlatformAdmin() {
  localStorage.setItem('muhafiz_admin_role', 'platform-admin')
  localStorage.setItem('muhafiz_admin_auth', 'true')
  return render(
    <AuthProvider>
      <CaseManagementPage />
    </AuthProvider>
  )
}

describe('CaseManagementPage unassign confirmation dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockedCasesApi.get.mockImplementation((url: string) => {
      if (url.includes('/assignments/')) return Promise.resolve({ data: [ASSIGNMENT] })
      return Promise.resolve({ data: [CASE] })
    })
    mockedCasesApi.delete.mockResolvedValue({ data: {} })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not call the API when the confirmation dialog is dismissed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()
    renderAsPlatformAdmin()

    await waitFor(() => expect(screen.getByText('FIR-1')).toBeInTheDocument())
    await user.click(screen.getByText('FIR-1'))

    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument())
    await user.click(screen.getByText('Remove'))

    expect(window.confirm).toHaveBeenCalled()
    expect(mockedCasesApi.delete).not.toHaveBeenCalled()
  })

  it('calls the delete API when the dialog is confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()
    renderAsPlatformAdmin()

    await waitFor(() => expect(screen.getByText('FIR-1')).toBeInTheDocument())
    await user.click(screen.getByText('FIR-1'))

    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument())
    await user.click(screen.getByText('Remove'))

    await waitFor(() => expect(mockedCasesApi.delete).toHaveBeenCalledWith('/cases/case-1/assignments/user-1'))
  })
})
