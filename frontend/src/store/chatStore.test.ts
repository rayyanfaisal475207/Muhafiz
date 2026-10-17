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

describe('evidence session reload', () => {
  it('preserves a legacy turn written after an evidence run during rollback', async () => {
    const get=vi.spyOn(api.apiClient,'get')
    get.mockResolvedValueOnce({data:{
      history:[{role:'assistant',content:'Legacy after rollback',created_at:'2026-09-24T11:00:00Z'}],
      evidence_runs:[{run_id:'run-1',question:'Evidence question',created_at:'2026-09-24T10:00:00Z',
        status:'answered',response:{text:'Checked answer',status:'answered',results:[]}}],
    }})
    get.mockResolvedValueOnce({data:{attachments:[]}})
    await useChatStore.getState().loadSession('session-1')
    expect(useChatStore.getState().messages.map(m=>m.content)).toEqual([
      'Evidence question','Checked answer','Legacy after rollback',
    ])
    get.mockRestore()
  })

  it('restores the recorded trace so a refresh does not lose it', async () => {
    // The runtime writes its step-by-step trace into
    // evidence.runs.response.health, so it outlives the SSE stream. Before
    // this, a reloaded conversation showed the answer with no record of how
    // it was reached -- and the traces that explain a wrong answer are
    // exactly the ones worth keeping.
    const trace = [
      { seq: 1, stage: 'question', title: 'Question received', at_ms: 0 },
      { seq: 2, stage: 'plan_rejected', title: 'The plan was rejected', at_ms: 1200 },
    ]
    const get = vi.spyOn(api.apiClient, 'get')
    get.mockResolvedValueOnce({ data: {
      history: [],
      evidence_runs: [{ run_id: 'run-2', question: 'Who is Ahmed?', created_at: '2026-09-25T10:00:00Z',
        status: 'partial',
        response: { text: 'Not established', status: 'partial', results: [], health: { trace } } }],
    }})
    get.mockResolvedValueOnce({ data: { attachments: [] } })
    await useChatStore.getState().loadSession('session-2')

    const answer = useChatStore.getState().messages.find((m) => m.id === 'run-2')
    expect(answer).toBeTruthy()
    const events = answer!.pipelineEvents ?? []
    expect(events).toHaveLength(1)
    expect(events[0].step).toBe('response')
    expect(events[0].trace).toEqual(trace)
    get.mockRestore()
  })

  it('leaves pipelineEvents alone when a run recorded no trace', async () => {
    const get = vi.spyOn(api.apiClient, 'get')
    get.mockResolvedValueOnce({ data: {
      history: [],
      evidence_runs: [{ run_id: 'run-3', question: 'Older run', created_at: '2026-09-25T10:00:00Z',
        status: 'answered', response: { text: 'An answer', status: 'answered', results: [] } }],
    }})
    get.mockResolvedValueOnce({ data: { attachments: [] } })
    await useChatStore.getState().loadSession('session-3')
    const answer = useChatStore.getState().messages.find((m) => m.id === 'run-3')
    expect(answer?.pipelineEvents).toBeUndefined()
    get.mockRestore()
  })
})
