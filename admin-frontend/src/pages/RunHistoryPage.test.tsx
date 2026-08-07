import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StepTrace } from './RunHistoryPage'

/**
 * StepTrace renders the per-query degradation trace the harness supervisor
 * writes into pipeline_steps.output_summary.trace.
 *
 * The case worth guarding hardest is the OVERLAP: a tool appearing in both
 * tools_used and degraded_from contributed data but degraded internally while
 * doing so. Showing it as merely "contributed" overstates the evidence base;
 * showing it as merely "fell back" understates what the answer rests on. It
 * must read as its own third thing.
 */

const TRACE_BASE = {
  v: 1,
  sub_agent_status: 'partial',
  tools_used: [] as string[],
  degraded_from: [] as string[],
  contributed_only: [] as string[],
  degraded_and_contributed: [] as string[],
  degraded_only: [] as string[],
  caveats: [] as string[],
  disclosure_rendered: null as boolean | null,
}

function summaryWith(overrides: Partial<typeof TRACE_BASE>) {
  return { detail: 'a step happened', trace: { ...TRACE_BASE, ...overrides } }
}

describe('StepTrace', () => {
  it('renders nothing when the step carries no trace', () => {
    const { container } = render(<StepTrace summary={{ detail: 'tool ran' }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a legacy row with no output_summary at all', () => {
    const { container } = render(<StepTrace summary={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when a trace exists but reports no degradation', () => {
    // A fully clean run should not add visual noise to every step.
    const { container } = render(
      <StepTrace summary={summaryWith({ sub_agent_status: 'ok' })} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('labels a plain contributor', () => {
    render(
      <StepTrace
        summary={summaryWith({ tools_used: ['RAG'], contributed_only: ['RAG'] })}
      />,
    )
    expect(screen.getByText(/RAG · contributed$/)).toBeTruthy()
  })

  it('labels a tool that fell back', () => {
    render(
      <StepTrace
        summary={summaryWith({ degraded_from: ['GRAPH'], degraded_only: ['GRAPH'] })}
      />,
    )
    expect(screen.getByText(/GRAPH · fell back/)).toBeTruthy()
  })

  it('distinguishes the overlap case from both exclusive cases', () => {
    // Case Summarization already produces this: RAG returned evidence, but its
    // relevance gate was unavailable while doing so.
    render(
      <StepTrace
        summary={summaryWith({
          tools_used: ['RAG', 'GRAPH'],
          degraded_from: ['RAG', 'SQL'],
          contributed_only: ['GRAPH'],
          degraded_and_contributed: ['RAG'],
          degraded_only: ['SQL'],
        })}
      />,
    )

    expect(screen.getByText(/RAG · contributed, degraded/)).toBeTruthy()
    expect(screen.getByText(/GRAPH · contributed$/)).toBeTruthy()
    expect(screen.getByText(/SQL · fell back/)).toBeTruthy()
    // The overlapped tool must NOT also appear as a plain contributor.
    expect(screen.queryByText(/RAG · contributed$/)).toBeNull()
  })

  it('renders the Investigative Analysis collapse shape correctly', () => {
    // Three tools attempted, GRAPH and SQL both fell back to RAG, so one
    // effective source remains. The display must not imply three sources.
    render(
      <StepTrace
        summary={summaryWith({
          tools_used: ['RAG'],
          degraded_from: ['GRAPH', 'SQL'],
          contributed_only: ['RAG'],
          degraded_only: ['GRAPH', 'SQL'],
        })}
      />,
    )

    expect(screen.getByText(/RAG · contributed$/)).toBeTruthy()
    expect(screen.getByText(/GRAPH · fell back/)).toBeTruthy()
    expect(screen.getByText(/SQL · fell back/)).toBeTruthy()
  })

  it('shows caveats', () => {
    render(
      <StepTrace
        summary={summaryWith({
          tools_used: ['GRAPH'],
          contributed_only: ['GRAPH'],
          caveats: ['The relevance check could not run for this search.'],
        })}
      />,
    )
    expect(screen.getByText(/relevance check could not run/)).toBeTruthy()
  })

  it('flags a document that discloses its own partiality', () => {
    render(
      <StepTrace
        summary={summaryWith({
          tools_used: ['GRAPH'],
          contributed_only: ['GRAPH'],
          disclosure_rendered: true,
        })}
      />,
    )
    expect(screen.getByText(/disclosure in document/)).toBeTruthy()
  })

  it('does not flag disclosure when a file was produced without one', () => {
    render(
      <StepTrace
        summary={summaryWith({
          tools_used: ['GRAPH'],
          contributed_only: ['GRAPH'],
          disclosure_rendered: false,
        })}
      />,
    )
    expect(screen.queryByText(/disclosure in document/)).toBeNull()
  })
})
