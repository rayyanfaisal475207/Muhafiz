import { describe, it, expect, vi, beforeEach } from 'vitest'
import { apiClient } from '../lib/api'
import { LAST_SESSION_KEY } from '../lib/constants'
import { useAuthStore } from './authStore'
import { useChatStore } from './chatStore'
import { useCaseStore } from './caseStore'
import { useProjectStore } from './projectStore'
import { useSessionStore } from './sessionStore'

describe('authStore.logout', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()

    useAuthStore.setState({ user: { id: '1', email: 'a@b.com', role: 'investigator', is_admin: false, company_name: null, plan: 'free' }, isAuthenticated: true, error: 'stale error' })
    useChatStore.setState({ messages: [{ id: 'm1', role: 'assistant', content: 'leftover', isStreaming: true }] as any })
    useCaseStore.setState({ cases: [{ case_id: 'c1' } as any], activeCaseId: 'c1', error: 'stale' })
    useProjectStore.setState({ projects: [{ id: 'p1' } as any], activeProjectId: 'p1', error: 'stale' })
    useSessionStore.setState({ sessions: [{ session_id: 's1' } as any], error: 'stale' })
    localStorage.setItem(LAST_SESSION_KEY, 's1')
  })

  it('clears every store and the unscoped last-session key, even when the backend call fails', async () => {
    vi.spyOn(apiClient, 'post').mockRejectedValueOnce(new Error('network down'))

    await useAuthStore.getState().logout()

    expect(useAuthStore.getState().user).toBeNull()
    expect(useAuthStore.getState().isAuthenticated).toBe(false)

    expect(useChatStore.getState().messages).toEqual([])
    expect(useChatStore.getState().isStreaming).toBe(false)

    expect(useCaseStore.getState().cases).toEqual([])
    expect(useCaseStore.getState().activeCaseId).toBeNull()

    expect(useProjectStore.getState().projects).toEqual([])
    expect(useProjectStore.getState().activeProjectId).toBeNull()

    expect(useSessionStore.getState().sessions).toEqual([])

    expect(localStorage.getItem(LAST_SESSION_KEY)).toBeNull()
  })
})
