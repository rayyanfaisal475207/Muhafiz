import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { RetrievedDocsSection } from './RetrievedDocsSection'
import type { PipelineEvent } from '../../types'

// Regression test for SCENARIO_TEST_LOG.md Finding E.
//
// The evaluator-feedback retry loop emits one evaluator event per attempt.
// A query that retried therefore has BOTH a "Relevant: False" (rejected
// first attempt) and a later "Relevant: True" (accepted final attempt).
// This panel used events.find(), which returns the FIRST match, while the
// step card above it showed the LAST — so the same response rendered
// "Relevant: True" in the trace and "✗ Not relevant" here at the same time.
// Observed on 3 separate scenarios during the manual run.

function ev(step: string, detail: string, status = 'done'): PipelineEvent {
  return { step, status, detail } as PipelineEvent
}

describe('RetrievedDocsSection relevance verdict', () => {
  it('shows the FINAL evaluator verdict when the retry loop produced several', () => {
    const events = [
      ev('retrieval', '10 chunks retrieved'),
      ev('evaluator', 'Relevant: False — documents do not mention the weapon'),
      ev('retrieval', '12 chunks retrieved'),
      ev('evaluator', 'Relevant: True — documents explicitly name the complainant'),
    ]
    const { container } = render(<RetrievedDocsSection events={events} />)
    expect(container.textContent).toContain('✓ Relevant')
    expect(container.textContent).not.toContain('Not relevant')
  })

  it('still shows not-relevant when the final verdict really is negative', () => {
    const events = [
      ev('retrieval', '3 chunks retrieved'),
      ev('evaluator', 'Relevant: True — looked promising'),
      ev('evaluator', 'Relevant: False — max retries reached, no sufficient evidence'),
    ]
    const { container } = render(<RetrievedDocsSection events={events} />)
    expect(container.textContent).toContain('Not relevant')
  })

  it('handles the simple single-attempt case unchanged', () => {
    const events = [
      ev('retrieval', '12 chunks retrieved'),
      ev('evaluator', 'Relevant: True — documents answer the question'),
    ]
    const { container } = render(<RetrievedDocsSection events={events} />)
    expect(container.textContent).toContain('✓ Relevant')
  })

  it('reports the final retrieval counts, not the first attempt’s', () => {
    const events = [
      ev('retrieval', '3 chunks retrieved'),
      ev('evaluator', 'Relevant: False — not enough'),
      ev('retrieval', '14 chunks retrieved'),
      ev('evaluator', 'Relevant: True — good now'),
    ]
    const { container } = render(<RetrievedDocsSection events={events} />)
    expect(container.textContent).toContain('14 chunks retrieved')
  })
})
