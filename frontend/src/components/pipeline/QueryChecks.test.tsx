import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryChecks } from './QueryChecks'
import type { DegradationTrace } from '../../types'

/**
 * QueryChecks shows the investigator what was checked for their own query.
 *
 * Two properties matter most and are guarded hardest:
 *  - ALWAYS-ON for a real trace, including clean runs. If it only appeared on
 *    failure, its absence would be ambiguous between "all good" and "didn't
 *    render", and degradation would stop being legible by contrast.
 *  - NOTHING at all when no trace was recorded (legacy/pre-harness message).
 *    That is a different fact from a clean run and must not be shown as one.
 */

const BASE: DegradationTrace = {
  v: 1,
  sub_agent_status: 'ok',
  tools_used: [],
  degraded_from: [],
  contributed_only: [],
  degraded_and_contributed: [],
  degraded_only: [],
  labels: { contributed_only: [], degraded_and_contributed: [], degraded_only: [] },
  caveats: [],
  disclosure_rendered: null,
}

function trace(overrides: Partial<DegradationTrace>): DegradationTrace {
  return {
    ...BASE,
    ...overrides,
    labels: { ...BASE.labels, ...(overrides.labels ?? {}) },
  }
}

const CLEAN = trace({
  tools_used: ['RAG', 'GRAPH'],
  contributed_only: ['RAG', 'GRAPH'],
  labels: { contributed_only: ['document search', 'case-graph search'], degraded_and_contributed: [], degraded_only: [] },
})

describe('QueryChecks', () => {
  it('renders nothing when no trace was recorded', () => {
    const { container } = render(<QueryChecks trace={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for an empty trace with no sources or caveats', () => {
    const { container } = render(<QueryChecks trace={trace({})} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows on a fully clean run, not only on failure', () => {
    render(<QueryChecks trace={CLEAN} />)
    expect(screen.getByText(/Checked: document search, case-graph search/)).toBeTruthy()
  })

  it('flags unavailable sources in the collapsed summary', () => {
    render(
      <QueryChecks
        trace={trace({
          tools_used: ['RAG'],
          degraded_from: ['GRAPH'],
          contributed_only: ['RAG'],
          degraded_only: ['GRAPH'],
          labels: {
            contributed_only: ['document search'],
            degraded_and_contributed: [],
            degraded_only: ['case-graph search'],
          },
        })}
      />,
    )
    expect(screen.getByText(/1 source unavailable/)).toBeTruthy()
  })

  it('pluralizes the unavailable-source count', () => {
    render(
      <QueryChecks
        trace={trace({
          degraded_only: ['GRAPH', 'SQL'],
          labels: {
            contributed_only: [],
            degraded_and_contributed: [],
            degraded_only: ['case-graph search', 'penal-code reference lookup'],
          },
        })}
      />,
    )
    expect(screen.getByText(/2 sources unavailable/)).toBeTruthy()
  })

  it('distinguishes partial contributors from clean ones when expanded', async () => {
    const user = userEvent.setup()
    render(
      <QueryChecks
        trace={trace({
          tools_used: ['RAG', 'GRAPH'],
          degraded_from: ['RAG', 'SQL'],
          contributed_only: ['GRAPH'],
          degraded_and_contributed: ['RAG'],
          degraded_only: ['SQL'],
          labels: {
            contributed_only: ['case-graph search'],
            degraded_and_contributed: ['document search'],
            degraded_only: ['penal-code reference lookup'],
          },
        })}
      />,
    )

    await user.click(screen.getByRole('button'))

    expect(screen.getByText('used')).toBeTruthy()
    expect(screen.getByText('partial')).toBeTruthy()
    expect(screen.getByText('unavailable')).toBeTruthy()
    expect(screen.getByText(/contributed, but not fully checked/)).toBeTruthy()
  })

  it('is collapsed until the investigator opens it', async () => {
    const user = userEvent.setup()
    render(<QueryChecks trace={CLEAN} />)

    const toggle = screen.getByRole('button')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('used')).toBeNull()

    await user.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getAllByText('used').length).toBeGreaterThan(0)
  })

  it('shows caveats when expanded', async () => {
    const user = userEvent.setup()
    render(
      <QueryChecks
        trace={trace({
          contributed_only: ['GRAPH'],
          labels: { contributed_only: ['case-graph search'], degraded_and_contributed: [], degraded_only: [] },
          caveats: ['The relevance check could not run for this search.'],
        })}
      />,
    )

    await user.click(screen.getByRole('button'))
    expect(screen.getByText(/relevance check could not run/)).toBeTruthy()
  })

  it('renders when a trace has only caveats and no sources', () => {
    render(
      <QueryChecks trace={trace({ caveats: ['Something was degraded.'] })} />,
    )
    expect(screen.getByText(/No sources were available/)).toBeTruthy()
  })

  it('renders backend labels verbatim, never raw tool identifiers', () => {
    render(<QueryChecks trace={CLEAN} />)

    const text = document.body.textContent ?? ''
    expect(text).toContain('document search')
    expect(text).not.toContain('RAG')
    expect(text).not.toContain('GRAPH')
  })

  it('survives a trace missing its labels block', () => {
    // Defensive: an older persisted payload (v1 pre-labels) must not crash the
    // chat. pipeline_steps/messages rows are permanent history.
    const legacy = { ...BASE, labels: undefined } as unknown as DegradationTrace
    const { container } = render(<QueryChecks trace={legacy} />)
    expect(container).toBeEmptyDOMElement()
  })
})
