import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import ErrorsPage from './ErrorsPage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

describe('ErrorsPage fetch failure handling', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('surfaces a visible error banner instead of rendering "no errors" when the fetch rejects', async () => {
    mockedApi.get.mockRejectedValue({ response: { data: { detail: 'backend unreachable' } } })
    render(<ErrorsPage />)

    await waitFor(() => expect(screen.getByText(/Could not load errors/)).toBeInTheDocument())
    expect(screen.getByText(/backend unreachable/)).toBeInTheDocument()
  })

  it('does not show the error banner on a successful fetch', async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === '/errors') return Promise.resolve({ data: { errors: [], facets: { modules: [], error_types: [], severities: [] } } })
      if (url === '/errors/trend') return Promise.resolve({ data: { total: 0, timeseries: [] } })
      return Promise.resolve({ data: { tables: {} } })
    })
    render(<ErrorsPage />)

    await waitFor(() => expect(screen.getByText('No errors recorded in this period.')).toBeInTheDocument())
    expect(screen.queryByText(/Could not load errors/)).not.toBeInTheDocument()
  })
})
