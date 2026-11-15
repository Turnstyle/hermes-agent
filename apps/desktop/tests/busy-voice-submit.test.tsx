import { act, cleanup, renderHook } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useComposerVoice } from '@/app/chat/composer/hooks/use-composer-voice'

const mocks = vi.hoisted(() => ({
  clearDraft: vi.fn(),
  stash: vi.fn(),
  takeDraft: vi.fn(() => ({ text: '', attachments: [] })),
  freshScope: vi.fn(() => '__new__:original'),
  conversationSubmits: [] as Array<(text: string) => Promise<void>>,
  liveSubmits: [] as Array<(text: string, context: string) => Promise<void>>,
  enqueue: vi.fn(() => ({ id: 'queued' })),
  canonical: vi.fn(() => false),
  onSubmit: vi.fn(async () => true),
  resetBrowseState: vi.fn(),
  triggerHaptic: vi.fn()
}))

vi.mock('@nanostores/react', () => ({
  useStore: (store: unknown) => store
}))

vi.mock('nanostores', async () => {
  const actual = await vi.importActual<typeof Nanostores>('nanostores')

  return { ...actual, computed: (deps: unknown, fn: unknown) => fn }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      assistant: { thread: { readAloudFailed: '' } },
      notifications: { voice: { sayStopToEnd: () => '' } },
      settings: { config: { autosaveFailed: '' } }
    }
  })
}))

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: mocks.triggerHaptic
}))

vi.mock('@/lib/spoken-reply', () => ({
  adoptSpokenReplySession: vi.fn(),
  markAssistantIdSpoken: vi.fn(),
  resolveSpokenReply: vi.fn(() => null)
}))

vi.mock('@/lib/tts-lease', () => ({
  CONVERSATION_LEASE: 'conversation',
  READ_ALOUD_LEASE: 'read-aloud',
  syncTtsLease: vi.fn(async () => undefined)
}))

vi.mock('@/lib/wake-indicator', () => ({
  clearWakeIndicator: vi.fn(),
  syncWakeIndicatorWithVoice: vi.fn(() => false)
}))

vi.mock('@/store/composer', () => ({
  $voiceConversationStartRequest: { get: () => null },
  takeVoiceConversationStart: vi.fn(() => false),
  stashSessionDraft: mocks.stash,
  takeSessionDraft: mocks.takeDraft,
  freshDraftScope: mocks.freshScope
}))

vi.mock('@/store/composer-input-history', () => ({
  resetBrowseState: mocks.resetBrowseState
}))

vi.mock('@/store/composer-queue', () => ({
  enqueueQueuedPrompt: mocks.enqueue,
  serverOwnsComposerQueue: mocks.canonical
}))

vi.mock('@/app/chat/composer/hooks/use-voice-live-conversation', () => ({
  useVoiceLiveConversation: ({ onSubmit }: { onSubmit: (text: string, context: string) => Promise<void> }) => {
    mocks.liveSubmits.push(onSubmit)

    return {
      end: vi.fn(),
      level: 0,
      muted: false,
      start: vi.fn(),
      status: 'idle',
      stopTurn: vi.fn(),
      toggleMute: vi.fn()
    }
  }
}))

vi.mock('@/store/gateway', () => ({
  $gateway: { get: () => null }
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/voice-prefs', () => ({
  $autoSpeakReplies: { get: () => false },
  $voiceStopPhrase: { get: () => null },
  setAutoSpeakReplies: vi.fn(async () => undefined)
}))

vi.mock('@/store/wake-word', () => ({
  resumeWakeAfterVoice: vi.fn(async () => undefined)
}))

vi.mock('@/app/chat/composer/focus', () => ({
  onComposerDictationRequest: vi.fn(() => () => undefined),
  onComposerVoiceToggleRequest: vi.fn(() => () => undefined)
}))

vi.mock('@/app/chat/composer/scope', () => ({
  useComposerScope: () => ({ $messages: { get: () => [] } }),
  useComposerSurfaceId: () => null
}))

vi.mock('@/app/chat/composer/hooks/use-auto-speak-replies', () => ({
  useAutoSpeakReplies: vi.fn()
}))

