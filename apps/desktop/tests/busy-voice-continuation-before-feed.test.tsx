import { act, cleanup, renderHook } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { useComposerVoice } from '@/app/chat/composer/hooks/use-composer-voice'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '@/app/chat/composer/scope'
import type { ChatMessage } from '@/lib/chat-messages'
import { clearSpokenRepliesForTests } from '@/lib/spoken-reply'
import type { VoiceLiveHandlers } from '@/lib/voice-live'
import { $connectionWires, recordConnectionWire } from '@/store/connection-wire'

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
  $connectionWires.set({})
  vi.useRealTimers()
  vi.clearAllMocks()
})

it.each([
  { mode: 'steer', accepted: true, initialText: 'First sentence. Still writing' },
  { mode: 'steer', accepted: true, initialText: '' },
  { mode: 'steer', accepted: false, initialText: 'First sentence. Still writing' },
  { mode: 'steer', accepted: false, initialText: '' },
  { mode: 'queue', accepted: false, initialText: 'First sentence. Still writing' },
  { mode: 'queue', accepted: false, initialText: '' }
] as const)(
  'speaks the same-bubble final answer before any feed tick: $mode accepted=$accepted initial="$initialText"',
  async ({ mode, accepted, initialText }) => {
    vi.useFakeTimers()
    recordConnectionWire(null, 'canonical')
    const $messages = atom<ChatMessage[]>([])

    const appendFollowUp = () =>
      $messages.set([
        ...$messages.get(),
        { id: 'follow-up-user', role: 'user', parts: [{ type: 'text', text: 'also check this' }] }
      ])

    const onSteer = vi.fn(async () => {
      if (accepted) {
        appendFollowUp()
      }

      return accepted
    })

    const onSubmit = vi.fn(async (_text: string, options?: { fromQueue?: boolean }) => {
      if (options?.fromQueue) {
        appendFollowUp()
      }

      return true
    })

    const onInterrupt = vi.fn()

    const wrapper = ({ children }: { children: ReactNode }) => (
      <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, $messages }}>{children}</ComposerScopeProvider>
    )

    const hook = renderHook(
      ({ busy }) =>
        useComposerVoice({
          busy,
          busyInputMode: mode,
          onSteer,
          onInterrupt,
          queueSessionKey: 'owner',
          sessionId: 'owner',
          clearDraft: vi.fn(),
          disabled: false,
          focusInput: vi.fn(),
          insertText: vi.fn(),
          maxRecordingSeconds: 60,
          onSubmit,
          onTranscribeAudio: undefined,
          target: 'tile:test'
        }),
      { initialProps: { busy: false }, wrapper }
    )

    await act(async () => hook.result.current.startConversation())
    act(() => transport.handlers!.onDelegation('first', [{ speaker: 'user', text: 'first task', startMs: 0, endMs: 1 }]))
    // Let the busy transition's immediate feed run while there is no assistant
    // yet. The next delegation arrives before the first 200 ms interval tick.
    hook.rerender({ busy: true })
    act(() =>
      $messages.set([
        { id: 'first-user', role: 'user', parts: [{ type: 'text', text: 'first task' }] },
        { id: 'reply', role: 'assistant', pending: true, parts: [{ type: 'text', text: initialText }] }
      ])
    )
    expect(transport.speak).not.toHaveBeenCalled()
    await act(async () =>
      transport.handlers!.onDelegation('follow-up', [{ speaker: 'user', text: 'also check this', startMs: 0, endMs: 1 }])
    )
    expect($messages.get().map(message => message.id)).toEqual(['first-user', 'reply', 'follow-up-user'])
    expect(onInterrupt).not.toHaveBeenCalled()

    if (mode === 'steer') {
      expect(onSteer).toHaveBeenCalledWith('also check this', 'steer')
    } else {
      expect(onSteer).not.toHaveBeenCalled()
    }

    if (!accepted) {
      expect(onSubmit).toHaveBeenLastCalledWith('also check this', {
        surface: 'voice-live',
        voiceContext: 'User: also check this',
        fromQueue: true,
        attachments: [],
        sessionId: 'owner',
        storedSessionId: 'owner'
      })
    }

    act(() =>
      $messages.set(
        $messages.get().map(message =>
          message.id === 'reply'
            ? { ...message, pending: false, parts: [{ type: 'text', text: 'First sentence. The final answer.' }] }
            : message
        )
      )
    )
    hook.rerender({ busy: false })
    act(() => vi.advanceTimersByTime(200))
    expect(transport.speak.mock.calls.map(call => call[1]).join('')).toBe('First sentence. The final answer.')
  }
)
