import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import KnowledgeBasePage from './KnowledgeBasePage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

const STATS = {
  total_chunks: 1,
  total_documents: 1,
  documents: [{ doc_id: 'd1', filename: 'report.pdf', doc_type: 'pdf', chunk_count: 3, is_global: true, ingested_at: null }],
}

describe('KnowledgeBasePage error handling', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a load-error banner and stops loading (does not hang forever) when refresh fails', async () => {
    mockedApi.get.mockRejectedValue({ response: { data: { detail: 'db down' } } })
    render(<KnowledgeBasePage />)

    await waitFor(() => expect(screen.getByText(/Could not load the knowledge base/)).toBeInTheDocument())
    expect(screen.getByText(/db down/)).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('shows a delete-error banner when a delete fails', async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === '/kb/stats') return Promise.resolve({ data: STATS })
      if (url === '/kb/jobs') return Promise.resolve({ data: [] })
      return Promise.resolve({ data: { tables: {} } })
    })
    mockedApi.delete.mockRejectedValue({ response: { data: { detail: 'permission denied' } } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    const user = userEvent.setup()
    render(<KnowledgeBasePage />)

    await waitFor(() => expect(screen.getByText('report.pdf')).toBeInTheDocument())
    await user.click(screen.getByText('Delete'))

    await waitFor(() => expect(screen.getByText(/Delete failed/)).toBeInTheDocument())
    expect(screen.getByText(/permission denied/)).toBeInTheDocument()
  })
})
