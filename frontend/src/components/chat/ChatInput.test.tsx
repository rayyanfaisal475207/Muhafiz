import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ChatInput } from './ChatInput'
import { useAuthStore } from '../../store/authStore'
import { useCaseStore } from '../../store/caseStore'

// Same stale-copy bug as ChatPanel's empty state, one level down: the
// composer's placeholder never reflected which retrieval scope the user
// is actually in (a case, "All Cases" for supervisor+, or the plain
// reference-only scope) -- it always said the same reference-lookup text
// regardless.

function setup(role: string, activeCaseId: string | null = null) {
  useAuthStore.setState({ user: { id: 'u1', email: 'a@b.com', role, is_admin: false } as any })
  useCaseStore.setState({ cases: [], activeCaseId, isLoading: false, error: null })
}

describe('ChatInput — placeholder reflects the active retrieval scope', () => {
  beforeEach(() => {
    setup('investigator')
  })

  it('shows the reference-lookup placeholder for an investigator with no case', () => {
    setup('investigator')
    render(<ChatInput onSend={vi.fn()} onNewSession={vi.fn()} disabled={false} />)
    expect(screen.getByPlaceholderText('Ask about any section, procedure, or SOP…')).toBeInTheDocument()
  })

  it.each(['supervisor', 'station-admin', 'platform-admin'])(
    'shows the All Cases placeholder for %s with no case',
    (role) => {
      setup(role)
      render(<ChatInput onSend={vi.fn()} onNewSession={vi.fn()} disabled={false} />)
      expect(screen.getByPlaceholderText('Ask about any case, connection, or SOP…')).toBeInTheDocument()
    },
  )

  it('shows the case-specific placeholder once a case is active, regardless of role', () => {
    setup('investigator', 'fir-430-26')
    render(<ChatInput onSend={vi.fn()} onNewSession={vi.fn()} disabled={false} />)
    expect(
      screen.getByPlaceholderText("Ask about this case's evidence, entities, or timeline…"),
    ).toBeInTheDocument()
  })
})
