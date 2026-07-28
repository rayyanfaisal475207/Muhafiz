import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DashboardPage from './DashboardPage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

const OK: Record<string, unknown> = {
  '/usage': { total_requests: 5, timeseries: [], routing: [] },
  '/latency': { summary: { count: 1, avg_ms: 1, p50_ms: 1, p95_ms: 1, max_ms: 1 }, timeseries: [], by_step: [] },
  '/kb/stats': { total_chunks: 1, total_documents: 1, documents: [] },
  '/errors/trend': { total: 0, timeseries: [] },
  '/verifier/stats': { total_gated: 0, passed: 0, regenerated: 0, pass_rate_pct: 0, regeneration_rate_pct: 0 },
  '/instrumentation': { tables: {} },
}

describe('DashboardPage loading indicator on range change', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows an "Updating…" indicator on a subsequent range change, not the full first-load skeleton', async () => {
    const pendingResolvers: Array<() => void> = []
    let callCount = 0
    mockedApi.get.mockImplementation((url: string) => {
      callCount += 1
      if (callCount <= 6) return Promise.resolve({ data: OK[url] })
      // second round of calls (after the range change) - hold until released below
      return new Promise((resolve) => {
        pendingResolvers.push(() => resolve({ data: OK[url] }))
      })
    })

    const user = userEvent.setup()
    render(<DashboardPage />)

    await waitFor(() => expect(screen.getByText('Dashboard')).toBeInTheDocument())
    expect(screen.queryByText('Updating…')).not.toBeInTheDocument()

    await user.click(screen.getByText('30d'))

    await waitFor(() => expect(screen.getByText('Updating…')).toBeInTheDocument())

    // release the held second-round requests so the effect can clean up
    pendingResolvers.forEach((resolve) => resolve())
  })
})
