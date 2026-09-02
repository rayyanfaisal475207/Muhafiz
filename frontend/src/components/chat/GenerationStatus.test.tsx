import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GenerationStatus } from './GenerationStatus'
import type { PipelineEvent } from '../../types'

// Companion to RetrievedDocsSection.test.tsx / PipelineStepCard's confidence
// badge — GenerationStatus now consolidates both, per
// FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md Module 1.

function ev(step: string, detail: string, extra: Partial<PipelineEvent> = {}): PipelineEvent {
  return { step, status: 'done', detail, ...extra } as PipelineEvent
}

/** Renders already-done (isStreaming=false), then expands the reasoning trail. */
async function renderExpanded(events: PipelineEvent[]) {
  const { container } = render(
    <GenerationStatus events={events} isStreaming={false} hasContent={true} />,
  )
  const button = container.querySelector('button')!
  await userEvent.click(button)
  return container
}

describe('GenerationStatus — consolidated pipeline signals', () => {
  it('shows not-relevant on a retries-exhausted abstention (status="done", no prior verdict)', async () => {
    const events = [
      ev('retrieval', '5 chunks retrieved'),
      ev(
        'evaluator',
        'Relevant: False — no sufficient evidence found after 2 retries; abstaining rather than answering unsupported',
      ),
    ]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('✗ Not relevant')
    expect(container.textContent).not.toContain('Evaluated')
  })

  it('shows the accepted verdict on a relevant/accepted-after-retry case', async () => {
    const events = [
      ev('retrieval', '10 chunks retrieved', { retry_num: 0 }),
      ev('evaluator', 'Relevant: False — documents do not mention the weapon', { retry_num: 0 }),
      ev('retrieval', '12 chunks retrieved', { retry_num: 1 }),
      ev('evaluator', 'Relevant: True — documents explicitly name the complainant', { retry_num: 1 }),
    ]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('✓ Relevant')
  })

  it('renders a high-confidence (green tier) graph badge with hop count', async () => {
    const events = [ev('retrieval', '4 chunks retrieved', { graph_confidence: 0.85, hop_count: 2 })]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('confidence 85%')
    expect(container.textContent).toContain('2 hops')
  })

  it('renders a medium-confidence (yellow tier) graph badge', async () => {
    const events = [ev('retrieval', '4 chunks retrieved', { graph_confidence: 0.5, hop_count: 1 })]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('confidence 50%')
    expect(container.textContent).toContain('1 hop')
  })

  it('renders a low-confidence (red tier) graph badge with "direct" wording at 0 hops', async () => {
    const events = [ev('retrieval', '4 chunks retrieved', { graph_confidence: 0.2, hop_count: 0 })]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('confidence 20%')
    expect(container.textContent).toContain('direct')
  })

  it('does not render a confidence badge when graph_confidence is absent', async () => {
    const events = [ev('retrieval', '4 chunks retrieved')]
    const container = await renderExpanded(events)
    expect(container.textContent).not.toContain('confidence')
  })

  it('labels the retrieval/reranker breakdown and truncates a long evaluator detail', async () => {
    const events = [
      ev('retrieval', '12 chunks retrieved'),
      ev('reranker', '5 chunks after rerank'),
      ev(
        'evaluator',
        'Relevant: True — this is a very long evaluator detail string that should be truncated past sixty characters',
      ),
    ]
    const container = await renderExpanded(events)
    expect(container.textContent).toContain('Semantic: 12 chunks retrieved')
    expect(container.textContent).toContain('After RRF: 5 chunks after rerank')
    expect(container.textContent).toContain('…')
  })
})
