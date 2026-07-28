import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AuditLogPage from './AuditLogPage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

const LOG_OK = {
  log_id: 'log-1',
  timestamp: '2026-07-01T12:00:00Z',
  event_type: 'admin_action',
  user_id: 'user-1111-2222',
  case_id: 'CASE-1',
  details: { action: 'delete_kb_document', target_email: 'someone@example.com', payload: { victim_info: 'sensitive' } },
}

describe('AuditLogPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders a date-range picker', async () => {
    mockedApi.get.mockResolvedValue({ data: [LOG_OK] })
    render(<AuditLogPage />)

    await waitFor(() => expect(screen.getByText('CASE-1')).toBeInTheDocument())
    expect(screen.getByRole('group', { name: 'Time range' })).toBeInTheDocument()
  })

  it('clears stale rows instead of leaving them under the error banner on a failed fetch', async () => {
    mockedApi.get.mockResolvedValueOnce({ data: [LOG_OK] })
    render(<AuditLogPage />)
    await waitFor(() => expect(screen.getByText('CASE-1')).toBeInTheDocument())

    mockedApi.get.mockRejectedValueOnce({ response: { data: { detail: 'Forbidden' } } })
    const user = userEvent.setup()
    await user.click(screen.getByText('↻ Refresh'))

    await waitFor(() => expect(screen.getByText(/Could not load audit logs/)).toBeInTheDocument())
    expect(screen.queryByText('CASE-1')).not.toBeInTheDocument()
  })

  it('debounces the event-type filter instead of firing a request on every keystroke', async () => {
    mockedApi.get.mockResolvedValue({ data: [] })
    render(<AuditLogPage />)

    await waitFor(() => expect(mockedApi.get).toHaveBeenCalledTimes(1))

    const input = screen.getByLabelText('Filter by event type')
    fireEvent.change(input, { target: { value: 'a' } })
    fireEvent.change(input, { target: { value: 'ad' } })
    fireEvent.change(input, { target: { value: 'adm' } })
    // Immediately after typing, no new request yet (still debouncing).
    expect(mockedApi.get).toHaveBeenCalledTimes(1)

    // Real 300ms debounce - one request, not one per keystroke.
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalledTimes(2), { timeout: 1000 })
  })

  it('redacts sensitive fields (payload, target_email) in the expanded details panel', async () => {
    mockedApi.get.mockResolvedValue({ data: [LOG_OK] })
    const user = userEvent.setup()
    render(<AuditLogPage />)

    await waitFor(() => expect(screen.getByText('CASE-1')).toBeInTheDocument())
    await user.click(screen.getByText('CASE-1').closest('tr')!)

    await waitFor(() => {
      const panel = document.querySelector('.expand-panel')
      expect(panel).not.toBeNull();
      expect(panel!.textContent).toContain('[redacted]')
      expect(panel!.textContent).not.toContain('someone@example.com')
      expect(panel!.textContent).not.toContain('sensitive')
    })
  })

  it('requests through the shared axios instance (withCredentials + CSRF, not a bare fetch)', async () => {
    mockedApi.get.mockResolvedValue({ data: [] })
    render(<AuditLogPage />)

    await waitFor(() => expect(mockedApi.get).toHaveBeenCalled())
    expect(mockedApi.get).toHaveBeenCalledWith('/audit-logs', expect.objectContaining({
      params: expect.objectContaining({ limit: 100, offset: 0, days: 30 }),
    }))
  })
})
