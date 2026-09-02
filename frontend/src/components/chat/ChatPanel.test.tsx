import { describe, it, expect, beforeEach, beforeAll, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { ChatPanel } from './ChatPanel'
import { useAuthStore } from '../../store/authStore'
import { useCaseStore } from '../../store/caseStore'
import { useChatStore } from '../../store/chatStore'

// jsdom doesn't implement scrollIntoView; ChatPanel's auto-scroll-to-bottom
// effect calls it whenever the message list is non-empty.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

// The empty-state landing copy went stale after "All Cases" shipped: a
// supervisor+ user with no case selected is now in a real cross-case
// search scope (orchestrator.py's _build_retrieval_where "All Cases"
// branch, XGRAPH/XAGG reachable), but the landing page still said "Ask
// about police procedure" with only reference-lookup suggestions --
// exactly the same framing an investigator with no cross-case access at
// all sees. These guard the fix: the heading/subtext/suggestions must
// reflect which scope the user is actually in.

function setup(role: string, activeCaseId: string | null = null) {
  useAuthStore.setState({ user: { id: 'u1', email: 'a@b.com', role, is_admin: false } as any })
  useCaseStore.setState({ cases: [], activeCaseId, isLoading: false, error: null })
  useChatStore.getState().reset?.()
  useChatStore.setState({ messages: [], sessionId: 's1' })
}

describe('ChatPanel — empty state reflects the actual retrieval scope', () => {
  beforeEach(() => {
    setup('investigator')
  })

  it('investigator with no case sees the reference-lookup framing, unchanged', () => {
    setup('investigator')
    render(<ChatPanel />)
    expect(screen.getByText('Ask about police procedure')).toBeInTheDocument()
    expect(screen.getByText('What PPC section covers mobile phone theft?')).toBeInTheDocument()
  })

  it.each(['supervisor', 'station-admin', 'platform-admin'])(
    '%s with no case sees the All Cases framing, not the generic one',
    (role) => {
      setup(role)
      render(<ChatPanel />)
      expect(screen.getByText('Ask across every case')).toBeInTheDocument()
      expect(
        screen.getByText('Which cases is a named suspect connected to across the database?'),
      ).toBeInTheDocument()
    },
  )

  it('a supervisor+ user still sees case-specific framing once a case is active', () => {
    useCaseStore.setState({
      cases: [{ case_id: 'fir-430-26', fir_number: '430/26' } as any],
    })
    setup('supervisor', 'fir-430-26')
    useCaseStore.setState({
      cases: [{ case_id: 'fir-430-26', fir_number: '430/26' } as any],
      activeCaseId: 'fir-430-26',
    })
    render(<ChatPanel />)
    expect(screen.getByText('Ask about 430/26')).toBeInTheDocument()
    expect(
      screen.getByText('Has any person, vehicle, or phone number in this case appeared in another case?'),
    ).toBeInTheDocument()
  })
})

// Module 6 (FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md): stickToBottom's
// existing ref already tracked whether the user had scrolled away, but had
// no visible affordance. These guard that the pill only ever appears in
// the exact case already tracked (scrolled away DURING an active stream),
// never when the user is at the bottom, and that it can be dismissed both
// by clicking it and by the user scrolling back down themselves.

function setupChat(messages: any[]) {
  useAuthStore.setState({ user: { id: 'u1', email: 'a@b.com', role: 'investigator', is_admin: false } as any })
  useCaseStore.setState({ cases: [], activeCaseId: null, isLoading: false, error: null })
  useChatStore.getState().reset?.()
  useChatStore.setState({ messages, sessionId: 's1', isStreaming: true })
}

/** jsdom's scrollHeight/clientHeight/scrollTop are inert getters returning
 * 0 — shadow them on the instance so handleScroll's "near bottom" math
 * sees a real scroll position. */
function setScrollState(el: HTMLElement, { scrollHeight, clientHeight, scrollTop }: { scrollHeight: number; clientHeight: number; scrollTop: number }) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  Object.defineProperty(el, 'scrollTop', { value: scrollTop, configurable: true })
  fireEvent.scroll(el)
}

const scrollAway = (el: HTMLElement) => setScrollState(el, { scrollHeight: 1000, clientHeight: 300, scrollTop: 100 }) // gap 600
const scrollToBottomState = (el: HTMLElement) => setScrollState(el, { scrollHeight: 1000, clientHeight: 300, scrollTop: 690 }) // gap 10

describe('ChatPanel — "↓ New messages" pill', () => {
  it('never shows the pill while the user is at the bottom', () => {
    setupChat([{ id: 'm1', role: 'assistant', content: 'hello', isStreaming: true }])
    const { container } = render(<ChatPanel />)
    const scrollEl = container.querySelector('.overflow-y-auto') as HTMLElement
    scrollToBottomState(scrollEl)

    act(() => {
      useChatStore.setState({
        messages: [{ id: 'm1', role: 'assistant', content: 'hello world', isStreaming: true }],
      })
    })

    expect(screen.queryByText('New messages')).not.toBeInTheDocument()
  })

  it('shows the pill once new content lands while the user is scrolled away during an active stream', () => {
    setupChat([{ id: 'm1', role: 'assistant', content: 'hello', isStreaming: true }])
    const { container } = render(<ChatPanel />)
    const scrollEl = container.querySelector('.overflow-y-auto') as HTMLElement
    scrollAway(scrollEl)

    expect(screen.queryByText('New messages')).not.toBeInTheDocument()

    // Next SSE token: content grows while still streaming and scrolled away.
    act(() => {
      useChatStore.setState({
        messages: [{ id: 'm1', role: 'assistant', content: 'hello world', isStreaming: true }],
      })
    })

    expect(screen.getByText('New messages')).toBeInTheDocument()
  })

  it('clicking the pill scrolls to bottom and hides it', () => {
    setupChat([{ id: 'm1', role: 'assistant', content: 'hello', isStreaming: true }])
    const { container } = render(<ChatPanel />)
    const scrollEl = container.querySelector('.overflow-y-auto') as HTMLElement
    scrollAway(scrollEl)
    act(() => {
      useChatStore.setState({
        messages: [{ id: 'm1', role: 'assistant', content: 'hello world', isStreaming: true }],
      })
    })
    expect(screen.getByText('New messages')).toBeInTheDocument()

    fireEvent.click(screen.getByText('New messages'))
    expect(screen.queryByText('New messages')).not.toBeInTheDocument()
  })

  it('hides again once the user scrolls back to the bottom themselves', () => {
    setupChat([{ id: 'm1', role: 'assistant', content: 'hello', isStreaming: true }])
    const { container } = render(<ChatPanel />)
    const scrollEl = container.querySelector('.overflow-y-auto') as HTMLElement
    scrollAway(scrollEl)
    act(() => {
      useChatStore.setState({
        messages: [{ id: 'm1', role: 'assistant', content: 'hello world', isStreaming: true }],
      })
    })
    expect(screen.getByText('New messages')).toBeInTheDocument()

    scrollToBottomState(scrollEl)
    expect(screen.queryByText('New messages')).not.toBeInTheDocument()
  })
})
