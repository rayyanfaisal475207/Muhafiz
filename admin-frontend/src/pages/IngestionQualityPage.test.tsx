import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import IngestionQualityPage from './IngestionQualityPage'
import api from '../api'

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api, true)

const FLAGGED_RUN = {
  run_id: 'sync-20260822T205432Z',
  source: 'sync_muhafiz_data',
  case_id: null,
  started_at: '2026-08-22T20:54:32Z',
  finished_at: '2026-08-22T21:05:25Z',
  tier_cnic_auto: 350,
  tier_flagged_unverified: 66,
  tier_human_review: 0,
  tier_new: 11,
  corroboration_gate_rejections: 11,
  extraction_errors: 0,
  flagged_for_review: true,
  flagged_reason: 'ambiguous-match rate 100.0% vs. baseline avg 10.3% over 6 prior run(s)',
}

const OK_RUN = {
  run_id: 'ingest-doc-1',
  source: 'ingest_file',
  case_id: 'CASE-A',
  started_at: '2026-08-22T10:00:00Z',
  finished_at: '2026-08-22T10:01:00Z',
  tier_cnic_auto: 5,
  tier_flagged_unverified: 1,
  tier_human_review: 0,
  tier_new: 0,
  corroboration_gate_rejections: 0,
  extraction_errors: 0,
  flagged_for_review: false,
  flagged_reason: null,
}

const RUNS_RESPONSE = { data: { runs: [FLAGGED_RUN, OK_RUN], count: 2 } }

describe('IngestionQualityPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.get.mockResolvedValue(RUNS_RESPONSE)
    mockedApi.post.mockResolvedValue({ data: { run_id: FLAGGED_RUN.run_id, acknowledged: true } })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders real tier counts from the API, nothing hardcoded', async () => {
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText(FLAGGED_RUN.run_id)).toBeInTheDocument())
    expect(screen.getByText('350')).toBeInTheDocument()
    expect(screen.getByText('66')).toBeInTheDocument()
    expect(screen.getByText('flagged')).toBeInTheDocument()
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('only shows an Acknowledge button for flagged runs', async () => {
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText(FLAGGED_RUN.run_id)).toBeInTheDocument())
    expect(screen.getAllByText('Acknowledge')).toHaveLength(1)
  })

  it('does not call the API when the acknowledge confirmation dialog is dismissed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText('Acknowledge')).toBeInTheDocument())
    await user.click(screen.getByText('Acknowledge'))

    expect(mockedApi.post).not.toHaveBeenCalled()
  })

  it('acknowledges the correct run_id when confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText('Acknowledge')).toBeInTheDocument())
    await user.click(screen.getByText('Acknowledge'))

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith(
        `/ingestion-quality/${encodeURIComponent(FLAGGED_RUN.run_id)}/acknowledge`, {},
      ),
    )
  })

  it('filters by source', async () => {
    const user = userEvent.setup()
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText(FLAGGED_RUN.run_id)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Muhafiz Data API sync' }))

    await waitFor(() =>
      expect(mockedApi.get).toHaveBeenCalledWith('/ingestion-quality/runs', {
        params: { limit: 100, source: 'sync_muhafiz_data' },
      }),
    )
  })

  it('shows an empty state with no runs', async () => {
    mockedApi.get.mockResolvedValue({ data: { runs: [], count: 0 } })
    render(<IngestionQualityPage />)

    await waitFor(() => expect(screen.getByText('No ingestion runs recorded yet.')).toBeInTheDocument())
  })
})
