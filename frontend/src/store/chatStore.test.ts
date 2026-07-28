import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useChatStore } from './chatStore'
import * as api from '../lib/api'

// sendMessage awaits streamChat(); resolve/reject it manually per call to
// simulate a slow first request still in flight when the second one fires.
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof api>('../lib/api')
  return {
    ...actual,
    streamChat: vi.fn(),
    apiClient: actual.apiClient,
  }
})

describe('chatStore.sendMessage — rapid double-send', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.mocked(api.streamChat).mockReset()
  })

  it('closes out an orphaned in-flight assistant message instead of leaving it stuck streaming', async () => {
    let releaseFirst!: () => void
    const firstStreamPromise = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    vi.mocked(api.streamChat)
      .mockImplementationOnce(() => firstStreamPromise) // first send: never resolves on its own
      .mockImplementationOnce(() => new Promise<void>((resolve) => resolve())) // second send: resolves immediately

    const firstSend = useChatStore.getState().sendMessage('first message')
    // Let the first send's synchronous setup (adding the placeholder message) run.
    await Promise.resolve()
    await Promise.resolve()

    const midState = useChatStore.getState()
    const firstAssistant = midState.messages.find((m) => m.role === 'assistant')
    expect(firstAssistant?.isStreaming).toBe(true)

    // Rapid second send before the first ever resolves.
    const secondSend = useChatStore.getState().sendMessage('second message')

    // The first assistant message must be closed out immediately, synchronously
    // with the second send's own state update — not left stuck forever.
    const afterSecondSendStarts = useChatStore.getState()
    const stillThereFirstAssistant = afterSecondSendStarts.messages.find((m) => m.id === firstAssistant!.id)
    expect(stillThereFirstAssistant?.isStreaming).toBe(false)

    releaseFirst()
    await firstSend
    await secondSend

    const finalMessages = useChatStore.getState().messages
    expect(finalMessages.find((m) => m.id === firstAssistant!.id)?.isStreaming).toBe(false)
  })
})
