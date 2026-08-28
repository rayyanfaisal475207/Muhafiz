import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { streamChat, STREAM_STALL_TIMEOUT_MS } from './api'

function sseChunk(obj: unknown): Uint8Array {
  return new TextEncoder().encode(`data: ${JSON.stringify(obj)}\n\n`)
}

describe('streamChat — stall detection', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.fetch = originalFetch
  })

  it('rejects with a stall error if no chunk arrives within the timeout, and cancels the reader', async () => {
    const cancel = vi.fn().mockResolvedValue(undefined)
    let readCallCount = 0
    const reader = {
      read: vi.fn().mockImplementation(() => {
        readCallCount += 1
        if (readCallCount === 1) {
          // First chunk arrives promptly.
          return Promise.resolve({ done: false, value: sseChunk({ step: 'retrieval', status: 'active' }) })
        }
        // Second read never resolves on its own — simulates a stalled connection.
        return new Promise(() => {})
      }),
      cancel,
    }

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: { getReader: () => reader },
      text: () => Promise.resolve(''),
    }) as unknown as typeof fetch

    const onEvent = vi.fn()
    const promise = streamChat('session-1', 'hello', onEvent)
    // Attach the rejection assertion before advancing timers, so the
    // rejection is handled the instant it occurs rather than flagged as
    // transiently "unhandled" by fake-timer microtask ordering.
    const assertion = expect(promise).rejects.toThrow(/stalled/i)

    // Let the first (immediately-resolved) read settle.
    await vi.advanceTimersByTimeAsync(0)
    expect(onEvent).toHaveBeenCalledTimes(1)

    // Advance past the stall timeout for the second read, which never
    // resolves. Imported, not a second hardcoded copy of the value -- a
    // prior bump of the real constant (90s -> 150s) left this test still
    // advancing only 90s, hanging on Vitest's real 5000ms watchdog instead
    // of the fake-timer path it exists to exercise.
    await vi.advanceTimersByTimeAsync(STREAM_STALL_TIMEOUT_MS)

    await assertion
    expect(cancel).toHaveBeenCalled()
  })
})

describe('streamChat — malformed SSE chunks', () => {
  const originalFetch = globalThis.fetch

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('logs a console.warn with the offending raw chunk instead of silently dropping it', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const chunks = [
      new TextEncoder().encode('data: {not valid json}\n\n'),
      sseChunk({ step: 'retrieval', status: 'active' }),
    ]
    let i = 0
    const reader = {
      read: vi.fn().mockImplementation(() => {
        if (i < chunks.length) {
          const value = chunks[i]
          i += 1
          return Promise.resolve({ done: false, value })
        }
        return Promise.resolve({ done: true, value: undefined })
      }),
      cancel: vi.fn().mockResolvedValue(undefined),
    }

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: { getReader: () => reader },
      text: () => Promise.resolve(''),
    }) as unknown as typeof fetch

    const onEvent = vi.fn()
    await streamChat('session-1', 'hello', onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('malformed SSE chunk'),
      expect.stringContaining('{not valid json}'),
      expect.anything(),
    )
  })
})
