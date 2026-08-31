import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageBubble } from './MessageBubble'
import type { ChatMessage } from '../../types'

// Regression tests for the two markdown-rendering defects found during the
// 20-scenario manual test run (SCENARIO_TEST_LOG.md Findings B and I).
//
// Finding B: single-asterisk *emphasis* rendered as literal asterisks
//   ("A *30-bore pistol*") because only the ** form was parsed.
// Finding I: a ranked numbered list rendered every item as "1." because a
//   blank line between items flushed the list, so each item became its own
//   <ol> restarting at 1 — losing the ranking, which WAS the information.

function assistant(content: string): ChatMessage {
  return { id: 'm1', role: 'assistant', content }
}

function renderContent(content: string) {
  const { container } = render(<MessageBubble message={assistant(content)} />)
  return container
}

describe('MessageBubble markdown rendering', () => {
  // ── Finding B ──────────────────────────────────────────────────────────
  it('renders single-asterisk emphasis as <em>, not literal asterisks', () => {
    const c = renderContent('The weapon was a *30-bore pistol* recovered on site.')
    const em = c.querySelector('em')
    expect(em?.textContent).toBe('30-bore pistol')
    expect(c.textContent).not.toContain('*')
  })

  it('still renders double-asterisk bold as <strong>', () => {
    const c = renderContent('Weapon: **30-bore pistol** seized.')
    expect(c.querySelector('strong')?.textContent).toBe('30-bore pistol')
    expect(c.textContent).not.toContain('*')
  })

  it('renders bold and emphasis together without mangling either', () => {
    const c = renderContent('**Bold** then *emphasis* then plain.')
    expect(c.querySelector('strong')?.textContent).toBe('Bold')
    expect(c.querySelector('em')?.textContent).toBe('emphasis')
    expect(c.textContent).not.toContain('*')
  })

  it('leaves a lone/unpaired asterisk alone rather than eating text', () => {
    const c = renderContent('Rate is 5 * 3 per unit.')
    expect(c.textContent).toContain('5 * 3')
  })

  // ── Finding I ──────────────────────────────────────────────────────────
  it('keeps one ordered list across blank-line-separated items', () => {
    const c = renderContent(
      ['1. Faisal appears in 3 cases', '', '2. Tariq appears in 2 cases', '', '3. Bilal appears in 2 cases'].join('\n'),
    )
    const lists = c.querySelectorAll('ol')
    expect(lists).toHaveLength(1)
    expect(lists[0].querySelectorAll('li')).toHaveLength(3)
  })

  it('preserves the list start number so a split list does not restart at 1', () => {
    // A numbered list that begins at 2 (e.g. after a heading split) must
    // render starting at 2, not silently renumber from 1.
    const c = renderContent('2. Second item\n3. Third item')
    expect(c.querySelector('ol')?.getAttribute('start')).toBe('2')
  })

  it('renders unordered lists as a single <ul>', () => {
    const c = renderContent('- alpha\n- beta\n- gamma')
    const uls = c.querySelectorAll('ul')
    expect(uls).toHaveLength(1)
    expect(uls[0].querySelectorAll('li')).toHaveLength(3)
  })

  // ── Existing behaviour that must not regress ──────────────────────────
  it('still renders [Document N] citations as chips', () => {
    const c = renderContent('The pistol was seized [Document 2].')
    expect(screen.getByText('[Document 2]')).toBeTruthy()
  })

  it('renders markdown headings as real heading elements', () => {
    const c = renderContent('#### Incident Details\nSome text.')
    expect(c.querySelector('h5')?.textContent).toBe('Incident Details')
    expect(c.textContent).not.toContain('####')
  })
})
