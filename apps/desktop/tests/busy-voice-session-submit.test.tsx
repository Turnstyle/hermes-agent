import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { submitBusyLiveVoiceTurn } from '@/app/chat/composer/hooks/live-voice-submit'
import { usePromptActions } from '@/app/session/hooks/use-prompt-actions'
import { storedJournal } from '@/app/session/hooks/use-prompt-actions/prepared-submissions-test-utils'
import { useSubmitPrompt } from '@/app/session/hooks/use-prompt-actions/submit'
import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

beforeEach(() => {
  window.localStorage.clear()
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

type ClientSessionState = ReturnType<typeof createClientSessionState>

it('admits busy voice through prompt.submit queued without interrupting the session or children', async () => {
  let state: ClientSessionState = { ...createClientSessionState(), busy: true }

  const requestGateway = vi.fn(
    async (_method: string, params?: Record<string, unknown>) =>
      ({ session_id: 'runtime-a', submission_id: params?.submission_id, status: 'queued' }) as never
  )

  const deps = {
    activeSessionIdRef: { current: 'runtime-a' as string | null },
    selectedStoredSessionIdRef: { current: 'stored-a' as string | null },
    busyRef: { current: true },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => 'runtime-a',
    getRouteToken: () => 'same-route',
    requestGateway: requestGateway as GatewayRequest,
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', 'runtime-a']]) },
    resumeStoredSession: vi.fn(),
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string) => ({ sessionId, attachments: [] })),
    updateSessionState: vi.fn((_sid: string, updater: (s: ClientSessionState) => ClientSessionState) => {
      state = updater(state)

      return state
    })
  }

  const { result } = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => {
    expect(
      await result.current('keep working', {
        fromQueue: true,
        surface: 'voice-live',
        voiceContext: 'User: keep working',
        sessionId: 'runtime-a',
        storedSessionId: 'stored-a',
        attachments: []
      })
    ).toBe(true)
  })
  expect(storedJournal()).toEqual({})
  expect(requestGateway.mock.calls.map(call => call[0])).toEqual(['prompt.submit'])
  expect(requestGateway.mock.calls[0][1]).toMatchObject({
    session_id: 'runtime-a',
    queued: true,
    surface: 'voice-live',
    voice_context: 'User: keep working'
  })
})

it('the voice steering callback reaches session.steer without redirect, interrupt, or child cancellation', async () => {
  let state: ClientSessionState = { ...createClientSessionState(), busy: true }
  const requestGateway = vi.fn(async () => ({ status: 'queued' }) as never)

  const deps = {
    activeSessionIdRef: { current: 'runtime-a' as string | null },
    selectedStoredSessionIdRef: { current: 'stored-a' as string | null },
    busyRef: { current: true },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => 'runtime-a',
    getRouteToken: () => 'same-route',
    requestGateway: requestGateway as GatewayRequest,
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', 'runtime-a']]) },
    resumeStoredSession: vi.fn(),
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string) => ({ sessionId, attachments: [] })),
    updateSessionState: vi.fn((_sid: string, updater: (s: ClientSessionState) => ClientSessionState) => {
      state = updater(state)

      return state
    })
  }

  const { result } = renderHook(() =>
    usePromptActions({
      ...deps,
      activeSessionId: 'runtime-a',
      branchCurrentSession: async () => false,
      handleSkinCommand: () => '',
      openMemoryGraph: () => undefined,
      refreshSessions: async () => undefined,
      startFreshSessionDraft: () => undefined,
      sttEnabled: false
    })
  )

  await act(async () =>
    submitBusyLiveVoiceTurn({
      mode: 'steer',
      text: 'keep the children running',
      voiceContext: 'User: keep the children running',
      sessionId: 'runtime-a',
      queueSessionKey: 'stored-a',
      onSteer: result.current.redirectPrompt,
      onSubmit: result.current.submitText
    })
  )
  expect(requestGateway.mock.calls).toEqual([
    ['session.steer', { session_id: 'runtime-a', text: 'keep the children running' }]
  ])
})
