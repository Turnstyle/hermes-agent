import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { useVoiceLiveConversation } from '@/app/chat/composer/hooks/use-voice-live-conversation'
import type { VoiceLiveHandlers } from '@/lib/voice-live'
import { $voiceStopPhrase } from '@/store/voice-prefs'

const transport = vi.hoisted(() => ({ handlers: null as VoiceLiveHandlers | null, speak: vi.fn(), close: vi.fn() }))
vi.mock('@/lib/voice-live', async original => ({
  ...(await original<Record<string, unknown>>()),
  VoiceLiveSession: class {
    speak = transport.speak
    close = transport.close
    think = vi.fn()
    instruct = vi.fn()
    setMuted = vi.fn()
    constructor(handlers: VoiceLiveHandlers) {
      transport.handlers = handlers
    }
    async start() {}
  }
}))

function mount(mode: 'steer' | 'queue' | 'interrupt' | null = 'steer', initialBusy = true) {
  const onInterrupt = vi.fn()
  const onSubmit = vi.fn()
  const onStopWord = vi.fn()
  let consumed = false
  let response: { id: string; pending: boolean; text: string } | null = null

  const hook = renderHook(
    ({ busy }) =>
      useVoiceLiveConversation({
        busy,
        busyInputMode: mode,
        enabled: true,
        onInterrupt,
        onSubmit,
        onStopWord,
        consumePendingResponse: (includePending = true) => {
          if (includePending || !response?.pending) {
            consumed = true
          }
        },
        pendingResponse: () => (consumed ? null : response),
        seedHistory: () => []
      }),
    { initialProps: { busy: initialBusy } }
  )

  return {
    ...hook,
    onInterrupt,
    onSubmit,
    onStopWord,
    reply: (text: string, pending: boolean) => {
      response = { id: 'same-reply', text, pending }
      consumed = false
    }
  }
}

function delegate(id = 'request', text = 'continue the task') {
  act(() => transport.handlers!.onDelegation(id, [{ speaker: 'user', text, startMs: 0, endMs: 1 }]))
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  $voiceStopPhrase.set('stop')
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it.each(['steer', 'queue', null] as const)('does not hard interrupt for %s mode', async mode => {
  const hook = mount(mode)
  await act(async () => hook.result.current.start())
  delegate()
  expect(hook.onSubmit).toHaveBeenCalledWith('continue the task', 'User: continue the task')
  expect(hook.onInterrupt).not.toHaveBeenCalled()
})
it('retains the explicit interrupt policy', async () => {
  const hook = mount('interrupt')
  await act(async () => hook.result.current.start())
  delegate()
  expect(hook.onInterrupt).toHaveBeenCalledTimes(1)
})
it.each(['transcript', 'delegation'])('ends voice on a bare stop from %s', async source => {
  const hook = mount()
  await act(async () => hook.result.current.start())

  if (source === 'delegation') {
    delegate('stop-request', 'stop')
  } else {
    act(() => transport.handlers!.onTranscript?.({ speaker: 'user', text: 'stop', startMs: 0, endMs: 1 }))
    act(() => vi.advanceTimersByTime(1500))
  }

  expect(transport.close).toHaveBeenCalledTimes(1)
  expect(hook.onStopWord).toHaveBeenCalledTimes(1)
  expect(hook.onSubmit).not.toHaveBeenCalled()
})
it('narrates the continuation of the same assistant message once after steering', async () => {
  const hook = mount('steer', false)
  await act(async () => hook.result.current.start())
  delegate('first')
  hook.rerender({ busy: true })
  hook.reply('First sentence. Still writing', true)
  act(() => vi.advanceTimersByTime(200))
  expect(transport.speak).toHaveBeenCalledWith('first', 'First sentence.')
  delegate('steer')
  hook.reply('First sentence. Still writing the final answer.', false)
  hook.rerender({ busy: false })
  act(() => vi.advanceTimersByTime(200))
  expect(
    transport.speak.mock.calls
      .map(call => call[1])
      .join(' ')
      .match(/First sentence\./g)
  ).toHaveLength(1)
  expect(transport.speak).toHaveBeenCalledWith('steer', ' Still writing the final answer.')
})
