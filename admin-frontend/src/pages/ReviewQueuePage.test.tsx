import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ReviewQueuePage from './ReviewQueuePage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

const PENDING_RESPONSE = {
  data: {
    pending: [
      {
        edge_id: 42,
        tier: 'flagged_unverified' as const,
        confidence: 0.9,
        basis: 'matched on name + shared case',
        as_of: null,
        source_doc_id: null,
        source_chunk_id: null,
        mention: { entity_id: 'e1', type: 'Person', canonical_name: 'Ali Khan', cnic: null, plate: null },
        candidate: { entity_id: 'e2', type: 'Person', canonical_name: 'Ali Khan', cnic: null, plate: null },
      },
    ],
    count: 1,
  },
}
const STATS_RESPONSE = { data: {} }

describe('ReviewQueuePage confirm/reject confirmation dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.get.mockImplementation((url: string) => {
      if (url.includes('/pending')) return Promise.resolve(PENDING_RESPONSE)
      return Promise.resolve(STATS_RESPONSE)
    })
    mockedApi.post.mockResolvedValue({ data: {} })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not call the API when the confirmation dialog is dismissed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()
    render(<ReviewQueuePage />)

    await waitFor(() => expect(screen.getByText('Confirm')).toBeInTheDocument())
    await user.click(screen.getByText('Confirm'))

    expect(window.confirm).toHaveBeenCalled()
    expect(mockedApi.post).not.toHaveBeenCalled()
  })

  it('calls the API without a client-supplied reviewed_by when confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()
    render(<ReviewQueuePage />)

    await waitFor(() => expect(screen.getByText('Confirm')).toBeInTheDocument())
    await user.click(screen.getByText('Confirm'))

    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith('/graph-review/42/confirm', {}))
  })

  it('does not call the API for reject when the dialog is dismissed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()
    render(<ReviewQueuePage />)

    await waitFor(() => expect(screen.getByText('Reject')).toBeInTheDocument())
    await user.click(screen.getByText('Reject'))

    expect(mockedApi.post).not.toHaveBeenCalled()
  })
})
