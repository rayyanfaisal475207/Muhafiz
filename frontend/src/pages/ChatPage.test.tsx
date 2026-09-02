import { describe, it, expect, beforeAll, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { ChatPanel } from '../components/chat/ChatPanel'
import { CitationPanel } from '../components/chat/CitationPanel'
import { useAuthStore } from '../store/authStore'
import { useCaseStore } from '../store/caseStore'
import { useChatStore } from '../store/chatStore'
import type { Source } from '../types'

// Module 2 of FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md: PipelinePanel's
// permanently-reserved right column is gone; chat is full width, and a
// clicked citation opens CitationPanel as an on-demand overlay instead.
// This exercises the exact composition ChatPage uses (ChatPanel's
// onSourceClick driving CitationPanel's mount), without ChatPage's own
// routing/session-restore plumbing, which is unit-tested at the store level
// elsewhere.

function ChatPageLike() {
  const [activeSource, setActiveSource] = useState<Source | null>(null)
  return (
    <div>
      <div data-testid="chat-column">
        <ChatPanel onSourceClick={(s) => setActiveSource(s)} />
      </div>
      {activeSource && <CitationPanel source={activeSource} onClose={() => setActiveSource(null)} />}
    </div>
  )
}

// jsdom doesn't implement scrollIntoView; ChatPanel's auto-scroll-to-bottom
// effect calls it on every render once messages are non-empty (unrelated to
// what this file tests).
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

function setup() {
  useAuthStore.setState({ user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
  useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
  useChatStore.getState().reset?.()
  useChatStore.setState({
    sessionId: 's1',
    messages: [
      {
        id: 'm1',
        role: 'assistant',
        content: 'Section 379 PPC covers mobile phone theft.',
        sources: [{ filename: 'FIR-1001-26.pdf', type: 'document' } as Source],
        isStreaming: false,
      },
    ],
  })
}

describe('Chat layout — citation overlay, not a reserved column', () => {
  it('renders no PipelinePanel/citation column when nothing is active', () => {
    setup()
    render(<ChatPageLike />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Pipeline Trace')).not.toBeInTheDocument()
    expect(screen.queryByText('Retrieved Docs')).not.toBeInTheDocument()
  })

  it('clicking a citation chip opens the overlay dialog', async () => {
    setup()
    render(<ChatPageLike />)
    await userEvent.click(screen.getByTitle('FIR-1001-26.pdf'))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('FIR-1001-26.pdf')).toBeInTheDocument()
  })

  it('closing the overlay (Escape) unmounts it and returns focus to the chip', async () => {
    setup()
    render(<ChatPageLike />)
    const chip = screen.getByTitle('FIR-1001-26.pdf')
    await userEvent.click(chip)
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(chip).toHaveFocus())
  })
})