vi.mock('@/app/chat/composer/hooks/use-voice-conversation', () => ({
  useVoiceConversation: ({ onSubmit }: { onSubmit: (text: string) => Promise<void> }) => {
    mocks.conversationSubmits.push(onSubmit)

    return {
      end: vi.fn(async () => undefined),
      level: 0,
      muted: false,
      start: vi.fn(async () => undefined),
      status: 'idle',
      stopTurn: vi.fn(),
      toggleMute: vi.fn()
    }
  }
}))

vi.mock('@/app/chat/composer/hooks/use-voice-recorder', () => ({
  useVoiceRecorder: () => ({
    dictate: vi.fn(),
    voiceActivityState: { elapsedSeconds: 0, level: 0, status: 'idle' },
    voiceStatus: 'idle'
  })
}))

describe('busy voice policy', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.canonical.mockReturnValue(false)
  })
  it.each(['steer', 'queue', 'interrupt', null, 'invalid'] as const)(
    'routes busy live words with %s policy',
    async mode => {
      mocks.canonical.mockReturnValue(true)
      const onSteer = vi.fn(async () => true)
      renderHook(() =>
        useComposerVoice({
          busy: true,
          busyInputMode: mode as never,
          queueSessionKey: 'stored-owner',
          sessionId: 'runtime-owner',
          onSteer,
          clearDraft: mocks.clearDraft,
          disabled: false,
          focusInput: vi.fn(),
          insertText: vi.fn(),
          maxRecordingSeconds: 60,
          onSubmit: mocks.onSubmit,
          onTranscribeAudio: undefined,
          target: 'tile:test'
        })
      )
      await act(async () => mocks.liveSubmits.at(-1)!('follow up', 'User: follow up'))

      if (mode === 'steer') {
        expect(onSteer).toHaveBeenCalledWith('follow up', 'steer')
        expect(mocks.onSubmit).not.toHaveBeenCalled()
      } else if (mode === 'interrupt') {
        expect(mocks.onSubmit).toHaveBeenCalledWith('follow up', {
          surface: 'voice-live',
          voiceContext: 'User: follow up'
        })
      } else {
        expect(mocks.onSubmit).toHaveBeenCalledWith('follow up', {
          surface: 'voice-live',
          voiceContext: 'User: follow up',
          fromQueue: true,
          sessionId: 'runtime-owner',
          storedSessionId: 'stored-owner',
          attachments: []
        })
      }

      expect(mocks.enqueue).not.toHaveBeenCalled()
    }
  )
  it('keeps idle live submission unchanged', async () => {
    const onSteer = vi.fn()
    renderHook(() =>
      useComposerVoice({
        busy: false,
        busyInputMode: 'steer',
        onSteer,
        clearDraft: mocks.clearDraft,
        disabled: false,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 60,
        onSubmit: mocks.onSubmit,
        onTranscribeAudio: undefined,
        sessionId: 'runtime-owner',
        target: 'tile:test'
      })
    )
    await act(async () => mocks.liveSubmits.at(-1)!('new task', 'User: new task'))
    expect(onSteer).not.toHaveBeenCalled()
    expect(mocks.onSubmit).toHaveBeenCalledWith('new task', { surface: 'voice-live', voiceContext: 'User: new task' })
  })
})
describe('refused live steering retains the owning transcript', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.canonical.mockReturnValue(false)
  })

  it.each([false, true])('keeps the owning draft if canonical admission itself fails (throw=%s)', async throwing => {
    mocks.canonical.mockReturnValue(true)

    const onSubmit = vi.fn(async () => {
      if (throwing) {
        throw new Error('admission offline')
      }

      return false
    })

    renderHook(() =>
      useComposerVoice({
        busy: true,
        busyInputMode: 'steer',
        queueSessionKey: 'original-owner',
        onSteer: async () => false,
        clearDraft: vi.fn(),
        disabled: false,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 60,
        onSubmit,
        onTranscribeAudio: undefined,
        sessionId: 'original-runtime',
        target: 'tile:test'
      })
    )
    await act(async () => {
      await expect(mocks.liveSubmits.at(-1)!('retain these words', 'User: original')).rejects.toThrow()
    })
    expect(mocks.stash).toHaveBeenCalledWith('original-owner', 'retain these words', [])
    expect(mocks.enqueue).not.toHaveBeenCalled()
  })

  it('preserves an existing owner draft if canonical admission fails', async () => {
    mocks.canonical.mockReturnValue(true)
    mocks.takeDraft.mockReturnValueOnce({ text: 'existing unsent words', attachments: [] })
    renderHook(() =>
      useComposerVoice({
        busy: true,
        busyInputMode: 'steer',
        queueSessionKey: 'original-owner',
        onSteer: async () => false,
        clearDraft: vi.fn(),
        disabled: false,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 60,
        onSubmit: async () => false,
        onTranscribeAudio: undefined,
        sessionId: 'original-runtime',
        target: 'tile:test'
      })
    )
    await act(async () => {
      await expect(mocks.liveSubmits.at(-1)!('voice words', 'User: voice words')).rejects.toThrow()
    })
    expect(mocks.stash).toHaveBeenCalledWith('original-owner', 'existing unsent words\nvoice words', [])
  })

  it('retains a refused steer before a new draft has a queue owner', async () => {
    const insertText = vi.fn()
    renderHook(() =>
      useComposerVoice({
        busy: true,
        busyInputMode: 'steer',
        onSteer: async () => false,
        clearDraft: vi.fn(),
        disabled: false,
        focusInput: vi.fn(),
        insertText,
        maxRecordingSeconds: 60,
        onSubmit: mocks.onSubmit,
        onTranscribeAudio: undefined,
        sessionId: null,
        target: 'tile:test'
      })
    )
    await act(async () => mocks.liveSubmits.at(-1)!('save my words', 'User: save my words'))
    expect(mocks.stash).toHaveBeenCalledWith('__new__:original', 'save my words', [])
    expect(mocks.onSubmit).not.toHaveBeenCalled()
  })

  it.each([
    [true, false],
    [true, true],
    [false, false],
    [false, true]
  ])('canonical=%s throwing=%s keeps voice context across a pane switch', async (canonical, throwing) => {
    mocks.canonical.mockReturnValue(canonical)
    let settle!: (value: boolean) => void
    let reject!: (error: Error) => void

    const onSteer = vi.fn(
      () =>
        new Promise<boolean>((resolve, fail) => {
          settle = resolve
          reject = fail
        })
    )

    const onSubmit = vi.fn(async () => true)

    const hook = renderHook(
      ({ owner }) =>
        useComposerVoice({
          busy: true,
          busyInputMode: 'steer',
          queueSessionKey: `stored-${owner}`,
          onSteer,
          clearDraft: vi.fn(),
          disabled: false,
          focusInput: vi.fn(),
          insertText: vi.fn(),
          maxRecordingSeconds: 60,
          onSubmit,
          onTranscribeAudio: undefined,
          sessionId: `runtime-${owner}`,
          target: 'tile:test'
        }),
      { initialProps: { owner: 'original' } }
    )

    let submission!: Promise<void>
    act(() => {
      submission = mocks.liveSubmits.at(-1)!('keep this transcript', 'User: original context')
    })
    hook.rerender({ owner: 'other' })
    await act(async () => {
      if (throwing) {
        reject(new Error('steer unavailable'))
      } else {
        settle(false)
      }

      await submission
    })

    if (canonical) {
      expect(onSubmit).toHaveBeenCalledWith('keep this transcript', {
        surface: 'voice-live',
        voiceContext: 'User: original context',
        fromQueue: true,
        attachments: [],
        sessionId: 'runtime-original',
        storedSessionId: 'stored-original'
      })
      expect(mocks.enqueue).not.toHaveBeenCalled()
    } else {
      expect(mocks.enqueue).toHaveBeenCalledWith('stored-original', {
        text: 'keep this transcript',
        attachments: [],
        surface: 'voice-live',
        voiceContext: 'User: original context'
      })
      expect(onSubmit).not.toHaveBeenCalled()
    }
  })
})
