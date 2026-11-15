import { act, cleanup, renderHook } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { useComposerVoice } from '@/app/chat/composer/hooks/use-composer-voice'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '@/app/chat/composer/scope'
import type { ChatMessage } from '@/lib/chat-messages'
import { clearSpokenRepliesForTests } from '@/lib/spoken-reply'
import type { VoiceLiveHandlers } from '@/lib/voice-live'

const transport = vi.hoisted(() => ({ handlers: null as VoiceLiveHandlers | null, speak: vi.fn() }))
vi.mock('@/lib/voice-live', async original => ({
  ...(await original<Record<string, unknown>>()),
  VoiceLiveSession: class {
    speak = transport.speak
    close = vi.fn()
    think = vi.fn()
    instruct = vi.fn()
    setMuted = vi.fn()
    constructor(handlers: VoiceLiveHandlers) {
      transport.handlers = handlers
    }
    async start() {}
  }
}))
vi.mock('@/lib/tts-lease', () => ({
  CONVERSATION_LEASE: 'conversation',
  READ_ALOUD_LEASE: 'read-aloud',
  syncTtsLease: vi.fn(async () => undefined)
}))
vi.mock('@/store/voice-live', async () => {
  const { atom } = await import('nanostores')

  return {
    $voiceLiveStatus: atom({ available: true }),
    selectedVoiceChatMode: () => 'gpt-live',
    refreshVoiceLiveStatus: vi.fn(async () => undefined)
  }
})
vi.mock('@/app/chat/composer/hooks/use-voice-recorder', () => ({
  useVoiceRecorder: () => ({ dictate: vi.fn(), voiceStatus: 'idle' })
}))
vi.mock('@/app/chat/composer/hooks/use-voice-conversation', () => ({
  useVoiceConversation: () => ({ end: vi.fn(), status: 'idle' })
}))
vi.mock('@/app/chat/composer/hooks/use-auto-speak-replies', () => ({ useAutoSpeakReplies: vi.fn() }))

afterEach(() => {
  cleanup()
  clearSpokenRepliesForTests()
  vi.useRealTimers()
  vi.clearAllMocks()
})
it('keeps the first reply audible after a steer inserts a user row after its pending bubble', async () => {
  vi.useFakeTimers()
  const $messages = atom<ChatMessage[]>([])

  const onSteer = vi.fn(async () => {
    $messages.set([
      ...$messages.get(),
      { id: 'steer-user', role: 'user', parts: [{ type: 'text', text: 'also check this' }] }
    ])

    return true
  })

  const wrapper = ({ children }: { children: ReactNode }) => (
    <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, $messages }}>{children}</ComposerScopeProvider>
  )

  const hook = renderHook(
    ({ busy }) =>
      useComposerVoice({
        busy,
        busyInputMode: 'steer',
        onSteer,
        queueSessionKey: 'owner',
        sessionId: 'owner',
        clearDraft: vi.fn(),
        disabled: false,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 60,
        onSubmit: vi.fn(async () => true),
        onTranscribeAudio: undefined,
        target: 'tile:test'
      }),
    { initialProps: { busy: false }, wrapper }
  )

  await act(async () => hook.result.current.startConversation())
  act(() => transport.handlers!.onDelegation('first', [{ speaker: 'user', text: 'first task', startMs: 0, endMs: 1 }]))
  act(() =>
    $messages.set([
      { id: 'first-user', role: 'user', parts: [{ type: 'text', text: 'first task' }] },
      {
        id: 'reply',
        role: 'assistant',
        pending: true,
        parts: [{ type: 'text', text: 'First sentence. Still writing' }]
      }
    ])
  )
  hook.rerender({ busy: true })
  act(() => vi.advanceTimersByTime(200))
  expect(transport.speak).toHaveBeenCalledWith('first', 'First sentence.')
  await act(async () =>
    transport.handlers!.onDelegation('steer', [{ speaker: 'user', text: 'also check this', startMs: 0, endMs: 1 }])
  )
  act(() =>
    $messages.set(
      $messages
        .get()
        .map(message =>
          message.id === 'reply'
            ? {
                ...message,
                pending: false,
                parts: [{ type: 'text', text: 'First sentence. Still writing the final answer.' }]
              }
            : message
        )
    )
  )
  hook.rerender({ busy: false })
  act(() => vi.advanceTimersByTime(200))
  expect(transport.speak).toHaveBeenCalledWith('steer', ' Still writing the final answer.')
  expect(
    transport.speak.mock.calls
      .map(call => call[1])
      .join(' ')
      .match(/First sentence\./g)
  ).toHaveLength(1)
})
