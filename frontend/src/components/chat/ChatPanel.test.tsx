import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ChatPanel } from './ChatPanel'
import { useAuthStore } from '../../store/authStore'
import { useCaseStore } from '../../store/caseStore'
import { useChatStore } from '../../store/chatStore'

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
